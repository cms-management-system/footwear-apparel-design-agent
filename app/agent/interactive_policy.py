"""Private reusable local policy; grants are minted only in authenticated user transactions."""

import json
import os
import re
import time
from pathlib import Path

from sqlalchemy import select

from ..config import get_config
from ..design_auth import session_context
from ..models import DesignerSession, DesignerUser
from .access_context import authenticated_session
from .managed_store import ExecutionContextPolicy, ExecutionReservation, InteractiveGrant, canonical_bytes, sha256
from .store import AgentError, Record, change, now


def policy():
    try:
        p = Path(os.environ.get("DESIGN_INTERACTIVE_POLICY_FILE", ""))
        if not p.is_file() or p.stat().st_mode & 0o077:
            return None
        v = json.loads(p.read_text())
        if set(v) != {
            "policy_id",
            "mode",
            "enabled",
            "scope_id",
            "target_instance_id",
            "allowed_roles",
            "per_intent",
            "max_active_runs_per_subject",
        }:
            return None
        cfg = get_config()
        if (
            not re.fullmatch(r"[A-Za-z0-9_-]{8,100}", v["policy_id"])
            or v["mode"] != "interactive_local"
            or v["enabled"] is not True
            or v["scope_id"] != cfg.design_scope_id
            or v["target_instance_id"] != cfg.design_instance_id
            or v["allowed_roles"] != ["designer"]
            or type(v["max_active_runs_per_subject"]) is not int
            or v["max_active_runs_per_subject"] != 1
            or set(v["per_intent"]) != {"text", "image"}
        ):
            return None
        for cap in v["per_intent"].values():
            if (
                set(cap) != {"max_calls", "max_cost_fen"}
                or type(cap["max_calls"]) is not int
                or cap["max_calls"] != 1
                or type(cap["max_cost_fen"]) is not int
                or not 1 <= cap["max_cost_fen"] <= 20000
            ):
                return None
        return v
    except (OSError, ValueError, TypeError, KeyError):
        return None


def valid_policy(db):
    p = policy()
    if p and any(
        row.payload["policy_sha256"] != sha256(canonical_bytes(p))
        for row in db.scalars(select(InteractiveGrant).where(InteractiveGrant.policy_id == p["policy_id"]))
    ):
        return None  # A new policy version needs a new ID, even for a new action.
    return p


def session_for_context(db, context, actor):
    from ..design_demo_access import session_enabled
    return next(
        (
            s
            for s in db.scalars(select(DesignerSession).where(DesignerSession.username == actor))
            if session_context(s.token_hash) == context and session_enabled(db, s)
        ),
        None,
    )


def context_row(db, context):
    return db.get(ExecutionContextPolicy, context) if context else None


def request_identity(db, user):
    s = authenticated_session(db)
    if not s or not user or s.username != user.username:
        return None, None
    return s, session_context(s.token_hash)


def register_validation(db, context, subject, *, ledger_id, grant_ids, limits=None):
    if not session_for_context(db, context, subject):
        raise AgentError("AUTH_CONTEXT_CHANGED", "验收会话当前无效", 409)
    payload = {"ledger_id": ledger_id, "grant_ids": list(grant_ids), "limits": limits or {"text": 2, "image": 1}}
    prior = context_row(db, context)
    if prior:
        if prior.purpose != "validation" or prior.subject != subject or prior.payload != payload:
            raise AgentError("AUTH_CONTEXT_CHANGED", "会话执行分类已冻结，不能覆盖", 409)
        return prior
    row = ExecutionContextPolicy(auth_context_id=context, purpose="validation", subject=subject, payload=payload)
    db.add(row)
    db.flush()
    return row


def run_context(db, run):
    context = run.payload.get("auth_context_id")
    if not context and run.payload.get("intent_id"):
        intent = db.get(Record, run.payload["intent_id"])
        context = intent.payload["intent"].get("auth_context_id") if intent else None
    return context


def validation_count(db, ctx, stage):
    from .execution_quota import COUNTED

    contexts = list(db.scalars(select(ExecutionContextPolicy).where(ExecutionContextPolicy.purpose == "validation")))
    peers = {r.auth_context_id for r in contexts if r.payload["ledger_id"] == ctx.payload["ledger_id"]}
    grant_ids = {g for r in contexts if r.auth_context_id in peers for g in r.payload["grant_ids"]}
    count = 0
    for row in db.scalars(
        select(ExecutionReservation).where(
            ExecutionReservation.stage == stage, ExecutionReservation.status.in_(COUNTED)
        )
    ):
        run = db.get(Record, row.run_id)
        if row.grant_id in grant_ids or (run and run_context(db, run) in peers):
            count += 1
    return count


def validation_guard(db, run, stage, *, reserved=False):
    ctx = context_row(db, run_context(db, run))
    if ctx and ctx.purpose == "validation":
        if not session_for_context(db, ctx.auth_context_id, ctx.subject):
            raise AgentError("AUTH_CONTEXT_CHANGED", "原验收会话已退出或失效，未出网", 409)
        if validation_count(db, ctx, stage) + (0 if reserved else 1) > ctx.payload["limits"][stage]:
            raise AgentError("CALL_LIMIT_EXHAUSTED", "本轮验收中央额度已耗尽，不能回退运营预算", 409)


def subject_busy(db, subject, exclude=None):
    return any(
        r.id != exclude and r.status in {"queued", "running", "unknown"} and r.payload.get("actor") == subject
        for r in db.scalars(select(Record).where(Record.kind == "creation_run"))
    )


def explicit_image_intent(text):
    # Conservative original-message gate: a model proposal cannot promote a question into a paid image.
    if re.search(
        r"解释|说明.{0,8}(?:流程|方法|含义)|如何|怎么|怎样|流程|含义|什么意思|区别|咨询|"
        r"\b(?:explain|how|what does|meaning|process)\b",
        text,
        re.I,
    ):
        return False
    if re.search(
        r"(?:不要|别|无需|不必|先不|不需要).{0,4}(?:生成|画|出图)|(?:do not|don't)\s+(?:generate|draw|render)",
        text,
        re.I,
    ):
        return False
    return bool(
        re.search(
            r"(?:请|帮我|给我|直接).{0,12}(?:生成|绘制|画|出图|设计)|设计(?:一|个|款)|(?:生成|画)(?:一|张|个)|修改.{0,30}(?:并|后).{0,8}(?:出图|生成)|\b(?:generate|draw|render|create)\s+(?:an?\s+)?(?:image|picture|design|jacket|shoe)\b",
            text,
            re.I,
        )
    )


def preview(db, user):
    session, context = request_identity(db, user)
    ctx = context_row(db, context)
    if ctx and ctx.purpose == "validation":
        return None, ctx
    return (valid_policy(db) if session and user.role == "designer" else None), ctx


def allocate(db, run, user, action_id, provider, text, *, manual_image=False):
    from .execution_quota import busy

    session, context = request_identity(db, user)
    ctx = context_row(db, context)
    if ctx and ctx.purpose == "validation":
        return None
    p = valid_policy(db)
    if not p:
        return None
    if not session or run.payload["auth_context_id"] != context or user.role != "designer":
        raise AgentError("AUTH_CONTEXT_CHANGED", "执行授权必须来自当前有效会话", 409)
    if subject_busy(db, user.username, run.id) or busy(db):
        raise AgentError("PROVIDER_BUSY", "已有收费执行进行中或结果未知，未分配新授权", 409)
    image_allowed = manual_image or (
        run.payload["source_mode"] == "independent"
        and (
            run.payload["requested_intent"] == "generate_image"
            or (run.payload["requested_intent"] == "auto" and explicit_image_intent(text))
        )
    )
    if image_allowed and not provider.capabilities().get("image_only", False):
        raise AgentError("CAPABILITY_UNAVAILABLE", "图片能力未启用，未分配本次创作预算", 409)
    stages = {
        "text": {"max_calls": 0, "max_cost_fen": 0} if manual_image else dict(p["per_intent"]["text"]),
        "image": dict(p["per_intent"]["image"]) if image_allowed else {"max_calls": 0, "max_cost_fen": 0},
    }
    for stage, cost in [("text", provider.reasoning_fen), ("image", provider.image_fen)]:
        if stages[stage]["max_calls"] and (cost <= 0 or cost > stages[stage]["max_cost_fen"]):
            raise AgentError("BUDGET_EXHAUSTED", "本机策略不能覆盖本阶段费用预留", 409)
    grant_id = "interactive_" + sha256(
        canonical_bytes([p["policy_id"], p["scope_id"], p["target_instance_id"], user.username, action_id])
    )
    if db.get(InteractiveGrant, grant_id):
        raise AgentError("IDEMPOTENCY_CONFLICT", "原动作授权不可分给另一执行", 409)
    if ctx is None:
        db.add(
            ExecutionContextPolicy(auth_context_id=context, purpose="interactive", subject=user.username, payload={})
        )
    authorization = {
        "authorized": True,
        "grant_id": grant_id,
        "binding_mode": "interactive_local",
        "subject": user.username,
        "scope_id": p["scope_id"],
        "target_instance_id": p["target_instance_id"],
        "project_id": run.project_id,
        "run_id": run.id,
        "action_id": action_id,
        "intent_id": run.payload["intent_id"],
        "auth_context_id": context,
        "purpose": "interactive",
        "role": user.role,
        "expires_at": session.expires_at,
        "stages": stages,
        "policy_id": p["policy_id"],
        "policy_sha256": sha256(canonical_bytes(p)),
        "created_at": now(),
    }
    db.add(
        InteractiveGrant(
            grant_id=grant_id,
            policy_id=p["policy_id"],
            run_id=run.id,
            project_id=run.project_id,
            payload={"authorization": authorization, "policy_sha256": authorization["policy_sha256"]},
        )
    )
    change(run, interactive_grant_id=grant_id, image_intent_allowed=image_allowed)
    db.flush()
    return authorization


def current_grant(db, run):
    if run.payload.get("demo_access_revoked"):
        return None
    from .execution_quota import grant

    gid = run.payload.get("interactive_grant_id")
    if not gid:
        return grant()
    row = db.get(InteractiveGrant, gid)
    p = valid_policy(db)
    if (
        not row
        or not p
        or p["policy_id"] != row.policy_id
        or sha256(canonical_bytes(p)) != row.payload["policy_sha256"]
    ):
        return None
    g = row.payload["authorization"]
    ctx = context_row(db, g["auth_context_id"])
    actor = db.get(DesignerUser, g["subject"], populate_existing=True)
    if (
        not ctx
        or ctx.purpose != "interactive"
        or ctx.subject != g["subject"]
        or not actor
        or not actor.active
        or actor.role != g["role"]
        or not session_for_context(db, g["auth_context_id"], g["subject"])
        or g["expires_at"] <= time.time()
        or row.run_id != run.id
        or row.project_id != run.project_id
    ):
        return None
    return g


def guard_legacy(db, actor=None, context=None, *, worker=False, manual_image=False):
    if context is None:
        session = authenticated_session(db)
        context = session_context(session.token_hash) if session else None
    ctx = context_row(db, context)
    subject = actor if isinstance(actor, str) else getattr(actor, "username", None)
    if worker:
        user = db.get(DesignerUser, subject, populate_existing=True) if subject else None
        if (
            not context
            or not user
            or not user.active
            or user.role != "designer"
            or not session_for_context(db, context, subject)
        ):
            raise AgentError("AUTH_CONTEXT_CHANGED", "旧任务缺可信有效会话或已撤权，未派发收费请求", 409)
    legacy_validation = bool(
        not context
        and subject
        and db.scalar(
            select(ExecutionContextPolicy).where(
                ExecutionContextPolicy.subject == subject, ExecutionContextPolicy.purpose == "validation"
            )
        )
    )
    if (ctx and ctx.purpose == "validation") or legacy_validation or (policy() and not manual_image):
        raise AgentError(
            "CREATION_AUTHORIZATION_REQUIRED", "旧收费入口不能绕过会话/阶段预算，请使用当前有界创作对话", 409
        )


def legacy_submission(db, *, manual_image=False):
    from .access_context import validate_transaction_access

    user = validate_transaction_access(db)
    session, context = request_identity(db, user)
    if not session:
        raise AgentError("AUTH_CONTEXT_CHANGED", "收费动作必须来自可信登录会话", 409)
    guard_legacy(db, user, context, manual_image=manual_image)
    p = valid_policy(db)
    return {
        "actor": user.username,
        "auth_context_id": context,
        "legacy_policy_sha256": sha256(canonical_bytes(p)) if p else None,
    }


def manual_upstream_run(db, pid, user, action, provider, h, prompt, spec, meta, identity_digest):
    """Reuse the bounded run/reservation for an already manually confirmed upstream prompt."""
    from .store import create, uid

    session, context = request_identity(db, user)
    if not session or spec.status != "confirmed":
        raise AgentError("CONFIRM_REQUIRED", "原上游执行稿尚未人工确认", 409)
    intent = {
        "id": uid(),
        "project_id": pid,
        "message_id": h.payload.get("latest_user_message_id"),
        "requested_intent": "generate_image",
        "source_mode": "upstream",
        "output_kind": prompt["output_kind"],
        "actor": user.username,
        "auth_context_id": context,
        "authorized": True,
        "manual_image_action": action,
        "prompt_id": prompt["id"],
        "spec_id": spec.id,
        "created_at": now(),
    }
    intent["digest"] = sha256(canonical_bytes(intent))
    create(
        db, pid, "creation_intent", {"intent": intent, "resolved_intent": "generate_image"}, "saved", id=intent["id"]
    )
    run = create(
        db,
        pid,
        "creation_run",
        {
            "intent_id": intent["id"],
            "message_id": intent["message_id"],
            "actor": user.username,
            "auth_context_id": context,
            "user_action_id": action,
            "source_mode": "upstream",
            "source_identity_digest": identity_digest,
            "requested_intent": "generate_image",
            "output_kind": prompt["output_kind"],
            "max_text_calls": 0,
            "max_image_calls": 1,
            "prompt_id": prompt["id"],
            "spec_id": spec.id,
            "base_version_id": prompt["base_version_id"],
            "base_prompt_id": prompt["id"],
            "base_spec_id": spec.id,
            "expected_business_revision": meta.revision + 1,
            "stage": "image",
            "steps": [],
            "text_task_id": None,
            "image_task_id": None,
            "version_id": None,
            "reason": None,
            "error": None,
            "canvas_placement": "pending",
            "max_cost_fen": None,
        },
        "queued",
    )
    change(run, binding_run_id=run.id)
    g = allocate(db, run, user, action, provider, "", manual_image=True)
    if not g:
        raise AgentError("CREATION_AUTHORIZATION_REQUIRED", "当前会话不能分配上游手动单图预算", 409)
    from .execution_quota import reserve

    reservation = reserve(db, g, run, "image", provider.image_fen)
    return run, reservation
