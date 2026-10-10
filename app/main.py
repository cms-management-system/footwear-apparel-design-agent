"""款式工场：项目入口与对话式设计 Agent。"""

from __future__ import annotations

import json
import os
import re
import uuid

from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler, request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel
from starlette.exceptions import HTTPException

from .agent import cms
from .agent.handoff_routes import router as handoff_router
from .agent.managed_bridge import router as managed_bridge_router
from .agent.migrate import migrate
from .agent.routes import router as design_agent_router
from .agent.runner import worker
from .agent.store import AgentError
from .config import get_config
from .design_auth import current_user, path_project_id, permitted_project, seed_manager
from .design_auth import router as auth_router
from .models import DesignProjectAccess, Project, init_db, session

app = FastAPI(title="款式工场 · 鞋服智能设计 Agent", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5180", "http://127.0.0.1:5180"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(design_agent_router)
app.include_router(auth_router)
app.include_router(handoff_router)
app.include_router(managed_bridge_router)


def _error(status: int, code: str, message: str) -> JSONResponse:
    payload = {"error": {"code": code, "message": message}}
    if get_config().managed:
        payload["error"]["retryable"] = status in {429, 502, 503}
        payload["request_id"] = uuid.uuid4().hex
    return JSONResponse(payload, status_code=status,
                        headers={"Cache-Control": "private, no-store"})


@app.exception_handler(AgentError)
async def agent_error(_request: Request, exc: AgentError):
    return _error(exc.status, exc.code, exc.message)


@app.exception_handler(HTTPException)
async def http_error(request: Request, exc: HTTPException):
    if not get_config().managed:
        return await http_exception_handler(request, exc)
    codes = {400: "INVALID_INPUT", 401: "LOGIN_REQUIRED", 403: "ROLE_FORBIDDEN", 404: "NOT_FOUND",
             409: "STATE_CONFLICT", 410: "LEGACY_ROUTE_DISABLED", 422: "INVALID_INPUT",
             503: "CAPABILITY_UNAVAILABLE"}
    code = codes.get(exc.status_code, "REQUEST_FAILED")
    message = exc.detail if isinstance(exc.detail, str) else "请求未完成"
    if exc.status_code == 404:
        message = "记录不存在或当前账号不可访问"
    return _error(exc.status_code, code, message)


@app.exception_handler(RequestValidationError)
async def input_error(request: Request, exc: RequestValidationError):
    if not get_config().managed:
        return await request_validation_exception_handler(request, exc)
    return _error(422, "INVALID_INPUT", "请检查请求字段与格式")


async def managed_access(request: Request, call_next):
    cfg, path = get_config(), request.url.path
    origin = request.headers.get("origin")

    async def dispatch():
        try:
            return await call_next(request)
        except Exception:
            # Never return filesystem paths, provider details, or tracebacks to a managed client.
            return _error(500, "INTERNAL_ERROR", "服务暂时无法完成请求，请稍后重试")

    def finish(response):
        from .design_demo_access import apply_cookies
        apply_cookies(request, response)
        response.headers["Cache-Control"] = "private, no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        if origin in cfg.design_allowed_origins:
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Access-Control-Allow-Credentials"] = "true"
            vary = [item.strip() for item in response.headers.get("Vary", "").split(",") if item.strip()]
            if "Origin" not in vary:
                vary.append("Origin")
            response.headers["Vary"] = ", ".join(vary)
        return response

    if origin is not None and origin not in cfg.design_allowed_origins:
        return finish(_error(403, "ORIGIN_FORBIDDEN", "请求来源未获允许"))
    if request.method == "OPTIONS" and origin and request.headers.get("access-control-request-method"):
        response = Response(status_code=204)
        response.headers["Access-Control-Allow-Methods"] = "GET, HEAD, POST, PUT, PATCH, DELETE, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = request.headers.get(
            "access-control-request-headers", "Content-Type")
        return finish(response)
    if cfg.public_demo and path != "/api/health" and not path.startswith("/api/design-integration/"):
        from .design_demo_access import access_origin_guard
        try:
            access_origin_guard(request)
        except AgentError as exc:
            return finish(_error(exc.status, exc.code, exc.message))
    if cfg.design_demo_access_enabled:
        if path in {"/api/design-auth/login", "/api/design-auth/register"}:
            response = _error(409, "DEMO_LOGIN_FROZEN", "演示期间账号登录与注册暂停，请直接进入演示角色入口")
            from .design_demo_access import ENTRIES
            payload = json.loads(response.body)
            payload["demo_entry_urls"] = ENTRIES
            response.body = json.dumps(payload, ensure_ascii=False).encode()
            response.headers["content-length"] = str(len(response.body))
            return finish(response)
        if (path == "/api/design-auth/password"
                or (path.startswith("/api/design-auth/staff") and request.method != "GET")):
            return finish(_error(409, "DEMO_ACCOUNT_FROZEN", "演示期间账号维护暂停，业务分派仍可操作"))
        if path == "/api/design-auth/me" and request.method == "GET":
            from .design_demo_access import bootstrap
            try:
                bootstrap(request)
            except AgentError as exc:
                return finish(_error(exc.status, exc.code, exc.message))
    if path == "/api/design-auth/register":
        return finish(_error(403, "ROLE_FORBIDDEN", "请由设计负责人创建团队账号"))
    if path in {"/api/health", "/api/design-auth/login", "/api/design-auth/logout", "/api/design-auth/demo-access"}:
        return finish(await dispatch())
    if path.startswith("/api/design-integration/"):
        service_path = r"/api/design-integration/events/[^/]+/(review|assets/[^/]+)"
        if request.method != "GET" or not re.fullmatch(service_path, path):
            return finish(_error(404, "NOT_FOUND", "接口不存在"))
        return finish(await dispatch())  # Each handler requires its purpose-bound service identity.
    try:
        user = current_user(request)
    except AgentError as exc:
        return finish(_error(exc.status, exc.code, exc.message))
    except HTTPException:
        return finish(_error(401, "LOGIN_REQUIRED", "请先登录设计工作台"))
    request.state.design_user = user
    if path == "/api/cms" or path.startswith("/api/cms/") or re.fullmatch(r"/api/projects?/\d+/cms-response/?", path):
        return finish(_error(410, "LEGACY_ROUTE_DISABLED", "此实例已停用历史 CMS 交接，请使用产品直连工作流"))
    if path.rstrip("/") == "/api/project" and request.method == "POST":
        return finish(_error(403, "ROLE_FORBIDDEN", "正式设计项目须由负责人接收并分派"))
    project_id = path_project_id(path)
    if project_id is not None and not re.fullmatch(r"/api/design-projects/\d+/archive/?", path):
        if not permitted_project(user.username, user.role, project_id, "GET"):
            return finish(_error(404, "NOT_FOUND", "记录不存在或当前账号不可访问"))
        if not permitted_project(user.username, user.role, project_id, request.method):
            return finish(_error(403, "ROLE_FORBIDDEN", "设计负责人通过任务审查操作，本设计工作区仅供查看"))
    if request.method in {"POST", "PUT", "PATCH", "DELETE"} and not path.startswith("/api/design-handoffs"):
        if request.headers.get("x-design-context") != request.state.design_auth_context_id:
            return finish(_error(409, "AUTH_CONTEXT_CHANGED", "当前登录上下文已变化，请刷新身份后重试"))
    from .agent.access_context import bind_request, reset_request
    context_token = bind_request(request)
    try:
        return finish(await dispatch())
    finally:
        reset_request(context_token)


@app.middleware("http")
async def design_access(request: Request, call_next):
    path = request.url.path
    if get_config().managed and path.startswith("/api/"):
        return await managed_access(request, call_next)
    if (
        not get_config().design_auth_required
        or not path.startswith("/api/")
        or path in {"/api/health", "/api/design-auth/login", "/api/design-auth/register", "/api/design-auth/logout"}
        or path.startswith("/api/design-public/")
    ):
        return await call_next(request)
    try:
        user = current_user(request)
    except Exception:
        return JSONResponse({"error": {"code": "LOGIN_REQUIRED", "message": "请先登录设计工作台"}}, status_code=401)
    request.state.design_user = user
    if path == "/api/project" and request.method == "POST" and user.role != "designer":
        return JSONResponse({"error": {"message": "设计负责人只能分派任务"}}, status_code=403)
    project_id = path_project_id(path)
    if project_id is not None and not permitted_project(user.username, user.role, project_id, request.method):
        return JSONResponse({"error": {"message": "无权查看或修改这个设计项目"}}, status_code=403)
    return await call_next(request)


@app.on_event("startup")
def startup() -> None:
    init_db()
    migrate()
    from .design_demo_access import initialize
    initialize()
    from .agent.public_budget import initialize as initialize_public_budget
    with session() as db:
        initialize_public_budget(db)
        db.commit()
    seed_manager()
    if get_config().managed:
        from .agent.managed_bridge import recover_inflight
        recover_inflight()
        if os.getenv("AGENT_WORKER_ENABLED", "true") != "true":
            from .agent.runner import recover
            from .agent.store import transaction
            with transaction() as db:
                recover(db)
    if os.getenv("AGENT_WORKER_ENABLED", "true") == "true":
        worker.start()


@app.on_event("shutdown")
def shutdown() -> None:
    worker.stop()


@app.get("/api/health")
def health() -> dict[str, bool]:
    return {"ok": True}


class ProjectIn(BaseModel):
    name: str = "未命名项目"
    cms_package_id: str | None = None


@app.post("/api/project")
def create_project(payload: ProjectIn, request: Request) -> dict[str, int | str]:
    package_id = (payload.cms_package_id or "").strip() or None
    snapshot = None
    try:
        if package_id:
            snapshot = cms.fetch_package(package_id)
        with session() as db:
            project = Project(
                name=payload.name,
                cms_package_id=package_id,
                cms_package_snapshot=json.dumps(snapshot, ensure_ascii=False) if snapshot else None,
            )
            db.add(project)
            db.flush()
            user = getattr(request.state, "design_user", None)
            if user:
                db.add(DesignProjectAccess(project_id=project.id, username=user.username))
            db.commit()
            return {
                "id": project.id,
                "name": project.name,
                "status": project.status,
                "cms_package_id": project.cms_package_id,
            }
    except AgentError as exc:
        return JSONResponse(status_code=exc.status, content={"error": {"code": exc.code, "message": exc.message}})
