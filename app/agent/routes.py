import io
import json
import zipfile
from pathlib import Path

from fastapi import APIRouter, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.routing import APIRoute
from sqlalchemy import select

from ..config import get_config
from ..models import DesignProjectAccess, Project
from . import assets, chat, cms, events, models3d, sample_pack, service, style, technical_flat
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
from .store import AgentError, Record, create, head, records, require, serialize, transaction


class AgentRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()

        async def handler(request):
            try:
                return await original(request)
            except AgentError as exc:
                return JSONResponse(
                    status_code=exc.status, content={"error": {"code": exc.code, "message": exc.message}}
                )
            except RequestValidationError:
                return JSONResponse(
                    status_code=422,
                    content={"error": {"code": "INVALID_INPUT", "message": "请检查必填内容、长度、约束编号与素材用途"}},
                )

        return handler


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
def capabilities() -> dict:
    return {**Provider().capabilities(), "three_d": models3d.capabilities()}


@router.get("/design-projects")
def projects(request: Request) -> dict:
    with transaction() as db:
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
    return service.workspace(pid)


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
        return FileResponse(assets.file_path(row.payload["image"]), media_type="image/png")


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
def send_message(pid: int, data: ChatIn) -> dict:
    return chat.send(pid, data)


@router.get("/project/{pid}/design-events")
async def design_events(pid: int, request: Request):
    service.workspace(pid)
    return StreamingResponse(
        events.stream(pid, request),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
    )
