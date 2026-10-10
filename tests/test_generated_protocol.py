"""Synthetic metadata tests for 1.0.2; these do not prove actual paid image generation."""
import base64
import json

import pytest

from app.agent.managed_protocol import IMAGE_PROVENANCE_FIELDS, validate_event
from app.agent.store import AgentError
from tests.managed_fixtures import TIME, b64, json_bytes, make_event, sha


def generated_event(mutate=None):
    event = json.loads(make_event(kind="design_approved"))
    result = event["result"]
    prompt = "完整执行提示词；合成校验，不是实际生成".encode()
    result["generation_mode"] = "model_generated"
    result["assets"][0]["origin"] = "model_generated"
    result["generation_provenance"] = {
        "schema_version": "pa-image-generation/1", "provider": "synthetic.example.test", "model": "gateway-alias",
        "response_model": None, "generation_task_id": "synthetic-generation-task", "generation_attempt_id": "attempt-1",
        "provider_request_id": None, "execution_prompt_id": "prompt-1", "execution_prompt_digest": sha(prompt),
        "execution_prompt_base64": b64(prompt), "source_product_prompt_digest": event["prompt_digest"],
        "authorization_ref": "synthetic-authorization", "started_at": TIME, "completed_at": TIME,
        "actual_cost_fen": None,
    }
    projection = json.loads(base64.b64decode(result["design_version_base64"]))
    projection.update(generation_mode="model_generated", assets=result["assets"],
                      generation_provenance=json.loads(json.dumps(result["generation_provenance"])))
    if mutate:
        mutate(event, projection)
    raw = json_bytes(projection)
    result.update(design_version_base64=b64(raw), design_version_digest=sha(raw))
    result["review"]["design_version_digest"] = sha(raw)
    return json_bytes(event)


def test_generated_branch_and_old_fixture_remain_distinct():
    real_metadata = validate_event(generated_event())
    assert set(real_metadata["result"]["generation_provenance"]) == IMAGE_PROVENANCE_FIELDS
    old = make_event(kind="design_approved")
    assert validate_event(old)["result"]["generation_mode"] == "synthetic_fixture"
    assert "generation_provenance" not in json.loads(old)["result"]


@pytest.mark.parametrize("field", sorted(IMAGE_PROVENANCE_FIELDS))
def test_missing_any_of_fifteen_fields_rejected(field):
    with pytest.raises(AgentError):
        validate_event(generated_event(lambda e, p: e["result"]["generation_provenance"].pop(field)))


@pytest.mark.parametrize("field,value", [
    ("actual_cost_fen", True), ("actual_cost_fen", -1), ("provider", ""), ("model", ""),
    ("execution_prompt_digest", "a" * 64), ("source_product_prompt_digest", "b" * 64),
    ("execution_prompt_base64", "invalid-base64"), ("response_model", 1), ("provider_request_id", ""),
    ("completed_at", "2026-10-10T07:00:00Z"), ("started_at", "2026-10-10T09:00:00Z"),
    ("extra", "not-defined"),
])
def test_invalid_generated_metadata_rejected_even_when_version_matches(field, value):
    def mutate(event, projection):
        event["result"]["generation_provenance"][field] = value
        projection["generation_provenance"][field] = value
    with pytest.raises(AgentError):
        validate_event(generated_event(mutate))


def test_provenance_result_and_version_must_be_equal():
    with pytest.raises(AgentError):
        validate_event(generated_event(lambda e, p: p["generation_provenance"].update(model="different-alias")))


@pytest.mark.parametrize("change", ["origin", "two_images", "fixture_extra"])
def test_mode_origin_and_count_fail_closed(change):
    def mutate(event, projection):
        if change == "origin":
            event["result"]["assets"][0]["origin"] = "synthetic_fixture"
        elif change == "two_images":
            event["result"]["assets"].append({**event["result"]["assets"][0], "asset_id": "second"})
        else:
            event["result"]["generation_mode"] = "synthetic_fixture"
            projection["generation_mode"] = "synthetic_fixture"
    with pytest.raises(AgentError):
        validate_event(generated_event(mutate))
