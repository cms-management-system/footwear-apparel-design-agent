"""Personnel-write revalidation using isolated synthetic accounts, no network."""

import time
from contextlib import ExitStack, contextmanager

import pytest
from fastapi.testclient import TestClient

from app import config, design_auth, models
from app.main import app

PASSWORD = "synthetic-original-password-123"
NEW_PASSWORD = "synthetic-replacement-password-456"


@pytest.fixture()
def accounts(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "_load_env", lambda: None)
    stored = design_auth.password_hash(PASSWORD)
    for name, value in {
        "DATABASE_URL": f"sqlite:///{tmp_path / 'synthetic-account-transactions.sqlite3'}",
        "ASSETS_DIR": str(tmp_path / "synthetic-assets"),
        "AGENT_WORKER_ENABLED": "false",
        "DESIGN_INTEGRATION_MODE": "managed",
        "DESIGN_INSTANCE_ID": "synthetic-account-design",
        "DESIGN_SCOPE_ID": "synthetic-account-scope",
        "DESIGN_MANAGER_USERNAME": "lead",
        "DESIGN_MANAGER_PASSWORD_HASH": stored,
        "DESIGN_PAID_PROVIDERS_ENABLED": "false",
        "DESIGN_ALLOWED_ORIGINS": "http://127.0.0.1:3092",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("DESIGN_SESSION_COOKIE_NAME", raising=False)
    config.get_config.cache_clear()
    models.reset_engine_for_tests()
    try:
        with ExitStack() as stack:
            manager = stack.enter_context(TestClient(app))
            worker = TestClient(app)
            stack.callback(worker.close)
            clients = {"lead": manager, "worker": worker}
            with models.session() as db:
                db.add(
                    models.DesignerUser(
                        username="worker", display_name="合成设计员", password_hash=stored, role="designer", active=True
                    )
                )
                for name, client in clients.items():
                    token = f"synthetic-account-token-{name}"
                    db.add(
                        models.DesignerSession(
                            token_hash=design_auth.token_hash(token), username=name, expires_at=int(time.time()) + 3600
                        )
                    )
                    client.cookies.set(design_auth.cookie_name(), token)
                db.commit()
            for client in clients.values():
                identity = client.get("/api/design-auth/me")
                assert identity.status_code == 200, identity.text
                client.headers["X-Design-Context"] = identity.json()["auth_context_id"]
            yield {"clients": clients, "stored": stored}
    finally:
        models.reset_engine_for_tests()
        config.get_config.cache_clear()


def mutate(fixture, action):
    if action == "create":
        return fixture["clients"]["lead"].post(
            "/api/design-auth/staff",
            json={"username": "newperson", "display_name": "合成新员工", "password": NEW_PASSWORD},
        )
    if action == "reset":
        return fixture["clients"]["lead"].patch(
            "/api/design-auth/staff/worker", json={"action": "reset_password", "password": NEW_PASSWORD}
        )
    return fixture["clients"]["worker"].post(
        "/api/design-auth/password", json={"current_password": PASSWORD, "new_password": NEW_PASSWORD}
    )


def no_password_or_account_mutation(fixture):
    with models.session() as db:
        assert db.get(models.DesignerUser, "newperson") is None
        assert db.get(models.DesignerUser, "worker").password_hash == fixture["stored"]


@pytest.mark.parametrize("action", ["create", "reset", "password"])
@pytest.mark.parametrize("lost_authority", ["revoked", "expired", "deactivated", "role_changed"])
def test_authority_lost_after_dependencies_is_rejected_before_account_mutation(
    accounts, monkeypatch, action, lost_authority
):
    original = design_auth._account_mutation
    actor = "worker" if action == "password" else "lead"

    @contextmanager
    def revoke_after_dependencies(request, user, **kwargs):
        # This wrapper runs after middleware and dependency authorization, exactly
        # where the original account handlers previously trusted stale authority.
        with models.session() as db:
            if lost_authority == "revoked":
                db.query(models.DesignerSession).filter_by(username=actor).delete()
            elif lost_authority == "expired":
                db.query(models.DesignerSession).filter_by(username=actor).update({"expires_at": 0})
            elif lost_authority == "deactivated":
                db.get(models.DesignerUser, actor).active = False
            else:
                db.get(models.DesignerUser, actor).role = "manager" if actor == "worker" else "designer"
            db.commit()
        with original(request, user, **kwargs) as db:
            yield db

    monkeypatch.setattr(design_auth, "_account_mutation", revoke_after_dependencies)
    response = mutate(accounts, action)
    expected_status, expected_code = (
        (403, "ROLE_FORBIDDEN") if lost_authority == "role_changed" else (401, "LOGIN_REQUIRED")
    )
    assert response.status_code == expected_status, response.text
    assert response.json()["error"]["code"] == expected_code
    no_password_or_account_mutation(accounts)


@pytest.mark.parametrize("action", ["create", "reset", "password"])
def test_session_expiring_during_password_hash_rolls_back_all_mutations(accounts, monkeypatch, action):
    original_hash = design_auth.password_hash
    timestamp = [time.time()]
    monkeypatch.setattr(design_auth.time, "time", lambda: timestamp[0])

    def slow_hash(password, salt=None):
        result = original_hash(password, salt)
        if password == NEW_PASSWORD:
            timestamp[0] += 7200  # Simulated expensive step; no sleeping or wall-clock claim.
        return result

    monkeypatch.setattr(design_auth, "password_hash", slow_hash)
    response = mutate(accounts, action)
    assert response.status_code == 401, response.text
    assert response.json()["error"]["code"] == "LOGIN_REQUIRED"
    no_password_or_account_mutation(accounts)
    with models.session() as db:
        # Failed reset/change must not accidentally revoke the target sessions.
        assert db.query(models.DesignerSession).filter_by(username="worker").count() == 1


def test_successful_own_password_change_revokes_sessions_with_password_commit(accounts):
    response = mutate(accounts, "password")
    assert response.status_code == 200, response.text
    with models.session() as db:
        worker = db.get(models.DesignerUser, "worker")
        assert design_auth.verify_password(NEW_PASSWORD, worker.password_hash)
        assert db.query(models.DesignerSession).filter_by(username="worker").count() == 0
        assert db.query(models.DesignerSession).filter_by(username="lead").count() == 1
    assert accounts["clients"]["worker"].get("/api/design-auth/me").status_code == 401


def test_successful_manager_creation_reset_and_deactivation_remain_available(accounts):
    assert mutate(accounts, "create").status_code == 201
    assert mutate(accounts, "reset").status_code == 200
    with models.session() as db:
        assert db.get(models.DesignerUser, "newperson").role == "designer"
        assert design_auth.verify_password(NEW_PASSWORD, db.get(models.DesignerUser, "worker").password_hash)
        assert db.query(models.DesignerSession).filter_by(username="worker").count() == 0
    response = accounts["clients"]["lead"].patch("/api/design-auth/staff/worker", json={"action": "deactivate"})
    assert response.status_code == 200, response.text
    with models.session() as db:
        assert db.get(models.DesignerUser, "worker").active is False
