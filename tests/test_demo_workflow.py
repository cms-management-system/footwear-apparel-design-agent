"""Demo personnel workflow on synthetic source fixtures; no provider/network calls."""

import pytest
from fastapi import Request
from sqlalchemy import select
from test_demo_access import disable, enter, me
from test_managed_workflow import act, assigned, counts, success, version_fixture
from test_managed_workflow import workflow as workflow

from app import config, models
from app import design_demo_access as demo
from app.agent import managed_workflow
from app.agent.managed_store import ExecutionReservation, InteractiveGrant, ManagedAudit, ManagedReceipt
from app.agent.providers import Provider
from app.agent.store import AgentError


@pytest.fixture
def demo_workflow(workflow, monkeypatch):
    monkeypatch.setenv("DESIGN_DEMO_ACCESS_ENABLED", "true")
    monkeypatch.setenv("DESIGN_DEMO_DESIGNER_USERNAME", "alice")
    config.get_config.cache_clear()
    demo.initialize()
    monkeypatch.setattr(Provider, "_post", lambda *a, **kw: pytest.fail("provider POST forbidden"))
    for name, role in [("lead", "manager"), ("alice", "designer")]:
        client = workflow["clients"][name]
        client.base_url = "http://127.0.0.1:8092"
        identity = enter(client, role)
        client.headers["X-Design-Context"] = identity["auth_context_id"]
    yield workflow
    assert workflow["network"] == []
    with models.session() as db:
        assert list(db.scalars(select(ExecutionReservation))) == []
        assert list(db.scalars(select(InteractiveGrant))) == []


def test_demo_manager_and_designer_lists_detail_and_local_approval_write(demo_workflow):
    w = demo_workflow
    manager, employee = w["clients"]["lead"], w["clients"]["alice"]
    inbox = success(manager.get("/api/design-handoffs?offset=0&limit=20"))
    assert inbox["total"] == 1 and inbox["items"][0]["id"] == w["id"]
    assert success(employee.get("/api/design-handoffs"))["total"] == 0
    assert employee.get(f"/api/design-handoffs/{w['id']}").status_code == 404
    assert success(manager.get(f"/api/design-handoffs/{w['id']}"))["status"] == "new"
    result = assigned(w)  # Real accept/assign HTTP commits on the disposable database.
    assert result["status"] == "assigned" and result["assignee"] == "alice"
    task = success(employee.get("/api/design-handoffs"))
    assert task["total"] == 1 and task["items"][0]["allowed_actions"] == ["submit"]
    assert success(employee.get(f"/api/design-handoffs/{w['id']}"))["project_id"] == result["project_id"]
    original = success(manager.get("/api/design-operations/accept-first-001"))
    assert original["result"]["status"] == "accepted"  # Original snapshot remains read-only.
    assert employee.get("/api/design-operations/accept-first-001").status_code == 404
    version, _ = version_fixture(w)
    submission = success(act(w, "alice", "submit", {"expected_revision": 3, "version_id": version}, "demo-submit-001"))
    assert submission["status"] == "review"
    # A successful identity/list read never grants an automatic business approval.
    assert success(manager.get(f"/api/design-handoffs/{w['id']}"))["status"] == "review"
    bad = act(w, "lead", "review", {"expected_revision": 4, "action": "approve",
              "submitted_version_id": "wrong-version", "note": "合成审查"}, "demo-bad-review-001")
    assert bad.status_code == 409
    reviewed = success(act(w, "lead", "review", {"expected_revision": 4, "action": "approve",
                       "submitted_version_id": version, "note": "仅隔离合成候选审查"}, "demo-approve-001"))
    assert reviewed["status"] == "approved" and reviewed["revision"] == 5
    assert reviewed["delivery"]["status"] == "prepared"
    assert success(manager.get("/api/design-operations/demo-approve-001"))["result"] == reviewed
    with models.session() as db:
        row = db.get(models.DesignHandoff, w["id"])
        assert row.status == "approved" and row.submitted_version_id == version
        assert len(list(db.scalars(select(ManagedAudit)))) == 4
    assert len(w["frozen"]) == 1  # Local freeze double, no external delivery or human-review claim.


def test_demo_employee_cannot_manager_decide_review_or_delivery(demo_workflow):
    w = demo_workflow
    before = counts()
    for endpoint, body in [
        ("decision", {"action": "accept", "expected_revision": 1}),
        ("review", {"action": "approve", "expected_revision": 1, "submitted_version_id": "synthetic", "note": "合成"}),
        ("delivery", {"action": "send", "expected_revision": 1, "event_id": "synthetic-never-send"}),
    ]:
        response = act(w, "alice", endpoint, body, "demo-role-denied-" + endpoint)
        assert response.status_code == 403
    assert counts() == before


def test_demo_manager_delivery_still_requires_existing_approved_event(demo_workflow):
    w = demo_workflow
    before = counts()
    response = act(w, "lead", "delivery", {"event_id": "missing-local-event", "expected_revision": 1,
                   "action": "send"}, "demo-no-event-delivery")
    assert response.status_code == 404  # Principal reached; fails before any network/service credential use.
    assert counts() == before


def test_demo_old_context_on_role_switch_rejects_decision_and_keeps_original_data(demo_workflow):
    w = demo_workflow
    client = w["clients"]["lead"]
    old = client.headers["X-Design-Context"]
    employee = enter(client, "designer")
    client.headers["X-Design-Context"] = employee["auth_context_id"]
    response = act(w, "lead", "decision", {"action": "accept", "expected_revision": 1}, "demo-switched-001")
    assert response.status_code == 403
    enter(client, "manager")
    client.headers["X-Design-Context"] = employee["auth_context_id"]
    before = counts()
    response = act(w, "lead", "decision", {"action": "accept", "expected_revision": 1}, "demo-stale-context-001")
    assert response.status_code == 409 and response.json()["error"]["code"] == "AUTH_CONTEXT_CHANGED"
    assert counts() == before
    client.headers["X-Design-Context"] = old
    response = act(w, "lead", "decision", {"action": "accept", "expected_revision": 1}, "demo-restored-001")
    assert success(response)["status"] == "accepted"


@pytest.mark.parametrize("revocation", ["disable", "deactivate", "role_switch"])
def test_demo_workflow_final_transaction_recheck_rolls_back_decision(demo_workflow, monkeypatch, revocation):
    w = demo_workflow
    before = counts()
    original = managed_workflow.save_action
    def revoke(db, *args, **kw):
        original(db, *args, **kw)
        if revocation == "disable":
            disable(monkeypatch)
        elif revocation == "deactivate":
            db.get(models.DesignerUser, "lead").active = False
            db.flush()
        else:
            marker = db.scalar(select(models.DesignDemoSession).where(models.DesignDemoSession.subject == "lead"))
            db.get(models.DesignDemoChain, marker.chain_id).selected_role = "designer"
            db.flush()
    monkeypatch.setattr(managed_workflow, "save_action", revoke)
    response = act(w, "lead", "decision", {"action": "accept", "expected_revision": 1}, "demo-mid-transaction-001")
    assert response.status_code == 401 and response.json()["error"]["code"] == "LOGIN_REQUIRED"
    assert counts() == before
    with models.session() as db:
        assert db.get(models.DesignHandoff, w["id"]).status == "new"
        assert db.get(ManagedReceipt, w["id"]).revision == 1


def test_demo_principal_rechecks_same_loaded_session_after_role_change(demo_workflow):
    client = demo_workflow["clients"]["lead"]
    cfg = config.get_config()
    raw = client.cookies.get(cfg.design_demo_cookie)
    request = Request({"type": "http", "method": "GET", "path": "/api/design-handoffs",
                       "headers": [(b"cookie", f"{cfg.design_demo_cookie}={raw}".encode())],
                       "query_string": b"", "scheme": "http", "server": ("127.0.0.1", 8092)})
    with models.session() as db:
        assert managed_workflow.principal(db, request)[0].role == "manager"
        marker = db.scalar(select(models.DesignDemoSession).where(models.DesignDemoSession.subject == "lead"))
        chain = db.get(models.DesignDemoChain, marker.chain_id)
        chain.selected_role = "designer"
        db.flush()
        with pytest.raises(AgentError) as exc:
            managed_workflow.principal(db, request)
        assert exc.value.code == "LOGIN_REQUIRED"
        db.rollback()


def test_demo_off_restores_original_manager_cookie_and_workflow_access(demo_workflow, monkeypatch):
    w = demo_workflow
    client = w["clients"]["lead"]
    disable(monkeypatch)
    assert client.get("/api/design-handoffs").status_code == 401
    assert client.get(f"/api/design-handoffs/{w['id']}").status_code == 401
    response = client.post("/api/design-auth/login", json={"username": "lead", "password": w["password"]})
    assert response.status_code == 200
    identity = me(client)
    assert identity["access_mode"] == "authenticated"
    client.headers["X-Design-Context"] = identity["auth_context_id"]
    assert success(client.get("/api/design-handoffs"))["total"] == 1
    response = act(w, "lead", "decision", {"action": "accept", "expected_revision": 1}, "ordinary-restored-001")
    assert success(response)["status"] == "accepted"


def test_ordinary_workflow_still_lists_manager_and_employee(workflow):
    w = workflow
    assert success(w["clients"]["lead"].get("/api/design-handoffs"))["total"] == 1
    assert success(w["clients"]["alice"].get("/api/design-handoffs"))["total"] == 0
    assigned(w)
    assert success(w["clients"]["alice"].get("/api/design-handoffs"))["total"] == 1
    assert success(w["clients"]["bob"].get("/api/design-handoffs"))["total"] == 0
    assert w["network"] == []
