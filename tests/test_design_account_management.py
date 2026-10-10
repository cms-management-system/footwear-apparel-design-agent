from fastapi.testclient import TestClient

from app import config, models
from app.design_auth import password_hash
from app.main import app


def test_manager_controls_members_and_members_change_own_password(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'accounts.sqlite3'}")
    monkeypatch.setenv("ASSETS_DIR", str(tmp_path / "assets"))
    monkeypatch.setenv("AGENT_WORKER_ENABLED", "false")
    monkeypatch.setenv("DESIGN_AUTH_REQUIRED", "true")
    monkeypatch.setenv("DESIGN_MANAGER_USERNAME", "lead")
    monkeypatch.setenv("DESIGN_MANAGER_PASSWORD_HASH", password_hash("leader-password-123"))
    config.get_config.cache_clear()
    models.reset_engine_for_tests()
    try:
        with TestClient(app) as manager, TestClient(app) as designer:
            assert manager.post("/api/design-auth/login", json={"username": "lead", "password": "leader-password-123"}).status_code == 200
            assert manager.post("/api/design-auth/staff", json={"username": "worker", "display_name": "小林", "password": "designer-password-123"}).status_code == 201
            assert designer.post("/api/design-auth/login", json={"username": "worker", "password": "designer-password-123"}).status_code == 200
            assert designer.get("/api/design-auth/staff").status_code == 403
            assert designer.patch("/api/design-auth/staff/worker", json={"action": "deactivate"}).status_code == 403
            assert manager.patch("/api/design-auth/staff/worker", json={"action": "deactivate"}).status_code == 200
            assert designer.get("/api/design-auth/me").status_code == 401
            assert designer.post("/api/design-auth/login", json={"username": "worker", "password": "designer-password-123"}).status_code == 401
            assert manager.get("/api/design-auth/staff").json()["items"][0]["active"] is False
            assert manager.patch("/api/design-auth/staff/worker", json={"action": "activate"}).status_code == 200
            assert manager.patch("/api/design-auth/staff/worker", json={"action": "reset_password", "password": "fresh-password-123"}).status_code == 200
            assert designer.post("/api/design-auth/login", json={"username": "worker", "password": "designer-password-123"}).status_code == 401
            assert designer.post("/api/design-auth/login", json={"username": "worker", "password": "fresh-password-123"}).status_code == 200
            assert designer.post("/api/design-auth/password", json={"current_password": "wrong-password", "new_password": "another-password-123"}).status_code == 400
            assert designer.post("/api/design-auth/password", json={"current_password": "fresh-password-123", "new_password": "another-password-123"}).status_code == 200
            assert designer.get("/api/design-auth/me").status_code == 401
            assert designer.post("/api/design-auth/login", json={"username": "worker", "password": "another-password-123"}).status_code == 200
    finally:
        models.reset_engine_for_tests()
        config.get_config.cache_clear()


def test_designer_self_registration_uses_designer_role_and_manager_can_manage(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'registration.sqlite3'}")
    monkeypatch.setenv("ASSETS_DIR", str(tmp_path / "assets"))
    monkeypatch.setenv("AGENT_WORKER_ENABLED", "false")
    monkeypatch.setenv("DESIGN_AUTH_REQUIRED", "true")
    monkeypatch.setenv("DESIGN_MANAGER_USERNAME", "lead")
    monkeypatch.setenv("DESIGN_MANAGER_PASSWORD_HASH", password_hash("leader-password-123"))
    config.get_config.cache_clear()
    models.reset_engine_for_tests()
    try:
        with TestClient(app) as designer, TestClient(app) as manager:
            payload = {"username": "newdesigner", "display_name": "小陈", "password": "designer-password-123", "confirm_password": "designer-password-123"}
            assert designer.post("/api/design-auth/register", json={**payload, "confirm_password": "different-password"}).status_code == 422
            created = designer.post("/api/design-auth/register", json={**payload, "role": "manager"})
            assert created.status_code == 201, created.text
            assert created.json()["role"] == "designer"
            assert designer.get("/api/design-auth/me").json()["role"] == "designer"
            assert designer.get("/api/design-auth/staff").status_code == 403
            assert designer.post("/api/design-auth/register", json=payload).status_code == 409
            assert manager.post("/api/design-auth/login", json={"username": "lead", "password": "leader-password-123"}).status_code == 200
            assert manager.get("/api/design-auth/staff").json()["items"][0]["username"] == "newdesigner"
            assert manager.patch("/api/design-auth/staff/newdesigner", json={"action": "deactivate"}).status_code == 200
            assert designer.get("/api/design-auth/me").status_code == 401
    finally:
        models.reset_engine_for_tests()
        config.get_config.cache_clear()
