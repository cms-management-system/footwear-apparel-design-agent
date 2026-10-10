"""Short SQLite transactions; JSON snapshots are replaced, never mutated in-place."""

import hashlib
import json
import uuid
from contextlib import contextmanager
from contextvars import ContextVar, Token
from datetime import UTC, datetime

from sqlalchemy import JSON, Integer, String, Text, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from ..models import Project, session

_request_transaction: ContextVar[Session | None] = ContextVar("design_request_transaction", default=None)


def bind_transaction(db: Session) -> Token:
    """Borrow a single sequential unit of work across the route's worker calls."""
    if _request_transaction.get() is not None:
        raise RuntimeError("A request unit of work is already bound")
    return _request_transaction.set(db)


def reset_transaction(token: Token) -> None:
    _request_transaction.reset(token)


class AgentError(Exception):
    def __init__(self, code: str, message: str, status: int = 409, *, details=None, receipt=None):
        self.code, self.message, self.status = code, message, status
        self.details = details or []
        self.receipt = receipt or {}
        super().__init__(message)


def now():
    return datetime.now(UTC).isoformat()


def uid():
    return uuid.uuid4().hex


def fingerprint(data):
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


class AgentBase(DeclarativeBase):
    pass


class Record(AgentBase):
    """Immutable asset/spec/version content; task and head payloads are transactional state."""

    __tablename__ = "design_agent_record"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    project_id: Mapped[int] = mapped_column(Integer, index=True)
    kind: Mapped[str] = mapped_column(String(20), index=True)
    status: Mapped[str] = mapped_column(String(32))
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[str] = mapped_column(String(40), default=now)
    updated_at: Mapped[str] = mapped_column(String(40), default=now)
    # One globally unique key for tasks; UUID keys for other records.
    dedupe: Mapped[str] = mapped_column(String(160), unique=True)


class WorkerLease(AgentBase):
    __tablename__ = "design_agent_lease"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner: Mapped[str] = mapped_column(Text)
    expires: Mapped[int] = mapped_column(Integer)


@contextmanager
def transaction():
    from .access_context import validate_transaction_access

    shared = _request_transaction.get()
    if shared is not None:
        validate_transaction_access(shared)
        yield shared
        validate_transaction_access(shared)
        return
    with session() as db:
        try:
            if db.bind.dialect.name == "sqlite":
                db.connection().exec_driver_sql("BEGIN IMMEDIATE")
            validate_transaction_access(db)
            yield db
            validate_transaction_access(db)
            db.commit()
        except Exception:
            db.rollback()
            raise


def require(db, id, kind=None, project_id=None):
    row = db.get(Record, id)
    if row is None or (kind and row.kind != kind) or (project_id is not None and row.project_id != project_id):
        raise AgentError("NOT_FOUND", "记录不存在或不属于当前项目", 404)
    return row


def project(db, pid):
    if db.get(Project, pid) is None:
        raise AgentError("NOT_FOUND", "项目不存在", 404)


def head(db, pid):
    project(db, pid)
    row = db.get(Record, f"head_{pid}")
    if row is None:
        row = create(db, pid, "head", {}, id=f"head_{pid}")
    return row


def create(db, pid, kind, payload, status="draft", id=None, dedupe=None):
    id = id or uid()
    row = Record(id=id, project_id=pid, kind=kind, payload=payload, status=status, dedupe=dedupe or id)
    db.add(row)
    db.flush()
    return row


def change(row, **updates):
    row.payload = {**row.payload, **updates}
    row.updated_at = now()


def serialize(row):
    payload = row.payload
    if row.kind == "task":
        payload = {k: v for k, v in payload.items() if k not in {"auth_context_id", "legacy_policy_sha256"}}
    return {
        "id": row.id,
        "project_id": row.project_id,
        "status": row.status,
        "created_at": row.created_at,
        **payload,
    }


def records(db, pid, kind):
    return list(
        db.scalars(select(Record).where(Record.project_id == pid, Record.kind == kind).order_by(Record.created_at))
    )
