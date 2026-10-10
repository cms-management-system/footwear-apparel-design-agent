"""Direct server-to-server handoff with the approved product workbench."""

from typing import Literal

import httpx
from pydantic import BaseModel, Field, ValidationError

from ..config import get_config
from .store import AgentError


class ApprovedPackage(BaseModel):
    package_id: str = Field(min_length=1)
    source_agent: Literal["product"]
    signal_ids: list[str]
    source: str
    dedup_key: str
    dedup_rule: str
    sample_count: dict
    attribution: dict
    constraints: list[str] | None = None
    requirement_desc: str = Field(min_length=1)
    version: str = Field(min_length=1)
    status: Literal["approved"]
    trial_import: bool = False


class ProductBridge:
    def __init__(self, *, base_url: str | None = None, api_key: str | None = None, transport=None):
        settings = get_config()
        self.base_url = (base_url if base_url is not None else settings.product_handoff_url).rstrip("/")
        self.api_key = api_key if api_key is not None else settings.product_handoff_key
        self.transport = transport

    def _request(self, method: str, path: str, payload: dict | None = None):
        if not self.base_url or len(self.api_key) < 32:
            raise AgentError("PRODUCT_LINK_NOT_CONFIGURED", "尚未配置知需交接地址与密钥", 503)
        try:
            with httpx.Client(base_url=self.base_url, transport=self.transport, timeout=12.0, trust_env=False) as client:
                response = client.request(method, path, json=payload, headers={"X-Handoff-Key": self.api_key})
        except httpx.RequestError as error:
            raise AgentError("PRODUCT_LINK_UNAVAILABLE", "暂时无法连接知需，请稍后重试", 502) from error
        if response.status_code in (401, 403):
            raise AgentError("PRODUCT_LINK_FORBIDDEN", "知需交接接口未授权", 502)
        if response.status_code >= 400:
            raise AgentError("PRODUCT_LINK_FAILED", "知需未接收交接数据，请核对需求版本", 502)
        try:
            return response.json()
        except ValueError as error:
            raise AgentError("PRODUCT_LINK_INVALID", "知需交接数据无法解析", 502) from error

    def list_approved(self) -> list[ApprovedPackage]:
        payload = self._request("GET", "/api/design-link/packages")
        try:
            return [ApprovedPackage.model_validate(item) for item in payload["items"]]
        except (KeyError, TypeError, ValidationError) as error:
            raise AgentError("PRODUCT_LINK_INVALID", "知需需求版本字段不完整", 502) from error

    def send_event(self, payload: dict) -> dict:
        return self._request("POST", "/api/design-link/events", payload)
