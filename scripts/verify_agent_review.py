"""Opt-in real check of a previously generated synthetic candidate; no image generation."""

import argparse
import json
from pathlib import Path

from app import models
from app.agent import service
from app.agent.assets import save_image
from app.agent.providers import Provider
from app.agent.runner import fail_task, one_step
from app.agent.schemas import SpecIn, TaskIn
from app.agent.store import AgentError, change, create, require, transaction, uid

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true")
    args = parser.parse_args()
    if not args.run:
        parser.error("Explicit --run required for one real vision call, at most 100 fen reserved")
    fixture = ROOT / "data/vision-smoke/generation-138/design-1.png"
    if not fixture.is_file():
        raise SystemExit("Known synthetic fixture unavailable")
    provider = Provider()
    provider.require("understand")
    if provider.reasoning_fen > 100:
        raise SystemExit("Configured cost exceeds fixed ceiling")
    with transaction() as db:
        project = models.Project(name="合成图片·短编号核验验证")
        db.add(project)
        db.flush()
        pid = project.id
        spec = service.save_spec(
            db,
            pid,
            SpecIn(
                intent="正面连衣裙，保留方领、红色主体和画面右侧蓝色贴袋。",
                constraints=[
                    {"id": "c_neck", "kind": "must_keep", "text": "方形领口"},
                    {"id": "c_red", "kind": "must_keep", "text": "红色主体"},
                    {"id": "c_blue", "kind": "must_keep", "text": "画面右侧蓝色贴袋"},
                ],
            ),
        )
        spec.status = "confirmed"
        submitted = service.submit_in(
            db,
            pid,
            TaskIn(
                spec_id=spec.id,
                mode="design",
                idempotency_key="review-smoke-" + uid(),
                authorized=True,
                max_cost_fen=100,
                max_reasoning_calls=1,
                max_image_calls=1,
            ),
            provider,
        )
        task = require(db, submitted["id"], "task")
        # A persisted, clearly synthetic existing candidate, created before any paid call.
        version = create(
            db,
            pid,
            "version",
            {
                "spec_id": spec.id,
                "source_fingerprint": spec.payload["fingerprint"],
                "task_id": task.id,
                "parent_version_id": None,
                "image": save_image(fixture.read_bytes()),
                "review": None,
                "provider": provider.capabilities(),
                "prompt_version": "v1",
            },
            "candidate",
        )
        task.status = "running"
        change(
            task,
            current_version_id=version.id,
            pending_action={"action": "compare_design", "summary": "仅核验既有合成图"},
        )
        tid, vid = task.id, version.id
    print(json.dumps({"project_id": pid, "task_id": tid}), flush=True)
    try:
        one_step(tid, provider)
    except AgentError as error:
        fail_task(tid, error)
        raise
    with transaction() as db:
        task, version = require(db, tid, "task"), require(db, vid, "version")
        if task.status == "running":
            task.status = "awaiting_review"
            change(task, outcome="合成图真实核验已保存")
        result = {
            "project_id": pid,
            "task_id": tid,
            "version_id": vid,
            "status": task.status,
            "review": version.payload.get("review"),
            "steps": task.payload["steps"],
            "reserved_fen": task.payload["reserved_cost_fen"],
        }
    output = ROOT / "data/vision-smoke" / f"review-{pid}.json"
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    valid = (
        result["steps"][-1]["status"] == "done"
        and result["review"]
        and all(set(c["reference_ids"]).issubset({vid}) for c in result["review"]["checks"])
    )
    print(
        json.dumps(
            {
                "result": "PASS" if valid else "INCOMPLETE",
                "reserved_fen": result["reserved_fen"],
                "output": str(output),
            },
            ensure_ascii=False,
        )
    )
    if not valid:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
