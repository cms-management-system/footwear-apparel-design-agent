import base64
import json

import pytest

from app.agent.managed_protocol import (
    CONTENT_LIMIT,
    DELIVERY_LIMIT,
    LIST_LIMIT,
    decode_delivery,
    parse_delivery_list,
    validate_event,
    validate_event_receipt,
)
from app.agent.store import AgentError
from tests.managed_fixtures import (
    SCOPE,
    SOURCE_INSTANCE,
    TARGET_INSTANCE,
    b64,
    json_bytes,
    make_delivery,
    make_event,
    make_event_receipt,
    sha,
)


def decode(raw):
    return decode_delivery(raw, source_instance_id=SOURCE_INSTANCE, target_instance_id=TARGET_INSTANCE, scope_id=SCOPE)


def rewrite_package(raw, mutate):
    delivery = json.loads(raw)
    package = json.loads(base64.b64decode(delivery["content_base64"]))
    mutate(package)
    content = json_bytes(package)
    delivery.update(content_digest=sha(content), content_base64=b64(content))
    return json_bytes(delivery)


def rewrite_nested(raw, key, mutate, *, bind=True):
    def change(package):
        nested = json.loads(base64.b64decode(package[f"{key}_base64"]))
        mutate(nested)
        content = json_bytes(nested)
        package.update({f"{key}_digest": sha(content), f"{key}_base64": b64(content)})
        if bind:
            package["approval"][f"{key}_digest"] = sha(content)
    return rewrite_package(raw, change)


def rejected(raw, *, status=422, code=None):
    with pytest.raises(AgentError) as caught:
        decode(raw)
    assert caught.value.status == status
    if code:
        assert caught.value.code == code


def test_complete_long_prompt_and_original_bytes_survive_without_truncation():
    raw = make_delivery(long_prompt=True, pretty=True)
    decoded = decode(raw)
    assert len(decoded["prompt"]["positive_prompt"]) > 4000
    assert decoded["prompt"]["positive_prompt"] == json.loads(decoded["prompt_bytes"])["positive_prompt"]
    assert sha(decoded["content_bytes"]) == decoded["delivery"]["content_digest"]
    assert sha(decoded["prompt_bytes"]) == decoded["package"]["prompt_digest"]
    assert sha(decoded["requirement_bytes"]) == decoded["package"]["requirement_digest"]
    assert decoded["prompt"]["design_spec"]["material"] is None
    assert decoded["prompt"]["design_spec"]["craft"] is None
    assert decoded["prompt"]["design_spec"]["knowledge_status"] == "pending_database"


def test_semantically_equal_original_whitespace_is_a_distinct_digest_and_new_version_valid():
    first = decode(make_delivery())
    whitespace = decode(make_delivery(content_suffix=b" \n"))
    new = decode(make_delivery(version="v2"))
    assert first["package"] == whitespace["package"]
    assert first["content_bytes"] != whitespace["content_bytes"]
    assert first["delivery"]["content_digest"] != whitespace["delivery"]["content_digest"]
    assert new["package"]["version"] == "v2"
    assert new["package"]["version_group_id"] != first["package"]["version_group_id"]
    # Persistence must use this distinction to produce PACKAGE_CONFLICT;
    # validating a legitimate alternative is intentionally not an idempotency store.


@pytest.mark.parametrize("field,value", [
    ("source_instance_id", "other-source"), ("target_instance_id", "other-target"),
    ("authorized_scope", "other-scope"), ("target_product", "cms"),
    ("source_agent", "outfit"), ("status", "draft"), ("environment", "production"),
    ("data_origin", "real_market"), ("requirement_version", True), ("prompt_version", 0),
    ("dedup_key", "not-a-hash"), ("version", "v0"), ("unexpected", "value"),
])
def test_invalid_package_fields_fail_even_with_valid_content_digest(field, value):
    rejected(make_delivery(package_overrides={field: value}))


@pytest.mark.parametrize("field", ["approval", "provenance", "prompt_base64", "source_refs"])
def test_missing_required_fields_rejected(field):
    rejected(rewrite_package(make_delivery(), lambda package: package.pop(field)))


@pytest.mark.parametrize("field", ["version_group_id", "requirement_digest", "prompt_digest"])
def test_approval_must_bind_exact_group_and_both_original_digests(field):
    rejected(rewrite_package(make_delivery(), lambda p: p["approval"].update({field: "other"})))


def test_requirement_description_and_design_category_cross_checked():
    rejected(make_delivery(package_overrides={"requirement_desc": "另一个需求"}))
    rejected(make_delivery(requirement_overrides={"design_object": "鞋履"}))
    rejected(rewrite_nested(make_delivery(), "requirement", lambda p: p.update(requirement_desc="不同正文")))


@pytest.mark.parametrize("kind", ["package", "prompt", "requirement"])
def test_digest_is_original_bytes_not_reserialized_object(kind):
    delivery = json.loads(make_delivery())
    if kind == "package":
        delivery["content_base64"] = b64(base64.b64decode(delivery["content_base64"]) + b" ")
        raw = json_bytes(delivery)
    else:
        raw = rewrite_package(make_delivery(), lambda p: p.update({
            f"{kind}_base64": b64(base64.b64decode(p[f"{kind}_base64"]) + b" "),
        }))
    rejected(raw)


@pytest.mark.parametrize("mutation", [
    lambda p: p["provenance"].update(provenance_status="incomplete"),
    lambda p: p["provenance"].update(source_supplement_digest=None),
    lambda p: p["provenance"].update(signal_id="other"),
    lambda p: p["source_refs"][0].update(source_payload_digest="1" * 64),
    lambda p: p["source_refs"][0].update(signal_id="other"),
    lambda p: p.update(source_refs=[]),
    lambda p: p["source_refs"][0].update(evidence_revision=False),
    lambda p: p["provenance"].update(verified_at="2026-10-10T08:00:00"),
    lambda p: p["approval"].update(approved_at="2026-02-30T08:00:00Z"),
    lambda p: p["evidence_counts"].update(expressions=True),
    lambda p: p["sample_count"].update(real_purchase=0),
])
def test_provenance_and_count_semantics(mutation):
    rejected(rewrite_package(make_delivery(), mutation))


def test_missing_and_fabricated_prompt_content_rejected():
    rejected(make_delivery(prompt_overrides={"positive_prompt": ""}))
    rejected(make_delivery(prompt_overrides={"positive_prompt": "x" * 10001}))
    rejected(rewrite_nested(make_delivery(), "prompt", lambda p: p["design_spec"].update(material="防水面料")))
    rejected(rewrite_nested(make_delivery(), "prompt", lambda p: p["design_spec"].update(craft="无缝工艺")))
    rejected(rewrite_nested(make_delivery(), "prompt", lambda p: p["unknowns"][0].update(blocking=True)))
    rejected(rewrite_nested(make_delivery(), "prompt", lambda p: p["generation"].update(template_version=None)))
    rejected(rewrite_nested(make_delivery(), "prompt", lambda p: p["generation"].update(mode="text_model")),
             status=503, code="CAPABILITY_UNAVAILABLE")


@pytest.mark.parametrize("text", [
    "原图 https://example.test/photo.png", "本人身高170cm", "联系 demo@example.test",
    "手机号13800138000", "session_ref=raw-user-session", "api_key=secret-value",
])
def test_private_content_is_not_accepted_inside_allowed_text_field(text):
    rejected(make_delivery(prompt_overrides={"positive_prompt": text}))


def test_forbidden_private_field_and_extra_nested_field_fail():
    rejected(rewrite_nested(make_delivery(), "prompt", lambda p: p.update(photo_url="hidden")))
    rejected(rewrite_package(make_delivery(), lambda p: p["provenance"].update(session_ref="raw")))
    rejected(rewrite_nested(make_delivery(), "prompt", lambda p: p["design_spec"].update(secret="value")))


@pytest.mark.parametrize("raw", [
    b'{"delivery_schema":"pa-design-delivery/2","delivery_schema":"pa-design-delivery/2"}',
    b'{"x":NaN}', b'{"x":Infinity}', b'{"x":-Infinity}', b'{"x":1e999}', b'\xff',
    b'{"x":"\\ud800"}', b'[]', b'{} garbage',
])
def test_bad_json_and_unicode_fail_closed(raw):
    rejected(raw)


def test_duplicate_keys_invalid_json_and_unknown_schema_inside_base64_fail():
    for content in (b'{"package_schema":"pa-design-package/2","package_schema":"pa-design-package/2"}', b'\xff'):
        delivery = json.loads(make_delivery())
        delivery.update(content_base64=b64(content), content_digest=sha(content))
        rejected(json_bytes(delivery))
    rejected(make_delivery(package_overrides={"package_schema": "pa-design-package/1"}), code="UNSUPPORTED_SCHEMA")


@pytest.mark.parametrize("modify", [lambda value: value + "\n", lambda value: " " + value,
                                     lambda value: value + "=", lambda value: value[:-1] + "!"])
def test_base64_must_be_canonical_and_strict(modify):
    delivery = json.loads(make_delivery())
    delivery["content_base64"] = modify(delivery["content_base64"])
    rejected(json_bytes(delivery))


def test_base64_nonzero_padding_bits_are_rejected():
    delivery = json.loads(make_delivery())
    raw = b"{}"
    delivery.update(content_digest=sha(raw), content_base64="e31=")  # decodes to {}, unlike canonical e30=
    rejected(json_bytes(delivery))


def test_size_limits_enforced_before_processing():
    rejected(b" " * (DELIVERY_LIMIT + 1), status=413)
    delivery = json.loads(make_delivery())
    content = b" " * (CONTENT_LIMIT + 1)
    delivery.update(content_digest=sha(content), content_base64=b64(content))
    rejected(json_bytes(delivery), status=413)
    with pytest.raises(AgentError) as caught:
        parse_delivery_list(b" " * (LIST_LIMIT + 1))
    assert caught.value.status == 413


def test_list_preserves_exact_each_item_json_including_unicode_and_spaces():
    first, second = make_delivery(pretty=True), make_delivery(version="v2", long_prompt=True)
    raw = (b'{ "next_cursor":"next-2", "items" : [ \n' + first + b",\t" + second
           + b' ],"delivery_schema":"pa-design-list/2"}')
    items, cursor = parse_delivery_list(raw)
    assert items == [first, second]
    assert cursor == "next-2"
    assert decode(items[1])["package"]["version"] == "v2"
    assert parse_delivery_list(b'{"items":[],"next_cursor":null,"delivery_schema":"pa-design-list/2"}') == ([], None)


@pytest.mark.parametrize("payload", [
    {"delivery_schema": "pa-design-list/2", "items": [{}] * 51, "next_cursor": None},
    {"delivery_schema": "pa-design-list/2", "items": ["{}"], "next_cursor": None},
    {"delivery_schema": "pa-design-list/2", "items": [], "next_cursor": 1},
    {"delivery_schema": "pa-design-list/1", "items": [], "next_cursor": None},
    {"delivery_schema": "pa-design-list/2", "items": [], "next_cursor": None, "extra": True},
])
def test_invalid_lists_fail(payload):
    with pytest.raises(AgentError):
        parse_delivery_list(json_bytes(payload))


@pytest.mark.parametrize("kind", ["received", "clarification", "design_approved"])
def test_all_three_event_branches_are_supported_and_frozen(kind):
    event = validate_event(make_event(kind=kind))
    assert event["kind"] == kind
    assert event["event_revision"] == 1


@pytest.mark.parametrize("kind,mutate", [
    ("received", lambda e: e.update(event_revision=True)),
    ("received", lambda e: e.update(event_revision=2)),
    ("received", lambda e: e.update(clarification={})),
    ("received", lambda e: e["design_receipt"].update(target_instance_id="other")),
    ("received", lambda e: e["design_receipt"].update(content_digest="1" * 64)),
    ("clarification", lambda e: e["clarification"].update(reason="")),
    ("design_approved", lambda e: e["result"]["review"].update(design_version_id="other")),
    ("design_approved", lambda e: e["result"]["review"].update(submission_id="other")),
    ("design_approved", lambda e: e["result"].update(assets=[])),
    ("design_approved", lambda e: e["result"]["assets"][0].update(width=True)),
    ("design_approved", lambda e: e["result"]["assets"][0].update(mime_type="image/svg+xml")),
    ("design_approved", lambda e: e["result"]["assets"][0].update(image_url="https://example.test/image")),
    ("design_approved", lambda e: e["result"].update(generation_mode="text_model")),
])
def test_invalid_event_branches_and_result_bindings_rejected(kind, mutate):
    event = json.loads(make_event(kind=kind))
    mutate(event)
    with pytest.raises(AgentError):
        validate_event(json_bytes(event))


def test_event_receipt_exact_contract_and_expected_identity():
    raw, expected = make_event_receipt()
    receipt = validate_event_receipt(raw, expected)
    assert len(receipt) == 12
    for key in expected:
        wrong = {**expected, key: "wrong"}
        with pytest.raises(AgentError):
            validate_event_receipt(raw, wrong)
    for mutate in (lambda r: r.update(extra=True), lambda r: r.pop("event_receive_id"),
                   lambda r: r.update(event_revision=True), lambda r: r.update(status="approved")):
        changed = json.loads(raw)
        mutate(changed)
        with pytest.raises(AgentError):
            validate_event_receipt(json_bytes(changed), expected)
    with pytest.raises(AgentError):
        validate_event_receipt(raw, {})


def test_outer_delivery_whitespace_does_not_change_original_identity():
    raw = make_delivery()
    pretty_outer = json_bytes(json.loads(raw), pretty=True)
    first, second = decode(raw), decode(pretty_outer)
    assert raw != pretty_outer
    assert first["delivery"] == second["delivery"]
    assert first["content_bytes"] == second["content_bytes"]


def test_v101_requirement_constraints_and_unattributed_source_are_bound():
    rejected(rewrite_package(make_delivery(), lambda p: p.update(constraints=["不同约束"])))
    rejected(rewrite_package(make_delivery(), lambda p: p["attribution"].update(style_demand=True)))
    raw = rewrite_package(make_delivery(requirement_overrides={"priority_proposal": "P3"}),
                          lambda p: p["approval"].update(final_priority="P3"))
    assert decode(raw)["requirement"]["priority_proposal"] == "P3"


@pytest.mark.parametrize("mutate", [
    lambda p: p["design_spec"].update(display_requirements=["旧版字符串展示要求"]),
    lambda p: p["design_spec"]["display_requirements"][0].update(template_version=None),
    lambda p: p["design_spec"]["display_requirements"][0].update(origin="consumer"),
    lambda p: p["design_spec"]["display_requirements"][0].update(origin="human_authored"),
    lambda p: p["generation"].pop("derived_from"),
    lambda p: p["generation"].update(derived_from={"kind": "latest", "id": "x", "prompt_digest": "1" * 64}),
    lambda p: p["generation"].update(derived_from={"kind": "candidate", "id": "x", "prompt_digest": "not-a-hash"}),
])
def test_v101_display_and_generation_exact_shapes(mutate):
    rejected(rewrite_nested(make_delivery(), "prompt", mutate))


def test_v101_human_rewrite_retains_derivation_and_display_authorship():
    def change(prompt):
        prompt["generation"].update(mode="human_authored", derived_from={
            "kind": "candidate", "id": "CANDIDATE-SYNTHETIC-1", "prompt_digest": "1" * 64,
        })
        prompt["design_spec"]["display_requirements"][0].update(origin="human_authored", template_version=None)
    decoded = decode(rewrite_nested(make_delivery(), "prompt", change))
    assert decoded["prompt"]["generation"]["template_version"] == "synthetic-template/1"
    assert decoded["prompt"]["generation"]["derived_from"]["kind"] == "candidate"


@pytest.mark.parametrize("blocking,resolution,resolved_by,valid", [
    (False, None, None, True), (True, None, None, False),
    (True, "保留来源并采用已确认的方向", "synthetic-product-staff", True),
    (False, "已有处理", None, False), (False, None, "staff", False), ("false", None, None, False),
])
def test_v101_conflicts_use_explicit_blocking_flag(blocking, resolution, resolved_by, valid):
    def change(prompt):
        prompt["conflicts"] = [{
            "field_path": "design_spec.color", "source_ref_ids": ["REF-SYNTHETIC-1"],
            "description": "合成非关键颜色待确认", "blocking": blocking,
            "resolution": resolution, "resolved_by": resolved_by,
        }]
    raw = rewrite_nested(make_delivery(), "prompt", change)
    if valid:
        assert decode(raw)["prompt"]["conflicts"][0]["blocking"] == blocking
    else:
        rejected(raw)


def rewrite_result_projection(mutate, *, suffix=b""):
    event = json.loads(make_event(kind="design_approved"))
    result = event["result"]
    projection = json.loads(base64.b64decode(result["design_version_base64"]))
    mutate(projection)
    raw = json_bytes(projection) + suffix
    result.update(design_version_base64=b64(raw), design_version_digest=sha(raw))
    result["review"]["design_version_digest"] = sha(raw)
    return json_bytes(event)


@pytest.mark.parametrize("field,value", [
    ("schema_version", "pa-design-result-version/1"), ("design_version_id", "other-version"),
    ("task_id", "other-task"), ("assignment_id", "other-assignment"), ("design_receive_id", "other-receive"),
    ("package_id", "other-package"), ("package_version", "v2"), ("package_content_digest", "1" * 64),
    ("version_group_id", "other-group"), ("requirement_digest", "1" * 64), ("prompt_digest", "1" * 64),
    ("assets", []), ("summary", "different summary"), ("changes", "different changes"),
    ("checks", []), ("unknowns", []), ("generation_mode", "model_generated"),
    ("created_by", ""), ("created_at", "2026-10-10"), ("unexpected", True),
])
def test_frozen_result_projection_must_bind_even_when_both_hashes_are_self_consistent(field, value):
    raw = rewrite_result_projection(lambda p: p.update({field: value}))
    with pytest.raises(AgentError):
        validate_event(raw)


def test_design_version_projection_original_bytes_are_not_canonicalized():
    first = validate_event(make_event(kind="design_approved"))
    changed = validate_event(rewrite_result_projection(lambda p: None, suffix=b" \n"))
    assert first["result"]["design_version_digest"] != changed["result"]["design_version_digest"]
    assert json.loads(base64.b64decode(first["result"]["design_version_base64"])) == json.loads(
        base64.b64decode(changed["result"]["design_version_base64"]))
    event = json.loads(make_event(kind="design_approved"))
    event["result"]["design_version_base64"] = b64(
        base64.b64decode(event["result"]["design_version_base64"]) + b" ")
    with pytest.raises(AgentError):
        validate_event(json_bytes(event))


def test_duplicate_key_in_prompt_original_rejected_even_with_valid_all_hashes():
    def change(package):
        raw = base64.b64decode(package["prompt_base64"])
        raw = raw[:-1] + b',"positive_prompt":"duplicate"}'
        package.update(prompt_base64=b64(raw), prompt_digest=sha(raw))
        package["approval"]["prompt_digest"] = sha(raw)
    rejected(rewrite_package(make_delivery(), change))


@pytest.mark.parametrize("revision", [True, 1.0])
def test_deep_source_equality_does_not_treat_bool_or_float_as_integer(revision):
    raw = rewrite_nested(make_delivery(), "prompt", lambda p: p["source_refs"][0].update(evidence_revision=revision))
    rejected(raw)


def test_result_projection_deep_equality_preserves_integer_types():
    event = json.loads(make_event(kind="design_approved"))
    event["result"]["assets"][0]["width"] = 1
    projection = json.loads(base64.b64decode(event["result"]["design_version_base64"]))
    projection["assets"][0]["width"] = True
    raw = json_bytes(projection)
    event["result"].update(design_version_digest=sha(raw), design_version_base64=b64(raw))
    event["result"]["review"]["design_version_digest"] = sha(raw)
    with pytest.raises(AgentError):
        validate_event(json_bytes(event))
