"""Durable user-driven design conversation turns."""

from sqlalchemy import select

from ..config import get_config
from .providers import Provider
from .schemas import SpecIn, TaskIn
from .service import BUSY, save_spec, submit_in
from .store import AgentError, Record, change, create, fingerprint, head, now, records, require, serialize, transaction


def send(pid, data, provider=None):
    provider = provider or Provider()
    if get_config().managed:
        return send_managed(pid, data, provider)
    digest = fingerprint(data.model_dump(exclude={"idempotency_key"}))
    with transaction() as db:
        h = head(db, pid)
        prior = db.scalar(select(Record).where(Record.dedupe == f"message:{pid}:{data.idempotency_key}"))
        if prior:
            if prior.payload["request_hash"] != digest:
                raise AgentError("IDEMPOTENCY_CONFLICT", "同一消息编号不能用于不同内容")
            return serialize(prior)
        if data.expected_spec_id != h.payload.get("spec_id"):
            raise AgentError("STALE_SOURCE", "设计要求已更新，请刷新对话后发送")
        tasks = records(db, pid, "task")
        active = next((t for t in tasks if t.status in BUSY), None)
        if active is None and tasks:
            latest = tasks[-1]
            # Continue conversations stopped by the former fixed chat budget.
            # There is no external call in flight when a task has this status.
            if (
                latest.status == "budget_exhausted"
                and latest.payload.get("mode") == "understand"
                and latest.payload.get("spec_id") == h.payload.get("spec_id")
                and not any(s.get("status") in {"pending", "unknown"} for s in latest.payload.get("steps", []))
            ):
                active = latest
        if active and active.status not in {"awaiting_input", "budget_exhausted"}:
            raise AgentError("TASK_ACTIVE", "助手正在处理本轮，可以先输入下一条，或停止后发送")
        if active and data.references:
            if active.payload["mode"] != "understand":
                raise AgentError("INPUT_IMAGES_REQUIRE_NEW_TURN", "出图本轮已锁定参考图，请结束本轮后添加新图片")
            if not data.authorized:
                raise AgentError("AUTHORIZATION_REQUIRED", "请先确认这张图片用于本轮视觉理解")
        if len({r.asset_id for r in data.references}) != len(data.references):
            raise AgentError("DUPLICATE_REFERENCE", "同一张图片只能指定一种用途")
        for ref in data.references:
            require(db, ref.asset_id, "asset", pid)
        task_id = None
        if active:
            updates = {}
            if data.references:
                current = require(db, data.expected_spec_id, "spec", pid)
                content = dict(current.payload["spec"])
                refs = {r["asset_id"]: r for r in content.get("references", [])}
                refs.update({r.asset_id: r.model_dump() for r in data.references})
                if len(refs) > 6:
                    raise AgentError("REFERENCE_LIMIT", "当前设计已使用 6 张图片，请在设计详情中调整后发送")
                content["references"] = list(refs.values())
                updated = save_spec(db, pid, SpecIn(**content, expected_spec_id=current.id), check_idle=False)
                updates = {
                    "spec_id": updated.id,
                    "source_fingerprint": updated.payload["fingerprint"],
                    "observations": [],
                    "inspection_conflicts": [],
                    "pending_action": None,
                    "proposed_spec_id": None,
                }
            active.status = "queued"
            change(
                active,
                feedback=[*active.payload["feedback"], {"text": data.text, "at": now()}],
                questions=[],
                live_text="",
                live_summary="",
                error=None,
                outcome=None,
                **updates,
            )
            task_id = active.id
        else:
            current = require(db, data.expected_spec_id, "spec", pid) if data.expected_spec_id else None
            if current is None or data.references:
                content = dict(current.payload["spec"]) if current else {"intent": data.text}
                refs = {r["asset_id"]: r for r in content.get("references", [])}
                refs.update({r.asset_id: r.model_dump() for r in data.references})
                if len(refs) > 6:
                    raise AgentError("REFERENCE_LIMIT", "当前设计已使用 6 张图片，请在设计详情中调整后发送")
                content["references"] = list(refs.values())
                current = save_spec(db, pid, SpecIn(**content, expected_spec_id=data.expected_spec_id))
            if provider.capabilities()["understand"]:
                if not data.authorized:
                    raise AgentError("AUTHORIZATION_REQUIRED", "请先允许本轮发送需求和图片，并确认费用额度")
                task_id = submit_in(
                    db,
                    pid,
                    TaskIn(
                        spec_id=current.id,
                        mode="understand",
                        authorized=True,
                        idempotency_key="chat-" + data.idempotency_key,
                    ),
                    provider,
                )["id"]
        message = create(
            db,
            pid,
            "message",
            {
                "role": "user",
                "text": data.text,
                "references": [r.model_dump() for r in data.references],
                "task_id": task_id,
                "request_hash": digest,
            },
            "sent",
            dedupe=f"message:{pid}:{data.idempotency_key}",
        )
        if task_id is None:
            create(
                db,
                pid,
                "message",
                {
                    "role": "system",
                    "text": "已保存这条消息和素材。视觉服务尚未开通，助手还没有生成回复。",
                    "reply_to": message.id,
                },
                "saved",
            )
        return serialize(message)


def send_managed(pid, data, provider):
    """Discussion never advances the executable head, even with selected images."""
    from .access_context import project_binding, validate_transaction_access
    from .workbench import (
        check_head,
        conversation_grant,
        existing_image,
        selected_context,
        source_binding,
        source_digest,
    )

    if not {"expected_prompt_id", "expected_spec_id"}.issubset(data.model_fields_set):
        raise AgentError("INVALID_INPUT", "请明确当前提示词和要求单基准（可为null）", 422)
    digest = fingerprint(data.model_dump(exclude={"idempotency_key"}))
    with transaction() as db:
        user = validate_transaction_access(db)
        row, meta = project_binding(db, pid, user, write=True)
        prior = db.scalar(select(Record).where(Record.dedupe == f"message:{pid}:{data.idempotency_key}"))
        if prior:
            if prior.payload["request_hash"] != digest:
                raise AgentError("IDEMPOTENCY_CONFLICT", "同一消息编号不能用于不同内容", 409)
            return serialize(prior)
        h = check_head(db, pid, data.expected_prompt_id, data.expected_spec_id)
        from . import direct_create, execution_quota
        from .access_context import request_context_id
        from .interactive_policy import policy

        if (
            "intent" in data.model_fields_set
            or execution_quota.grant()
            or policy()
            or h.payload.get("creation_action_id")
        ):
            message, run = direct_create.save_turn(
                db,
                pid,
                user,
                text=data.text,
                requested=data.intent,
                output_kind=data.output_kind,
                authorized=data.authorized,
                selected=data.selected_context,
                references=[r.model_dump() for r in data.references],
                action_id=data.idempotency_key,
                auth_context=request_context_id(),
                provider=provider,
                max_cost_fen=data.max_cost_fen,
            )
            change(message, request_hash=digest)
            return {**serialize(message), "intent_id": run.payload["intent_id"], "run_id": run.id}
        selected = selected_context(db, pid, data.selected_context)
        if len({r.asset_id for r in data.references}) != len(data.references):
            raise AgentError("DUPLICATE_REFERENCE", "同一张图片只能指定一种用途", 422)
        for ref in data.references:
            existing_image(require(db, ref.asset_id, "asset", pid).payload)
        source = source_binding(db, pid, user)
        binding_digest = source_digest(db, pid, user)
        task_id = None
        grant = conversation_grant(pid, user.username)
        if provider.capabilities()["understand"] and grant:
            from .interactive_policy import legacy_submission

            trusted_execution = legacy_submission(db)
            # Configuration alone does not grant a paid conversation request.
            if not data.authorized:
                raise AgentError("AUTHORIZATION_REQUIRED", "请先明确授权本轮对话模型与费用", 409)
            if not h.payload.get("spec_id"):
                raise AgentError("CONFIRM_REQUIRED", "请先手动保存初始执行稿", 409)
            active = next((r for r in records(db, pid, "task") if r.status in BUSY), None)
            if active:
                if active.payload.get("auth_context_id") != trusted_execution["auth_context_id"]:
                    raise AgentError("AUTH_CONTEXT_CHANGED", "原收费对话缺少当前可信会话，不能恢复派发", 409)
                if active.status != "awaiting_input" or active.payload.get("mode") != "understand":
                    raise AgentError("TASK_ACTIVE", "当前任务处理中，请保留输入并等待", 409)
                if active.payload.get("spec_id") != data.expected_spec_id:
                    raise AgentError("STALE_SOURCE", "对话任务依据已变化", 409)
                active.status = "queued"
                change(
                    active,
                    feedback=[*active.payload.get("feedback", []), {"text": data.text, "at": now()}],
                    questions=[],
                    live_text="",
                    live_summary="",
                    error=None,
                    outcome=None,
                    pending_action=None,
                )
                task = active
            else:
                queued = submit_in(
                    db,
                    pid,
                    TaskIn(
                        spec_id=data.expected_spec_id,
                        mode="understand",
                        authorized=True,
                        max_cost_fen=data.max_cost_fen,
                        idempotency_key="chat-" + data.idempotency_key,
                    ),
                    provider,
                )
                task = require(db, queued["id"], "task", pid)
            task_id = task.id
            change(
                task,
                **trusted_execution,
                prompt_id=data.expected_prompt_id,
                selected_context=selected,
                source_binding_digest=binding_digest,
                conversation_authorization=grant,
                conversation_references=[r.model_dump() for r in data.references],
            )
        message = create(
            db,
            pid,
            "message",
            {
                "role": "user",
                "text": data.text,
                "references": [r.model_dump() for r in data.references],
                "task_id": task_id,
                "brief_id": None,
                "request_hash": digest,
                "created_by": user.username,
                "selected_context": selected,
                "prompt_id": data.expected_prompt_id,
                "spec_id": data.expected_spec_id,
                "source_binding": {**source, "revision": meta.revision, "status": row.status},
                "source_binding_digest": binding_digest,
                "reply_state": "queued" if task_id else "unavailable",
                "reason": None if task_id else "CAPABILITY_UNAVAILABLE",
            },
            "sent",
            dedupe=f"message:{pid}:{data.idempotency_key}",
        )
        change(h, latest_user_message_id=message.id)
        if task_id:
            change(task, message_id=message.id, based_on_latest_message_id=message.id)
        else:
            create(
                db,
                pid,
                "message",
                {
                    "role": "system",
                    "text": "消息已保存，当前未启用对话模型；可手动整理执行稿",
                    "reply_to": message.id,
                    "reason": "CAPABILITY_UNAVAILABLE",
                },
                "saved",
            )
        return serialize(message)
