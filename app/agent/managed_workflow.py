"""Version-bound design-team workflow. Approval and delivery are separate facts."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from datetime import date
from typing import Literal

from fastapi import Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import func, select

from ..config import get_config
from ..design_auth import session_context
from ..design_demo_access import session_for_request
from ..models import DesignerUser, DesignHandoff, DesignProjectAccess, Project
from . import assets, service
from .managed_store import ManagedAction, ManagedAudit, ManagedOutbox, ManagedReceipt
from .schemas import Constraint, SpecIn
from .store import AgentError, Record, create, head, now, records, transaction, uid


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class DecisionIn(Strict):
    action: Literal["accept", "return", "assign"]
    expected_revision: int = Field(ge=1)
    assignee: str | None = Field(default=None, max_length=80)
    note: str = Field(default="", max_length=2000)
    priority: Literal["P0", "P1", "P2", "P3"] | None = None
    due_date: date | None = None


class SubmitIn(Strict):
    expected_revision: int = Field(ge=1)
    version_id: str = Field(min_length=1, max_length=40)


class ReviewIn(Strict):
    expected_revision: int = Field(ge=1)
    action: Literal["revise", "approve"]
    submitted_version_id: str = Field(min_length=1, max_length=40)
    note: str = Field(min_length=1, max_length=2000)


class DeliveryIn(Strict):
    event_id: str = Field(min_length=1, max_length=180)
    expected_revision: int = Field(ge=1)
    action: Literal["send", "reconcile"]


def sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def canonical(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def parse(raw: bytes, schema):
    if len(raw) > 64 * 1024:
        raise AgentError("INVALID_INPUT", "请求过大", 413)
    try:

        def no_duplicates(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate JSON field")
                result[key] = value
            return result

        def no_constant(value):
            raise ValueError("non-finite JSON number")

        json.loads(raw.decode("utf-8"), object_pairs_hook=no_duplicates, parse_constant=no_constant)
        return schema.model_validate_json(raw)
    except (ValidationError, ValueError, UnicodeError) as exc:
        raise AgentError("INVALID_INPUT", "请核对动作、版本和必填字段", 422) from exc


def principal(db, request: Request, *, write=False, manager=False):
    """Revalidate the current session in the same transaction as the mutation."""
    cfg = get_config()
    if not cfg.managed:
        raise AgentError("NOT_FOUND", "接口未启用", 404)
    record = session_for_request(db, request)
    user = db.get(DesignerUser, record.username, populate_existing=True) if record else None
    if not user or not user.active:
        raise AgentError("LOGIN_REQUIRED", "请重新登录设计工作台", 401)
    if manager and user.role != "manager":
        raise AgentError("ROLE_FORBIDDEN", "仅设计负责人可操作", 403)
    context = session_context(record.token_hash)
    if write and request.headers.get("X-Design-Context") != context:
        raise AgentError("AUTH_CONTEXT_CHANGED", "登录身份已变化，请重新核对当前任务后操作", 409)
    return user, context


def visible(db, handoff_id, user):
    row = db.get(DesignHandoff, handoff_id)
    meta = db.get(ManagedReceipt, handoff_id)
    cfg = get_config()
    if not row or not meta or meta.scope_id != cfg.design_scope_id or meta.target_instance_id != cfg.design_instance_id:
        raise AgentError("NOT_FOUND", "未找到可访问的设计任务", 404)
    if user.role != "manager":
        access = db.get(DesignProjectAccess, row.project_id) if row.project_id else None
        if row.assignee != user.username or not access or access.username != user.username:
            raise AgentError("NOT_FOUND", "未找到可访问的设计任务", 404)
    return row, meta


def action_identity(request, user):
    action_id = request.headers.get("X-Design-Action-Id", "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,100}", action_id):
        raise AgentError("INVALID_INPUT", "请提供稳定的操作编号", 422)
    key = sha(canonical([get_config().design_scope_id, user.username, action_id]))
    return key, action_id


def replay(db, request, user, context, raw):
    key, action_id = action_identity(request, user)
    old = db.get(ManagedAction, key)
    if old:
        if old.auth_context_id != context:
            raise AgentError("AUTH_CONTEXT_CHANGED", "原操作属于另一登录会话；可只读查看原结果", 409)
        if old.method != request.method or old.path != request.url.path or old.raw_body != raw:
            raise AgentError("IDEMPOTENCY_CONFLICT", "同一操作编号不能用于不同请求", 409)
        return key, action_id, old.response
    return key, action_id, None


def save_action(db, request, user, context, raw, result, handoff_id, *, status_code=200):
    key, action_id = action_identity(request, user)
    db.add(
        ManagedAction(
            action_key=key,
            action_id=action_id,
            username=user.username,
            auth_context_id=context,
            method=request.method,
            path=request.url.path,
            raw_body=raw,
            status_code=status_code,
            response=result,
            handoff_id=handoff_id,
            created_at=now(),
        )
    )


def revision(meta, expected):
    if meta.revision != expected:
        raise AgentError("STATE_CONFLICT", "任务已有新修订，请保留草稿并重新核对", 409)


def audit(db, row, meta, user, kind, payload):
    meta.revision += 1
    event = ManagedAudit(
        id=uid(),
        handoff_id=row.id,
        revision=meta.revision,
        kind=kind,
        actor=user.username,
        payload=payload,
        created_at=now(),
    )
    db.add(event)
    db.flush()
    return event


def allowed(row, role):
    if role == "designer":
        return ["submit"] if row.status == "assigned" else []
    result = {
        "new": ["accept", "return"],
        "accepted": ["assign", "return"],
        "assigned": [],
        "review": ["approve", "revise"],
        "approved": ["send", "reconcile"],
        "returned": [],
    }.get(row.status, [])
    return result


def project_item(db, row, meta, user, *, detail=False):
    audits = list(
        db.scalars(select(ManagedAudit).where(ManagedAudit.handoff_id == row.id).order_by(ManagedAudit.revision))
    )
    submission = next((a.payload for a in reversed(audits) if a.kind == "submitted"), None)
    review = next(
        (
            {**a.payload, "reviewer": a.actor, "reviewed_at": a.created_at}
            for a in reversed(audits)
            if a.kind in {"approved", "revise"}
        ),
        None,
    )
    all_outboxes = list(
        db.scalars(
            select(ManagedOutbox).where(ManagedOutbox.handoff_id == row.id).order_by(ManagedOutbox.created_at.desc())
        )
    )
    outbox = all_outboxes[0] if all_outboxes else None
    result = {
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
        "revision": meta.revision,
        "priority": meta.priority,
        "due_date": meta.due_date,
        "allowed_actions": allowed(row, user.role),
        "source_receipt": meta.receipt,
        "submission": submission,
        "review": review,
        "delivery": {
            "status": outbox.status if outbox else "not_prepared",
            "event_id": outbox.event_id if outbox else None,
            "receipt": outbox.receipt if outbox else None,
        },
    }
    result["deliveries"] = [
        {
            "event_id": o.event_id,
            "status": o.status,
            "receipt": o.receipt,
            "kind": json.loads(base64.b64decode(json.loads(o.raw_body)["content_base64"]))["kind"],
        }
        for o in all_outboxes
    ]
    if user.role == "manager" and all_outboxes:
        result["allowed_actions"] = list(dict.fromkeys(result["allowed_actions"] + ["send", "reconcile"]))
    if detail:
        result["source_binding"] = {
            "package": meta.proof.get("package"),
            "requirement": meta.proof.get("requirement"),
            "prompt": meta.proof.get("prompt"),
            "receipt": meta.receipt,
        }
        result["history"] = [
            {
                "id": a.id,
                "revision": a.revision,
                "kind": a.kind,
                "actor": a.actor,
                "payload": a.payload,
                "created_at": a.created_at,
            }
            for a in audits
            if user.role == "manager" or a.kind in {"submitted", "approved", "revise"}
        ]
    return result


def list_handoffs(request, offset=0, limit=50):
    with transaction() as db:
        user, _ = principal(db, request)
        cfg = get_config()
        query = select(DesignHandoff, ManagedReceipt).join(
            ManagedReceipt, ManagedReceipt.handoff_id == DesignHandoff.id
        )
        query = query.where(
            ManagedReceipt.scope_id == cfg.design_scope_id, ManagedReceipt.target_instance_id == cfg.design_instance_id
        )
        if user.role != "manager":
            query = query.join(DesignProjectAccess, DesignProjectAccess.project_id == DesignHandoff.project_id).where(
                DesignHandoff.assignee == user.username, DesignProjectAccess.username == user.username
            )
        total = db.scalar(select(func.count()).select_from(query.subquery()))
        rows = db.execute(query.order_by(DesignHandoff.created_at.desc(), DesignHandoff.id).offset(offset).limit(limit))
        return {
            "items": [project_item(db, row, meta, user) for row, meta in rows],
            "total": total,
            "offset": offset,
            "limit": limit,
        }


def detail(request, handoff_id):
    with transaction() as db:
        user, _ = principal(db, request)
        row, meta = visible(db, handoff_id, user)
        return project_item(db, row, meta, user, detail=True)


def operation(request, action_id):
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,100}", action_id):
        raise AgentError("NOT_FOUND", "操作不存在", 404)
    with transaction() as db:
        user, _ = principal(db, request)
        key = sha(canonical([get_config().design_scope_id, user.username, action_id]))
        action = db.get(ManagedAction, key)
        if not action or action.username != user.username:
            raise AgentError("NOT_FOUND", "操作不存在", 404)
        if action.handoff_id:
            if action.handoff_id.startswith("independent:"):
                from .access_context import project_binding
                from .managed_store import IndependentProject

                pid = int(action.handoff_id.split(":", 1)[1])
                local = db.get(IndependentProject, pid)
                if local and local.status == "archived":
                    project_binding(db, pid, user, allow_archived=True)
                    if not action.response.get("archived") or action.path != f"/api/design-projects/{pid}/archive":
                        raise AgentError("NOT_FOUND", "操作不存在或项目已删除", 404)
                else:
                    project_binding(db, pid, user)
            else:
                visible(db, action.handoff_id, user)
        result = {
            "action_id": action.action_id,
            "method": action.method,
            "path": action.path,
            "status_code": action.status_code,
            "result": action.response,
            "created_at": action.created_at,
        }
        if action.response.get("revision_domain") == "layout":
            result.update(revision_domain="layout", layout_revision=action.response["layout_revision"],
                          base_layout_revision=action.response["base_layout_revision"])
        return result


def editable_project(db, pid):
    if any(r.status in service.BUSY for r in records(db, pid, "task")):
        raise AgentError("TASK_ACTIVE", "请等待当前生成结束或明确取消后再操作", 409)


def decide(request, handoff_id, raw):
    data = parse(raw, DecisionIn)
    with transaction() as db:
        user, context = principal(db, request, write=True, manager=True)
        row, meta = visible(db, handoff_id, user)
        _, _, old = replay(db, request, user, context, raw)
        if old is not None:
            return old
        revision(meta, data.expected_revision)
        before = {"status": row.status, "assignee": row.assignee, "priority": meta.priority, "due_date": meta.due_date}
        note = data.note.strip()
        if data.action == "accept" and row.status == "new":
            row.status = "accepted"
        elif data.action == "return" and row.status in {"new", "accepted"}:
            if not note:
                raise AgentError("INVALID_INPUT", "请填写需要澄清的问题", 422)
            row.status = "returned"
        elif data.action == "assign" and row.status == "accepted":
            staff = db.get(DesignerUser, data.assignee or "")
            if not staff or not staff.active or staff.role != "designer":
                raise AgentError("INVALID_ASSIGNEE", "请选择本工作区内有效的设计人员", 422)
            if row.project_id is not None:
                raise AgentError("STATE_CONFLICT", "此任务已有分派项目，不能重复分派", 409)
            prompt = meta.proof.get("prompt")
            requirement = meta.proof.get("requirement")
            if not isinstance(prompt, dict) or not isinstance(requirement, dict):
                raise AgentError("SOURCE_MAPPING_REQUIRED", "来源需求尚未完成正式字段映射", 409)
            project = Project(name=requirement["title"][:80])
            db.add(project)
            db.flush()
            row.project_id = project.id
            db.add(DesignProjectAccess(project_id=project.id, username=staff.username))
            db.flush()
            constraints = [
                Constraint(id=f"c_product_{i}", kind="must_keep", text=text)
                for i, text in enumerate(requirement["constraints"])
            ]
            constraints.extend(
                Constraint(id=f"c_avoid_{i}", kind="forbidden", text=text)
                for i, text in enumerate(prompt["avoid_items"])
            )
            service.save_spec(
                db,
                project.id,
                SpecIn(
                    intent=prompt["positive_prompt"],
                    constraints=constraints,
                    assumptions=[f"不可变来源 {row.package_id} {row.version}；材料工艺待知识库核验。"],
                ),
                actor=user.username,
            )
            create(
                db,
                project.id,
                "source",
                {
                    "handoff_id": row.id,
                    "package": row.snapshot,
                    "envelope_sha256": meta.envelope_sha256,
                    "source_receipt": meta.receipt,
                },
                "frozen",
            )
            create(
                db,
                project.id,
                "message",
                {
                    "role": "system",
                    "text": (f"已分派产品批准需求 {row.package_id} {row.version}。完整提示词与规格已载入；"
                             "本机合成技术验证不代表真实业务人审。"),
                },
                "saved",
            )
            row.assignee = staff.username
            row.status = "assigned"
            meta.priority = data.priority or meta.priority
            if "due_date" in data.model_fields_set:
                meta.due_date = data.due_date.isoformat() if data.due_date else None
        else:
            raise AgentError("STATE_CONFLICT", "当前状态不能执行该操作", 409)
        row.manager_note = note
        event = audit(
            db,
            row,
            meta,
            user,
            data.action,
            {
                "before": before,
                "status": row.status,
                "assignee": row.assignee,
                "priority": meta.priority,
                "due_date": meta.due_date,
                "note": note,
            },
        )
        if data.action == "return":
            from .managed_bridge import freeze_clarification

            freeze_clarification(db, row, meta, event)
        result = project_item(db, row, meta, user)
        save_action(db, request, user, context, raw, result, row.id)
        return result


def image_snapshot(version):
    image = version.payload.get("image")
    if not isinstance(image, dict):
        raise AgentError("ASSET_MISSING", "该版本尚无有效结果图片", 409)
    path = assets.file_path(image)
    digest = sha(path.read_bytes())
    if digest != image.get("sha256"):
        raise AgentError("ASSET_DIGEST_MISMATCH", "结果图片摘要不符，请核对原文件", 409)
    return {
        "asset_id": version.id,
        "sha256": digest,
        "media_type": image.get("mime_type", "image/png"),
        "width": image.get("width"),
        "height": image.get("height"),
        "byte_length": path.stat().st_size,
        "generation_mode": version.payload.get("generation_mode", "unknown"),
    }


def submit(request, handoff_id, raw):
    data = parse(raw, SubmitIn)
    with transaction() as db:
        user, context = principal(db, request, write=True)
        if user.role != "designer":
            raise AgentError("ROLE_FORBIDDEN", "仅当前设计人员可以提交方案", 403)
        row, meta = visible(db, handoff_id, user)
        _, _, old = replay(db, request, user, context, raw)
        if old is not None:
            return old
        revision(meta, data.expected_revision)
        if row.status != "assigned" or not row.project_id:
            raise AgentError("STATE_CONFLICT", "当前任务不能提交", 409)
        editable_project(db, row.project_id)
        version = db.get(Record, data.version_id)
        h = head(db, row.project_id)
        if not version or version.kind != "version" or version.project_id != row.project_id:
            raise AgentError("NOT_FOUND", "版本不存在", 404)
        if version.status != "confirmed" or h.payload.get("confirmed_version_id") != version.id:
            raise AgentError("STATE_CONFLICT", "请提交当前已确认的明确版本", 409)
        source_spec = db.get(Record, version.payload.get("spec_id"))
        if not source_spec or source_spec.project_id != row.project_id or source_spec.kind != "spec":
            raise AgentError("STATE_CONFLICT", "版本要求单绑定缺失", 409)
        snap = {
            "version_id": version.id,
            "version_payload": version.payload,
            "version_payload_sha256": sha(canonical(version.payload)),
            "spec_id": source_spec.id,
            "spec_payload": source_spec.payload,
            "image": image_snapshot(version),
            "source_envelope_sha256": meta.envelope_sha256,
            "submitted_by": user.username,
            "submitted_at": now(),
        }
        from .managed_bridge import result_projection

        projection = result_projection(db, row, meta, snap, user.username)
        projection_raw = canonical(projection)
        snap["design_version_base64"] = base64.b64encode(projection_raw).decode()
        snap["design_version_digest"] = sha(projection_raw)
        row.submitted_version_id = version.id
        row.status = "review"
        audit(db, row, meta, user, "submitted", snap)
        result = project_item(db, row, meta, user)
        save_action(db, request, user, context, raw, result, row.id)
        return result


def review(request, handoff_id, raw):
    data = parse(raw, ReviewIn)
    with transaction() as db:
        user, context = principal(db, request, write=True, manager=True)
        row, meta = visible(db, handoff_id, user)
        _, _, old = replay(db, request, user, context, raw)
        if old is not None:
            return old
        revision(meta, data.expected_revision)
        if row.status != "review" or row.submitted_version_id != data.submitted_version_id:
            raise AgentError("STATE_CONFLICT", "提交版本已变化，请重新核对", 409)
        note = data.note.strip()
        if not note:
            raise AgentError("INVALID_INPUT", "请填写本次审查意见", 422)
        submission = db.scalar(
            select(ManagedAudit)
            .where(ManagedAudit.handoff_id == row.id, ManagedAudit.kind == "submitted")
            .order_by(ManagedAudit.revision.desc())
        )
        version = db.get(Record, row.submitted_version_id)
        if not submission or not version or version.project_id != row.project_id:
            raise AgentError("STATE_CONFLICT", "提交快照不存在", 409)
        if sha(canonical(version.payload)) != submission.payload["version_payload_sha256"]:
            raise AgentError("STATE_CONFLICT", "提交版本内容已变化，不能审查旧快照", 409)
        if image_snapshot(version) != submission.payload["image"]:
            raise AgentError("STATE_CONFLICT", "提交图片已变化，不能审查旧快照", 409)
        row.manager_note = note
        row.status = "assigned" if data.action == "revise" else "approved"
        event = audit(
            db,
            row,
            meta,
            user,
            data.action if data.action == "revise" else "approved",
            {
                "submission_id": submission.id,
                "submitted_version_id": version.id,
                "version_payload_sha256": submission.payload["version_payload_sha256"],
                "design_version_digest": submission.payload["design_version_digest"],
                "image": submission.payload["image"],
                "note": note,
            },
        )
        if data.action == "revise":
            row.submitted_version_id = None
        else:
            from .managed_bridge import freeze_result

            freeze_result(db, row, meta, event, submission)
        result = project_item(db, row, meta, user)
        save_action(db, request, user, context, raw, result, row.id)
        return result
