"""Recheck managed personnel access inside the transaction that reads or writes.

Only request-scoped metadata crosses worker threads. Background workers have no
request context and retain their own execution/lease rules. No raw cookie lives
in the context variable.
"""

from __future__ import annotations

import re
import time
from contextvars import ContextVar, Token
from dataclasses import dataclass

from fastapi import Request
from sqlalchemy import select

from ..config import get_config
from ..design_auth import session_context, token_hash
from ..models import DesignerUser, DesignHandoff, DesignProjectAccess, Project
from .managed_store import IndependentProject, ManagedReceipt


@dataclass(frozen=True)
class RequestAccess:
    token_hash: str
    expected_context: str | None
    method: str
    path: str
    demo: bool = False


_request_access: ContextVar[RequestAccess | None] = ContextVar("design_request_access", default=None)
_READ_METHODS = {"GET", "HEAD", "OPTIONS"}


def bind_request(request: Request) -> Token:
    cfg = get_config()
    access = None
    if cfg.managed:
        from ..design_demo_access import is_demo_request, request_token
        cookie = request_token(request)
        access = RequestAccess(
            token_hash=token_hash(cookie) if cookie else "",
            expected_context=request.headers.get("x-design-context"),
            method=request.method.upper(),
            path=request.url.path,
            demo=is_demo_request(request),
        )
    return _request_access.set(access)


def reset_request(token: Token) -> None:
    _request_access.reset(token)


def suspend_request() -> Token:
    """Temporarily enter a system transaction; the caller must restore the token.

    Intended for recording already-observed delivery outcomes independently of
    the original personnel session, never for authorizing a personnel action.
    """
    return _request_access.set(None)


def _project_id(db, path: str) -> int | None:
    """Resolve object ownership on the caller's connection, never a second DB."""
    from .store import Record

    direct = re.match(r"^/api/(?:projects?|design-projects)/(\d+)(?:/|$)", path)
    if direct:
        return int(direct.group(1))
    record = re.match(
        r"^/api/(?:design-assets|design-specs|design-tasks|style-plans|design-versions|technical-flats|design-models3d|design-creation-runs)/([^/]+)",
        path,
    )
    if record:
        row = db.get(Record, record.group(1), populate_existing=True)
        return row.project_id if row else -1
    public = re.fullmatch(r"/api/design-public/([^/]+)\.png", path)
    if public:
        row = db.scalar(
            select(DesignHandoff)
            .where(DesignHandoff.image_token == public.group(1))
            .execution_options(populate_existing=True)
        )
        version = (
            db.get(Record, row.submitted_version_id, populate_existing=True)
            if row and row.submitted_version_id
            else None
        )
        if not row or not version or version.kind != "version" or version.project_id != row.project_id:
            return -1
        return row.project_id
    return None


def project_binding(db, project_id: int, user: DesignerUser, *, write: bool = False, allow_archived: bool = False):
    """Return an authorized handoff and receipt without leaking hidden objects."""
    from .store import AgentError

    cfg = get_config()
    independent = db.get(IndependentProject, project_id, populate_existing=True)
    if independent is not None:
        if (
            independent.scope_id != cfg.design_scope_id
            or independent.target_instance_id != cfg.design_instance_id
            or user.role not in {"manager", "designer"}
            or (user.role != "manager" and independent.owner_subject != user.username)
        ):
            raise AgentError("NOT_FOUND", "记录不存在或当前账号不可访问", 404)
        if independent.status == "archived" and not allow_archived:
            raise AgentError("NOT_FOUND", "记录不存在或当前账号不可访问", 404)
        if write and user.role != "designer":
            raise AgentError("ROLE_FORBIDDEN", "自主设计仅所有者可以修改", 403)
        return independent, independent
    query = (
        select(DesignHandoff, ManagedReceipt)
        .join(ManagedReceipt, ManagedReceipt.handoff_id == DesignHandoff.id)
        .join(Project, Project.id == DesignHandoff.project_id)
        .where(
            DesignHandoff.project_id == project_id,
            ManagedReceipt.scope_id == cfg.design_scope_id,
            ManagedReceipt.target_instance_id == cfg.design_instance_id,
        )
    )
    if user.role != "manager":
        if user.role != "designer":
            raise AgentError("ROLE_FORBIDDEN", "当前角色不能访问设计项目", 403)
        query = query.join(DesignProjectAccess, DesignProjectAccess.project_id == Project.id).where(
            DesignHandoff.assignee == user.username,
            DesignProjectAccess.username == user.username,
        )
    binding = db.execute(query.execution_options(populate_existing=True)).first()
    if binding is None:
        raise AgentError("NOT_FOUND", "记录不存在或当前账号不可访问", 404)
    if write:
        if user.role == "manager":
            raise AgentError("ROLE_FORBIDDEN", "设计负责人通过任务审查操作，设计工作区仅供查看", 403)
        if binding[0].status != "assigned":
            raise AgentError("STATE_CONFLICT", "任务已提交审查或不在制作状态，请刷新任务", 409)
    return binding


def request_project_id(db) -> int | None:
    access = _request_access.get()
    return _project_id(db, access.path) if access else None


def request_context_id():
    access = _request_access.get()
    return access.expected_context if access else None


def authenticated_session(db):
    """Trusted session identity for private execution policy, never a caller-selected label."""
    access = _request_access.get()
    if not access or not access.token_hash:
        return None
    from ..design_demo_access import session_for_token_hash
    return session_for_token_hash(db, access.token_hash, demo=access.demo)


def validate_transaction_access(
    db, *, require_editable: bool = True, allow_archived: bool = False
) -> DesignerUser | None:
    """Authenticate again before work and commit using the current transaction."""
    from .store import AgentError

    access = _request_access.get()
    if not get_config().managed or access is None:
        return None
    record = authenticated_session(db)
    user = (
        db.get(DesignerUser, record.username, populate_existing=True)
        if record and record.expires_at > int(time.time())
        else None
    )
    if not user or not user.active:
        raise AgentError("LOGIN_REQUIRED", "登录已失效，请重新登录设计工作台", 401)
    write = access.method not in _READ_METHODS
    if write and access.expected_context != session_context(record.token_hash):
        raise AgentError("AUTH_CONTEXT_CHANGED", "当前登录上下文已变化，请核对任务后重试", 409)
    project_id = _project_id(db, access.path)
    if project_id is not None:
        project_binding(db, project_id, user, write=write and require_editable, allow_archived=allow_archived)
    return user
