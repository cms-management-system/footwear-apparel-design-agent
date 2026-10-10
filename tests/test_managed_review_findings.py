"""Independent review probes of session races and immutable-source behavior.

These are isolated SQLite/ASGI checks with a synthetic network counterpart, not
real cross-product acceptance or business human review.
"""

import base64
import copy
import json
from http.cookies import SimpleCookie

import pytest
from fastapi import Response
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import config, design_auth, models
from app.agent import managed_bridge as bridge
from app.agent import managed_workflow as workflow
from app.agent import service
from app.agent.access_context import bind_request, reset_request
from app.agent.managed_store import ManagedAction, ManagedAttempt, ManagedReceipt
from app.agent.schemas import SpecIn
from app.agent.store import AgentError, records, transaction
from app.main import app
from tests.managed_fixtures import b64, json_bytes, make_delivery, sha
from tests.test_managed_bridge import (
    DESIGN,
    PRODUCT,
    SCOPE,
    approved,
    assigned,
    counts,
    deliver,
    outboxes,
    request,
    sync,
)
from tests.test_managed_bridge import paired as paired


def sign_in_again(fixture, actor="lead"):
    response = Response()
    with models.session() as db:
        design_auth._start_session(db, response, actor)
    parsed = SimpleCookie()
    parsed.load(response.headers["set-cookie"])
    fixture["cookies"][actor] = parsed[config.get_config().design_session_cookie].value


def test_sync_logout_while_source_request_inflight_rejects_without_partial_import(paired, monkeypatch):
    original = bridge.network

    async def logout_after_response(*args, **kwargs):
        result = await original(*args, **kwargs)
        design_auth.logout(request(paired, path="/api/design-auth/logout"), Response())
        return result

    monkeypatch.setattr(bridge, "network", logout_after_response)
    with pytest.raises(AgentError) as caught:
        sync(paired)
    assert caught.value.code == "LOGIN_REQUIRED" and caught.value.status == 401
    assert len(paired["product"].requests) == 1
    assert all(value == 0 for value in counts().values())


def test_delivery_logout_during_network_preserves_ack_and_original_action_for_new_login(paired, monkeypatch):
    item = sync(paired)
    event_id = outboxes(item["id"])["received"]["event_id"]
    original = bridge.network

    async def logout_after_response(*args, **kwargs):
        result = await original(*args, **kwargs)
        design_auth.logout(request(paired, path="/api/design-auth/logout"), Response())
        return result

    monkeypatch.setattr(bridge, "network", logout_after_response)
    with pytest.raises(AgentError) as caught:
        deliver(paired, item, event_id)
    assert caught.value.code == "LOGIN_REQUIRED" and caught.value.status == 401
    frozen = outboxes(item["id"])["received"]
    assert frozen["status"] == "received" and frozen["receipt"] == paired["product"].saved[event_id][1]
    with models.session() as db:
        attempt = db.scalar(select(ManagedAttempt))
        assert attempt.status == "received" and attempt.finished_at
        action = db.scalar(select(ManagedAction).where(ManagedAction.action_id == "synthetic-send-001"))
        expected = copy.deepcopy(action.response)
        assert action.status_code == 200 and expected["transport_outcome"]["status"] == "received"
    sign_in_again(paired)
    req = request(paired, path="/api/design-operations/synthetic-send-001", method="GET")
    result = workflow.operation(req, "synthetic-send-001")
    assert result["result"] == expected
    with pytest.raises(AgentError) as caught:
        deliver(paired, item, event_id)
    assert caught.value.code == "AUTH_CONTEXT_CHANGED"
    assert len(paired["product"].requests) == 2


def test_conflict_later_in_one_page_rolls_back_new_earlier_package_and_outbox(paired):
    old = sync(paired)
    before = counts()
    second = make_delivery(source_instance_id=PRODUCT, target_instance_id=DESIGN, scope_id=SCOPE, version="v2")
    changed = json.loads(paired["product"].deliveries[0])
    original = base64.b64decode(changed["content_base64"]) + b" \n"
    changed.update(content_base64=b64(original), content_digest=sha(original))
    paired["product"].deliveries = [second, json_bytes(changed)]
    with pytest.raises(AgentError) as caught:
        sync(paired, action_id="synthetic-mixed-conflict")
    assert caught.value.code == "PACKAGE_CONFLICT"
    assert counts() == before
    assert set(outboxes(old["id"])) == {"received"}


def test_local_execution_edit_keeps_complete_approved_prompt_and_actor_diff(paired):
    item = assigned(paired)
    with models.session() as db:
        spec = records(db, item["project_id"], "spec")[-1]
        old_id = spec.id
        old_payload = copy.deepcopy(spec.payload)
        source = db.get(ManagedReceipt, item["id"]).proof
    assert len(old_payload["product_source"]["prompt"]["positive_prompt"]) > 4000
    change = {**old_payload["spec"], "expected_spec_id": old_id, "intent": "合成执行调整，明确保留原产品批准正文"}
    req = request(paired, actor="alice", path=f"/api/project/{item['project_id']}/design-specs")
    token = bind_request(req)
    try:
        with transaction() as db:
            updated = service.save_spec(db, item["project_id"], SpecIn.model_validate(change))
            new_payload = copy.deepcopy(updated.payload)
    finally:
        reset_request(token)
    assert new_payload["product_source"] == old_payload["product_source"]
    assert new_payload["product_source"]["prompt"] == source["prompt"]
    assert new_payload["execution_changes"]["actor"] == "alice"
    assert new_payload["execution_changes"]["parent_spec_id"] == old_id
    assert new_payload["execution_changes"]["fields"]["intent"] == {
        "before": old_payload["spec"]["intent"], "after": change["intent"],
    }
    with models.session() as db:
        assert records(db, item["project_id"], "spec")[0].payload == old_payload


def test_main_service_routes_never_exchange_personnel_cookie_for_service_authority(paired):
    item, asset_id, _ = approved(paired)
    event_id = outboxes(item["id"])["design_approved"]["event_id"]
    client = TestClient(app)  # No startup/server: fixture owns this synthetic DB.
    try:
        client.cookies.set(config.get_config().design_session_cookie, paired["cookies"]["lead"])
        for suffix in ("review", f"assets/{asset_id}"):
            response = client.get(f"/api/design-integration/events/{event_id}/{suffix}")
            assert response.status_code == 401
            assert response.json()["error"]["code"] == "SERVICE_UNAUTHORIZED"
            assert response.headers["Cache-Control"] == "private, no-store"
    finally:
        client.close()
