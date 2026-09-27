"""Opt-in two-turn real conversation smoke on an existing synthetic test project.

At most RMB 4 reserved for the chat task. Never retries message submissions.
Observes public partial replies through the real Next SSE proxy.
"""

import argparse
import json
import time
import uuid
from pathlib import Path

import httpx


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--project", type=int, required=True)
    args = parser.parse_args()
    if not args.run:
        parser.error("Real calls require --run")
    pid = args.project
    path = f"/api/project/{pid}"
    destination = Path(__file__).resolve().parents[1] / "data/vision-smoke" / f"chat-{pid}"
    destination.mkdir(parents=True, exist_ok=True)
    events = []
    with httpx.Client(base_url="http://127.0.0.1:5180", trust_env=False, timeout=40) as client:

        def get_state():
            res = client.get(path + "/design-workspace")
            res.raise_for_status()
            return res.json()

        state = get_state()
        if "合成" not in state["project"]["name"]:
            raise SystemExit("Only explicitly synthetic test projects are accepted")

        def send(text, authorized):
            state = get_state()
            response = client.post(
                path + "/design-messages",
                json={
                    "text": text,
                    "references": [],
                    "expected_spec_id": state["head"]["spec_id"],
                    "authorized": authorized,
                    "max_cost_fen": 400,
                    "idempotency_key": "chat-smoke-" + uuid.uuid4().hex,
                },
            )
            response.raise_for_status()
            return response.json()["task_id"]

        def observe(tid):
            started = time.monotonic()
            deadline = started + 180
            last = None
            while time.monotonic() < deadline:
                with client.stream("GET", path + "/design-events") as response:
                    response.raise_for_status()
                    event = ""
                    for line in response.iter_lines():
                        if time.monotonic() >= deadline:
                            raise SystemExit("Observation timed out; task was not resubmitted")
                        if line.startswith("event:"):
                            event = line[6:].strip()
                        elif line.startswith("data:") and event == "workspace":
                            snapshot = json.loads(line[5:])
                            task = next(t for t in snapshot["tasks"] if t["id"] == tid)
                            partial = task.get("live_text") or task.get("live_summary") or ""
                            if partial and partial != last:
                                last = partial
                                events.append(
                                    {
                                        "seconds": round(time.monotonic() - started, 3),
                                        "task_id": tid,
                                        "status": task["status"],
                                        "text": partial,
                                    }
                                )
                                print(
                                    json.dumps(
                                        {"stream_seconds": events[-1]["seconds"], "characters": len(partial)},
                                        ensure_ascii=False,
                                    ),
                                    flush=True,
                                )
                            if task["status"] not in {"queued", "running"}:
                                return snapshot, task
                        elif line.startswith("data:") and event == "error":
                            raise SystemExit("SSE returned an error; no task retry")
            raise SystemExit("Observation timed out")

        tid = send(
            "请基于草图先确认你看见的领口、主体颜色和贴袋颜色，再问我希望的裙长。"
            "这一轮先向我提问，不要整理新要求单或出图。",
            True,
        )
        first, task = observe(tid)
        (destination / "question.json").write_text(json.dumps(first, ensure_ascii=False, indent=2))
        if task["status"] != "awaiting_input":
            raise SystemExit(f"Expected a question, got {task['status']}; no automatic retry")
        second_id = send(
            "裙长到脚踝。请保留之前识别的领口、主体颜色和贴袋颜色。"
            "用一小段话复述我的选择和还不能从图片确定的信息即可，先不要生成要求单或图片。",
            False,
        )
        if second_id != tid:
            raise SystemExit("Follow-up did not resume the same task")
        final, task = observe(tid)
        (destination / "reply.json").write_text(json.dumps(final, ensure_ascii=False, indent=2))
        (destination / "stream.json").write_text(json.dumps(events, ensure_ascii=False, indent=2))
        replies = [m["text"] for m in final["messages"] if m.get("task_id") == tid and m["role"] == "assistant"]
        print(
            json.dumps(
                {
                    "project_id": pid,
                    "task_id": tid,
                    "status": task["status"],
                    "reasoning_calls": task["reasoning_calls"],
                    "reserved_cost_fen": task["reserved_cost_fen"],
                    "partial_updates": len(events),
                    "replies": replies,
                    "error": task.get("error"),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        if task["status"] != "awaiting_review" or not events or len(replies) < 2:
            raise SystemExit("Conversation acceptance incomplete; inspect saved evidence")


if __name__ == "__main__":
    main()
