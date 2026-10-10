"""Full approved context and local-spec lineage, using synthetic source bytes only."""

import copy
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app import models
from app.agent import runner
from app.agent.managed_protocol import decode_delivery
from app.agent.managed_store import ManagedReceipt
from app.agent.store import Record, head, transaction
from tests.test_managed_workflow import assigned, counts, error
from tests.test_managed_workflow import workflow as workflow


def stored_spec(pid):
    with transaction() as db:
        spec = db.get(Record, head(db, pid).payload["spec_id"])
        return spec.id, copy.deepcopy(spec.payload)


def edit(workflow, pid, data, action_id="synthetic-source-edit", revision=3):
    return workflow["clients"]["alice"].post(
        f"/api/project/{pid}/design-specs",
        json=data,
        headers={"X-Design-Action-Id": action_id, "X-Design-Revision": str(revision)},
    )


def test_valid_full_long_prompt_and_all_source_fields_reach_runner_sources(workflow):
    w = workflow
    # Local protocol decoding verifies the fixture's shape, not a real upstream approval.
    decoded = decode_delivery(
        w["envelope"],
        source_instance_id="synthetic-product-workflow",
        target_instance_id="synthetic-design-workflow",
        scope_id="synthetic-workflow-scope",
    )
    prompt, requirement = decoded["prompt"], decoded["requirement"]
    assert 4000 < len(prompt["positive_prompt"]) <= 10000
    task = assigned(w)
    spec_id, saved = stored_spec(task["project_id"])
    with transaction() as db:
        context, images = runner.sources(
            db, SimpleNamespace(project_id=task["project_id"], payload={"spec_id": spec_id})
        )
    assert images == []
    assert context["intent"] == prompt["positive_prompt"]
    assert context["product_source"]["prompt"] == prompt
    assert context["product_source"]["requirement"] == requirement
    assert context["product_source"]["prompt_base64"] == w["source"]["prompt_base64"]
    assert context["product_source"]["requirement_base64"] == w["source"]["requirement_base64"]
    assert context["product_source"]["approval"] == w["source"]["approval"]
    for key in ("avoid_items", "design_spec", "source_refs", "human_additions", "unknowns", "conflicts", "generation"):
        assert context["product_source"]["prompt"][key] == prompt[key]
    assert context["execution_changes"]["actor"] == "lead"
    assert context["execution_changes"]["parent_spec_id"] is None
    assert saved["parent_spec_id"] is None and w["network"] == []


def test_local_edits_record_parent_actor_and_diff_without_changing_approved_source(workflow):
    w = workflow
    task = assigned(w)
    old_id, old_payload = stored_spec(task["project_id"])
    body = copy.deepcopy(old_payload["spec"])
    body["intent"] += " 本地补充：合成画面采用浅灰背景。"
    body["constraints"].append(
        {
            "id": "c_local_background",
            "kind": "preference",
            "text": "合成画面采用浅灰背景",
            "region": "整体",
            "verification": "visual",
        }
    )
    body["expected_spec_id"] = old_id
    response = edit(w, task["project_id"], body)
    assert response.status_code == 201, response.text[:1000]
    changed = response.json()
    assert changed["id"] != old_id
    assert changed["parent_spec_id"] == old_id
    assert changed["execution_changes"]["parent_spec_id"] == old_id
    assert changed["execution_changes"]["actor"] == "alice"
    changes = changed["execution_changes"]["fields"]
    assert changes["intent"] == {"before": old_payload["spec"]["intent"], "after": body["intent"]}
    assert changes["constraints"] == {"before": old_payload["spec"]["constraints"], "after": body["constraints"]}
    assert changed["product_source"] == old_payload["product_source"]
    assert changed["fingerprint"] != old_payload["fingerprint"]
    with transaction() as db:
        assert db.get(Record, old_id).payload == old_payload
        receipt = db.get(ManagedReceipt, w["id"])
        assert receipt.envelope_bytes == w["envelope"]
        assert receipt.proof["package"] == w["source"]
        assert db.get(models.DesignHandoff, w["id"]).snapshot == w["source"]
        context, _ = runner.sources(
            db, SimpleNamespace(project_id=task["project_id"], payload={"spec_id": changed["id"]})
        )
    assert context["intent"] == body["intent"]
    assert context["product_source"]["prompt"]["positive_prompt"] == old_payload["spec"]["intent"]
    assert context["execution_changes"] == changed["execution_changes"]


@pytest.mark.parametrize("constraint_id", ["c_product_0", "c_avoid_0"])
@pytest.mark.parametrize(
    "operation", ["delete", "change_text", "downgrade_kind", "narrow_region", "change_verification"]
)
def test_source_mandatory_constraints_and_avoid_items_cannot_be_removed_or_changed(workflow, constraint_id, operation):
    w = workflow
    task = assigned(w)
    spec_id, original = stored_spec(task["project_id"])
    body = copy.deepcopy(original["spec"])
    target = next(item for item in body["constraints"] if item["id"] == constraint_id)
    assert target["kind"] == ("must_keep" if constraint_id == "c_product_0" else "forbidden")
    if operation == "delete":
        body["constraints"] = [item for item in body["constraints"] if item["id"] != constraint_id]
    elif operation == "change_text":
        target["text"] += " 已被本地修改"
    elif operation == "downgrade_kind":
        target["kind"] = "preference"
    elif operation == "narrow_region":
        target["region"] = "仅局部袖口"
    else:
        target["verification"] = "physical"
    body["expected_spec_id"] = spec_id
    before = counts()
    error(edit(w, task["project_id"], body), 409, "SOURCE_CONSTRAINT_CHANGED")
    assert counts() == before
    with transaction() as db:
        assert (
            list(db.scalars(select(Record).where(Record.project_id == task["project_id"], Record.kind == "spec")))[0].id
            == spec_id
        )
        assert (
            len(list(db.scalars(select(Record).where(Record.project_id == task["project_id"], Record.kind == "spec"))))
            == 1
        )
        assert db.get(Record, spec_id).payload == original
        assert head(db, task["project_id"]).payload["spec_id"] == spec_id
        assert db.get(ManagedReceipt, w["id"]).revision == 3
        assert db.get(ManagedReceipt, w["id"]).envelope_bytes == w["envelope"]
