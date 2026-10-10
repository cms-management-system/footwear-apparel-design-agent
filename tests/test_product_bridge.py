import httpx
import pytest

from app.agent.product_bridge import ProductBridge
from app.agent.store import AgentError

PACKAGE = {
    "package_id": "PKG-WB-SIG-1", "source_agent": "product", "signal_ids": ["SIG-1"],
    "source": "知需", "dedup_key": "通勤-裤装", "dedup_rule": "人工核验",
    "sample_count": {"intent": 1, "simulated_cart": 0, "real_purchase": 0},
    "attribution": {"supply_gap": False, "image_gap": False, "style_demand": True},
    "constraints": ["通勤场景"], "requirement_desc": "改善裤长适配", "version": "v1", "status": "approved",
}


def test_direct_workbench_handoff_uses_server_key():
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"items": [PACKAGE]} if request.method == "GET" else {"event": {}})

    bridge = ProductBridge(base_url="https://product.example", api_key="x" * 40, transport=httpx.MockTransport(respond))
    assert bridge.list_approved()[0].package_id == "PKG-WB-SIG-1"
    bridge.send_event({"event_id": "event-1"})
    assert [request.url.path for request in requests] == ["/api/design-link/packages", "/api/design-link/events"]
    assert all(request.headers["X-Handoff-Key"] == "x" * 40 for request in requests)


def test_unapproved_or_unauthorized_workbench_data_fail_closed():
    pending = ProductBridge(base_url="https://product.example", api_key="x" * 40, transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"items": [{**PACKAGE, "status": "pending"}]})))
    with pytest.raises(AgentError) as invalid:
        pending.list_approved()
    assert invalid.value.code == "PRODUCT_LINK_INVALID"
    with pytest.raises(AgentError) as missing:
        ProductBridge(base_url="https://product.example", api_key="").list_approved()
    assert missing.value.code == "PRODUCT_LINK_NOT_CONFIGURED"
    denied = ProductBridge(base_url="https://product.example", api_key="x" * 40, transport=httpx.MockTransport(lambda _: httpx.Response(403)))
    with pytest.raises(AgentError) as forbidden:
        denied.list_approved()
    assert forbidden.value.code == "PRODUCT_LINK_FORBIDDEN"
