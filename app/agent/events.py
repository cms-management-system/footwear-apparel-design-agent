"""Replayable workspace snapshots over SSE; connection lifetime is independent of tasks."""

import asyncio
import json
import time
import uuid

from ..config import get_config
from .access_context import bind_request, project_binding, reset_request, validate_transaction_access
from .managed_store import IndependentProject
from .service import workspace
from .store import AgentError, bind_transaction, fingerprint, head, records, reset_transaction, transaction


def frame(event, data):
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def workspace_snapshot(pid):
    if not get_config().managed:
        return workspace(pid)
    with transaction() as db:
        user = validate_transaction_access(db)
        if user is None:
            raise AgentError("LOGIN_REQUIRED", "请先登录设计工作台", 401)
        row, meta = project_binding(db, pid, user)
        proof = {} if isinstance(meta, IndependentProject) else meta.proof
        source_binding = (
            None
            if isinstance(meta, IndependentProject)
            else {
                "handoff_id": row.id,
                "source_instance_id": meta.source_instance_id,
                "target_instance_id": meta.target_instance_id,
                "scope_id": meta.scope_id,
                "package_id": meta.package_id,
                "package_version": meta.package_version,
                "content_digest": meta.receipt["content_digest"],
                "envelope_sha256": meta.envelope_sha256,
                "revision": meta.revision,
                "status": row.status,
                "package": proof.get("package"),
                "prompt": proof.get("prompt"),
                "requirement": proof.get("requirement"),
                "receipt": meta.receipt,
            }
        )
        token = bind_transaction(db)
        try:
            snapshot = workspace(pid)
        finally:
            reset_transaction(token)
        snapshot["source_binding"] = source_binding
        snapshot["revision"] = meta.revision
        from .dual_entry import binding_context, image_capability

        snapshot["project_context"] = binding_context(db, pid, user)
        snapshot["prompts"] = [r.payload["prompt"] for r in records(db, pid, "prompt")]
        snapshot["current_prompt_id"] = head(db, pid).payload.get("current_prompt_id")
        snapshot["image_only_execution"] = image_capability(db, pid, user)
        from .workbench import briefs, execution_state

        snapshot["briefs"] = briefs(db, pid)
        snapshot["latest_user_message_id"] = head(db, pid).payload.get("latest_user_message_id")
        snapshot["execution_brief_state"] = execution_state(db, pid, user)
        from .direct_create import projection

        snapshot["creation_runs"] = [projection(r) for r in records(db, pid, "creation_run")]
        for message in snapshot["messages"]:
            message.setdefault("selected_context", None)
    return snapshot


def error_frame(code, message):
    data = {"code": code, "message": message}
    if get_config().managed:
        data.update(retryable=False)
        return frame("error", {"error": data, "request_id": uuid.uuid4().hex})
    return frame("error", data)


async def stream(pid, request, duration=25):
    deadline, previous = time.monotonic() + duration, None
    context = bind_request(request) if get_config().managed else None
    try:
        while True:
            if await request.is_disconnected():
                return
            snapshot = await asyncio.to_thread(workspace_snapshot, pid)
            revision = fingerprint(snapshot)
            if revision != previous:
                yield frame("workspace", snapshot)
                previous = revision
            else:
                yield ": heartbeat\n\n"
            if time.monotonic() >= deadline:
                yield frame("done", {"reconnect": True})
                return
            await asyncio.sleep(0.35)
    except AgentError as error:
        yield error_frame(error.code, error.message)
    except Exception:
        yield error_frame("STREAM_INTERRUPTED", "实时连接中断，重连后恢复已保存内容")
    finally:
        if context is not None:
            reset_request(context)
