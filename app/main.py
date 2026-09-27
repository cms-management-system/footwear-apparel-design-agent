"""款式工场：项目入口与对话式设计 Agent。"""

from __future__ import annotations

import json
import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .agent import cms
from .agent.migrate import migrate
from .agent.routes import router as design_agent_router
from .agent.runner import worker
from .agent.store import AgentError
from .models import Project, init_db, session

app = FastAPI(title="款式工场 · 鞋服智能设计 Agent", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5180", "http://127.0.0.1:5180"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(design_agent_router)


@app.on_event("startup")
def startup() -> None:
    init_db()
    migrate()
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
def create_project(payload: ProjectIn):
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
            db.commit()
            return {
                "id": project.id,
                "name": project.name,
                "status": project.status,
                "cms_package_id": project.cms_package_id,
            }
    except AgentError as exc:
        return JSONResponse(status_code=exc.status, content={"error": {"code": exc.code, "message": exc.message}})
