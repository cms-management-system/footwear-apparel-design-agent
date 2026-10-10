"""项目基础表；设计版本与任务记录由 app.agent.store 管理。"""

from __future__ import annotations

import time
from pathlib import Path

from sqlalchemy import JSON, ForeignKey, Integer, String, Text, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from .config import ROOT, get_config


class Base(DeclarativeBase):
    pass


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


class Project(Base):
    __tablename__ = "project"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(120))
    category: Mapped[str] = mapped_column(String(40), default="连衣裙")
    status: Mapped[str] = mapped_column(String(40), default="draft")
    created_at: Mapped[str] = mapped_column(String(32), default=_now)
    cms_package_id: Mapped[str | None] = mapped_column(String(80), nullable=True, default=None)
    cms_package_snapshot: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    cms_responses: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)


class DesignerUser(Base):
    __tablename__ = "designer_user"
    username: Mapped[str] = mapped_column(String(80), primary_key=True)
    password_hash: Mapped[str] = mapped_column(String(256))
    display_name: Mapped[str] = mapped_column(String(100))
    role: Mapped[str] = mapped_column(String(20))  # manager / designer
    active: Mapped[bool] = mapped_column(default=True)


class DesignerSession(Base):
    __tablename__ = "designer_session"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    username: Mapped[str] = mapped_column(ForeignKey("designer_user.username"))
    expires_at: Mapped[int] = mapped_column(Integer)


class DesignDemoChain(Base):
    __tablename__ = "design_demo_chain"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    scope_id: Mapped[str] = mapped_column(String(160))
    instance_id: Mapped[str] = mapped_column(String(160))
    purpose: Mapped[str] = mapped_column(String(20))
    payload: Mapped[dict] = mapped_column(JSON)
    selected_role: Mapped[str] = mapped_column(String(20))
    active: Mapped[bool] = mapped_column(default=True)


class DesignDemoSession(Base):
    __tablename__ = "design_demo_session"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    chain_id: Mapped[str] = mapped_column(ForeignKey("design_demo_chain.id"))
    role: Mapped[str] = mapped_column(String(20))
    subject: Mapped[str] = mapped_column(String(80))


class DesignProjectAccess(Base):
    __tablename__ = "design_project_access"
    project_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(ForeignKey("designer_user.username"))


class DesignHandoff(Base):
    __tablename__ = "design_handoff"
    id: Mapped[str] = mapped_column(String(220), primary_key=True)
    package_id: Mapped[str] = mapped_column(String(160))
    version: Mapped[str] = mapped_column(String(40))
    snapshot: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(32), default="new")
    assignee: Mapped[str | None] = mapped_column(String(80), nullable=True)
    project_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    submitted_version_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    manager_note: Mapped[str] = mapped_column(String(2000), default="")
    image_token: Mapped[str | None] = mapped_column(String(96), nullable=True)
    created_at: Mapped[str] = mapped_column(String(40), default=_now)


_engine = None
_Session = None


def engine():
    global _engine, _Session
    url = get_config().db_url
    if url.startswith("sqlite:///"):
        path = Path(url.removeprefix("sqlite:///"))
        if not path.is_absolute():
            path = ROOT / path
        path.parent.mkdir(parents=True, exist_ok=True)
        url = f"sqlite:///{path}"
    if _engine is None:
        _engine = create_engine(url, future=True)
        _Session = sessionmaker(bind=_engine, expire_on_commit=False)
    return _engine


def session() -> Session:
    engine()
    assert _Session is not None
    return _Session()


def reset_engine_for_tests() -> None:
    global _engine, _Session
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _Session = None


def init_db() -> None:
    Base.metadata.create_all(engine())
