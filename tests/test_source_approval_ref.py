"""Real product producer's exact source approval object contract; no model/network calls."""
import base64
import json

import pytest

from app.agent.managed_protocol import decode_delivery
from app.agent.store import AgentError
from tests.managed_fixtures import SCOPE, SOURCE_INSTANCE, TARGET_INSTANCE, make_delivery


def raw_with_ref(ref):
    initial = json.loads(make_delivery())
    package = json.loads(base64.b64decode(initial["content_base64"]))
    return make_delivery(package_overrides={
        "provenance": {**package["provenance"], "source_approval_ref": ref},
    })


def decode(raw):
    return decode_delivery(raw, source_instance_id=SOURCE_INSTANCE, target_instance_id=TARGET_INSTANCE, scope_id=SCOPE)


def test_exact_source_approval_ref_from_actual_product_producer_is_consumed_unchanged():
    original = {"approval_record_id": "STAPR-bd338e02661c44328608d203b9665a2d"}
    decoded = decode(raw_with_ref(original))
    assert decoded["package"]["provenance"]["source_approval_ref"] == original


@pytest.mark.parametrize("invalid", [None, "STAPR-bd338e02661c44328608d203b9665a2d", {},
                                    {"approval_record_id": ""}, {"approval_record_id": None},
                                    {"approval_record_id": 1}, {"approval_record_id": "value", "extra": "value"}])
def test_source_approval_ref_does_not_accept_missing_or_forged_alternative_shapes(invalid):
    with pytest.raises(AgentError):
        decode(raw_with_ref(invalid))
