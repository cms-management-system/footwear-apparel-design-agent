"""Real bridge/workflow/SQLite tests against a clearly synthetic HTTP transport.

No external server, real account, provider, old database, or old asset is used.
The HTTP transport models the product contract; it is not product runtime proof.
"""

from __future__ import annotations

import asyncio
import base64
import copy
import io
import json
import time
from pathlib import Path

import httpx
import pytest
from fastapi import Request
from PIL import Image
from sqlalchemy import func, select

from app import config, models
from app.agent import assets
from app.agent import managed_bridge as bridge
from app.agent import managed_workflow as workflow
from app.agent.access_context import bind_request, reset_request
from app.agent.managed_protocol import validate_event
from app.agent.managed_store import (
    ManagedAction,
    ManagedAttempt,
    ManagedAudit,
    ManagedBase,
    ManagedOutbox,
    ManagedReceipt,
)
from app.agent.store import AgentBase, AgentError, create, head, records, transaction
from app.design_auth import session_context, token_hash
from tests.managed_fixtures import TIME, b64, json_bytes, make_delivery, sha

PRODUCT = "synthetic-product-bridge"
DESIGN = "synthetic-design-bridge"
SCOPE = "synthetic-bridge-scope"
TOKENS = {key: f"synthetic-{key}-purpose-token-" + "0" * 40
          for key in ("package_reader", "event_writer", "asset_reader")}


class SyntheticProduct:
    def __init__(self):
        self.deliveries = [make_delivery(source_instance_id=PRODUCT, target_instance_id=DESIGN, scope_id=SCOPE,
                                        long_prompt=True)]
        self.requests = []
        self.saved = {}
        self.timeout_after_store = False
        self.reply_mutation = None
        self.status_override = None

    def __call__(self, request):
        self.requests.append(request)
        path = request.url.path
        purpose = "package_reader" if path.endswith("packages") else "event_writer"
        if request.headers.get("Authorization") != "Bearer " + TOKENS[purpose]:
            return httpx.Response(401, json={"error": {"code": "UNAUTHORIZED"}})
        if self.status_override is not None:
            return httpx.Response(self.status_override, json={"error": {"code": "SYNTHETIC_FAILURE"}})
        if path == "/api/design-link/packages" and request.method == "GET":
            return httpx.Response(200, content=(
                b'{"delivery_schema":"pa-design-list/2","items":[' + b",".join(self.deliveries)
                + b'],"next_cursor":null}'))
        if path == "/api/design-link/events" and request.method == "POST":
            envelope = json.loads(request.content)
            assert set(envelope) == {
                "delivery_schema", "source_product", "target_product", "source_instance_id", "target_instance_id",
                "authorized_scope", "event_id", "event_revision", "content_digest", "content_base64",
            }
            raw = base64.b64decode(envelope["content_base64"], validate=True)
            event = validate_event(raw)
            assert sha(raw) == envelope["content_digest"]
            assert request.headers["Idempotency-Key"] == envelope["event_id"] == event["event_id"]
            if envelope["event_id"] in self.saved:
                original, receipt = self.saved[envelope["event_id"]]
                if original != raw:
                    return httpx.Response(409, json={"error": {"code": "EVENT_CONFLICT"}})
                status = 200
            else:
                receipt = {
                    "receipt_schema": "pa-design-event-receipt/2", "event_receive_id": "ER-" + event["event_id"],
                    **{key: envelope[key] for key in ("event_id", "event_revision", "source_instance_id",
                       "target_instance_id", "authorized_scope", "content_digest")},
                    "package_id": event["package_id"], "version": event["version"],
                    "status": "received", "received_at": TIME,
                }
                self.saved[event["event_id"]] = raw, receipt
                status = 201
            if self.timeout_after_store:
                self.timeout_after_store = False
                raise httpx.ReadTimeout("synthetic dropped response after persistence", request=request)
            result = copy.deepcopy(receipt)
            if self.reply_mutation:
                self.reply_mutation(result)
            return httpx.Response(status, content=json_bytes(result))
        if path == "/api/design-link/events" and request.method == "GET":
            saved = self.saved.get(request.url.params.get("event_id"))
            return httpx.Response(200, content=json_bytes(saved[1])) if saved else httpx.Response(404, json={})
        raise AssertionError("unexpected synthetic product route")


@pytest.fixture()
def paired(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "_load_env", lambda: None)
    for key, value in {
        "DATABASE_URL": f"sqlite:///{tmp_path / 'synthetic-bridge.sqlite3'}",
        "ASSETS_DIR": str(tmp_path / "synthetic-assets"), "DESIGN_INTEGRATION_MODE": "managed",
        "DESIGN_AUTH_REQUIRED": "true", "DESIGN_INSTANCE_ID": DESIGN, "DESIGN_SCOPE_ID": SCOPE,
        "DESIGN_PAID_PROVIDERS_ENABLED": "false", "AGENT_WORKER_ENABLED": "false",
    }.items():
        monkeypatch.setenv(key, value)
    pairing = {
        "product_instance_id": PRODUCT, "design_instance_id": DESIGN, "authorized_scope": SCOPE,
        "product_base_url": "http://127.0.0.1:39991",
        **{purpose: {
            "subject": f"synthetic-{purpose}", "token": token,
            "purpose": {"package_reader": "design_package_reader", "event_writer": "design_event_writer",
                        "asset_reader": "design_asset_reader"}[purpose],
            "source_instance_id": PRODUCT if purpose == "asset_reader" else DESIGN,
            "target_instance_id": DESIGN if purpose == "asset_reader" else PRODUCT,
            "authorized_scope": SCOPE,
        } for purpose, token in TOKENS.items()},
    }
    path = tmp_path / "synthetic-pairing.json"
    path.write_bytes(json_bytes(pairing))
    path.chmod(0o600)
    monkeypatch.setenv("DESIGN_PAIRING_FILE", str(path))
    config.get_config.cache_clear()
    models.reset_engine_for_tests()
    models.init_db()
    AgentBase.metadata.create_all(models.engine())
    ManagedBase.metadata.create_all(models.engine())
    cookies = {name: f"synthetic-bridge-person-session-{name}" for name in ("lead", "alice", "bob")}
    with models.session() as db, db.begin():
        for name, cookie in cookies.items():
            db.add(models.DesignerUser(username=name, display_name=f"合成{name}", active=True,
                                       password_hash="synthetic-unused-password-hash",
                                       role="manager" if name == "lead" else "designer"))
            db.add(models.DesignerSession(token_hash=token_hash(cookie), username=name,
                                          expires_at=int(time.time()) + 3600))
    product = SyntheticProduct()
    client = httpx.AsyncClient
    client_options = []

    def synthetic_client(**options):
        client_options.append(dict(options))
        return client(transport=httpx.MockTransport(product), **options)

    monkeypatch.setattr(bridge.httpx, "AsyncClient", synthetic_client)
    try:
        yield {"cookies": cookies, "product": product, "pairing": pairing, "pairing_path": path,
               "client_options": client_options, "tmp_path": tmp_path}
    finally:
        models.reset_engine_for_tests()
        config.get_config.cache_clear()


def request(fixture, *, actor="lead", action_id="synthetic-action-001", path="/api/design-handoffs/sync",
            method="POST", service_token=None):
    headers = []
    if actor:
        cookie = fixture["cookies"][actor]
        headers.extend([
            (b"cookie", f"{config.get_config().design_session_cookie}={cookie}".encode()),
            (b"x-design-context", session_context(token_hash(cookie)).encode()),
            (b"x-design-action-id", action_id.encode()),
        ])
    if service_token:
        headers.append((b"authorization", ("Bearer " + service_token).encode()))
    return Request({"type": "http", "method": method, "path": path, "headers": headers,
                    "scheme": "http", "server": ("testserver", 80), "query_string": b""})


def call(fixture, function, payload, *, actor="lead", action_id="synthetic-action-001", handoff_id=None,
         endpoint="sync"):
    path = f"/api/design-handoffs/{handoff_id}/{endpoint}" if handoff_id else "/api/design-handoffs/sync"
    req = request(fixture, actor=actor, action_id=action_id, path=path)
    token = bind_request(req)
    try:
        result = function(req, handoff_id, json_bytes(payload)) if handoff_id else function(req, json_bytes(payload))
        return asyncio.run(result) if asyncio.iscoroutine(result) else result
    finally:
        reset_request(token)


def sync(fixture, *, action_id="synthetic-sync-001", actor="lead", payload=None):
    result = call(fixture, bridge.sync_packages, payload or {}, actor=actor, action_id=action_id)
    return result["items"][0] if result["items"] else result


def counts():
    with models.session() as db:
        return {model.__name__: db.scalar(select(func.count()).select_from(model))
                for model in (ManagedReceipt, models.DesignHandoff, ManagedOutbox, ManagedAudit,
                              ManagedAttempt, ManagedAction)}


def event_of(row):
    return validate_event(base64.b64decode(json.loads(row.raw_body)["content_base64"]))


def outboxes(handoff_id):
    with models.session() as db:
        return {event_of(row)["kind"]: {"event_id": row.event_id, "status": row.status, "raw": row.raw_body,
                                      "event": event_of(row), "receipt": row.receipt}
                for row in db.scalars(select(ManagedOutbox).where(ManagedOutbox.handoff_id == handoff_id))}


def decision(fixture, item, action, *, action_id, **fields):
    return call(fixture, workflow.decide, {"action": action, "expected_revision": item["revision"], **fields},
                action_id=action_id, handoff_id=item["id"], endpoint="decision")


def assigned(fixture):
    item = sync(fixture)
    item = decision(fixture, item, "accept", action_id="synthetic-accept-001")
    return decision(fixture, item, "assign", assignee="alice", action_id="synthetic-assign-001")


def version_fixture(item):
    raw = io.BytesIO()
    Image.new("RGB", (128, 128), "navy").save(raw, "PNG")
    image = assets.save_image(raw.getvalue())
    with transaction() as db:
        spec = records(db, item["project_id"], "spec")[-1]
        version = create(db, item["project_id"], "version", {
            "spec_id": spec.id, "image": image, "generation_mode": "synthetic_fixture",
            "change_description": "合成技术图，仅测试冻结与权限", "review": {
                "summary": "明确合成技术结果，真实设计效果未验", "checks": [
                    {"constraint_id": "synthetic-format", "status": "pass", "evidence": "合成图片文件存在"},
                ],
            },
        }, "confirmed")
        h = head(db, item["project_id"])
        h.payload = {**h.payload, "confirmed_version_id": version.id}
        return version.id, assets.file_path(image)


def submitted(fixture):
    item = assigned(fixture)
    version_id, path = version_fixture(item)
    item = call(fixture, workflow.submit, {"expected_revision": item["revision"], "version_id": version_id},
                actor="alice", action_id="synthetic-submit-001", handoff_id=item["id"], endpoint="submit")
    return item, version_id, path


def approved(fixture):
    item, version_id, path = submitted(fixture)
    item = call(fixture, workflow.review, {
        "expected_revision": item["revision"], "action": "approve", "submitted_version_id": version_id,
        "note": "合成技术审查通过，不代表真实业务人审",
    }, action_id="synthetic-review-001", handoff_id=item["id"], endpoint="review")
    return item, version_id, path


def deliver(fixture, item, event_id, *, action="send", action_id="synthetic-send-001"):
    payload = {"event_id": event_id, "expected_revision": item["revision"], "action": action}
    return call(fixture, bridge.deliver, payload, action_id=action_id, handoff_id=item["id"], endpoint="delivery")


def error(code, status):
    class ExpectedError:
        def __enter__(self):
            self.capture = pytest.raises(AgentError)
            return self.capture.__enter__()

        def __exit__(self, *args):
            result = self.capture.__exit__(*args)
            assert self.capture.excinfo.value.code == code
            assert self.capture.excinfo.value.status == status
            return result
    return ExpectedError()


def test_sync_uses_fixed_authenticated_get_and_atomically_freezes_received(paired):
    item = sync(paired)
    request = paired["product"].requests[0]
    assert request.method == "GET" and str(request.url) == "http://127.0.0.1:39991/api/design-link/packages?limit=50"
    assert request.headers["Authorization"] == "Bearer " + TOKENS["package_reader"]
    assert all(options["trust_env"] is False and options["follow_redirects"] is False
               for options in paired["client_options"])
    assert item["status"] == "new" and item["assignee"] is None and item["project_id"] is None
    assert counts() == {"ManagedReceipt": 1, "DesignHandoff": 1, "ManagedOutbox": 1, "ManagedAudit": 0,
                        "ManagedAttempt": 0, "ManagedAction": 1}
    with models.session() as db:
        meta = db.get(ManagedReceipt, item["id"])
        assert meta.proof["verification_method"] == "authenticated_fixed_source_pull"
        assert len(meta.proof["prompt"]["positive_prompt"]) > 4000
        assert meta.envelope_bytes == paired["product"].deliveries[0]
        outbox = db.scalar(select(ManagedOutbox))
        event = event_of(outbox)
        assert event["kind"] == "received" and event["design_receipt"] == meta.receipt
        assert event["source_instance_id"] == DESIGN and event["design_receipt"]["source_instance_id"] == PRODUCT
        assert outbox.status == "prepared" and outbox.receipt is None


def test_sync_personnel_rechecks_before_network_and_old_action_does_not_repull(paired):
    with error("ROLE_FORBIDDEN", 403):
        sync(paired, actor="alice")
    assert paired["product"].requests == []
    first = sync(paired)
    assert sync(paired) == first
    assert len(paired["product"].requests) == 1
    with models.session() as db, db.begin():
        db.get(models.DesignerUser, "lead").active = False
    with error("LOGIN_REQUIRED", 401):
        sync(paired)
    assert len(paired["product"].requests) == 1


def test_sync_same_original_with_outer_whitespace_reuses_receipt_but_inner_bytes_conflict(paired):
    first = sync(paired)
    initial = outboxes(first["id"])["received"]["raw"]
    paired["product"].deliveries = [json_bytes(json.loads(paired["product"].deliveries[0]), pretty=True)]
    second = sync(paired, action_id="synthetic-sync-002")
    assert second["id"] == first["id"] and second["source_receipt"] == first["source_receipt"]
    assert outboxes(first["id"])["received"]["raw"] == initial
    delivery = json.loads(paired["product"].deliveries[0])
    content = base64.b64decode(delivery["content_base64"]) + b" \n"
    delivery.update(content_digest=sha(content), content_base64=b64(content))
    paired["product"].deliveries = [json_bytes(delivery)]
    with error("PACKAGE_CONFLICT", 409):
        sync(paired, action_id="synthetic-sync-003")
    assert counts()["ManagedReceipt"] == counts()["ManagedOutbox"] == 1
    assert outboxes(first["id"])["received"]["raw"] == initial


def test_failed_received_freeze_rolls_back_handoff_receipt_and_action(paired, monkeypatch):
    def fail_freeze(*args):
        raise AgentError("STATE_CONFLICT", "synthetic freeze failure", 409)
    monkeypatch.setattr(bridge, "freeze_received", fail_freeze)
    with error("STATE_CONFLICT", 409):
        sync(paired)
    assert all(value == 0 for value in counts().values())


def test_new_version_is_separate_and_exact_query_cannot_import_another_version(paired):
    first = sync(paired)
    paired["product"].deliveries = [make_delivery(source_instance_id=PRODUCT, target_instance_id=DESIGN,
                                                scope_id=SCOPE, version="v2")]
    with error("PACKAGE_CONFLICT", 409):
        sync(paired, action_id="synthetic-sync-002", payload={"package_id": first["package_id"], "version": "v1"})
    second = sync(paired, action_id="synthetic-sync-003")
    assert second["id"] != first["id"] and second["version"] == "v2"
    assert counts()["ManagedReceipt"] == counts()["ManagedOutbox"] == 2


def test_return_freezes_clarification_without_network_or_original_overwrite(paired):
    first = sync(paired)
    before = outboxes(first["id"])["received"]["raw"]
    item = decision(paired, first, "return", note="合成澄清：请核对展示要求", action_id="synthetic-return-001")
    assert item["status"] == "returned" and len(paired["product"].requests) == 1
    frozen = outboxes(item["id"])
    assert frozen["received"]["raw"] == before
    clarification = frozen["clarification"]["event"]["clarification"]
    with models.session() as db:
        audit = db.get(ManagedAudit, clarification["decision_id"])
        assert audit.actor == clarification["manager_subject"] == "lead"
        assert audit.payload["note"] == clarification["reason"]
        assert audit.created_at == clarification["decided_at"]
    replay = decision(paired, first, "return", note="合成澄清：请核对展示要求", action_id="synthetic-return-001")
    assert replay == item and len(outboxes(item["id"])) == 2


def test_real_submit_and_review_freeze_complete_result_without_transport(paired):
    item, version_id, path = approved(paired)
    assert item["status"] == "approved" and len(paired["product"].requests) == 1
    result = outboxes(item["id"])["design_approved"]["event"]["result"]
    assert result["design_version_id"] == version_id and result["generation_mode"] == "synthetic_fixture"
    projection_raw = base64.b64decode(result["design_version_base64"])
    assert sha(projection_raw) == result["design_version_digest"] == result["review"]["design_version_digest"]
    projection = json.loads(projection_raw)
    assert projection["assets"][0]["sha256"] == sha(path.read_bytes())
    assert projection["created_by"] == "alice" and result["review"]["manager_subject"] == "lead"
    with models.session() as db:
        submission = db.get(ManagedAudit, result["submission_id"])
        review = db.get(ManagedAudit, result["review"]["review_id"])
        assert submission.kind == "submitted" and review.kind == "approved"
        assert submission.payload["design_version_base64"] == result["design_version_base64"]
        assert review.payload["submission_id"] == submission.id
        assert projection["prompt_digest"] == db.get(ManagedReceipt, item["id"]).proof["package"]["prompt_digest"]


def test_received_confirmation_orders_later_delivery_without_blocking_internal_design(paired):
    item, _, _ = approved(paired)
    frozen = outboxes(item["id"])
    with error("RECEIPT_NOT_CONFIRMED", 409):
        deliver(paired, item, frozen["design_approved"]["event_id"])
    assert counts()["ManagedAttempt"] == 0 and len(paired["product"].requests) == 1
    received = deliver(paired, item, frozen["received"]["event_id"], action_id="synthetic-send-received")
    assert received["transport_outcome"]["status"] == "received"
    result = deliver(paired, item, frozen["design_approved"]["event_id"], action_id="synthetic-send-result")
    assert result["transport_outcome"]["status"] == "received"
    assert set(paired["product"].saved) == {entry["event_id"] for entry in frozen.values()}
    for entry in outboxes(item["id"]).values():
        assert entry["status"] == "received" and len(entry["receipt"]) == 12


def test_dropped_send_response_is_unconfirmed_and_only_reconcile_can_recover(paired):
    item = sync(paired)
    event_id = outboxes(item["id"])["received"]["event_id"]
    paired["product"].timeout_after_store = True
    first = deliver(paired, item, event_id)
    assert first["transport_outcome"]["status"] == "unconfirmed"
    assert first["transport_outcome"]["error_code"] == "PRODUCT_LINK_UNAVAILABLE"
    assert deliver(paired, item, event_id) == first
    assert len(paired["product"].requests) == 2
    with error("STATE_CONFLICT", 409):
        deliver(paired, item, event_id, action_id="synthetic-send-new-id")
    recovered = deliver(paired, item, event_id, action="reconcile", action_id="synthetic-reconcile-001")
    assert recovered["transport_outcome"]["status"] == "received"
    assert [r.method for r in paired["product"].requests] == ["GET", "POST", "GET"]
    assert counts()["ManagedAttempt"] == 2 and len(paired["product"].saved) == 1
    with models.session() as db:
        original = db.scalar(select(ManagedAction).where(ManagedAction.action_id == "synthetic-send-001"))
        assert original.response["transport_outcome"]["status"] == "unconfirmed"


@pytest.mark.parametrize("mutate", [
    lambda r: r.update(extra=True), lambda r: r.pop("event_receive_id"),
    lambda r: r.update(content_digest="1" * 64), lambda r: r.update(authorized_scope="other"),
    lambda r: r.update(source_instance_id=PRODUCT), lambda r: r.update(event_revision=True),
])
def test_invalid_event_receipts_never_mark_received(paired, mutate):
    item = sync(paired)
    event_id = outboxes(item["id"])["received"]["event_id"]
    paired["product"].reply_mutation = mutate
    result = deliver(paired, item, event_id)
    assert result["transport_outcome"]["status"] == "unconfirmed"
    assert result["transport_outcome"]["error_code"] == "INVALID_INPUT"
    assert outboxes(item["id"])["received"]["receipt"] is None


def test_reconcile_404_allows_explicit_original_retry(paired):
    item = sync(paired)
    event_id = outboxes(item["id"])["received"]["event_id"]
    paired["product"].status_override = 503
    first = deliver(paired, item, event_id)
    assert first["transport_outcome"]["status"] == "unconfirmed"
    original = outboxes(item["id"])["received"]["raw"]
    paired["product"].status_override = None
    missing = deliver(paired, item, event_id, action="reconcile", action_id="synthetic-reconcile-missing")
    assert missing["transport_outcome"]["status"] == "prepared"
    result = deliver(paired, item, event_id, action_id="synthetic-send-after-lookup")
    assert result["transport_outcome"]["status"] == "received"
    posts = [r for r in paired["product"].requests if r.method == "POST"]
    assert len(posts) == 2 and posts[0].content == posts[1].content == original


def test_recover_inflight_preserves_original_and_prevents_send_replay(paired):
    item = sync(paired)
    event_id = outboxes(item["id"])["received"]["event_id"]
    before = outboxes(item["id"])["received"]["raw"]
    with models.session() as db, db.begin():
        outbox = db.get(ManagedOutbox, event_id)
        outbox.status = "sending"
        db.add(ManagedAttempt(id="synthetic-inflight-attempt", event_id=event_id, action_id="synthetic-inflight-action",
                              actor="lead", status="sending"))
        db.add(ManagedAction(action_key="a" * 64, action_id="synthetic-inflight-action", username="lead",
                             auth_context_id="synthetic-inflight-context", method="POST", path="synthetic",
                             raw_body=b"{}", status_code=202, response={"operation_state": "in_progress"},
                             handoff_id=item["id"]))
    bridge.recover_inflight()
    bridge.recover_inflight()
    assert outboxes(item["id"])["received"]["raw"] == before
    assert outboxes(item["id"])["received"]["status"] == "unconfirmed"
    with models.session() as db:
        attempt = db.get(ManagedAttempt, "synthetic-inflight-attempt")
        assert attempt.status == "unconfirmed" and attempt.error_code == "PROCESS_RESTARTED" and attempt.finished_at
        action = db.get(ManagedAction, "a" * 64)
        assert action.response["operation_state"] == "unconfirmed"
    with error("STATE_CONFLICT", 409):
        deliver(paired, item, event_id)
    assert len(paired["product"].requests) == 1


def test_approved_asset_requires_purpose_credential_and_matches_sha(paired):
    item, version_id, path = approved(paired)
    frozen = outboxes(item["id"])
    event_id = frozen["design_approved"]["event_id"]
    route = f"/api/design-integration/events/{event_id}/assets/{version_id}"
    for token in (None, TOKENS["package_reader"], TOKENS["event_writer"]):
        with error("SERVICE_UNAUTHORIZED", 401):
            bridge.approved_asset(event_id, version_id, request(paired, actor=None, method="GET", path=route,
                                                               service_token=token))
    req = request(paired, actor=None, method="GET", path=route, service_token=TOKENS["asset_reader"])
    response = bridge.approved_asset(event_id, version_id, req)
    assert Path(response.path) == path
    assert response.headers["X-Content-SHA256"] == sha(path.read_bytes())
    assert response.headers["Cache-Control"] == "private, no-store"
    proof = bridge.review_evidence(event_id, req)
    assert sha(base64.b64decode(proof["content_base64"])) == proof["content_digest"]
    with error("NOT_FOUND", 404):
        bridge.approved_asset(event_id, "other-asset", req)
    with error("NOT_FOUND", 404):
        bridge.approved_asset(frozen["received"]["event_id"], version_id, req)
    path.write_bytes(path.read_bytes() + b"synthetic-tamper")
    with error("ASSET_DIGEST_MISMATCH", 409):
        bridge.approved_asset(event_id, version_id, req)


def test_asset_scope_and_review_binding_cannot_be_forged(paired):
    item, version_id, _ = approved(paired)
    frozen = outboxes(item["id"])["design_approved"]
    req = request(paired, actor=None, method="GET", service_token=TOKENS["asset_reader"])
    with models.session() as db, db.begin():
        row = db.get(ManagedOutbox, frozen["event_id"])
        row.scope_id = "foreign-scope"
    with error("NOT_FOUND", 404):
        bridge.approved_asset(frozen["event_id"], version_id, req)
    with models.session() as db, db.begin():
        db.get(ManagedOutbox, frozen["event_id"]).scope_id = SCOPE
        review = db.get(ManagedAudit, frozen["event"]["result"]["review"]["review_id"])
        review.payload = {**review.payload, "design_version_digest": "1" * 64}
    with error("NOT_FOUND", 404):
        bridge.approved_asset(frozen["event_id"], version_id, req)


@pytest.mark.parametrize("url", ["https://external.invalid", "http://127.0.0.1:39991/path",
                                 "http://user:secret@127.0.0.1:39991", "http://127.0.0.1:39991?next=evil"])
def test_pairing_rejects_nonfixed_or_external_destination_without_network(paired, url):
    paired["pairing"]["product_base_url"] = url
    paired["pairing_path"].write_bytes(json_bytes(paired["pairing"]))
    with error("NOT_CONFIGURED", 503):
        sync(paired)
    assert paired["product"].requests == [] and all(value == 0 for value in counts().values())


@pytest.mark.parametrize("purpose", ["package_reader", "event_writer", "asset_reader"])
@pytest.mark.parametrize("field,value", [
    ("purpose", "other_service_purpose"), ("source_instance_id", "foreign-source"),
    ("target_instance_id", "foreign-target"), ("authorized_scope", "foreign-scope"),
])
def test_pairing_credential_binds_exact_purpose_direction_and_scope(paired, purpose, field, value):
    paired["pairing"][purpose][field] = value
    paired["pairing_path"].write_bytes(json_bytes(paired["pairing"]))
    with error("NOT_CONFIGURED", 503):
        bridge.Settings().credential(purpose)
    assert paired["product"].requests == []
    assert all(value == 0 for value in counts().values())


@pytest.mark.parametrize("purpose", ["package_reader", "event_writer", "asset_reader"])
def test_pairing_rejects_missing_purpose_metadata_instead_of_legacy_unscoped_token(paired, purpose):
    paired["pairing"][purpose] = {"subject": "synthetic-legacy-service", "token": TOKENS[purpose]}
    paired["pairing_path"].write_bytes(json_bytes(paired["pairing"]))
    with error("NOT_CONFIGURED", 503):
        bridge.Settings().credential(purpose)
    assert paired["product"].requests == []


def test_recover_inflight_does_not_rewrite_successful_engine_202_response(paired):
    responses = [
        {"task_id": "synthetic-engine-task-1", "status": "queued", "version_id": None},
        {"operation_state": "succeeded", "task_id": "synthetic-engine-task-2", "result": {"accepted": True}},
        {"operation_state": "unconfirmed", "event_id": "synthetic-existing-event", "error_code": "ORIGINAL_ERROR"},
    ]
    with models.session() as db, db.begin():
        for index, response in enumerate(responses):
            db.add(ManagedAction(action_key=str(index) * 64, action_id=f"synthetic-engine-action-{index}",
                                 username="alice", auth_context_id="synthetic-engine-context", method="POST",
                                 path="/api/project/1/design-tasks", raw_body=b"{}", status_code=202,
                                 response=copy.deepcopy(response), handoff_id=None))
    bridge.recover_inflight()
    bridge.recover_inflight()
    with models.session() as db:
        for index, expected in enumerate(responses):
            action = db.get(ManagedAction, str(index) * 64)
            assert action.status_code == 202 and action.response == expected
            assert action.raw_body == b"{}"
    assert paired["product"].requests == []
