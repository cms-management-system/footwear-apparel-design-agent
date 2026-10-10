"""Local design-team accounts and project access controls."""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import time
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from .config import get_config
from .models import DesignerSession, DesignerUser, DesignProjectAccess, DesignHandoff, Project, session

COOKIE = "design_session"
router = APIRouter(prefix="/api/design-auth", tags=["design-auth"])


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


def current_user(request: Request) -> DesignerUser:
    if not get_config().design_auth_required:
        raise HTTPException(503, "设计团队账号尚未启用")
    token = request.cookies.get(COOKIE)
    if not token:
        raise HTTPException(401, "请先登录设计工作台")
    with session() as db:
        record = db.get(DesignerSession, token_hash(token))
        user = db.get(DesignerUser, record.username) if record and record.expires_at > int(time.time()) else None
        if not user or not user.active:
            raise HTTPException(401, "登录已失效")
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
def login(data: LoginIn, response: Response):
    if not get_config().design_auth_required:
        raise HTTPException(503, "设计团队账号尚未启用")
    with session() as db:
        user = db.get(DesignerUser, data.username)
        if not user or not user.active or not verify_password(data.password, user.password_hash):
            raise HTTPException(401, "账号或密码不正确")
        _start_session(db, response, user.username)
        return {"username": user.username, "display_name": user.display_name, "role": user.role}


def _start_session(db, response: Response, username: str) -> None:
    token = secrets.token_urlsafe(48)
    db.add(DesignerSession(token_hash=token_hash(token), username=username, expires_at=int(time.time()) + 12 * 3600))
    db.commit()
    response.set_cookie(COOKIE, token, httponly=True, samesite="strict", secure=False, path="/", max_age=12 * 3600)


@router.post("/register", status_code=201)
def register(data: RegistrationIn, response: Response):
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
        db.add(DesignerUser(username=data.username, display_name=display_name, password_hash=password_hash(data.password), role="designer", active=True))
        _start_session(db, response, data.username)
    return {"username": data.username, "display_name": display_name, "role": "designer"}


@router.post("/logout")
def logout(request: Request, response: Response):
    token = request.cookies.get(COOKIE)
    if token:
        with session() as db:
            record = db.get(DesignerSession, token_hash(token))
            if record:
                db.delete(record)
                db.commit()
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}


@router.get("/me")
def me(user: CurrentDesigner):
    return {"username": user.username, "display_name": user.display_name, "role": user.role}


@router.get("/staff")
def staff(_: ManagerDesigner):
    with session() as db:
        return {
            "items": [
                {"username": user.username, "display_name": user.display_name, "active": user.active}
                for user in db.query(DesignerUser).filter_by(role="designer").order_by(DesignerUser.username).all()
            ]
        }


@router.post("/staff", status_code=201)
def create_staff(data: StaffIn, _: ManagerDesigner):
    with session() as db:
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
        db.commit()
    return {"username": data.username, "display_name": data.display_name.strip(), "role": "designer"}


@router.post("/password")
def change_password(data: PasswordChangeIn, user: CurrentDesigner):
    with session() as db:
        record = db.get(DesignerUser, user.username)
        if not record or not verify_password(data.current_password, record.password_hash):
            raise HTTPException(400, "当前密码不正确")
        if data.current_password == data.new_password:
            raise HTTPException(400, "新密码不能与当前密码相同")
        record.password_hash = password_hash(data.new_password)
        db.query(DesignerSession).filter_by(username=user.username).delete()
        db.commit()
    return {"ok": True}


@router.patch("/staff/{username}")
def update_staff(username: str, data: StaffUpdateIn, _: ManagerDesigner):
    with session() as db:
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
        db.commit()
        return {"username": username, "active": record.active}


def permitted_project(username: str, role: str, project_id: int, method: str) -> bool:
    with session() as db:
        if db.get(Project, project_id) is None:
            return False
        if role == "manager":
            return method in {"GET", "HEAD"}
        access = db.get(DesignProjectAccess, project_id)
        return bool(access and access.username == username)


def path_project_id(path: str) -> int | None:
    direct = re.match(r"^/api/project/(\d+)/", path)
    if direct:
        return int(direct.group(1))
    item = re.match(
        r"^/api/(?:design-assets|design-specs|design-tasks|style-plans|design-versions|technical-flats|design-models3d)/([^/]+)",
        path,
    )
    if item:
        from .agent.store import Record

        with session() as db:
            row = db.get(Record, item.group(1))
            return row.project_id if row else -1
    return None
