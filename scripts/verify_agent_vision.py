"""Opt-in real visual smoke through the budgeted API; synthetic assets only.

Run only after enabling the model in Ark and restarting with AGENT_ENABLED=true.
Does not edit credentials, activate cloud services, or retry failed model calls.
"""

import argparse
import io
import json
import time
import uuid
from pathlib import Path

import httpx
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--run", action="store_true", help="Allow one real task, at most 4 reasoning calls / RMB 4 reserved"
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:8020")
    args = parser.parse_args()
    if not args.run:
        parser.error("Real calls require --run; use --help for details")
    if args.base_url not in {"http://127.0.0.1:8020", "http://localhost:8020"}:
        parser.error("Smoke runner only targets the local product backend")
    with httpx.Client(base_url=args.base_url, timeout=20, trust_env=False) as client:

        def post(path, **kwargs):
            response = client.post(path, **kwargs)
            response.raise_for_status()
            return response.json()

        caps = client.get("/api/design-agent/capabilities").json()
        if not caps["understand"]:
            raise SystemExit(
                "Visual calls disabled. Activate the configured model, enable AGENT_ENABLED, and restart backend first."
            )
        pid = post("/api/project", json={"name": "视觉接通冒烟 · 合成方领裙（非效果验收）"})["id"]
        image = Image.new("RGB", (768, 1024), "white")
        draw = ImageDraw.Draw(image)
        draw.polygon(
            [
                (245, 140),
                (320, 140),
                (320, 230),
                (448, 230),
                (448, 140),
                (523, 140),
                (550, 390),
                (630, 870),
                (138, 870),
                (218, 390),
            ],
            fill="#be3030",
            outline="black",
            width=6,
        )
        draw.line([(218, 390), (550, 390)], fill="black", width=5)
        draw.rectangle((465, 460, 530, 535), fill="#2255bd", outline="black", width=4)
        raw = io.BytesIO()
        image.save(raw, format="PNG")
        asset = post(f"/api/project/{pid}/design-assets?name=合成结构图.png", content=raw.getvalue())
        spec = post(
            f"/api/project/{pid}/design-specs",
            json={
                "intent": "理解此合成服装草图，整理设计要求。保持图中领口轮廓、整体颜色及画面右侧贴袋配色。"
                "依据可见特征，不追问品牌、季节、人群或成分。仅用于识图接口测试。",
                "references": [
                    {
                        "asset_id": asset["id"],
                        "role": "structure",
                        "region": "整体",
                        "instruction": "观察领口、配色和贴袋；这是合成草图。",
                    }
                ],
                "constraints": [
                    {
                        "id": "c_fabric",
                        "kind": "preference",
                        "text": "面料成分和克重需要实物资料验证",
                        "verification": "physical",
                        "region": "整体",
                    }
                ],
            },
        )
        task = post(
            f"/api/project/{pid}/design-tasks",
            json={
                "spec_id": spec["id"],
                "mode": "understand",
                "authorized": True,
                "idempotency_key": "vision-smoke-" + uuid.uuid4().hex,
                "max_cost_fen": 400,
                "max_reasoning_calls": 4,
                "max_image_calls": 1,
            },
        )
        destination = ROOT / "data" / "vision-smoke" / task["id"]
        destination.mkdir(parents=True, exist_ok=True)
        image.save(destination / "structure.png")
        print(json.dumps({"project_id": pid, "task_id": task["id"]}, ensure_ascii=False), flush=True)
        deadline = time.monotonic() + 240
        while task["status"] in {"queued", "running"} and time.monotonic() < deadline:
            time.sleep(2)
            response = client.get("/api/design-tasks/" + task["id"])
            response.raise_for_status()
            task = response.json()
        (destination / "result.json").write_text(json.dumps(task, ensure_ascii=False, indent=2))
        report = {
            "project_id": pid,
            "status": task["status"],
            "reasoning_calls": task["reasoning_calls"],
            "reserved_cost_fen": task["reserved_cost_fen"],
            "error": task.get("error"),
            "observations": task["observations"],
            "proposed_spec_id": task.get("proposed_spec_id"),
            "receipts": [s.get("receipt", {}) for s in task["steps"]],
        }
        print(json.dumps(report, ensure_ascii=False, indent=2))
        # Human must compare observations with the actual synthetic image. This is not garment quality acceptance.
        if task["status"] != "awaiting_review" or not task["observations"] or not task.get("proposed_spec_id"):
            raise SystemExit("Smoke incomplete; inspect saved task. No automatic retry.")


if __name__ == "__main__":
    main()
