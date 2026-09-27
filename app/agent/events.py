"""Replayable workspace snapshots over SSE; connection lifetime is independent of tasks."""

import asyncio
import json
import time

from .service import workspace
from .store import AgentError, fingerprint


def frame(event, data):
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def stream(pid, request, duration=25):
    deadline, previous = time.monotonic() + duration, None
    try:
        while True:
            if await request.is_disconnected():
                return
            snapshot = await asyncio.to_thread(workspace, pid)
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
        yield frame("error", {"code": error.code, "message": error.message})
    except Exception:
        yield frame("error", {"code": "STREAM_INTERRUPTED", "message": "实时连接中断，重连后恢复已保存内容"})
