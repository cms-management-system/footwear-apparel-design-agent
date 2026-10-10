"""Approved product requirements become accountable design-team work."""

from __future__ import annotations

import hashlib
import secrets

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import select

from ..config import get_config
from ..design_auth import CurrentDesigner, ManagerDesigner
from ..models import DesignerUser, DesignHandoff, DesignProjectAccess, Project, session
from . import assets, service
from .product_bridge import ProductBridge
from .schemas import Constraint, SpecIn
from .store import Record, create, head

router = APIRouter(prefix="/api", tags=["design-handoff"])


def item(row: DesignHandoff) -> dict:
    return {
        "id": row.id,
        "package_id": row.package_id,
        "version": row.version,
        "package": row.snapshot,
        "status": row.status,
        "assignee": row.assignee,
        "project_id": row.project_id,
        "submitted_version_id": row.submitted_version_id,
        "manager_note": row.manager_note,
        "created_at": row.created_at,
    }


@router.post("/design-handoffs/sync")
async def sync(request: Request, _: ManagerDesigner):
    if get_config().managed:
        from .managed_bridge import sync_packages
        return await sync_packages(request, await request.body())
    packages = ProductBridge().list_approved()
    with session() as db:
        for package in packages:
            if package.package_id.startswith("PKG-DEMO-"):
                continue
            key = f"{package.package_id}@{package.version}"
            if db.get(DesignHandoff, key) is None:
                db.add(
                    DesignHandoff(
                        id=key,
                        package_id=package.package_id,
                        version=package.version,
                        snapshot=package.model_dump(),
                        status="new",
                    )
                )
        db.commit()
    return {"synced": len(packages)}


@router.get("/design-handoffs")
def list_handoffs(
    request: Request, user: CurrentDesigner, offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100)
):
    if get_config().managed:
        from . import managed_workflow
        return managed_workflow.list_handoffs(request, offset, limit)
    with session() as db:
        query = select(DesignHandoff).order_by(DesignHandoff.created_at.desc())
        if user.role != "manager":
            query = query.where(DesignHandoff.assignee == user.username)
        return {"items": [item(row) for row in db.scalars(query)]}


class Decision(BaseModel):
    action: str
    assignee: str | None = None
    note: str = Field(default="", max_length=2000)


@router.post("/design-handoffs/{handoff_id}/decision")
async def decide(handoff_id: str, data: Decision, request: Request, _: ManagerDesigner):
    if get_config().managed:
        from . import managed_workflow
        return managed_workflow.decide(request, handoff_id, await request.body())
    with session() as db:
        row = db.get(DesignHandoff, handoff_id)
        if not row:
            raise HTTPException(404, "需求包不存在")
        if data.action == "accept" and row.status in {"new", "returned"}:
            row.status = "accepted"
        elif data.action == "return" and row.status in {"new", "accepted"}:
            if not data.note.strip():
                raise HTTPException(400, "请填写需要产品团队澄清的问题")
            event_id = hashlib.sha256(f"{row.id}:clarification:{data.note.strip()}".encode()).hexdigest()
            ProductBridge().send_event({
                "event_id": event_id,
                "package_id": row.package_id,
                "version": row.version,
                "kind": "clarification",
                "note": data.note.strip(),
            })
            row.status = "returned"
        elif data.action == "assign" and row.status == "accepted":
            staff = db.get(DesignerUser, data.assignee or "")
            if not staff or not staff.active or staff.role != "designer":
                raise HTTPException(400, "请选一位有效的设计人员")
            if row.project_id is None:
                project = Project(name=row.snapshot["requirement_desc"][:80])
                db.add(project)
                db.flush()
                row.project_id = project.id
                db.add(DesignProjectAccess(project_id=project.id, username=staff.username))
                # The original approved requirement remains visible and editable as a draft spec.
                service.save_spec(
                    db,
                    project.id,
                    SpecIn(
                        intent=row.snapshot["requirement_desc"][:4000],
                        constraints=[
                            Constraint(id=f"c_product_{index}", kind="preference", text=value[:500])
                            for index, value in enumerate((row.snapshot.get("constraints") or [])[:30])
                        ],
                        assumptions=[f"来源：知需 {row.package_id} {row.version}；设计前请核对证据与尺寸"],
                    ),
                )
                head(db, project.id)
                create(
                    db,
                    project.id,
                    "message",
                    {
                        "role": "system",
                        "text": (
                            (
                                f"测试导入需求包 {row.package_id} {row.version}，未经过正式产品审批。"
                                if row.snapshot.get("trial_import")
                                else f"产品负责人已批准需求包 {row.package_id} {row.version}。"
                            )
                            + "请核对要求单，澄清后再开始生成设计。"
                        ),
                    },
                    "saved",
                )
            else:
                access = db.get(DesignProjectAccess, row.project_id)
                if access:
                    access.username = staff.username
            row.assignee = staff.username
            row.status = "assigned"
        else:
            raise HTTPException(409, "当前状态不能执行该操作")
        row.manager_note = data.note.strip()
        db.commit()
        db.refresh(row)
        return item(row)


@router.post("/design-handoffs/{handoff_id}/submit")
async def submit_design(handoff_id: str, request: Request, user: CurrentDesigner):
    if get_config().managed:
        from . import managed_workflow
        return managed_workflow.submit(request, handoff_id, await request.body())
    if user.role != "designer":
        raise HTTPException(403, "仅设计人员可以提交方案")
    with session() as db:
        row = db.get(DesignHandoff, handoff_id)
        if not row or row.assignee != user.username:
            raise HTTPException(404, "未找到分派给你的任务")
        if row.status != "assigned" or row.project_id is None:
            raise HTTPException(409, "任务当前不能提交")
        versions = list(
            db.scalars(
                select(Record)
                .where(Record.project_id == row.project_id, Record.kind == "version", Record.status == "confirmed")
                .order_by(Record.created_at.desc())
            )
        )
        if not versions:
            raise HTTPException(409, "请先在设计项目中确认一个版本")
        row.submitted_version_id = versions[0].id
        row.status = "review"
        db.commit()
        db.refresh(row)
        return item(row)


class Review(BaseModel):
    action: str
    note: str = Field(default="", max_length=2000)


@router.post("/design-handoffs/{handoff_id}/review")
async def review_design(handoff_id: str, data: Review, request: Request, _: ManagerDesigner):
    if get_config().managed:
        from . import managed_workflow
        return managed_workflow.review(request, handoff_id, await request.body())
    with session() as db:
        row = db.get(DesignHandoff, handoff_id)
        if not row or row.status != "review" or row.project_id is None:
            raise HTTPException(409, "当前没有待审核的设计版本")
        if data.action == "revise":
            row.status = "assigned"
            row.submitted_version_id = None
            row.manager_note = data.note.strip() or "请根据审核意见修订"
            db.commit()
            return item(row)
        if data.action != "approve":
            raise HTTPException(400, "未知审核操作")
        version = db.get(Record, row.submitted_version_id) if row.submitted_version_id else None
        if not version:
            raise HTTPException(409, "没有已确认的设计版本")
        if not row.image_token:
            row.image_token = secrets.token_urlsafe(32)
            db.commit()  # Keep retry URL stable if the product workbench is temporarily unavailable.
        image_url = f"{get_config().design_public_base_url}/api/design-public/{row.image_token}.png"
        payload = {
            "event_id": hashlib.sha256(f"{row.id}:design:{version.id}".encode()).hexdigest(),
            "package_id": row.package_id,
            "kind": "design_approved",
            "design_version_id": version.id,
            "image_url": image_url,
            "note": data.note.strip() or f"回应需求版本 {row.package_id} {row.version}",
            "version": row.version,
        }
        ProductBridge().send_event(payload)
        row.status = "sent"
        row.manager_note = data.note.strip()
        db.commit()
        db.refresh(row)
        return item(row)


@router.get("/design-public/{token}.png")
def public_image(token: str, request: Request):
    if get_config().managed:
        from .managed_bridge import private_legacy_image
        return private_legacy_image(request, token)
    if len(token) < 30:
        raise HTTPException(404)
    with session() as db:
        row = db.scalar(select(DesignHandoff).where(DesignHandoff.image_token == token))
        if not row or row.status != "sent" or row.project_id is None:
            raise HTTPException(404)
        version = db.get(Record, row.submitted_version_id) if row.submitted_version_id else None
        if not version:
            raise HTTPException(404)
        return FileResponse(assets.file_path(version.payload["image"]), media_type="image/png")


@router.get("/design-handoffs/{handoff_id}")
def handoff_detail(handoff_id: str, request: Request, _: CurrentDesigner):
    from . import managed_workflow
    return managed_workflow.detail(request, handoff_id)


@router.get("/design-operations/{action_id}")
def operation_detail(action_id: str, request: Request, _: CurrentDesigner):
    from . import managed_workflow
    return managed_workflow.operation(request, action_id)


@router.post("/design-handoffs/{handoff_id}/delivery")
async def deliver_handoff(handoff_id: str, request: Request, _: ManagerDesigner):
    from .managed_bridge import deliver
    return await deliver(request, handoff_id, await request.body())
