import io
import json
import re
import uuid
import zipfile
from pathlib import Path
from typing import Annotated

import anyio
from fastapi import APIRouter, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.routing import APIRoute
from sqlalchemy import select

from ..config import get_config
from ..models import DesignProjectAccess, Project, session
from . import (
    assets,
    chat,
    cms,
    direct_create,
    dual_entry,
    events,
    models3d,
    sample_pack,
    service,
    style,
    technical_flat,
    workbench,
)
from .access_context import project_binding, request_project_id, validate_transaction_access
from .managed_store import ManagedAction, ManagedAudit, action_key
from .providers import Provider
from .schemas import (
    ChatIn,
    CorrectionIn,
    FeedbackIn,
    RecheckIn,
    RevisionIn,
    SamplingSheetIn,
    SpecIn,
    StylePlanEdit,
    TaskIn,
)
from .store import (
    AgentError,
    Record,
    bind_transaction,
    create,
    head,
    records,
    require,
    reset_transaction,
    serialize,
    transaction,
)


class EngineAction:
    """One DB commit owns the engine result, action snapshot and task revision."""

    def __init__(self, request: Request, raw: bytes):
        self.request, self.raw = request, raw
        query = request.scope.get("query_string", b"").decode("ascii")
        self.target = request.url.path + ("?" + query if query else "")
        self.action_id = request.headers.get("x-design-action-id", "")
        self.context = request.headers.get("x-design-context", "")
        revision = request.headers.get("x-design-revision", "")
        self.creating = (
            request.url.path in {"/api/design-projects", "/api/design-projects/direct"} and request.method == "POST"
        )
        self.archiving = request.method == "POST" and bool(
            re.fullmatch(r"/api/design-projects/\d+/archive/?", request.url.path)
        )
        self.project_edit = bool(re.fullmatch(r"/api/design-projects/\d+(?:/archive)?/?", request.url.path))
        self.layout = request.method == "PUT" and bool(
            re.fullmatch(r"/api/project/\d+/design-canvas/?", request.url.path)
        )
        if not re.fullmatch(r"[A-Za-z0-9_-]{8,100}", self.action_id):
            raise AgentError("INVALID_INPUT", "请提供稳定的操作编号", 422)
        if self.project_edit:
            try:
                schema = direct_create.ArchiveIn if self.archiving else direct_create.RenameIn
                revision = str(schema.model_validate_json(raw).expected_revision)
            except ValueError:
                raise AgentError("INVALID_INPUT", "请检查项目操作字段和修订号", 422) from None
        if not self.creating and not self.layout and not re.fullmatch(r"[1-9][0-9]{0,9}", revision):
            raise AgentError("INVALID_INPUT", "请提供当前任务修订号", 422)
        if self.layout:
            try:
                self.base_revision = workbench.CanvasIn.model_validate_json(raw).expected_layout_revision
            except ValueError:
                raise AgentError("INVALID_INPUT", "请检查画布布局字段、节点与数值范围", 422) from None
        else:
            self.base_revision = 0 if self.creating else int(revision)
        self.base_key = "base_layout_revision" if self.layout else "base_revision"
        self.db = None

    def prepare(self):
        self.db = db = session()
        if db.bind.dialect.name == "sqlite":
            db.connection().exec_driver_sql("BEGIN IMMEDIATE")
        self.user = validate_transaction_access(db, require_editable=False, allow_archived=self.archiving)
        if self.user is None:
            raise AgentError("LOGIN_REQUIRED", "请先登录设计工作台", 401)
        pid = request_project_id(db)
        if pid is None and not self.creating:
            raise AgentError("NOT_FOUND", "此接口不支持设计工作区操作", 404)
        if not self.creating:
            self.row, self.meta = project_binding(db, pid, self.user, allow_archived=self.archiving)
        self.key = action_key(get_config().design_scope_id, self.user.username, self.action_id)
        prior = db.get(ManagedAction, self.key)
        if prior:
            if self.creating:
                project_binding(db, prior.response["project_id"], self.user)
            if prior.auth_context_id != self.context:
                raise AgentError("AUTH_CONTEXT_CHANGED", "原操作属于另一登录会话；可只读查看原结果", 409)
            if (
                prior.method != self.request.method
                or prior.path != self.target
                or prior.raw_body != self.raw
                or prior.response.get(self.base_key) != self.base_revision
            ):
                raise AgentError("IDEMPOTENCY_CONFLICT", "同一操作编号不能用于不同请求或基准修订", 409)
            validate_transaction_access(db, require_editable=False, allow_archived=self.archiving)
            headers = {} if self.layout else {"X-Design-Revision": str(prior.response["revision"])}
            return JSONResponse(
                prior.response,
                status_code=prior.status_code,
                headers=headers,
            )
        if self.creating:
            if self.user.role != "designer":
                raise AgentError("ROLE_FORBIDDEN", "仅设计员工可创建本人自主项目", 403)
            return None
        project_binding(db, pid, self.user, write=True)
        if self.layout:
            if workbench.canvas_document(db, pid)["layout_revision"] != self.base_revision:
                raise AgentError("LAYOUT_CONFLICT", "画布已有新布局，请保留本地布局并重新核对", 409)
            return None
        if self.meta.revision != self.base_revision:
            raise AgentError("STATE_CONFLICT", "任务已有新修订，请保留草稿并重新核对", 409)
        if re.fullmatch(r"/api/design-versions/[^/]+/technical-flat/?", self.request.url.path) or re.fullmatch(
            r"/api/technical-flats/[^/]+/revise/?",
            self.request.url.path,
        ):
            raise AgentError("CAPABILITY_UNAVAILABLE", "此实例未启用同步技术图生成，请保留已保存设计", 503)
        return None

    def finish(self, response):
        if not 200 <= response.status_code < 300:
            return response
        try:
            result = json.loads(response.body)
        except (ValueError, AttributeError) as error:
            raise AgentError("INTERNAL_ERROR", "设计操作没有产生可恢复结果", 500) from error
        if not isinstance(result, dict):
            raise AgentError("INTERNAL_ERROR", "设计操作没有产生可恢复结果", 500)
        db = self.db
        validate_transaction_access(db)
        if self.creating:
            self.row, self.meta = project_binding(db, result["project_id"], self.user, write=True)
        if self.layout:
            if result.get("layout_revision") != self.base_revision + 1:
                raise AgentError("INTERNAL_ERROR", "画布操作没有产生独立修订", 500)
            result.update(action_id=self.action_id, base_layout_revision=self.base_revision, revision_domain="layout")
            revision = result["layout_revision"]
        else:
            if self.meta.revision != self.base_revision:
                raise AgentError("STATE_CONFLICT", "任务已有新修订，请重新核对", 409)
            self.meta.revision += 1
            result.update(action_id=self.action_id, base_revision=self.base_revision, revision=self.meta.revision)
            revision = self.meta.revision
        db.add(
            ManagedAudit(
                handoff_id=self.row.id,
                revision=revision,
                kind="layout_mutation" if self.layout else "engine_mutation",
                actor=self.user.username,
                payload={
                    "action_id": self.action_id,
                    "method": self.request.method,
                    "path": self.target,
                    self.base_key: self.base_revision,
                    "result_id": result.get("id"),
                    "revision_domain": "layout" if self.layout else "business",
                },
            )
        )
        db.add(
            ManagedAction(
                action_key=self.key,
                action_id=self.action_id,
                username=self.user.username,
                auth_context_id=self.context,
                method=self.request.method,
                path=self.target,
                raw_body=self.raw,
                status_code=response.status_code,
                response=result,
                handoff_id=self.row.id,
            )
        )
        db.flush()
        validate_transaction_access(db)
        if self.archiving:
            self.meta.status = "archived"
        headers = dict(response.headers)
        headers.pop("content-length", None)
        if self.layout:
            headers.pop("x-design-revision", None)
        else:
            headers["X-Design-Revision"] = str(self.meta.revision)
        frozen = JSONResponse(result, status_code=response.status_code, headers=headers)
        db.commit()
        return frozen

    def close(self):
        if self.db is not None:
            self.db.rollback()
            self.db.close()


async def engine_action(request: Request, original):
    limit = assets.MAX_BYTES if re.fullmatch(r"/api/project/\d+/design-assets/?", request.url.path) else 256 * 1024
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > limit:
            raise AgentError("INVALID_INPUT", "请求体超过允许大小", 413)
    raw = bytes(body)

    async def receive():
        return {"type": "http.request", "body": raw, "more_body": False}

    cached = Request(request.scope, receive=receive)
    await cached.body()  # The original parser/upload stream consumes the exact bounded bytes.
    operation, token = EngineAction(cached, raw), None
    try:
        replay = await anyio.to_thread.run_sync(operation.prepare)
        if replay is not None:
            return replay
        token = bind_transaction(operation.db)
        response = await original(cached)
        return await anyio.to_thread.run_sync(operation.finish, response)
    finally:
        if token is not None:
            reset_transaction(token)
        # Wait for the DB worker even if the client disconnects during dispatch.
        with anyio.CancelScope(shield=True):
            await anyio.to_thread.run_sync(operation.close)


class AgentRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()

        async def handler(request):
            try:
                if get_config().managed and request.method in {"POST", "PUT", "PATCH", "DELETE"}:
                    return await engine_action(request, original)
                return await original(request)
            except AgentError as exc:
                error = {"code": exc.code, "message": exc.message}
                payload = {"error": error}
                if get_config().managed:
                    error["retryable"] = exc.status in {429, 502, 503, 504}
                    payload["request_id"] = uuid.uuid4().hex
                return JSONResponse(status_code=exc.status, content=payload)
            except RequestValidationError:
                error = {"code": "INVALID_INPUT", "message": "请检查必填内容、长度、约束编号与素材用途"}
                payload = {"error": error}
                if get_config().managed:
                    error["retryable"] = False
                    payload["request_id"] = uuid.uuid4().hex
                return JSONResponse(
                    status_code=422,
                    content=payload,
                )

        async def private_handler(request):
            response = await handler(request)
            if re.fullmatch(r"/api/project/\d+/design-(canvas|messages|briefs|prompts)/?", request.url.path):
                response.headers["Cache-Control"] = "private, no-store"
            return response

        return private_handler


router = APIRouter(prefix="/api", tags=["design-agent"], route_class=AgentRoute)


@router.get("/cms/packages")
def cms_packages(offset: int = Query(default=0, ge=0)) -> dict:
    return cms.fetch_packages(offset)


@router.get("/cms/packages/{package_id}")
def cms_package(package_id: str) -> dict:
    return cms.fetch_package(package_id)


@router.post("/projects/{pid}/cms-response")
def cms_response(pid: int, data: cms.CmsResponseIn) -> dict:
    return cms.submit_response(pid, data)


@router.get("/design-agent/capabilities")
def capabilities(project_id: int | None = None) -> dict:
    with transaction() as db:
        user = validate_transaction_access(db)
        image_only = dual_entry.image_capability(db, project_id, user) if get_config().managed else None
        direct = direct_create.capabilities(db, project_id, user, Provider()) if get_config().managed else None
    return {
        **Provider().capabilities(),
        "three_d": models3d.capabilities(),
        "image_only_execution": image_only,
        "direct_creation": direct,
    }


@router.get("/design-projects")
def projects(
    request: Request,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> dict:
    with transaction() as db:
        cfg = get_config()
        if cfg.managed:
            user = validate_transaction_access(db)
            if user is None:
                raise AgentError("LOGIN_REQUIRED", "请先登录设计工作台", 401)
            return dual_entry.list_projects(db, user, offset, limit)
        rows = db.scalars(select(Record).where(Record.kind == "head").order_by(Record.created_at.desc()))
        user = getattr(request.state, "design_user", None)
        if get_config().design_auth_required and user:
            if user.role == "manager":
                return {"items": []}
            allowed = {
                r.project_id
                for r in db.scalars(select(DesignProjectAccess).where(DesignProjectAccess.username == user.username))
            }
            return {
                "items": [
                    {"id": r.project_id, "name": db.get(Project, r.project_id).name}
                    for r in rows
                    if r.project_id in allowed
                ]
            }
        return {"items": [{"id": r.project_id, "name": db.get(Project, r.project_id).name} for r in rows]}


@router.get("/project/{pid}/design-workspace")
def workspace(pid: int) -> dict:
    return events.workspace_snapshot(pid)


@router.post("/design-projects", status_code=201)
def independent_project(data: dual_entry.ProjectIn) -> dict:
    if not get_config().managed:
        raise AgentError("CAPABILITY_UNAVAILABLE", "此入口需要独立人员会话", 503)
    with transaction() as db:
        return dual_entry.create_project(db, data)


@router.post("/design-projects/direct", status_code=201)
def direct_project(data: direct_create.DirectIn, request: Request) -> dict:
    if not get_config().managed:
        raise AgentError("CAPABILITY_UNAVAILABLE", "直接创作需要独立人员会话", 503)
    with transaction() as db:
        return direct_create.create_project(
            db, data, request.headers.get("x-design-action-id"), request.headers.get("x-design-context")
        )


@router.patch("/design-projects/{pid}")
def rename_project(pid: int, data: direct_create.RenameIn) -> dict:
    with transaction() as db:
        return direct_create.rename(db, pid, data)


@router.post("/design-projects/{pid}/archive")
def archive_project(pid: int, data: direct_create.ArchiveIn, request: Request) -> dict:
    with transaction() as db:
        return direct_create.archive(db, pid, data, request.headers.get("x-design-action-id"))


@router.get("/design-creation-runs/{id}")
def creation_run(id: str) -> dict:
    with transaction() as db:
        return direct_create.projection(require(db, id, "creation_run"))


@router.post("/project/{pid}/design-prompts", status_code=201)
def design_prompt(pid: int, data: dual_entry.PromptIn) -> dict:
    with transaction() as db:
        prompt = dual_entry.save_prompt(db, pid, data)
        return {"prompt": prompt, "spec_id": head(db, pid).payload["spec_id"], "source_brief_id": data.candidate_id}


@router.get("/project/{pid}/design-canvas")
def design_canvas(pid: int) -> dict:
    with transaction() as db:
        user = validate_transaction_access(db)
        if user is None:
            raise AgentError("LOGIN_REQUIRED", "请先登录设计工作台", 401)
        project_binding(db, pid, user)
        return workbench.canvas_document(db, pid)


@router.put("/project/{pid}/design-canvas")
def update_design_canvas(pid: int, data: workbench.CanvasIn) -> dict:
    with transaction() as db:
        return workbench.save_canvas(db, pid, data)


@router.post("/project/{pid}/design-briefs", status_code=201)
def design_brief(pid: int, data: workbench.BriefIn) -> dict:
    with transaction() as db:
        return {"brief": workbench.save_brief(db, pid, data)}


@router.post("/project/{pid}/design-text-to-image", status_code=202)
def image_only_task(pid: int, data: dual_entry.ImageIn, request: Request) -> dict:
    with transaction() as db:
        return dual_entry.queue_image(db, pid, data, request.headers.get("x-design-action-id"))


@router.post("/project/{pid}/design-assets", status_code=201)
async def upload(pid: int, request: Request, name: str = "参考图") -> dict:
    # Raw binary stream avoids accepting an unbounded multipart body in memory.
    parts, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > assets.MAX_BYTES:
            raise AgentError("IMAGE_TOO_LARGE", "每张图片须小于 10 MB", 413)
        parts.append(chunk)
    with transaction() as db:
        head(db, pid)
        if len(records(db, pid, "asset")) >= 40:
            raise AgentError("ASSET_LIMIT", "本项目最多保存 40 张素材，请新建项目")
        saved = assets.save_image(b"".join(parts))
        row = create(db, pid, "asset", {**saved, "name": Path(name).name[:100] or "参考图"}, "ready")
        return serialize(row)


@router.get("/design-assets/{id}/image")
def asset_image(id: str):
    with transaction() as db:
        row = require(db, id, "asset")
        return FileResponse(
            assets.file_path(row.payload),
            media_type="image/png",
            headers={"Cache-Control": "private, max-age=3600", "X-Content-Type-Options": "nosniff"},
        )


@router.post("/project/{pid}/design-specs", status_code=201)
def save_spec(pid: int, data: SpecIn) -> dict:
    with transaction() as db:
        return serialize(service.save_spec(db, pid, data))


@router.post("/design-specs/{id}/confirm")
def confirm_spec(id: str) -> dict:
    return service.confirm_spec(id)


@router.post("/style-plans/{id}/revise", status_code=201)
def revise_style_plan(id: str, data: StylePlanEdit) -> dict:
    return style.revise(id, data)


@router.post("/style-plans/{id}/confirm")
def confirm_style_plan(id: str) -> dict:
    return style.confirm(id)


@router.post("/project/{pid}/design-tasks", status_code=202)
def submit(pid: int, data: TaskIn) -> dict:
    return service.submit(pid, data)


@router.get("/design-tasks/{id}")
def task(id: str) -> dict:
    with transaction() as db:
        return serialize(require(db, id, "task"))


@router.post("/design-tasks/{id}/cancel")
def cancel(id: str) -> dict:
    return service.task_control(id, "cancel")


@router.post("/design-tasks/{id}/resume")
def resume(id: str) -> dict:
    return service.task_control(id, "resume")


@router.post("/design-tasks/{id}/input")
def feedback(id: str, data: FeedbackIn) -> dict:
    return service.task_control(id, "input", data.text)


@router.get("/project/{pid}/design-versions")
def versions(pid: int) -> dict:
    with transaction() as db:
        head(db, pid)
        return {"items": [serialize(r) for r in records(db, pid, "version")]}


@router.post("/design-versions/{id}/revise", status_code=201)
def revise(id: str, data: RevisionIn) -> dict:
    return service.revise(id, data)


@router.post("/design-versions/{id}/confirm")
def confirm_version(id: str) -> dict:
    return service.confirm_version(id)


@router.post("/design-versions/{id}/recheck", status_code=202)
def recheck_version(id: str, data: RecheckIn) -> dict:
    return service.recheck_version(id, data)


@router.post("/design-versions/{id}/sampling-sheet", status_code=201)
def save_sampling_sheet(id: str, data: SamplingSheetIn) -> dict:
    return service.save_sampling_sheet(id, data)


@router.post("/design-versions/{id}/sampling-sheet/auto")
def auto_sampling_sheet(id: str) -> dict:
    return service.auto_sampling_sheet(id)


@router.get("/design-versions/{id}/technical-flat")
def get_technical_flat(id: str) -> dict:
    return {"item": technical_flat.latest(id)}


@router.post("/design-versions/{id}/technical-flat")
def generate_technical_flat(id: str) -> dict:
    return technical_flat.generate(id)


@router.post("/technical-flats/{id}/revise")
def revise_technical_flat(id: str, data: technical_flat.FlatRevisionIn) -> dict:
    with transaction() as db:
        row = require(db, id, "technical_flat")
        version_id = row.payload["version_id"]
    return technical_flat.generate(version_id, feedback=data.feedback, expected_id=id)


@router.post("/technical-flats/{id}/confirm")
def confirm_technical_flat(id: str) -> dict:
    return technical_flat.confirm(id)


@router.get("/technical-flats/{id}/front.svg")
def technical_flat_svg(id: str):
    with transaction() as db:
        row = require(db, id, "technical_flat")
        if row.status not in {"draft", "confirmed"}:
            raise AgentError("FLAT_NOT_READY", "技术图草稿尚未生成", 409)
        return Response(
            technical_flat.front_svg(row.payload["features"]),
            media_type="image/svg+xml",
            headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store"},
        )


@router.get("/design-versions/{id}/image")
def version_image(id: str):
    with transaction() as db:
        row = require(db, id, "version")
        return FileResponse(
            assets.file_path(row.payload["image"]),
            media_type=row.payload["image"].get("mime_type", "image/png"),
            headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
        )


@router.post("/design-versions/{id}/model3d", status_code=202)
def start_model3d(id: str, data: models3d.Generate3DIn) -> dict:
    return models3d.submit(id, data)


@router.get("/design-models3d/{id}/file")
def model3d_file(id: str):
    with transaction() as db:
        row = require(db, id, "model3d")
        return FileResponse(
            models3d.model_path(row),
            media_type="model/gltf-binary",
            headers={"Cache-Control": "private, max-age=3600", "X-Content-Type-Options": "nosniff"},
        )


@router.get("/design-versions/{id}/delivery")
def delivery(id: str):
    with transaction() as db:
        row = require(db, id, "version")
        spec = require(db, row.payload["spec_id"], "spec", row.project_id)
        data = {
            "version": serialize(row),
            "requirements": spec.payload["spec"],
            "notice": "设计效果图；不代表纸样、实物、面料物理性能或生产批准。",
        }
        text = [
            "# 服装设计交付",
            f"版本：{id}",
            f"状态：{row.status}",
            data["notice"],
            "## 设计意图",
            spec.payload["spec"]["intent"],
            "## 要求与检查",
        ]
        checks = {c["constraint_id"]: c for c in (row.payload.get("review") or {}).get("checks", [])}
        for c in spec.payload["spec"]["constraints"]:
            check = checks.get(c["id"])
            text.append(f"- {c['text']}：" + (f"{check['status']} — {check['evidence']}" if check else "尚未检查"))
        sheets = [r for r in records(db, row.project_id, "sampling_sheet") if r.payload["version_id"] == id]
        sheet = sheets[-1] if sheets else None
        target = io.BytesIO()
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
            z.write(assets.file_path(row.payload["image"]), "design.png")
            z.writestr("requirements.json", json.dumps(data, ensure_ascii=False, indent=2))
            z.writestr("design-notes.md", "\n\n".join(text))
            if sheet:
                fields = sheet.payload["fields"]
                labels = [
                    ("面料与辅料", "material"),
                    ("颜色与色号", "color"),
                    ("尺码与关键尺寸", "measurements"),
                    ("图案或装饰位置", "graphic_placement"),
                    ("图案或装饰尺寸", "graphic_dimensions"),
                    ("制作工艺", "construction"),
                    ("其他打样说明", "notes"),
                ]
                notice = "打样准备草稿；未填写项目和实物指标须由设计师及打样方核对，不等于可直接生产的工艺单。"
                z.writestr(
                    "sampling-draft.json",
                    json.dumps({"sheet": serialize(sheet), "notice": notice}, ensure_ascii=False, indent=2),
                )
                basis = sheet.payload.get("basis") or {}
                summary = [
                    "## 已有设计依据",
                    f"设计意图：{basis.get('intent', '见要求单')}",
                    *[f"已确认要求：{item}" for item in basis.get("requirements", [])],
                    f"效果图检查：{basis.get('visual_review', '见 design-notes.md')}",
                ]
                sections = [f"## {label}\n{fields[key] or '待核对'}" for label, key in labels]
                z.writestr(
                    "sampling-draft.md",
                    "\n\n".join(["# 打样准备单（草稿）", f"设计版本：{id}", notice, *summary, *sections]),
                )
        target.seek(0)
        return StreamingResponse(
            target,
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="design-{id[:8]}.zip"'},
        )


def _sample_source(db, id: str):
    version = require(db, id, "version")
    if version.status != "confirmed":
        raise AgentError("CONFIRM_REQUIRED", "请先确认当前设计方案，再准备首版打样资料")
    spec = require(db, version.payload["spec_id"], "spec", version.project_id).payload["spec"]
    sheets = [r for r in records(db, version.project_id, "sampling_sheet") if r.payload["version_id"] == id]
    sheet = sheets[-1] if sheets else None
    fields = sheet.payload["fields"] if sheet else {}
    data = sample_pack.brief_data(id, spec, fields, sheet.id if sheet else None)
    return version, spec, data


@router.get("/design-versions/{id}/sample-pack/{side}.svg")
def sample_flat(id: str, side: str):
    if side not in {"front", "back"}:
        raise AgentError("INVALID_INPUT", "结构示意仅支持正面或背面", 422)
    with transaction() as db:
        version, spec, _ = _sample_source(db, id)
        flat = technical_flat.confirmed_for_version(db, version)
        if side == "front" and flat:
            svg = technical_flat.front_svg(flat.payload["features"])
        elif sample_pack.is_tshirt(spec):
            svg = sample_pack.flat_svg(side, spec)
        else:
            raise AgentError("FLAT_UNAVAILABLE", "当前品类尚无可靠结构图；可下载效果图和打样沟通资料", 404)
        return Response(
            svg,
            media_type="image/svg+xml",
            headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store"},
        )


@router.get("/design-versions/{id}/sample-pack")
def sample_handoff(id: str):
    with transaction() as db:
        version, spec, data = _sample_source(db, id)
        flat = technical_flat.confirmed_for_version(db, version)
        front_flat = technical_flat.front_svg(flat.payload["features"]) if flat else None
        image_bytes = assets.file_path(version.payload["image"]).read_bytes()
    target = io.BytesIO()
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("selected-design.png", image_bytes)
        if front_flat:
            z.writestr("front-flat.svg", front_flat)
        elif sample_pack.is_tshirt(spec):
            z.writestr("front-flat.svg", sample_pack.flat_svg("front", spec))
        if sample_pack.is_tshirt(spec):
            z.writestr("back-flat.svg", sample_pack.flat_svg("back", spec))
        z.writestr("first-sample-brief.pdf", sample_pack.brief_pdf(data, image_bytes))
        z.writestr("first-sample-brief.json", sample_pack.manifest_json(data))
        z.writestr(
            "README.txt",
            "首版打样沟通包。仅在有可靠模板时附带可编辑 SVG；结构示意不是裁剪纸样。"
            "PDF 供沟通，JSON 保存完整字段；待核对项不可当作已确认生产参数。\n",
        )
    target.seek(0)
    return StreamingResponse(
        target,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="first-sample-{id[:8]}.zip"',
            "Cache-Control": "private, no-store",
        },
    )


@router.post("/design-versions/{id}/check-corrections")
def correct_check(id: str, data: CorrectionIn) -> dict:
    return service.correct_check(id, data)


@router.post("/project/{pid}/design-messages", status_code=201)
def send_message(pid: int, data: ChatIn, request: Request) -> dict:
    if get_config().managed and data.idempotency_key != request.headers.get("x-design-action-id"):
        raise AgentError("INVALID_INPUT", "消息编号须与动作编号一致", 422)
    return chat.send(pid, data)


@router.get("/project/{pid}/design-events")
async def design_events(pid: int, request: Request):
    events.workspace_snapshot(pid)
    return StreamingResponse(
        events.stream(pid, request),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
    )
