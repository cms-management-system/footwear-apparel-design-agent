"""Opt-in three-design end-to-end smoke with a synthetic reference, at most 550 fen reserved."""

import argparse
import json
import time
import uuid
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--count", type=int, choices=[2, 3], default=3)
    args = parser.parse_args()
    count = args.count
    ceiling = count * 50 + (count + 1) * 100
    if not args.run:
        parser.error("Explicit --run required for real calls")
    reference = ROOT / "data/vision-smoke/structure.png"
    if not reference.is_file():
        raise SystemExit("Synthetic reference fixture missing")
    with httpx.Client(base_url="http://127.0.0.1:5180", trust_env=False, timeout=40) as client:

        def post(path, **kwargs):
            response = client.post(path, **kwargs)
            response.raise_for_status()
            return response.json()

        caps = client.get("/api/design-agent/capabilities").json()
        if not caps["design"]:
            raise SystemExit("Image service is not enabled")
        if count * caps["image_call_max_fen"] + (count + 1) * caps["reasoning_call_max_fen"] > ceiling:
            raise SystemExit("Configured costs exceed this smoke's fixed ceiling")
        pid = post("/api/project", json={"name": f"合成参考图·{count}款真实生成验证"})["id"]
        path = f"/api/project/{pid}"
        asset = post(
            path + "/design-assets?name=synthetic-structure.png",
            content=reference.read_bytes(),
            headers={"Content-Type": "image/png"},
        )
        spec = post(
            path + "/design-specs",
            json={
                "intent": "以合成服装草图为参考，设计正面完整连衣裙。保留方领、红色主体与画面右侧蓝色贴袋。"
                f"在允许的腰部与裙摆裁片细节上探索{count}种不同方案。纯白背景，无人物，不出文字。",
                "references": [
                    {
                        "asset_id": asset["id"],
                        "role": "structure",
                        "region": "整体",
                        "instruction": "合成草图，只作方领和配色结构参考；腰部与裙摆细节可变化。",
                    }
                ],
                "constraints": [
                    {"id": "c_neck", "kind": "must_keep", "text": "保留清晰的方形领口", "region": "领口"},
                    {"id": "c_red", "kind": "must_keep", "text": "衣身主体为红色", "region": "衣身"},
                    {"id": "c_blue", "kind": "must_keep", "text": "画面右侧有蓝色贴袋", "region": "口袋"},
                    {
                        "id": "c_change",
                        "kind": "may_change",
                        "text": "腰部收省、裙摆裁片和缝线细节可变化，保持领口和配色",
                        "region": "腰部及裙摆",
                    },
                ],
            },
        )
        post(f"/api/design-specs/{spec['id']}/confirm")
        task = post(
            path + "/design-tasks",
            json={
                "spec_id": spec["id"],
                "mode": "design",
                "authorized": True,
                "idempotency_key": "generation-smoke-" + uuid.uuid4().hex,
                "design_count": count,
                "max_image_calls": count,
                "max_reasoning_calls": count + 1,
                "max_cost_fen": ceiling,
            },
        )
        output = ROOT / "data/vision-smoke" / f"generation-{pid}"
        output.mkdir(parents=True, exist_ok=True)
        (output / "task.json").write_text(json.dumps({"project_id": pid, "task_id": task["id"]}))
        print(json.dumps({"project_id": pid, "task_id": task["id"]}), flush=True)
        deadline, last = time.monotonic() + 600, None
        while time.monotonic() < deadline:
            response = client.get(path + "/design-workspace")
            response.raise_for_status()
            state = response.json()
            task = next(t for t in state["tasks"] if t["id"] == task["id"])
            progress = (task["status"], task["image_calls"], task["reasoning_calls"], len(state["versions"]))
            if progress != last:
                print(
                    json.dumps(
                        {
                            "status": progress[0],
                            "image_calls": progress[1],
                            "reasoning_calls": progress[2],
                            "saved_images": progress[3],
                            "error": task.get("error"),
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
                last = progress
            if task["status"] not in {"queued", "running"}:
                break
            time.sleep(2)
        else:
            post(f"/api/design-tasks/{task['id']}/cancel")
            raise SystemExit("Timed out; cancelled remaining steps, no automatic retry")
        (output / "result.json").write_text(json.dumps(state, ensure_ascii=False, indent=2))
        for index, version in enumerate(state["versions"], 1):
            response = client.get(f"/api/design-versions/{version['id']}/image")
            response.raise_for_status()
            (output / f"design-{index}.png").write_bytes(response.content)
        if (
            task["status"] != "awaiting_review"
            or len(state["versions"]) != count
            or not all(v["review"] for v in state["versions"])
        ):
            raise SystemExit("Generation smoke incomplete; saved results retained, no retry")
        print(
            json.dumps(
                {
                    "result": "PASS",
                    "project_id": pid,
                    "images": len(state["versions"]),
                    "reserved_fen": task["reserved_cost_fen"],
                    "output": str(output),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )


if __name__ == "__main__":
    main()
