"""Local demo access on disposable synthetic databases; no external POST."""

import asyncio
import io

import pytest
from fastapi import Request
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import func, select
from test_direct_create import BLANK, BODY, execute, run_state
from test_interactive_local import DecisionProvider
from test_interactive_local import configured as configured
from test_interactive_local import managed_access as managed_access
from test_interactive_local import operating as operating

from app import config, models
from app import design_demo_access as demo
from app.agent import access_context, assets, events, interactive_policy
from app.agent.managed_store import ExecutionContextPolicy, ExecutionReservation, InteractiveGrant
from app.agent.providers import Provider
from app.agent.store import AgentError, Record, transaction
from app.design_auth import password_hash, session_context, token_hash
from app.main import app


@pytest.fixture
def demo_client(operating, monkeypatch):
    monkeypatch.setenv("DESIGN_DEMO_ACCESS_ENABLED", "true")
    monkeypatch.setenv("DESIGN_DEMO_DESIGNER_USERNAME", "alice")
    monkeypatch.setenv("DESIGN_MANAGER_USERNAME", "lead")
    config.get_config.cache_clear()
    demo.initialize()
    monkeypatch.setattr(Provider, "_post", lambda *a, **k: pytest.fail("no paid POST allowed"))
    client = TestClient(app, base_url="http://127.0.0.1:8092")
    try:
        yield client
    finally:
        client.close()


def count(cls):
    with models.session() as db:
        return db.scalar(select(func.count()).select_from(cls))


def me(client):
    response = client.get("/api/design-auth/me")
    assert response.status_code == 200, response.json()
    return response.json()


def enter(client, role):
    response = client.post("/api/design-auth/demo-access", json={"role": role})
    assert response.status_code == 200, response.json()
    return response.json()


def write_headers(identity, action="demo-blank-synthetic", revision=None):
    result = {"X-Design-Context": identity["auth_context_id"], "X-Design-Action-Id": action}
    if revision is not None:
        result["X-Design-Revision"] = str(revision)
    return result


def new(client, identity, action="demo-blank-synthetic", body=BLANK):
    return client.post("/api/design-projects/direct", json=body, headers=write_headers(identity, action))


def request(client, path):
    cfg = config.get_config()
    raw = client.cookies.get(cfg.design_demo_cookie)
    async def connected():
        return {"type": "http.request", "body": b"", "more_body": False}
    return Request({"type": "http", "method": "GET", "path": path, "query_string": b"",
                    "scheme": "http", "server": ("127.0.0.1", 8092),
                    "headers": [(b"cookie", f"{cfg.design_demo_cookie}={raw}".encode())]}, receive=connected)


def disable(monkeypatch):
    monkeypatch.setenv("DESIGN_DEMO_ACCESS_ENABLED", "false")
    config.get_config.cache_clear()


def test_me_defaults_and_bootstrap_have_no_business_or_budget_side_effect(demo_client):
    before = (count(models.Project), count(Record), count(InteractiveGrant), count(ExecutionReservation))
    identity = me(demo_client)
    assert identity["username"] == "alice" and identity["role"] == "designer"
    assert identity["access_mode"] == "demo" and identity["display_name"] == "演示设计员工"
    assert identity["demo_entry_urls"] == demo.ENTRIES
    for _ in range(3):
        assert me(demo_client) == identity
        assert enter(demo_client, "designer") == identity
    assert count(models.DesignDemoChain) == count(models.DesignDemoSession) == 1
    assert before == (count(models.Project), count(Record), count(InteractiveGrant), count(ExecutionReservation))
    response = demo_client.get("/api/design-auth/me")
    cookies = response.headers.get_list("set-cookie")
    assert len(cookies) == 2 and all("HttpOnly" in c and "SameSite=strict" in c for c in cookies)
    assert "private, no-store" == response.headers["cache-control"]


def test_demo_manager_handoff_list_uses_actual_demo_session(demo_client):
    enter(demo_client, "manager")
    response = demo_client.get("/api/design-handoffs?offset=0&limit=20")
    assert response.status_code == 200, response.json()
    assert {item["id"] for item in response.json()["items"]} == {
        "synthetic-alice", "synthetic-bob", "synthetic-mismatch"
    }
    enter(demo_client, "designer")
    response = demo_client.get("/api/design-handoffs?offset=0&limit=20")
    assert response.status_code == 200
    assert [item["id"] for item in response.json()["items"]] == ["synthetic-alice"]
    assert demo_client.get("/api/design-handoffs/synthetic-bob").status_code == 404


@pytest.mark.parametrize("body", [{"role": "admin"}, {"role": "designer", "purpose": "interactive"},
                                    {"role": "manager", "username": "bob"}, {"role": "designer", "grant": {}}])
def test_bootstrap_strict_body(demo_client, body):
    assert demo_client.post("/api/design-auth/demo-access", json=body).status_code == 422
    assert count(models.DesignDemoChain) == 0


def test_origin_and_local_host(demo_client):
    response = demo_client.post("/api/design-auth/demo-access", json={"role": "manager"},
                                headers={"Origin": "https://untrusted.invalid"})
    assert response.status_code == 403 and response.json()["error"]["code"] == "ORIGIN_FORBIDDEN"
    response = demo_client.get("/api/design-auth/me", headers={"Host": "public.invalid"})
    assert response.status_code == 403 and response.json()["error"]["code"] == "DEMO_LOCAL_ONLY"
    assert count(models.DesignDemoChain) == 0


def test_blank_rename_role_switch_stale_context_and_hidden_owner(demo_client, managed_access):
    employee = me(demo_client)
    result = new(demo_client, employee)
    assert result.status_code == 201
    pid = result.json()["project_id"]
    response = demo_client.patch(f"/api/design-projects/{pid}", json={"title": "合成演示改名", "expected_revision": 1},
                                 headers=write_headers(employee, "demo-rename", 1))
    assert response.status_code == 200, response.json()
    other = managed_access["projects"]["bob"]["pid"]
    assert demo_client.get(f"/api/project/{other}/design-workspace").status_code == 404
    assert demo_client.get("/api/design-auth/staff").status_code == 403
    manager = enter(demo_client, "manager")
    assert manager["username"] == "lead" and manager["auth_context_id"] != employee["auth_context_id"]
    assert me(demo_client) == manager
    assert demo_client.get("/api/design-auth/staff").status_code == 200
    assert demo_client.get(f"/api/project/{pid}/design-workspace").status_code == 200
    assert new(demo_client, employee, "old-employee-tab").status_code == 409
    assert new(demo_client, manager, "manager-cannot-create").status_code == 403
    assert enter(demo_client, "designer") == employee
    assert count(InteractiveGrant) == 0


def test_normal_demo_continuous_mock_generation_and_read_refresh_no_grants(demo_client):
    employee = me(demo_client)
    for index in range(2):
        data = new(demo_client, employee, f"demo-model-action-{index}", BODY).json()
        assert data["run_status"] == "queued", data
        provider = DecisionProvider()
        execute(run_state(demo_client, data["run_id"])["text_task_id"], provider)
        execute(run_state(demo_client, data["run_id"])["image_task_id"], provider)
        assert provider.text_calls == provider.calls == 1
        assert run_state(demo_client, data["run_id"])["status"] == "succeeded"
        assert me(demo_client) == employee
    assert count(InteractiveGrant) == 2
    assert count(ExecutionReservation) == 4
    for _ in range(3):
        assert enter(demo_client, "designer") == employee
        cap = demo_client.get("/api/design-agent/capabilities").json()["direct_creation"]
        assert cap["remaining"] == {"text_calls": 1, "image_calls": 1}
    assert count(InteractiveGrant) == 2


def validation_source(client, fixture):
    cfg = config.get_config()
    raw = fixture["tokens"]["alice"]
    client.cookies.set(cfg.design_session_cookie, raw)
    context = session_context(token_hash(raw))
    with transaction() as db:
        interactive_policy.register_validation(db, context, "alice", ledger_id="synthetic-exhausted-ledger",
                                               grant_ids=["synthetic-used-text", "synthetic-used-image"])
        for index, stage in enumerate(["text", "text", "image"]):
            db.add(ExecutionReservation(id=f"exhausted-{index}", run_id=f"old-{index}", stage=stage,
                                        grant_id="synthetic-used-text" if stage == "text" else "synthetic-used-image",
                                        project_id=fixture["projects"]["alice"]["pid"], status="succeeded", payload={}))
    return context


def test_validation_ordinary_cookie_link_role_switch_logout_renewal_no_fallback(demo_client, managed_access):
    original = validation_source(demo_client, managed_access)
    identity = me(demo_client)
    cfg = config.get_config()
    assert demo_client.cookies.get(cfg.design_session_cookie) == managed_access["tokens"]["alice"]
    assert identity["auth_context_id"] != original
    for role in ["manager", "designer"]:
        identity = enter(demo_client, role)
        cap = demo_client.get("/api/design-agent/capabilities").json()["direct_creation"]
        assert cap["remaining"] == {"text_calls": 0, "image_calls": 0}
    data = new(demo_client, identity, "exhausted-demo-new-action", BODY).json()
    assert data["run_status"] == "blocked"
    assert count(InteractiveGrant) == 0 and count(ExecutionReservation) == 3
    assert demo_client.post("/api/design-auth/logout").status_code == 200
    # Original cookie may expire or be absent: server chain still carries validation.
    demo_client.cookies.delete(cfg.design_session_cookie)
    assert me(demo_client) == identity
    cap = demo_client.get("/api/design-agent/capabilities").json()["direct_creation"]
    assert cap["remaining"] == {"text_calls": 0, "image_calls": 0}
    with models.session() as db:
        contexts = list(db.scalars(select(ExecutionContextPolicy)))
        assert len(contexts) == 3 and all(c.purpose == "validation" for c in contexts)
        assert all(c.payload["ledger_id"] == "synthetic-exhausted-ledger" for c in contexts)
    assert count(InteractiveGrant) == 0


def test_validation_adopts_existing_ordinary_demo_without_reclassifying_old_context(demo_client, managed_access):
    ordinary_demo = me(demo_client)
    validation_source(demo_client, managed_access)
    trusted = me(demo_client)
    assert trusted["auth_context_id"] != ordinary_demo["auth_context_id"]
    with models.session() as db:
        assert db.get(ExecutionContextPolicy, ordinary_demo["auth_context_id"]).purpose == "interactive"
        assert db.get(ExecutionContextPolicy, trusted["auth_context_id"]).purpose == "validation"
    assert count(InteractiveGrant) == 0


def test_validation_broken_link_and_corrupt_recovery_cookie_fail_closed(demo_client, managed_access):
    validation_source(demo_client, managed_access)
    identity = me(demo_client)
    with transaction() as db:
        chain = db.scalar(select(models.DesignDemoChain))
        chain.payload = {**chain.payload, "source_context": "missing-trusted-link"}
    response = demo_client.get("/api/design-auth/me")
    assert response.status_code == 409 and response.json()["error"]["code"] == "DEMO_RECOVERY_BLOCKED"
    assert new(demo_client, identity, "broken-link-write", BODY).status_code == 401
    assert count(InteractiveGrant) == 0
    cfg = config.get_config()
    demo_client.cookies.clear()
    demo_client.cookies.set(cfg.design_demo_chain_cookie, "unrecognized-chain-cookie")
    assert demo_client.get("/api/design-auth/me").status_code == 409
    assert count(models.DesignDemoChain) == 1


def test_logout_cancels_unsent_and_does_not_restart_old_run_after_reentry(demo_client):
    identity = me(demo_client)
    data = new(demo_client, identity, "logout-unsent-model-action", BODY).json()
    task_id = run_state(demo_client, data["run_id"])["text_task_id"]
    assert demo_client.post("/api/design-auth/logout").status_code == 200
    assert me(demo_client) == identity
    with models.session() as db:
        assert db.get(Record, task_id).status == "cancelled"
        run = db.get(Record, data["run_id"])
        assert run.status == "blocked" and run.payload["demo_access_revoked"]
        assert interactive_policy.current_grant(db, run) is None
        reserve = db.scalar(select(ExecutionReservation))
        assert reserve.status == "failed_not_sent"
    assert count(InteractiveGrant) == 1


@pytest.mark.parametrize("path,method", [("/api/design-auth/login", "post"),
    ("/api/design-auth/register", "post"), ("/api/design-auth/password", "post"),
    ("/api/design-auth/staff", "post"), ("/api/design-auth/staff/alice", "patch")])
def test_account_operations_frozen(demo_client, path, method):
    before = count(models.DesignerUser)
    response = getattr(demo_client, method)(path, json={})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == ("DEMO_LOGIN_FROZEN" if path.endswith(("login", "register"))
                                                else "DEMO_ACCOUNT_FROZEN")
    assert count(models.DesignerUser) == before


def test_disable_restores_ordinary_session_and_revokes_demo_transaction_worker(
    demo_client, managed_access, monkeypatch
):
    cfg = config.get_config()
    ordinary = managed_access["tokens"]["alice"]
    demo_client.cookies.set(cfg.design_session_cookie, ordinary)
    identity = me(demo_client)
    data = new(demo_client, identity, "disable-unsent-action", BODY).json()
    req = request(demo_client, f"/api/project/{data['project_id']}/design-workspace")
    disable(monkeypatch)
    assert demo_client.get("/api/design-auth/me").status_code == 401
    assert new(demo_client, identity, "disabled-write").status_code == 401
    assert demo_client.post("/api/design-auth/demo-access", json={"role": "designer"}).status_code == 404
    binding = access_context.bind_request(req)
    try:
        with pytest.raises(AgentError, match="登录已失效"):
            events.workspace_snapshot(data["project_id"])
        with models.session() as db:
            assert interactive_policy.session_for_context(db, identity["auth_context_id"], "alice") is None
            assert interactive_policy.current_grant(db, db.get(Record, data["run_id"])) is None
    finally:
        access_context.reset_request(binding)
    assert demo_client.post("/api/design-auth/logout").status_code == 200
    restored = me(demo_client)
    assert restored["access_mode"] == "authenticated" and restored["demo_entry_urls"] is None
    assert restored["auth_context_id"] == session_context(token_hash(ordinary))
    assert demo_client.cookies.get(cfg.design_session_cookie) == ordinary


def test_sse_current_connection_rechecks_role_switch(demo_client):
    identity = me(demo_client)
    pid = new(demo_client, identity).json()["project_id"]
    req = request(demo_client, f"/api/project/{pid}/design-events")
    async def check():
        stream = events.stream(pid, req, duration=1)
        assert "event: workspace" in await anext(stream)
        # Worker thread simulates another tab selecting manager after first frame.
        await asyncio.to_thread(enter, demo_client, "manager")
        assert "LOGIN_REQUIRED" in await anext(stream)
        await stream.aclose()
    asyncio.run(check())


def test_private_asset_permission_tracks_demo_role_and_disable(demo_client, managed_access, monkeypatch):
    me(demo_client)
    own = managed_access["projects"]["alice"]["asset"]
    other = managed_access["projects"]["bob"]["asset"]
    png = io.BytesIO()
    Image.new("RGB", (64, 64), "blue").save(png, "PNG")
    with transaction() as db:
        for asset_id in [own, other]:
            db.get(Record, asset_id).payload = assets.save_image(png.getvalue())
    assert demo_client.get(f"/api/design-assets/{other}/image").status_code == 404
    response = demo_client.get(f"/api/design-assets/{own}/image")
    assert response.status_code == 200 and response.content == png.getvalue()
    assert response.headers["cache-control"] == "private, no-store"
    enter(demo_client, "manager")
    assert demo_client.get(f"/api/design-assets/{other}/image").status_code == 200
    disable(monkeypatch)
    assert demo_client.get(f"/api/design-assets/{own}/image").status_code == 401


@pytest.mark.parametrize("validation", [False, True])
def test_explicit_normal_login_clears_demo_cookie_without_get_fallback_or_old_action_replay(
    demo_client, managed_access, monkeypatch, validation
):
    if validation:
        validation_source(demo_client, managed_access)
    identity = me(demo_client)
    saved = new(demo_client, identity, "normal-login-original-action")
    assert saved.status_code == 201
    with transaction() as db:
        db.get(models.DesignerUser, "alice").password_hash = password_hash("synthetic-password-for-test")
    cfg = config.get_config()
    old_demo_cookie = demo_client.cookies.get(cfg.design_demo_cookie)
    disable(monkeypatch)
    assert demo_client.get("/api/design-auth/me").status_code == 401
    response = demo_client.post("/api/design-auth/login", json={
        "username": "alice", "password": "synthetic-password-for-test"})
    assert response.status_code == 200, response.json()
    assert demo_client.cookies.get(cfg.design_demo_cookie) is None
    assert demo_client.cookies.get(cfg.design_demo_chain_cookie)
    ordinary = me(demo_client)
    assert ordinary["access_mode"] == "authenticated" and ordinary["auth_context_id"] != identity["auth_context_id"]
    assert new(demo_client, identity, "normal-login-original-action").status_code == 409
    assert new(demo_client, ordinary, "normal-login-original-action").status_code == 409
    assert count(models.Project) == len(managed_access["projects"]) + 1
    with models.session() as db:
        policy = db.get(ExecutionContextPolicy, ordinary["auth_context_id"])
        if validation:
            assert policy.purpose == "validation" and policy.payload["ledger_id"] == "synthetic-exhausted-ledger"
        else:
            assert policy is None
        # Copying the old demo credential into the normal cookie cannot restore authority.
        record = db.get(models.DesignerSession, token_hash(old_demo_cookie))
        assert not demo.session_enabled(db, record)


def test_disabled_demo_queued_worker_never_dispatches_after_configuration_reload(demo_client, monkeypatch):
    identity = me(demo_client)
    data = new(demo_client, identity, "demo-worker-disabled", BODY).json()
    task = run_state(demo_client, data["run_id"])["text_task_id"]
    disable(monkeypatch)
    provider = DecisionProvider()
    execute(task, provider)
    assert provider.text_calls == provider.calls == 0
    with models.session() as db:
        run = db.get(Record, data["run_id"])
        assert run.status in {"failed", "blocked"}
        assert all(r.status == "failed_not_sent" for r in db.scalars(select(ExecutionReservation)))


@pytest.mark.parametrize("validation", [False, True])
def test_access_cookie_recovers_missing_chain_cookie_with_original_context_and_purpose(
    demo_client, managed_access, validation
):
    if validation:
        validation_source(demo_client, managed_access)
    identity = me(demo_client)
    cfg = config.get_config()
    original_chain = demo_client.cookies.get(cfg.design_demo_chain_cookie)
    demo_client.cookies.delete(cfg.design_demo_chain_cookie)
    if validation:
        demo_client.cookies.delete(cfg.design_session_cookie)
    assert me(demo_client) == identity
    assert demo_client.cookies.get(cfg.design_demo_chain_cookie) == original_chain
    assert count(models.DesignDemoChain) == 1
    assert count(InteractiveGrant) == 0


def test_ordinary_credential_cannot_be_substituted_for_demo_marker(demo_client, managed_access):
    cfg = config.get_config()
    demo_client.cookies.set(cfg.design_demo_cookie, managed_access["tokens"]["bob"])
    assert demo_client.get("/api/design-auth/me").status_code == 409
    assert demo_client.get("/api/design-agent/capabilities").status_code == 401
    assert count(models.DesignDemoChain) == 0


def test_original_interactive_chain_cannot_reactivate_after_validation_adoption(demo_client, managed_access):
    initial = me(demo_client)
    cfg = config.get_config()
    old_chain = demo_client.cookies.get(cfg.design_demo_chain_cookie)
    old_access = demo_client.cookies.get(cfg.design_demo_cookie)
    validation_source(demo_client, managed_access)
    classified = me(demo_client)
    demo_client.cookies.clear()
    demo_client.cookies.set(cfg.design_demo_chain_cookie, old_chain)
    demo_client.cookies.set(cfg.design_demo_cookie, old_access)
    assert demo_client.get("/api/design-agent/capabilities").status_code == 401
    assert me(demo_client) == classified
    assert initial["auth_context_id"] != classified["auth_context_id"]
    assert count(InteractiveGrant) == 0


def test_renewal_restart_uses_same_context_and_expired_validation_classification(demo_client, managed_access):
    context = validation_source(demo_client, managed_access)
    with transaction() as db:
        record = db.get(models.DesignerSession, token_hash(managed_access["tokens"]["alice"]))
        record.expires_at = 0
    identity = me(demo_client)
    cfg = config.get_config()
    with transaction() as db:
        record = db.get(models.DesignerSession, token_hash(demo_client.cookies.get(cfg.design_demo_cookie)))
        record.expires_at = 0
    config.get_config.cache_clear()
    demo.initialize()
    assert me(demo_client) == identity
    with models.session() as db:
        assert db.get(ExecutionContextPolicy, context).purpose == "validation"
    assert count(InteractiveGrant) == 0 and count(ExecutionReservation) == 3


def test_mapping_error_never_repairs_existing_account(demo_client, monkeypatch):
    with models.session() as db:
        original = db.get(models.DesignerUser, "alice").password_hash
    monkeypatch.setenv("DESIGN_DEMO_DESIGNER_USERNAME", "lead")
    config.get_config.cache_clear()
    assert demo_client.get("/api/design-auth/me").status_code == 503
    with models.session() as db:
        user = db.get(models.DesignerUser, "alice")
        assert user.password_hash == original and user.role == "designer"


def test_clean_install_initializes_only_once_and_no_password_steps(demo_client, monkeypatch, tmp_path):
    # Switch to another disposable empty database, never the actual runtime.
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'clean-install.sqlite3'}")
    monkeypatch.setenv("ASSETS_DIR", str(tmp_path / "clean-assets" / "assets"))
    config.get_config.cache_clear()
    models.reset_engine_for_tests()
    models.init_db()
    from app.agent.migrate import migrate
    migrate()
    demo.initialize()
    assert count(models.DesignerUser) == 2
    cfg = config.get_config()
    assert cfg.design_demo_secret_file.stat().st_mode & 0o777 == 0o600
    secret = cfg.design_demo_secret_file.read_bytes()
    demo.initialize()
    assert count(models.DesignerUser) == 2 and cfg.design_demo_secret_file.read_bytes() == secret
    demo_client.cookies.clear()
    assert me(demo_client)["username"] == "alice"
    assert enter(demo_client, "manager")["username"] == "lead"
    assert count(models.Project) == count(InteractiveGrant) == 0
