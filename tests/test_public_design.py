"""Disposable public HTTPS/proxy and cumulative quota checks; no paid POST."""
import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import Request
from fastapi.testclient import TestClient
from test_demo_access import count, enter, me, new, validation_source
from test_direct_create import BODY, execute, run_state
from test_interactive_local import DecisionProvider
from test_interactive_local import configured as configured
from test_interactive_local import managed_access as managed_access
from test_interactive_local import operating as operating

from app import config, models
from app import design_demo_access as demo
from app.agent import execution_quota, interactive_policy, public_budget
from app.agent.managed_store import InteractiveGrant, ProviderSlot, PublicProviderCall
from app.agent.providers import Provider
from app.agent.store import AgentError, transaction
from app.main import app

ORIGIN = "https://synthetic-public.invalid"


@pytest.fixture
def public_client(operating, monkeypatch):
    for key, value in {
        "DESIGN_DEMO_ACCESS_ENABLED": "true", "DESIGN_DEMO_DESIGNER_USERNAME": "alice",
        "DESIGN_MANAGER_USERNAME": "lead", "DESIGN_DEMO_DEPLOYMENT_MODE": "public_demo",
        "DESIGN_PUBLIC_ORIGIN": ORIGIN, "DESIGN_ALLOWED_ORIGINS": ORIGIN,
        "DESIGN_SESSION_COOKIE_SECURE": "true", "DESIGN_COOKIE_PATH": "/v2/design",
        "DESIGN_INSTANCE_ID": "design-public-20261010", "DESIGN_SCOPE_ID": "public-three-agent-20261010",
    }.items():
        monkeypatch.setenv(key, value)
    config.get_config.cache_clear()
    policy = json.loads(operating.read_text())
    cfg = config.get_config()
    policy.update(scope_id=cfg.design_scope_id, target_instance_id=cfg.design_instance_id)
    operating.write_text(json.dumps(policy))
    demo.initialize()
    with transaction() as db:
        public_budget.initialize(db)
    monkeypatch.setattr(Provider, "_post", lambda *a, **k: pytest.fail("no external paid POST"))

    async def strip_prefix(scope, receive, send):
        scope = dict(scope)
        scope["path"] = scope["path"].removeprefix("/v2/design")
        scope["raw_path"] = scope["path"].encode()
        await app(scope, receive, send)

    client = TestClient(strip_prefix, base_url=ORIGIN + "/v2/design/", headers={"Origin": ORIGIN})
    try:
        yield client
    finally:
        client.close()


def test_public_roles_cookie_scope_csrf_and_owner(public_client, managed_access):
    employee = me(public_client)
    cookies = public_client.get("/api/design-auth/me").headers.get_list("set-cookie")
    assert all("Secure" in x and "HttpOnly" in x and "SameSite=strict" in x and "Path=/v2/design" in x
               for x in cookies)
    assert count(InteractiveGrant) == count(PublicProviderCall) == 0
    pid = new(public_client, employee).json()["project_id"]
    other = managed_access["projects"]["bob"]["pid"]
    assert public_client.get(f"/api/project/{other}/design-workspace").status_code == 404
    manager = enter(public_client, "manager")
    assert manager["auth_context_id"] != employee["auth_context_id"]
    assert public_client.get("/api/design-handoffs").status_code == 200
    assert public_client.get("/api/design-auth/staff").status_code == 200
    assert new(public_client, manager, "public-manager-forbidden").status_code == 403
    assert new(public_client, employee, "public-stale-context").status_code == 409
    assert public_client.get(f"/api/project/{pid}/design-workspace").status_code == 200
    assert enter(public_client, "designer") == employee
    for bad in ["https://other.invalid", "null"]:
        r = public_client.post("/api/design-auth/demo-access", json={"role": "manager"}, headers={"Origin": bad})
        assert r.status_code == 403
    del public_client.headers["Origin"]
    assert public_client.post("/api/design-auth/demo-access", json={"role": "manager"}).status_code == 403
    assert public_client.get("/api/design-auth/me", headers={"Host": "other.invalid"}).status_code == 403
    assert count(PublicProviderCall) == 0


@pytest.mark.parametrize("key,value", [
    ("DESIGN_PUBLIC_ORIGIN", "http://synthetic-public.invalid"), ("DESIGN_SESSION_COOKIE_SECURE", "false"),
    ("DESIGN_COOKIE_PATH", "/"), ("DESIGN_SCOPE_ID", "private-scope"),
    ("DESIGN_ALLOWED_ORIGINS", ORIGIN + ",https://other.invalid"),
    ("DESIGN_PUBLIC_ORIGIN", "https://*.invalid"), ("DESIGN_DEMO_DEPLOYMENT_MODE", "oops"),
])
def test_public_config_fail_closed(public_client, monkeypatch, key, value):
    monkeypatch.setenv(key, value)
    with pytest.raises(ValueError):
        config.Config()


@pytest.mark.parametrize("peer,host,proto,accepted", [
    ("127.0.0.1", "synthetic-public.invalid", "https", True),
    ("::1", "synthetic-public.invalid", "https", True),
    ("198.51.100.8", "synthetic-public.invalid", "https", False),
    ("127.0.0.1", "other.invalid", "https", False),
    ("127.0.0.1", "synthetic-public.invalid", "http", False),
])
def test_proxy_requires_actual_loopback_and_fixed_headers(public_client, peer, host, proto, accepted):
    request = Request({"type": "http", "method": "GET", "path": "/api/design-auth/me", "query_string": b"",
                       "scheme": "http", "server": ("127.0.0.1", 8192), "client": (peer, 5000),
                       "headers": [(b"host", b"127.0.0.1:8192"), (b"x-forwarded-host", host.encode()),
                                   (b"x-forwarded-proto", proto.encode())]})
    if accepted:
        demo.access_origin_guard(request)
    else:
        with pytest.raises(AgentError):
            demo.access_origin_guard(request)


def test_exact_20_4_restart_duplicate_and_operator_increase(public_client, monkeypatch):
    with transaction() as db:
        for stage, limit in [("text", 20), ("image", 4)]:
            for n in range(limit):
                public_budget.reserve_dispatch(db, stage, f"{stage}-{n}", f"run-{n}")
            assert public_budget.remaining(db, stage) == 0
            with pytest.raises(AgentError, match="暂不可生成"):
                public_budget.reserve_dispatch(db, stage, f"{stage}-overflow", "overflow")
    models.reset_engine_for_tests()
    with transaction() as db:
        public_budget.initialize(db)
        assert public_budget.projection(db)["remaining"] == {"text_calls": 0, "image_calls": 0}
        with pytest.raises(AgentError):
            public_budget.reserve_dispatch(db, "text", "text-0", "replay")
    identity = me(public_client)
    assert new(public_client, identity, "after-zero-blank").status_code == 201
    assert new(public_client, identity, "after-zero-generation", BODY).json()["run_status"] == "blocked"
    assert count(InteractiveGrant) == 0
    enter(public_client, "manager")
    assert public_client.get("/api/design-handoffs").status_code == 200
    enter(public_client, "designer")
    another = TestClient(app, base_url=ORIGIN, headers={"Origin": ORIGIN})
    try:
        me(another)
    finally:
        another.close()
    assert count(PublicProviderCall) == 24
    monkeypatch.setenv("DESIGN_PUBLIC_TEXT_CALL_LIMIT", "21")
    config.get_config.cache_clear()
    with transaction() as db:
        assert public_budget.remaining(db, "text") == 0  # No HTTP/config reload self-increase.
        public_budget.initialize(db)
        assert public_budget.remaining(db, "text") == 1
    assert count(PublicProviderCall) == 24


def test_actual_bounded_mock_dispatches_consume_cumulative_budget(public_client, monkeypatch):
    monkeypatch.setenv("DESIGN_PUBLIC_TEXT_CALL_LIMIT", "2")
    monkeypatch.setenv("DESIGN_PUBLIC_IMAGE_CALL_LIMIT", "1")
    config.get_config.cache_clear()
    with transaction() as db:
        public_budget.initialize(db)
    identity = me(public_client)
    data = new(public_client, identity, "public-real-mock-dispatch", BODY).json()
    assert data["run_status"] == "queued", data
    provider = DecisionProvider()
    execute(run_state(public_client, data["run_id"])["text_task_id"], provider)
    execute(run_state(public_client, data["run_id"])["image_task_id"], provider)
    assert run_state(public_client, data["run_id"])["status"] == "succeeded"
    assert provider.text_calls == provider.calls == 1 and count(PublicProviderCall) == 2
    assert new(public_client, identity, "image-global-exhausted", BODY).json()["run_status"] == "blocked"
    discussion = new(public_client, identity, "text-still-available", {**BODY, "intent": "discuss"}).json()
    p = DecisionProvider(resolved="discuss")
    execute(run_state(public_client, discussion["run_id"])["text_task_id"], p)
    assert run_state(public_client, discussion["run_id"])["status"] == "succeeded"
    assert p.text_calls == 1 and p.calls == 0 and count(PublicProviderCall) == 3
    cap = public_client.get("/api/design-agent/capabilities").json()
    assert cap["public_execution_budget"]["remaining"] == {"text_calls": 0, "image_calls": 0}
    assert cap["direct_creation"]["reason"] == "PUBLIC_BUDGET_EXHAUSTED"


def test_concurrent_dispatch_and_unknown_do_not_refund_or_expire(public_client):
    def dispatch(attempt):
        try:
            with transaction() as db:
                execution_quota.acquire_slot(db, attempt)
                public_budget.reserve_dispatch(db, "text", attempt, attempt)
            return True
        except AgentError:
            return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(dispatch, ["attempt-a", "attempt-b"])) == [False, True]
    with transaction() as db:
        db.get(ProviderSlot, 1).status = "unknown"
    models.reset_engine_for_tests()
    with transaction() as db:
        public_budget.initialize(db)
        assert public_budget.remaining(db, "text") == 19
        with pytest.raises(AgentError):
            execution_quota.acquire_slot(db, "restart-unknown")
    assert count(PublicProviderCall) == 1


def test_validation_exhausted_never_becomes_public_operating(public_client, managed_access):
    validation_source(public_client, managed_access)
    identity = me(public_client)
    assert new(public_client, identity, "public-validation-exhausted", BODY).json()["run_status"] == "blocked"
    for role in ["manager", "designer"]:
        enter(public_client, role)
        cap = public_client.get("/api/design-agent/capabilities").json()
        assert cap["direct_creation"]["remaining"] == {"text_calls": 0, "image_calls": 0}
        assert cap["public_execution_budget"]["remaining"] == {"text_calls": 20, "image_calls": 4}
    assert count(PublicProviderCall) == count(InteractiveGrant) == 0


def test_public_disabling_demo_restores_normal_auth_and_no_legacy_fallback(public_client, monkeypatch):
    me(public_client)
    monkeypatch.setenv("DESIGN_DEMO_ACCESS_ENABLED", "false")
    monkeypatch.setenv("DESIGN_INTERACTIVE_POLICY_FILE", "")
    config.get_config.cache_clear()
    assert public_client.get("/api/design-auth/me").status_code == 401
    with transaction() as db:
        with pytest.raises(AgentError):
            interactive_policy.guard_legacy(db)
        assert public_budget.active(db)
    assert count(PublicProviderCall) == 0


def test_budget_transaction_rollback_and_binding_guard(public_client, monkeypatch):
    with pytest.raises(RuntimeError), transaction() as db:
        execution_quota.acquire_slot(db, "rollback")
        public_budget.reserve_dispatch(db, "image", "rollback", "rollback")
        raise RuntimeError("abort before outbound")
    with models.session() as db:
        assert db.get(ProviderSlot, 1) is None
    assert count(PublicProviderCall) == 0
    monkeypatch.setenv("DESIGN_DEMO_DEPLOYMENT_MODE", "local")
    monkeypatch.setenv("DESIGN_DEMO_ACCESS_ENABLED", "false")
    monkeypatch.setenv("DESIGN_SCOPE_ID", "other-scope")
    config.get_config.cache_clear()
    with transaction() as db, pytest.raises(AgentError):
        public_budget.remaining(db, "text")
