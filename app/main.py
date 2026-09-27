"""款式工场：项目入口与对话式设计 Agent。"""

from __future__ import annotations

import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from .agent.migrate import migrate
from .agent.routes import router as design_agent_router
from .agent.runner import worker
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


@app.post("/api/project")
def create_project(payload: ProjectIn) -> dict[str, int | str]:
    with session() as db:
        project = Project(name=payload.name)
        db.add(project)
        db.commit()
        return {"id": project.id, "name": project.name, "status": project.status}
