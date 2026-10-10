from fastapi.testclient import TestClient

from app import config, models
from app.agent.product_bridge import ApprovedPackage, ProductBridge
from app.design_auth import password_hash
from app.main import app


def test_approved_package_reaches_only_assigned_designer(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'agent.sqlite3'}")
    monkeypatch.setenv("ASSETS_DIR", str(tmp_path / "assets"))
    monkeypatch.setenv("AGENT_WORKER_ENABLED", "false")
    monkeypatch.setenv("DESIGN_AUTH_REQUIRED", "true")
    monkeypatch.setenv("DESIGN_MANAGER_USERNAME", "lead")
    monkeypatch.setenv("DESIGN_MANAGER_PASSWORD_HASH", password_hash("leader-password-123"))
    config.get_config.cache_clear()
    models.reset_engine_for_tests()
    package = ApprovedPackage.model_validate(
        {
            "package_id": "PKG-REAL-1",
            "source_agent": "product",
            "signal_ids": ["SIG-1"],
            "source": "穿搭型号",
            "dedup_key": "通勤:裤装",
            "dedup_rule": "人工核验",
            "sample_count": {"intent": 1, "simulated_cart": 0, "real_purchase": 0},
            "attribution": {"supply_gap": False, "image_gap": False, "style_demand": True},
            "constraints": ["裤长适合通勤"],
            "requirement_desc": "改善通勤西裤版型",
            "version": "v1",
            "status": "approved",
        }
    )
    monkeypatch.setattr(ProductBridge, "list_approved", lambda self: [package])
    try:
        with TestClient(app) as manager:
            assert manager.get("/api/design-handoffs").status_code == 401
            assert (
                manager.post(
                    "/api/design-auth/login", json={"username": "lead", "password": "leader-password-123"}
                ).status_code
                == 200
            )
            assert manager.post("/api/design-handoffs/sync").status_code == 200
            assert manager.post("/api/design-handoffs/sync").status_code == 200
            rows = manager.get("/api/design-handoffs").json()["items"]
            assert len(rows) == 1
            handoff_id = rows[0]["id"]
            assert (
                manager.post(
                    "/api/design-auth/staff",
                    json={"username": "worker", "display_name": "设计师", "password": "designer-password-123"},
                ).status_code
                == 201
            )
            assert (
                manager.post(f"/api/design-handoffs/{handoff_id}/decision", json={"action": "accept"}).status_code
                == 200
            )
            assigned = manager.post(
                f"/api/design-handoffs/{handoff_id}/decision", json={"action": "assign", "assignee": "worker"}
            )
            assert assigned.status_code == 200, assigned.text
            project_id = assigned.json()["project_id"]
            assert manager.get(f"/api/project/{project_id}/design-workspace").status_code == 200
            assert manager.post(f"/api/project/{project_id}/design-specs", json={"intent": "改设计"}).status_code == 403
            with TestClient(app) as worker:
                assert (
                    worker.post(
                        "/api/design-auth/login", json={"username": "worker", "password": "designer-password-123"}
                    ).status_code
                    == 200
                )
                assert worker.post("/api/design-handoffs/sync").status_code == 403
                assert len(worker.get("/api/design-handoffs").json()["items"]) == 1
                workspace = worker.get(f"/api/project/{project_id}/design-workspace")
                assert workspace.status_code == 200
                assert workspace.json()["specs"][0]["spec"]["intent"] == "改善通勤西裤版型"
                assert worker.post(f"/api/design-handoffs/{handoff_id}/submit").status_code == 409
    finally:
        models.reset_engine_for_tests()
        config.get_config.cache_clear()
