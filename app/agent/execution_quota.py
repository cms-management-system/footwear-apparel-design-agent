"""Private grants, durable stage reservations and a non-expiring unknown dispatch slot."""

import json
import os
import re
import time
from pathlib import Path

from sqlalchemy import select

from ..config import get_config
from .managed_store import CreationGrantBinding, ExecutionReservation, ProviderSlot, canonical_bytes, sha256
from .store import AgentError, Record, create, now, uid

COUNTED = {"reserved", "sent", "succeeded", "failed_after_sent", "unknown"}


def grant():
    try:
        p = Path(os.environ.get("DESIGN_CREATION_AUTHORIZATION_FILE", ""))
        if not p.is_file() or p.stat().st_mode & 0o077:
            return None
        g = json.loads(p.read_text())
        cfg = get_config()
        if (
            g.get("authorized") is not True
            or g.get("scope_id") != cfg.design_scope_id
            or g.get("target_instance_id") != cfg.design_instance_id
            or type(g.get("expires_at")) is not int
            or g["expires_at"] <= time.time()
            or not re.fullmatch(r"[A-Za-z0-9_-]{8,100}", g.get("grant_id", ""))
            or not isinstance(g.get("subject"), str)
        ):
            return None
        mode = binding_mode(g)
        if mode == "fixed_project":
            if "creation_action_id" in g or type(g.get("project_id")) is not int or g["project_id"] < 1:
                return None
        elif mode == "fixed_action":
            if "project_id" in g or not re.fullmatch(r"[A-Za-z0-9_-]{8,100}", g.get("creation_action_id", "")):
                return None
        elif mode == "next_creation":
            if "project_id" in g or "creation_action_id" in g:
                return None
        else:
            return None
        if g.get("project_id") is not None and (type(g["project_id"]) is not int or g["project_id"] < 1):
            return None
        if g.get("creation_action_id") and not re.fullmatch(r"[A-Za-z0-9_-]{8,100}", g["creation_action_id"]):
            return None
        if set(g.get("stages", {})) - {"text", "image"} or "text" not in g.get("stages", {}):
            return None
        for limits in g["stages"].values():
            if (
                set(limits) != {"max_calls", "max_cost_fen"}
                or type(limits["max_calls"]) is not int
                or not 0 <= limits["max_calls"] <= 100
                or type(limits["max_cost_fen"]) is not int
                or not 0 <= limits["max_cost_fen"] <= 20000
            ):
                return None
        return g
    except (OSError, ValueError, TypeError, KeyError):
        return None


def binding_mode(g):
    if "binding_mode" in g:
        return g["binding_mode"]
    if ("project_id" in g) == ("creation_action_id" in g):
        return None
    return "fixed_project" if "project_id" in g else "fixed_action"


def matches(g, pid, actor, creation_action=None, bound=None, *, db=None, allow_unbound_next=False):
    if not g or g["subject"] != actor:
        return False
    # A committed claim freezes this grant ID even if no stage was reserved.
    # Changing its mode must never bypass the original claim's digest or scope.
    b = db.get(CreationGrantBinding, g["grant_id"]) if db else None
    if b:
        return (
            b.grant_sha256 == sha256(canonical_bytes(g))
            and b.project_id == pid
            and (bound or {}).get("creation_action_id") == b.creation_action_id
            and (not (bound or {}).get("binding_run_id") or bound["binding_run_id"] == b.run_id)
        )
    if binding_mode(g) == "interactive_local":
        return (
            g.get("project_id") == pid
            and g.get("run_id") == (bound or {}).get("binding_run_id")
            and g.get("intent_id") == (bound or {}).get("intent_id")
            and g.get("action_id") == (bound or {}).get("user_action_id")
        )
    if binding_mode(g) == "next_creation":
        return pid is None and allow_unbound_next
    if g.get("project_id"):
        return g["project_id"] == pid
    # The binding record is created in the creation transaction, never chosen by the client.
    return g["creation_action_id"] == (bound or {}).get("creation_action_id", creation_action)


def claim_next(db, g, run, action, provider):
    if db.get(CreationGrantBinding, g["grant_id"]):
        raise AgentError("GRANT_BINDING_CONFLICT", "下一次创作授权已绑定原项目，未派发新请求", 409)
    if busy(db):
        raise AgentError("PROVIDER_BUSY", "原调用尚未确认，不能领取新的创作授权", 409)
    provider.require("understand")
    provider.require("image_only")
    for stage, cost in [("text", provider.reasoning_fen), ("image", provider.image_fen)]:
        cap = g["stages"].get(stage, {})
        count, total = usage(db, g, stage)
        if cost <= 0 or count >= cap.get("max_calls", 0) or total + cost > cap.get("max_cost_fen", 0):
            raise AgentError("BUDGET_EXHAUSTED", "下一次创作需要完整文本与图片预算，未领取授权", 409)
    db.add(
        CreationGrantBinding(
            grant_id=g["grant_id"],
            project_id=run.project_id,
            creation_action_id=action,
            intent_id=run.payload["intent_id"],
            run_id=run.id,
            grant_sha256=sha256(canonical_bytes(g)),
            payload={
                "mode": "next_creation",
                "subject": g["subject"],
                "scope_id": g["scope_id"],
                "target_instance_id": g["target_instance_id"],
            },
        )
    )
    db.flush()


def usage(db, g, stage):
    rows = list(
        db.scalars(
            select(ExecutionReservation).where(
                ExecutionReservation.grant_id == g["grant_id"],
                ExecutionReservation.stage == stage,
                ExecutionReservation.status.in_(COUNTED),
            )
        )
    )
    return len(rows), sum(r.payload["estimated_cost_fen"] for r in rows)


def remaining(db, g, stage):
    if not g:
        return 0
    return max(0, g["stages"].get(stage, {}).get("max_calls", 0) - usage(db, g, stage)[0])


def reserve(db, g, run, stage, cost):
    from .interactive_policy import validation_guard

    validation_guard(db, run, stage)
    if not g or cost <= 0:
        raise AgentError("CREATION_AUTHORIZATION_REQUIRED", "本次创作尚未配置阶段授权与费用预留", 409)
    if not matches(g, run.project_id, run.payload["actor"], bound=run.payload, db=db):
        raise AgentError("CREATION_AUTHORIZATION_REQUIRED", "阶段授权未绑定本执行，未预留额度", 409)
    prior = db.scalar(
        select(ExecutionReservation).where(
            ExecutionReservation.run_id == run.id,
            ExecutionReservation.stage == stage,
        )
    )
    if prior:
        raise AgentError("CALL_LIMIT_EXHAUSTED", "原执行阶段已经预留，不能重复派发", 409)
    for prior_grant in db.scalars(select(ExecutionReservation).where(ExecutionReservation.grant_id == g["grant_id"])):
        if prior_grant.payload["authorization"] != g:
            raise AgentError("CREATION_AUTHORIZATION_REQUIRED", "同一授权编号不可改写原阶段范围或费用", 409)
    count, total = usage(db, g, stage)
    limits = g["stages"].get(stage, {})
    if count >= limits.get("max_calls", 0):
        raise AgentError("CALL_LIMIT_EXHAUSTED", "本次授权的调用次数已预留或用完", 409)
    if total + cost > limits.get("max_cost_fen", 0):
        raise AgentError("BUDGET_EXHAUSTED", "本次授权不能覆盖调用费用预留", 409)
    row = ExecutionReservation(
        id=sha256(canonical_bytes([g["grant_id"], run.id, stage])),
        grant_id=g["grant_id"],
        run_id=run.id,
        stage=stage,
        project_id=run.project_id,
        status="reserved",
        payload={
            "authorization": g,
            "actor": run.payload["actor"],
            "source_mode": run.payload["source_mode"],
            "attempt_id": uid(),
            "idempotency": f"{run.id}:{stage}",
            "estimated_cost_fen": cost,
            "actual_cost_fen": None,
            "created_at": now(),
        },
    )
    db.add(row)
    db.flush()
    return row


def acquire_slot(db, attempt):
    slot = db.get(ProviderSlot, 1)
    if slot and slot.status in {"sent", "unknown"}:
        raise AgentError("PROVIDER_BUSY", "原供应商请求仍在处理中或结果未知，请先核对原调用", 409)
    # Historic dispatch journals also participate, including interrupted unknown calls.
    for task in db.scalars(select(Record).where(Record.kind == "task")):
        if any(
            s.get("status") in {"pending", "unknown"} and s.get("id") != attempt for s in task.payload.get("steps", [])
        ):
            raise AgentError("PROVIDER_BUSY", "已有尚未确认结果的供应商调用，未派发新请求", 409)
    if slot is None:
        db.add(ProviderSlot(id=1, attempt_id=attempt, status="sent"))
    else:
        slot.attempt_id, slot.status = attempt, "sent"


def busy(db):
    slot = db.get(ProviderSlot, 1)
    if slot and slot.status in {"sent", "unknown"}:
        return True
    return any(
        any(s.get("status") in {"pending", "unknown"} for s in t.payload.get("steps", []))
        for t in db.scalars(select(Record).where(Record.kind == "task"))
    )


def dispatch(db, reservation, run, cost):
    from .interactive_policy import current_grant, validation_guard

    validation_guard(db, run, reservation.stage, reserved=True)
    g = current_grant(db, run)
    frozen = reservation.payload["authorization"]
    if (
        g != frozen
        or not matches(g, run.project_id, run.payload["actor"], bound=run.payload, db=db)
        or reservation.status != "reserved"
        or cost != reservation.payload["estimated_cost_fen"]
    ):
        raise AgentError("CREATION_AUTHORIZATION_REQUIRED", "阶段授权或费用配置已变化；未出网", 409)
    count, total = usage(db, g, reservation.stage)
    cap = g["stages"][reservation.stage]
    if count > cap["max_calls"] or total > cap["max_cost_fen"]:
        raise AgentError("BUDGET_EXHAUSTED", "阶段费用或次数已超出授权；未出网", 409)
    acquire_slot(db, reservation.payload["attempt_id"])
    from .public_budget import reserve_dispatch
    reserve_dispatch(db, reservation.stage, reservation.payload["attempt_id"], run.id)
    reservation.status = "sent"
    reservation.payload = {**reservation.payload, "sent_at": now()}


def complete(db, reservation, status, receipt=None):
    reservation.status = status
    receipt = receipt or {}
    reservation.payload = {
        **reservation.payload,
        "finished_at": now(),
        "receipt": receipt,
        "actual_cost_fen": receipt.get("actual_cost_fen"),
    }
    slot = db.get(ProviderSlot, 1)
    if slot and slot.attempt_id == reservation.payload["attempt_id"]:
        slot.status = "unknown" if status == "unknown" else "idle"


def release_unsent(db, run):
    for r in db.scalars(select(ExecutionReservation).where(ExecutionReservation.run_id == run.id)):
        if r.status == "reserved":
            complete(db, r, "failed_not_sent")
            create(
                db,
                run.project_id,
                "reservation_audit",
                {
                    "run_id": run.id,
                    "reservation_id": r.id,
                    "attempt_id": r.payload["attempt_id"],
                    "stage": r.stage,
                    "status": "failed_not_sent",
                    "actor": run.payload["actor"],
                    "evidence": "reservation remained reserved; no dispatch committed",
                    "released_at": now(),
                },
                "saved",
            )
