"""Fixed-source v2 pull and durable design events. No CMS or public-image fallback."""

from __future__ import annotations

import base64
import hmac
import json
import os
import stat
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import FileResponse
from pydantic import Field, model_validator
from sqlalchemy import select

from ..config import get_config
from ..models import DesignHandoff
from . import assets
from .managed_store import ManagedAction, ManagedAttempt, ManagedAudit, ManagedOutbox, ManagedReceipt
from .managed_workflow import (
    DeliveryIn,
    Strict,
    action_identity,
    canonical,
    image_snapshot,
    parse,
    principal,
    project_item,
    replay,
    revision,
    save_action,
    sha,
    visible,
)
from .store import AgentError, Record, now, transaction, uid

router = APIRouter(prefix="/api/design-integration", tags=["design-private-integration"])


class SyncIn(Strict):
    package_id: str | None = Field(default=None, min_length=1, max_length=180)
    version: str | None = Field(default=None, pattern=r"^v[1-9][0-9]*$")
    cursor: str | None = Field(default=None, max_length=500)
    limit: int = Field(default=50, ge=1, le=50)

    @model_validator(mode="after")
    def pair(self):
        if bool(self.package_id) != bool(self.version) or self.package_id and self.cursor:
            raise ValueError("package and version are required together")
        return self


class Settings:
    def __init__(self):
        cfg = get_config()
        path = os.environ.get("DESIGN_PAIRING_FILE", "")
        if not cfg.managed or not path:
            raise AgentError("NOT_CONFIGURED", "尚未配置本次产品与设计服务配对", 503)
        try:
            file = Path(path)
            if stat.S_IMODE(file.stat().st_mode) & 0o077:
                raise ValueError("private permissions required")
            data = json.loads(file.read_text())
            if data["design_instance_id"] != cfg.design_instance_id or data["authorized_scope"] != cfg.design_scope_id:
                raise ValueError("wrong instance or scope")
            self.product = data["product_instance_id"]
            self.design = data["design_instance_id"]
            self.scope = data["authorized_scope"]
            self.base_url = data["product_base_url"].rstrip("/")
            parsed = urlsplit(self.base_url)
            if (
                parsed.scheme != "http"
                or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
                or parsed.username
                or parsed.password
                or parsed.path
                or parsed.query
                or parsed.fragment
                or not parsed.port
            ):
                raise ValueError("this run permits a fixed loopback endpoint only")
            self.credentials = data
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise AgentError("NOT_CONFIGURED", "服务配对配置无效，请由后端核对", 503) from exc

    def credential(self, name):
        try:
            value = self.credentials[name]
            source, target = (self.product, self.design) if name == "asset_reader" else (self.design, self.product)
            purpose = {
                "package_reader": "design_package_reader",
                "event_writer": "design_event_writer",
                "asset_reader": "design_asset_reader",
            }[name]
            if (
                not isinstance(value["subject"], str)
                or not value["subject"]
                or not isinstance(value["token"], str)
                or len(value["token"]) < 32
                or value["purpose"] != purpose
                or value["source_instance_id"] != source
                or value["target_instance_id"] != target
                or value["authorized_scope"] != self.scope
            ):
                raise ValueError("credential binding mismatch")
            return value
        except (KeyError, TypeError, ValueError) as exc:
            raise AgentError("NOT_CONFIGURED", "本用途服务凭据尚未配置", 503) from exc


async def network(settings, purpose, method, path, *, params=None, raw=None, event_id=None):
    credential = settings.credential(purpose)
    headers = {"Authorization": "Bearer " + credential["token"], "Accept": "application/json"}
    if raw is not None:
        headers["Content-Type"] = "application/json"
    if event_id:
        headers["Idempotency-Key"] = event_id
    limit = 16 * 1024 * 1024 if purpose == "package_reader" else 256 * 1024
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(15, connect=5), trust_env=False, follow_redirects=False
        ) as client:
            async with client.stream(
                method, settings.base_url + path, params=params, content=raw, headers=headers
            ) as res:
                body = bytearray()
                async for chunk in res.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > limit:
                        raise AgentError("PAYLOAD_TOO_LARGE", "对端响应超过协议限制", 413)
                return res.status_code, bytes(body)
    except httpx.RequestError as exc:
        raise AgentError("PRODUCT_LINK_UNAVAILABLE", "产品服务暂不可达，原记录已保留", 502) from exc


def _event_base(row, meta, kind, identity, note=""):
    package = meta.proof["package"]
    event_id = "DSE-" + sha(canonical([meta.handoff_id, kind, identity]))
    return {
        "event_schema": "pa-design-event/2",
        "event_id": event_id,
        "event_revision": 1,
        "kind": kind,
        "source_product": "footwear_design",
        "target_product": "product_management",
        "source_instance_id": meta.target_instance_id,
        "target_instance_id": meta.source_instance_id,
        "authorized_scope": meta.scope_id,
        "created_at": now(),
        "package_id": row.package_id,
        "version": row.version,
        "package_content_digest": meta.receipt["content_digest"],
        "design_receive_id": meta.receipt["design_receive_id"],
        "version_group_id": package["version_group_id"],
        "requirement_digest": package["requirement_digest"],
        "prompt_digest": package["prompt_digest"],
        "note": note,
        "design_receipt": None,
        "clarification": None,
        "result": None,
    }


def _freeze(db, row, meta, event):
    from .managed_protocol import validate_event

    content = canonical(event)
    validate_event(content)
    delivery = {
        "delivery_schema": "pa-design-event-delivery/2",
        "source_product": "footwear_design",
        "target_product": "product_management",
        "source_instance_id": meta.target_instance_id,
        "target_instance_id": meta.source_instance_id,
        "authorized_scope": meta.scope_id,
        "event_id": event["event_id"],
        "event_revision": 1,
        "content_digest": sha(content),
        "content_base64": base64.b64encode(content).decode(),
    }
    raw = canonical(delivery)
    previous = db.get(ManagedOutbox, event["event_id"])
    if previous:
        if previous.raw_body != raw:
            raise AgentError("EVENT_CONFLICT", "此事件身份已有不同原件", 409)
        return previous
    outbox = ManagedOutbox(
        event_id=event["event_id"],
        handoff_id=row.id,
        source_instance_id=meta.target_instance_id,
        target_instance_id=meta.source_instance_id,
        scope_id=meta.scope_id,
        raw_body=raw,
        body_sha256=sha(raw),
        status="prepared",
        receipt=None,
        created_at=now(),
        updated_at=now(),
    )
    db.add(outbox)
    db.flush()
    return outbox


def freeze_received(db, row, meta):
    event = _event_base(row, meta, "received", meta.receipt["design_receive_id"])
    event["design_receipt"] = meta.receipt
    return _freeze(db, row, meta, event)


def freeze_clarification(db, row, meta, decision):
    event = _event_base(row, meta, "clarification", decision.id, decision.payload["note"])
    event["clarification"] = {
        "decision_id": decision.id,
        "manager_subject": decision.actor,
        "decided_at": decision.created_at,
        "reason": decision.payload["note"],
    }
    return _freeze(db, row, meta, event)


def freeze_result(db, row, meta, review, submission):
    assignment = db.scalar(
        select(ManagedAudit)
        .where(ManagedAudit.handoff_id == row.id, ManagedAudit.kind == "assign")
        .order_by(ManagedAudit.revision.desc())
    )
    if not assignment:
        raise AgentError("STATE_CONFLICT", "分派记录缺失", 409)
    snap = submission.payload
    image = snap["image"]
    origin = image.get("generation_mode", "unknown")
    if origin not in {"synthetic_fixture", "model_generated", "authorized_reference"}:
        raise AgentError("ASSET_ORIGIN_REQUIRED", "请先核对并记录设计结果的合法来源", 409)
    # This run does not authorise paid generation. A fixture is never relabelled as a real image result.
    if not (get_config().design_paid_providers_enabled or get_config().design_image_only_enabled) \
            and origin != "synthetic_fixture":
        raise AgentError("ASSET_ORIGIN_REQUIRED", "本轮技术验证仅接受明确合成夹具结果", 409)
    event = _event_base(row, meta, "design_approved", review.id, review.payload["note"])
    projection = json.loads(base64.b64decode(snap["design_version_base64"], validate=True))
    event["result"] = {
        "task_id": row.id,
        "assignment_id": assignment.id,
        "submission_id": submission.id,
        "design_version_id": snap["version_id"],
        "design_version_digest": snap["design_version_digest"],
        "design_version_base64": snap["design_version_base64"],
        "review": {
            "review_id": review.id,
            "manager_subject": review.actor,
            "approved_at": review.created_at,
            "decision": "approved",
            "submission_id": submission.id,
            "design_version_id": snap["version_id"],
            "design_version_digest": snap["design_version_digest"],
        },
        **{key: projection[key] for key in ("assets", "summary", "changes", "checks", "unknowns", "generation_mode")},
    }
    if origin == "model_generated":
        event["result"]["generation_provenance"] = projection["generation_provenance"]
    return _freeze(db, row, meta, event)


async def sync_packages(request, raw):
    from .managed_protocol import decode_delivery, parse_delivery_list

    data = parse(raw, SyncIn)
    with transaction() as db:
        user, context = principal(db, request, write=True, manager=True)
        _, _, old = replay(db, request, user, context, raw)
        if old is not None:
            return old
    settings = Settings()
    status, body = await network(
        settings, "package_reader", "GET", "/api/design-link/packages", params=data.model_dump(exclude_none=True)
    )
    if status != 200:
        raise AgentError(
            "PRODUCT_LINK_FORBIDDEN" if status in {401, 403} else "PRODUCT_LINK_FAILED",
            "产品批准包未能读取，未新增设计接收",
            502,
        )
    item_bytes, cursor = parse_delivery_list(body)
    decoded = [
        decode_delivery(
            item, source_instance_id=settings.product, target_instance_id=settings.design, scope_id=settings.scope
        )
        for item in item_bytes
    ]
    if data.package_id and any(
        d["package"]["package_id"] != data.package_id or d["package"]["version"] != data.version for d in decoded
    ):
        raise AgentError("PACKAGE_CONFLICT", "精确查询返回了其他包版本", 409)
    with transaction() as db:
        user, context = principal(db, request, write=True, manager=True)
        _, _, old = replay(db, request, user, context, raw)
        if old is not None:
            return old
        result_items = []
        for wire, decoded_item in zip(item_bytes, decoded, strict=True):
            package = decoded_item["package"]
            identity = [settings.product, settings.scope, package["package_id"], package["version"], settings.design]
            handoff_id = "DHI-" + sha(canonical(identity))
            meta = db.get(ManagedReceipt, handoff_id)
            if meta:
                if base64.b64decode(meta.proof["content_base64"], validate=True) != decoded_item["content_bytes"]:
                    raise AgentError("PACKAGE_CONFLICT", "此产品包版本已经接收过不同原件", 409)
                row = db.get(DesignHandoff, handoff_id)
                if not row:
                    raise AgentError("STATE_CONFLICT", "接收记录缺少任务投影", 409)
            else:
                timestamp = now()
                receipt = {
                    "receipt_schema": "pa-design-receipt/2",
                    "design_receive_id": "DRCV-" + uid(),
                    "source_instance_id": settings.product,
                    "target_instance_id": settings.design,
                    "authorized_scope": settings.scope,
                    "package_id": package["package_id"],
                    "version": package["version"],
                    "content_digest": sha(decoded_item["content_bytes"]),
                    "status": "received",
                    "received_at": timestamp,
                }
                proof = {
                    "verification_method": "authenticated_fixed_source_pull",
                    "verified_at": timestamp,
                    "source_endpoint": "/api/design-link/packages",
                    "package": package,
                    "requirement": decoded_item["requirement"],
                    "prompt": decoded_item["prompt"],
                    "content_base64": base64.b64encode(decoded_item["content_bytes"]).decode(),
                    "approval": package["approval"],
                }
                meta = ManagedReceipt(
                    handoff_id=handoff_id,
                    source_instance_id=settings.product,
                    target_instance_id=settings.design,
                    scope_id=settings.scope,
                    request_id=handoff_id,
                    package_id=package["package_id"],
                    package_version=package["version"],
                    envelope_bytes=wire,
                    envelope_sha256=sha(wire),
                    proof=proof,
                    receipt=receipt,
                    revision=1,
                    priority=package["approval"]["final_priority"],
                    created_at=timestamp,
                )
                row = DesignHandoff(
                    id=handoff_id,
                    package_id=package["package_id"],
                    version=package["version"],
                    snapshot=package,
                    status="new",
                    created_at=timestamp,
                )
                db.add_all([meta, row])
                db.flush()
                freeze_received(db, row, meta)
            result_items.append(project_item(db, row, meta, user))
        result = {
            "synced": len(result_items),
            "items": result_items,
            "next_cursor": cursor,
            "source_verification": "authenticated_fixed_source_pull",
        }
        save_action(db, request, user, context, raw, result, None)
        return result


def event_content(outbox):
    envelope = json.loads(outbox.raw_body)
    content = base64.b64decode(envelope["content_base64"], validate=True)
    if sha(outbox.raw_body) != outbox.body_sha256 or sha(content) != envelope["content_digest"]:
        raise AgentError("STATE_CONFLICT", "冻结回传原件摘要异常", 409)
    return envelope, content, json.loads(content)


def _allowed_outbox(db, handoff_id, event_id, user):
    row, meta = visible(db, handoff_id, user)
    outbox = db.get(ManagedOutbox, event_id)
    if not outbox or outbox.handoff_id != row.id or outbox.scope_id != meta.scope_id:
        raise AgentError("NOT_FOUND", "回传事件不存在", 404)
    envelope, content, event = event_content(outbox)
    if event["kind"] == "design_approved":
        review = db.get(ManagedAudit, event["result"]["review"]["review_id"])
        if not review or review.kind != "approved" or review.handoff_id != row.id:
            raise AgentError("STATE_CONFLICT", "事件没有匹配的管理审查记录", 409)
    return row, meta, outbox, envelope, content, event


async def deliver(request, handoff_id, raw):
    from .managed_protocol import validate_event_receipt

    data = parse(raw, DeliveryIn)
    with transaction() as db:
        user, context = principal(db, request, write=True, manager=True)
        row, meta, outbox, envelope, _, event = _allowed_outbox(db, handoff_id, data.event_id, user)
        _, action_id, old = replay(db, request, user, context, raw)
        if old is not None:
            return old
        revision(meta, data.expected_revision)
        settings = Settings()
        settings.credential("event_writer")
        if event["kind"] != "received":
            received = [
                o
                for o in db.scalars(select(ManagedOutbox).where(ManagedOutbox.handoff_id == row.id))
                if event_content(o)[2]["kind"] == "received"
            ]
            if not received or received[0].status != "received":
                raise AgentError("RECEIPT_NOT_CONFIRMED", "请先完成本包接收事件回告", 409)
        if (outbox.source_instance_id, outbox.target_instance_id, outbox.scope_id) != (
            settings.design,
            settings.product,
            settings.scope,
        ):
            raise AgentError("SCOPE_FORBIDDEN", "回传目标与配对配置不符", 403)
        if outbox.status == "received":
            result = project_item(db, row, meta, user)
            save_action(db, request, user, context, raw, result, row.id)
            return result
        if outbox.status == "sending":
            raise AgentError("STATE_CONFLICT", "原事件仍在途，请查询原操作", 409)
        if data.action == "send" and outbox.status not in {"prepared", "failed_before_send"}:
            raise AgentError("STATE_CONFLICT", "原发送结果待确认，请先对账原事件", 409)
        attempt = ManagedAttempt(
            id=uid(),
            event_id=outbox.event_id,
            action_id=action_id,
            actor=user.username,
            status="sending",
            started_at=now(),
        )
        db.add(attempt)
        outbox.status = "sending"
        outbox.updated_at = now()
        # Journal the accepted intent before network; replays cannot dispatch a second request.
        save_action(
            db,
            request,
            user,
            context,
            raw,
            {"operation_state": "in_progress", "event_id": outbox.event_id, "attempt_id": attempt.id},
            row.id,
            status_code=202,
        )
        action_key, _ = action_identity(request, user)
        attempt_id, outbound = attempt.id, outbox.raw_body
    status, reply, failure = None, None, None
    try:
        if data.action == "send":
            status, reply = await network(
                settings, "event_writer", "POST", "/api/design-link/events", raw=outbound, event_id=data.event_id
            )
        else:
            status, reply = await network(
                settings, "event_writer", "GET", "/api/design-link/events", params={"event_id": data.event_id}
            )
        if status in {200, 201}:
            expected = {
                key: envelope[key]
                for key in (
                    "event_id",
                    "event_revision",
                    "source_instance_id",
                    "target_instance_id",
                    "authorized_scope",
                    "content_digest",
                )
            }
            expected.update(package_id=event["package_id"], version=event["version"])
            receipt = validate_event_receipt(reply, expected)
        elif status == 404 and data.action == "reconcile":
            receipt = None
        else:
            failure = "EVENT_CONFLICT" if status == 409 else "PRODUCT_LINK_FAILED"
            receipt = None
    except AgentError as exc:
        failure, receipt = exc.code, None
    from .access_context import reset_request, suspend_request

    system_context = suspend_request()
    try:
        with transaction() as db:
            # Persist transport outcome even if the initiating human session has since expired.
            outbox = db.get(ManagedOutbox, data.event_id)
            attempt = db.get(ManagedAttempt, attempt_id)
            action = db.get(ManagedAction, action_key)
            outbox.status = (
                "received"
                if receipt
                else ("prepared" if status == 404 and data.action == "reconcile" else "unconfirmed")
            )
            outbox.receipt = receipt or outbox.receipt
            outbox.updated_at = now()
            attempt.status = outbox.status
            attempt.error_code = failure
            attempt.finished_at = now()
            row = db.get(DesignHandoff, handoff_id)
            meta = db.get(ManagedReceipt, handoff_id)
            # The stored actor is used only for the immutable operation projection, never for new authority.
            from ..models import DesignerUser

            actor = db.get(DesignerUser, action.username)
            result = project_item(db, row, meta, actor)
            result["transport_outcome"] = {
                "event_id": outbox.event_id,
                "attempt_id": attempt_id,
                "status": outbox.status,
                "error_code": failure,
            }
            action.status_code, action.response = 200, result
    finally:
        reset_request(system_context)
    with transaction() as db:
        principal(db, request, write=True, manager=True)
        # If access was revoked during I/O, the caller sees an auth failure, not private results.
    return result


def recover_inflight():
    if not get_config().managed:
        return
    with transaction() as db:
        for outbox in db.scalars(select(ManagedOutbox).where(ManagedOutbox.status == "sending")):
            outbox.status = "unconfirmed"
            outbox.updated_at = now()
        for attempt in db.scalars(select(ManagedAttempt).where(ManagedAttempt.status == "sending")):
            attempt.status, attempt.error_code, attempt.finished_at = "unconfirmed", "PROCESS_RESTARTED", now()
        for action in db.scalars(select(ManagedAction).where(ManagedAction.status_code == 202)):
            if action.response.get("operation_state") != "in_progress":
                continue
            action.response = {**action.response, "operation_state": "unconfirmed", "error_code": "PROCESS_RESTARTED"}


def service_identity(request):
    settings = Settings()
    value = request.headers.get("Authorization", "")
    credential = settings.credential("asset_reader")
    if not value.startswith("Bearer ") or not hmac.compare_digest(value[7:], credential["token"]):
        raise AgentError("SERVICE_UNAUTHORIZED", "服务身份无效", 401)
    return settings


def approved_event(db, settings, event_id):
    outbox = db.get(ManagedOutbox, event_id)
    if not outbox or (outbox.source_instance_id, outbox.target_instance_id, outbox.scope_id) != (
        settings.design,
        settings.product,
        settings.scope,
    ):
        raise AgentError("NOT_FOUND", "结果不存在", 404)
    envelope, content, event = event_content(outbox)
    if event["kind"] != "design_approved":
        raise AgentError("NOT_FOUND", "没有已审查结果", 404)
    review = db.get(ManagedAudit, event["result"]["review"]["review_id"])
    submission = db.get(ManagedAudit, event["result"]["submission_id"])
    meta = db.get(ManagedReceipt, outbox.handoff_id)
    if (
        not review
        or review.kind != "approved"
        or review.handoff_id != outbox.handoff_id
        or not submission
        or submission.handoff_id != outbox.handoff_id
        or not meta
        or event["package_content_digest"] != meta.receipt["content_digest"]
        or review.payload["submission_id"] != submission.id
        or review.payload["design_version_digest"] != event["result"]["design_version_digest"]
    ):
        raise AgentError("NOT_FOUND", "结果没有匹配的冻结审查事实", 404)
    return envelope, content, event, submission


@router.get("/events/{event_id}/review")
def review_evidence(event_id: str, request: Request):
    settings = service_identity(request)
    with transaction() as db:
        envelope, content, event, _ = approved_event(db, settings, event_id)
        return {
            "event_schema": event["event_schema"],
            "event_id": event_id,
            "event_revision": 1,
            "content_digest": envelope["content_digest"],
            "content_base64": base64.b64encode(content).decode(),
        }


@router.get("/events/{event_id}/assets/{asset_id}")
def approved_asset(event_id: str, asset_id: str, request: Request):
    settings = service_identity(request)
    with transaction() as db:
        _, _, event, submission = approved_event(db, settings, event_id)
        asset = next((a for a in event["result"]["assets"] if a["asset_id"] == asset_id), None)
        if not asset or asset_id != submission.payload["version_id"]:
            raise AgentError("NOT_FOUND", "结果资源不存在", 404)
        path = assets.file_path(submission.payload["version_payload"]["image"])
        if path.stat().st_size > 10 * 1024 * 1024 or sha(path.read_bytes()) != asset["sha256"]:
            raise AgentError("ASSET_DIGEST_MISMATCH", "结果资源摘要校验失败", 409)
        return FileResponse(
            path,
            media_type=asset["mime_type"],
            headers={
                "X-Content-SHA256": asset["sha256"],
                "Cache-Control": "private, no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )


def private_legacy_image(request, token):
    with transaction() as db:
        user, _ = principal(db, request)
        row = db.scalar(select(DesignHandoff).where(DesignHandoff.image_token == token))
        if not row:
            raise AgentError("NOT_FOUND", "结果不存在", 404)
        row, _ = visible(db, row.id, user)
        version = db.get(Record, row.submitted_version_id) if row.submitted_version_id else None
        if row.status != "approved" or not version or version.project_id != row.project_id:
            raise AgentError("NOT_FOUND", "结果不存在", 404)
        image_snapshot(version)
        return FileResponse(
            assets.file_path(version.payload["image"]),
            media_type="image/png",
            headers={"Cache-Control": "private, no-store"},
        )


def result_projection(db, row, meta, snap, author):
    assignment = db.scalar(
        select(ManagedAudit)
        .where(ManagedAudit.handoff_id == row.id, ManagedAudit.kind == "assign")
        .order_by(ManagedAudit.revision.desc())
    )
    if not assignment:
        raise AgentError("STATE_CONFLICT", "分派记录缺失", 409)
    image = snap["image"]
    origin = image.get("generation_mode", "unknown")
    if origin not in {"synthetic_fixture", "model_generated", "authorized_reference"}:
        raise AgentError("ASSET_ORIGIN_REQUIRED", "结果尚未记录可核对的来源", 409)
    old_review = snap["version_payload"].get("review") or {}
    package = meta.proof["package"]
    projection = {
        "schema_version": "pa-design-result-version/2",
        "design_version_id": snap["version_id"],
        "task_id": row.id,
        "assignment_id": assignment.id,
        "design_receive_id": meta.receipt["design_receive_id"],
        "package_id": row.package_id,
        "package_version": row.version,
        "package_content_digest": meta.receipt["content_digest"],
        "version_group_id": package["version_group_id"],
        "requirement_digest": package["requirement_digest"],
        "prompt_digest": package["prompt_digest"],
        "assets": [
            {
                "asset_id": image["asset_id"],
                "sha256": image["sha256"],
                "mime_type": image["media_type"],
                "width": image["width"],
                "height": image["height"],
                "origin": origin,
            }
        ],
        "summary": str(old_review.get("summary", "合成技术结果，仅验证接口与审查流程")),
        "changes": str(snap["version_payload"].get("change_description", "无额外修改说明")),
        "checks": [
            {
                "code": str(c.get("constraint_id", "unknown")),
                "status": {"pass": "passed", "deviation": "failed"}.get(c.get("status"), "not_checked"),
                "note": str(c.get("evidence", "尚未核验")),
            }
            for c in old_review.get("checks", [])
        ],
        "unknowns": ["真实设计效果、实物打样、经营结果与业务人工审查未验"],
        "generation_mode": origin,
        "created_by": author,
        "created_at": snap["submitted_at"],
    }

    if origin == "model_generated":
        from .managed_store import ImageReservation
        provenance = snap["version_payload"].get("generation_provenance")
        task = db.get(Record, snap["version_payload"].get("task_id"))
        reservation = db.scalar(select(ImageReservation).where(ImageReservation.task_id == task.id)) if task else None
        if (not provenance or not task or task.kind != "task" or task.status != "succeeded"
                or task.project_id != row.project_id or task.payload.get("mode") != "image_only"
                or snap["version_id"] not in task.payload.get("version_ids", [])
                or task.payload.get("image_calls") != 1 or task.payload.get("reasoning_calls") != 0
                or not reservation or reservation.subject != author
                or provenance["authorization_ref"] != reservation.grant_id
                or provenance["generation_task_id"] != task.id
                or provenance["source_product_prompt_digest"] != package["prompt_digest"]
                or provenance["execution_prompt_digest"] != sha(task.payload["render_prompt"].encode())
                or base64.b64decode(provenance["execution_prompt_base64"]) != task.payload["render_prompt"].encode()
                or provenance["generation_attempt_id"] != task.payload["steps"][0]["id"]
                or task.payload["steps"][0]["status"] != "done"
                or task.payload["steps"][0]["asset_sha256"] != image["sha256"]):
            raise AgentError("GENERATION_PROVENANCE_REQUIRED", "实际成功任务、授权和原图证据不完整", 409)
        projection["generation_provenance"] = provenance
        projection["summary"] = "应用实际生成的二维鞋服设计示意；付费视觉检查未运行，待人工核验"
    return projection
