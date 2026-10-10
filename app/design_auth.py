"""Local design-team accounts and project access controls."""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import time
from contextlib import contextmanager
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from .config import get_config
from .models import DesignerSession, DesignerUser, DesignHandoff, DesignProjectAccess, Project, session

COOKIE = "design_session"
router = APIRouter(prefix="/api/design-auth", tags=["design-auth"])


def cookie_name() -> str:
    return get_config().design_session_cookie


def password_hash(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 260_000)
    return f"{salt}:{digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt, expected = stored.split(":", 1)
        if len(salt) != 32 or len(expected) != 64:
            return False
        return hmac.compare_digest(password_hash(password, salt), stored)
    except (ValueError, TypeError):
        return False


def seed_manager() -> None:
    cfg = get_config()
    if not cfg.design_auth_required:
        return
    with session() as db:
        if db.query(DesignerUser).filter_by(role="manager", active=True).first():
            return
        if not cfg.design_manager_username or not cfg.design_manager_password_hash:
            raise RuntimeError("DESIGN_AUTH_REQUIRED needs a configured manager account")
        user = db.get(DesignerUser, cfg.design_manager_username)
        if user is None:
            db.add(
                DesignerUser(
                    username=cfg.design_manager_username,
                    password_hash=cfg.design_manager_password_hash,
                    display_name="设计负责人",
                    role="manager",
                    active=True,
                )
            )
            db.commit()


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def session_context(session_token_hash: str) -> str:
    """Stable, non-credential session label bound to this instance and scope."""
    cfg = get_config()
    return hashlib.sha256(
        "\0".join(("design-auth-context-v1", cfg.design_instance_id, cfg.design_scope_id, session_token_hash)).encode()
    ).hexdigest()


def current_user(request: Request) -> DesignerUser:
    cfg = get_config()
    if not cfg.design_auth_required:
        raise HTTPException(503, "设计团队账号尚未启用")
    from .design_demo_access import session_for_request
    with session() as db:
        record = session_for_request(db, request)
        user = db.get(DesignerUser, record.username, populate_existing=True) if record else None
        if not user or not user.active:
            raise HTTPException(401, "登录已失效")
        if cfg.managed:
            request.state.design_auth_context_id = session_context(record.token_hash)
        db.expunge(user)
        return user


CurrentDesigner = Annotated[DesignerUser, Depends(current_user)]


def manager(user: CurrentDesigner) -> DesignerUser:
    if user.role != "manager":
        raise HTTPException(403, "仅设计负责人可操作")
    return user


ManagerDesigner = Annotated[DesignerUser, Depends(manager)]


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1)


class StaffIn(BaseModel):
    username: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,39}$")
    display_name: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=12, max_length=128)


class RegistrationIn(StaffIn):
    confirm_password: str


class PasswordChangeIn(BaseModel):
    current_password: str
    new_password: str = Field(min_length=12, max_length=128)


class StaffUpdateIn(BaseModel):
    action: str = Field(pattern=r"^(deactivate|activate|reset_password)$")
    password: str | None = Field(default=None, min_length=12, max_length=128)


@router.post("/login")
def login(data: LoginIn, request: Request, response: Response):
    if not get_config().design_auth_required:
        raise HTTPException(503, "设计团队账号尚未启用")
    with session() as db:
        user = db.get(DesignerUser, data.username)
        if not user or not user.active or not verify_password(data.password, user.password_hash):
            raise HTTPException(401, "账号或密码不正确")
        _start_session(db, response, user.username, request=request)
        from .design_demo_access import is_demo_request
        from .design_demo_access import logout as demo_logout
        if is_demo_request(request):
            demo_logout(request, response)
        return {"username": user.username, "display_name": user.display_name, "role": user.role}


def _start_session(db, response: Response, username: str, *, request=None) -> None:
    token = secrets.token_urlsafe(48)
    db.add(DesignerSession(token_hash=token_hash(token), username=username, expires_at=int(time.time()) + 12 * 3600))
    if request is not None and get_config().managed:
        from .design_demo_access import inherit_validation
        inherit_validation(db, request, session_context(token_hash(token)), username)
    db.commit()
    cfg = get_config()
    response.set_cookie(
        cfg.design_session_cookie, token, httponly=True, samesite="strict",
        secure=cfg.design_session_cookie_secure, path="/", max_age=12 * 3600,
    )


@router.post("/register", status_code=201)
def register(data: RegistrationIn, response: Response):
    if get_config().managed:
        raise HTTPException(403, "请由设计负责人创建团队账号")
    if not get_config().design_auth_required:
        raise HTTPException(503, "设计团队账号尚未启用")
    if data.password != data.confirm_password:
        raise HTTPException(422, "两次输入的密码不一致")
    display_name = data.display_name.strip()
    if not display_name:
        raise HTTPException(422, "请填写姓名")
    with session() as db:
        if db.get(DesignerUser, data.username):
            raise HTTPException(409, "账号已存在，请换一个账号")
        db.add(DesignerUser(
            username=data.username, display_name=display_name, password_hash=password_hash(data.password),
            role="designer", active=True,
        ))
        _start_session(db, response, data.username)
    return {"username": data.username, "display_name": display_name, "role": "designer"}


@router.post("/logout")
def logout(request: Request, response: Response):
    from . import design_demo_access
    if get_config().design_demo_access_enabled or design_demo_access.is_demo_request(request):
        return design_demo_access.logout(request, response)
    cookie = get_config().design_session_cookie
    token = request.cookies.get(cookie)
    if token:
        with session() as db:
            record = db.get(DesignerSession, token_hash(token))
            if record:
                db.delete(record)
                db.commit()
    response.delete_cookie(cookie, path="/")
    return {"ok": True}


@router.get("/me")
def me(request: Request, user: CurrentDesigner):
    result = {"username": user.username, "display_name": user.display_name, "role": user.role}
    cfg = get_config()
    if cfg.managed:
        result.update(instance_id=cfg.design_instance_id, scope_id=cfg.design_scope_id,
                      auth_context_id=request.state.design_auth_context_id, integration_mode="managed")
    from .design_demo_access import ENTRIES, is_demo_request
    demo = cfg.design_demo_access_enabled and is_demo_request(request)
    result.update(access_mode="demo" if demo else "authenticated", demo_entry_urls=ENTRIES if demo else None)
    if demo:
        result["display_name"] = "演示设计员工" if user.role == "designer" else "演示设计经理"
    return result


class DemoAccessIn(BaseModel):
    model_config = {"extra": "forbid"}
    role: str = Field(pattern=r"^(designer|manager)$")


@router.post("/demo-access")
def demo_access(data: DemoAccessIn, request: Request):
    from .design_demo_access import bootstrap
    bootstrap(request, data.role)
    return me(request, current_user(request))


@router.get("/staff")
def staff(_: ManagerDesigner):
    with session() as db:
        return {
            "items": [
                {"username": user.username, "display_name": user.display_name, "active": user.active}
                for user in db.query(DesignerUser).filter_by(role="designer").order_by(DesignerUser.username).all()
            ]
        }


def _account_principal(db, request: Request, expected_user: DesignerUser, *, manager_only: bool):
    """Re-read account authority on the same connection used for personnel writes."""
    from .agent.store import AgentError
    from .design_demo_access import session_for_request
    record = session_for_request(db, request)
    user = (db.get(DesignerUser, record.username, populate_existing=True)
            if record and record.expires_at > int(time.time()) else None)
    if not user or not user.active:
        raise AgentError("LOGIN_REQUIRED", "登录已失效，请重新登录设计工作台", 401)
    if user.username != expected_user.username:
        raise AgentError("AUTH_CONTEXT_CHANGED", "当前登录主体已变化，请重新核对账号", 409)
    if (user.role not in {"manager", "designer"} or user.role != expected_user.role
            or (manager_only and user.role != "manager")):
        raise AgentError("ROLE_FORBIDDEN", "当前角色已无权执行此账号操作", 403)
    if request.headers.get("X-Design-Context") != session_context(record.token_hash):
        raise AgentError("AUTH_CONTEXT_CHANGED", "当前登录上下文已变化，请重新核对账号", 409)


@contextmanager
def _account_mutation(request: Request, user: DesignerUser, *, manager_only=False, revoke_own_sessions=False):
    with session() as db:
        try:
            managed = get_config().managed
            if managed:
                if db.bind.dialect.name == "sqlite":
                    db.connection().exec_driver_sql("BEGIN IMMEDIATE")
                _account_principal(db, request, user, manager_only=manager_only)
            yield db
            if managed:
                _account_principal(db, request, user, manager_only=manager_only)
            # A successful password change intentionally revokes its own session, after
            # the final authority check and in the same commit as the password update.
            if revoke_own_sessions:
                db.query(DesignerSession).filter_by(username=user.username).delete()
            db.commit()
        except Exception:
            db.rollback()
            raise


@router.post("/staff", status_code=201)
def create_staff(data: StaffIn, request: Request, user: ManagerDesigner):
    with _account_mutation(request, user, manager_only=True) as db:
        if db.get(DesignerUser, data.username):
            raise HTTPException(409, "账号已存在")
        db.add(
            DesignerUser(
                username=data.username,
                display_name=data.display_name.strip(),
                password_hash=password_hash(data.password),
                role="designer",
                active=True,
            )
        )
    return {"username": data.username, "display_name": data.display_name.strip(), "role": "designer"}


@router.post("/password")
def change_password(data: PasswordChangeIn, request: Request, user: CurrentDesigner):
    with _account_mutation(request, user, revoke_own_sessions=True) as db:
        record = db.get(DesignerUser, user.username)
        if not record or not verify_password(data.current_password, record.password_hash):
            raise HTTPException(400, "当前密码不正确")
        if data.current_password == data.new_password:
            raise HTTPException(400, "新密码不能与当前密码相同")
        record.password_hash = password_hash(data.new_password)
    return {"ok": True}


@router.patch("/staff/{username}")
def update_staff(username: str, data: StaffUpdateIn, request: Request, user: ManagerDesigner):
    with _account_mutation(request, user, manager_only=True) as db:
        record = db.get(DesignerUser, username)
        if not record or record.role != "designer":
            raise HTTPException(404, "设计人员不存在")
        if data.action == "reset_password":
            if not data.password:
                raise HTTPException(422, "请设置至少 12 位的新密码")
            record.password_hash = password_hash(data.password)
            db.query(DesignerSession).filter_by(username=username).delete()
        else:
            active = data.action == "activate"
            if not active and db.query(DesignHandoff).filter(
                DesignHandoff.assignee == username,
                DesignHandoff.status.in_(["assigned", "review"]),
            ).first():
                raise HTTPException(409, "此成员还有进行中的设计任务，请先处理任务")
            record.active = active
            if not active:
                db.query(DesignerSession).filter_by(username=username).delete()
        return {"username": username, "active": record.active}


def permitted_project(username: str, role: str, project_id: int, method: str) -> bool:
    with session() as db:
        if db.get(Project, project_id) is None:
            return False
        if get_config().managed:
            from .agent.managed_store import IndependentProject
            local = db.get(IndependentProject, project_id)
            if local:
                cfg = get_config()
                return bool(
                    local.status != "archived"
                    and
                    local.scope_id == cfg.design_scope_id and local.target_instance_id == cfg.design_instance_id
                    and ((role == "manager" and method in {"GET", "HEAD"})
                         or (role == "designer" and local.owner_subject == username))
                )
            handoffs = db.query(DesignHandoff).filter_by(project_id=project_id)
            if role == "manager":
                return method in {"GET", "HEAD"} and handoffs.first() is not None
            if role != "designer" or handoffs.filter_by(assignee=username).first() is None:
                return False
            access = db.get(DesignProjectAccess, project_id)
            return bool(access and access.username == username)
        if role == "manager":
            return method in {"GET", "HEAD"}
        access = db.get(DesignProjectAccess, project_id)
        return bool(access and access.username == username)


def path_project_id(path: str) -> int | None:
    direct = re.match(r"^/api/(?:projects?|design-projects)/(\d+)(?:/|$)", path)
    if direct:
        return int(direct.group(1))
    item = re.match(
        r"^/api/(?:design-assets|design-specs|design-tasks|style-plans|design-versions|technical-flats|design-models3d|design-creation-runs)/([^/]+)",
        path,
    )
    if item:
        from .agent.store import Record

        with session() as db:
            row = db.get(Record, item.group(1))
            return row.project_id if row else -1
    if get_config().managed:
        public = re.fullmatch(r"/api/design-public/([^/]+)\.png", path)
        if public:
            with session() as db:
                handoff = db.query(DesignHandoff).filter_by(image_token=public.group(1)).first()
                if handoff is None or handoff.project_id is None:
                    return -1
                # A token may locate an image, but must never authorize another project's version.
                from .agent.store import Record

                version = db.get(Record, handoff.submitted_version_id) if handoff.submitted_version_id else None
                if version is None or version.kind != "version" or version.project_id != handoff.project_id:
                    return -1
                return handoff.project_id
    return None
