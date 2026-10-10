"""款式工场：项目入口与对话式设计 Agent。"""

from __future__ import annotations

import json
import os

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .agent import cms
from .agent.handoff_routes import router as handoff_router
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


@app.middleware("http")
async def design_access(request: Request, call_next):
    path = request.url.path
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
    seed_manager()
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
