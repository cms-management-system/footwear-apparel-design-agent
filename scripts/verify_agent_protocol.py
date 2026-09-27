"""Opt-in two-turn synthetic conversation check; at most RMB 2 reserved, no images."""

import argparse
import json
import time
import uuid
from pathlib import Path

import httpx


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true")
    args = parser.parse_args()
    if not args.run:
        parser.error("Explicit --run required")
    with httpx.Client(base_url="http://127.0.0.1:5180", trust_env=False, timeout=35) as client:

        def post(path, body):
            response = client.post(path, json=body)
            response.raise_for_status()
            return response.json()

        pid = post("/api/project", {"name": "合成协议修复验证·两轮通勤裙对话"})["id"]
        root = f"/api/project/{pid}"
        output = Path(__file__).resolve().parents[1] / "data/vision-smoke" / f"protocol-{pid}"
        output.mkdir(parents=True, exist_ok=True)
        used = 0
        try:
            for turn, message in enumerate(["连衣裙", "就上班穿"], 1):
                state = client.get(root + "/design-workspace").json()
                sent = post(
                    root + "/design-messages",
                    {
                        "text": message,
                        "references": [],
                        "expected_spec_id": state["head"].get("spec_id"),
                        "idempotency_key": "protocol-" + uuid.uuid4().hex,
                        "authorized": True,
                        "max_cost_fen": 200 - used,
                    },
                )
                tid = sent["task_id"]
                deadline = time.monotonic() + 160
                while time.monotonic() < deadline:
                    state = client.get(root + "/design-workspace").json()
                    task = next(t for t in state["tasks"] if t["id"] == tid)
                    if task["status"] not in {"queued", "running"}:
                        break
                    time.sleep(1)
                else:
                    raise RuntimeError("Timed out; no resubmission")
                (output / f"turn-{turn}.json").write_text(json.dumps(state, ensure_ascii=False, indent=2))
                used = sum(t["reserved_cost_fen"] for t in state["tasks"])
                print(
                    json.dumps(
                        {
                            "project": pid,
                            "turn": turn,
                            "status": task["status"],
                            "questions": task["questions"],
                            "error": task.get("error"),
                            "reserved_fen": used,
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
                assert task["status"] == "awaiting_input", (
                    "Expected continued conversation, not a premature specification"
                )
                assert len(task["questions"]) == 1
                assert not task.get("proposed_spec_id")
                assert used <= 200
            print("PASS: two real turns continued with one question each, no premature specification.", flush=True)
        finally:
            # Release only this synthetic conversation; never retry or change designer projects.
            state = client.get(root + "/design-workspace").json()
            for task in state["tasks"]:
                if task["status"] in {"queued", "running", "awaiting_input"}:
                    post(f"/api/design-tasks/{task['id']}/cancel", {})


if __name__ == "__main__":
    main()
