"""Durable user-driven design conversation turns."""

from sqlalchemy import select

from .providers import Provider
from .schemas import SpecIn, TaskIn
from .service import BUSY, save_spec, submit_in
from .store import AgentError, Record, change, create, fingerprint, head, now, records, require, serialize, transaction


def send(pid, data, provider=None):
    provider = provider or Provider()
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
