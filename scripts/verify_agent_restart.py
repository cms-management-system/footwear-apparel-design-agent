"""Boot two real backend processes against an isolated DB; never invokes a model."""

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    with tempfile.TemporaryDirectory(prefix="design-agent-restart-") as temp:
        os.environ.update(
            DATABASE_URL=f"sqlite:///{temp}/test.sqlite3",
            ASSETS_DIR=f"{temp}/assets",
            AGENT_ENABLED="false",
            AGENT_WORKER_ENABLED="true",
            MODEL_PROVIDER="mock",
        )
        from app import models
        from app.agent.migrate import migrate
        from app.agent.store import create, transaction

        models.init_db()
        migrate()
        with transaction() as db:
            p = models.Project(name="重启工程测试")
            db.add(p)
            db.flush()
            t = create(
                db,
                p.id,
                "task",
                {
                    "spec_id": "test-only",
                    "reasoning_calls": 1,
                    "image_calls": 0,
                    "reserved_cost_fen": 12,
                    "steps": [{"id": "pending-call", "tool": "generate_design", "status": "pending"}],
                },
                status="running",
            )
            tid = t.id
        base = "http://127.0.0.1:8027"
        for cycle in range(2):
            process = subprocess.Popen(
                [sys.executable, "-m", "uvicorn", "app.main:app", "--port", "8027"],
                cwd=ROOT,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                for _ in range(100):
                    if process.poll() is not None:
                        raise RuntimeError("测试后端未能启动；检查 8027 是否被占用")
                    try:
                        data = httpx.get(f"{base}/api/design-tasks/{tid}", timeout=1).json()
                        if data.get("status") == "interrupted":
                            break
                    except (httpx.HTTPError, ValueError):
                        pass
                    time.sleep(0.1)
                else:
                    raise RuntimeError("未恢复中断状态")
                assert data["reserved_cost_fen"] == 12
                assert data["steps"][0]["status"] == "unknown"
                response = httpx.post(f"{base}/api/design-tasks/{tid}/resume", timeout=3)
                assert response.status_code == 409 and response.json()["error"]["code"] == "UNKNOWN_CALL"
                print(f"Backend boot {cycle + 1}: unknown-call state + budget persisted; replay rejected")
            finally:
                process.terminate()
                process.wait(timeout=10)
        models.reset_engine_for_tests()


if __name__ == "__main__":
    main()
