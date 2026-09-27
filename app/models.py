"""项目基础表；设计版本与任务记录由 app.agent.store 管理。"""

from __future__ import annotations

import time
from pathlib import Path

from sqlalchemy import String, create_engine
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
