"""CMS evidence packages and design responses. The API key stays on the server."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Literal

import httpx
from pydantic import BaseModel, Field

from ..config import get_config
from ..models import Project
from .store import AgentError, require, transaction

_PACKAGE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
SOURCE_AGENT = "design"
NOT_APPROVED = "这个证据包尚未批准，产品负责人确认后才能使用。"
PACKAGE_MISSING = "没有找到这个证据包，请核对编号。"
RESPONSE_MISSING = "CMS 中没有找到要回传的证据包，请确认编号仍然有效。"
TIMEOUT_MESSAGE = "连接 CMS 超时，请稍后重试。同一设计编号重试不会重复创建记录。"
UNAVAILABLE = "无法连接 CMS，请确认服务已启动后重试。"
NOT_CONFIGURED = "CMS 访问密钥未配置，请在服务端设置 CMS_API_KEY。"


class CmsResponseIn(BaseModel):
    version_id: str = Field(min_length=1, max_length=80)
    response_status: Literal["adopted", "not_adopted", "need_evidence"]
    design_note: str = Field(default="", max_length=4000)


def _text(value) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _signal_ids(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list):
        return [str(item) for item in value if item is not None and str(item).strip()]
    return [str(value)]


def constraints_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        lines = []
        for item in value:
            if isinstance(item, str):
                lines.append(item)
            elif isinstance(item, dict):
                picked = item.get("text") or item.get("desc") or item.get("name")
                lines.append(str(picked) if picked else json.dumps(item, ensure_ascii=False))
            else:
                lines.append(str(item))
        return "\n".join(line for line in lines if line)
    return str(value)


def normalize_package(body: dict, package_id: str = "") -> dict:
    signals = _signal_ids(body.get("signal_ids"))
    resolved_id = _text(body.get("package_id")) or package_id
    return {
        "package_id": resolved_id,
        "signal_ids": signals,
        "signal_count": len(signals),
        "requirement_desc": _text(body.get("requirement_desc")),
        "constraints": body.get("constraints") if body.get("constraints") is not None else [],
        "constraints_text": constraints_text(body.get("constraints")),
        "style_demand": _text(body.get("style_demand")),
        "dedup_key": body.get("dedup_key"),
        "version": body.get("version"),
        "status": body.get("status"),
        "raw": body,
    }


def design_version_public_id(version) -> str:
    created = version.created_at or ""
    digits = "".join(ch for ch in created[:10] if ch.isdigit())
    ymd = digits if len(digits) == 8 else datetime.now(UTC).strftime("%Y%m%d")
    local = "".join(ch for ch in version.id if ch.isalnum()).lower()
    suffix = (local[:8] or "0000")[:8]
    return f"DSG-{ymd}-{suffix}"


def build_design_note(package: dict | None, spec: dict | None, version) -> str:
    package = package or {}
    spec = spec or {}
    review = ((version.payload or {}).get("review") or {}).get("summary") or ""
    lines = []
    if package.get("requirement_desc"):
        lines.append(f"本设计回应需求：{package['requirement_desc']}")
    if package.get("constraints_text"):
        lines.append(f"约束处理：{package['constraints_text']}")
    if spec.get("intent"):
        lines.append(f"设计说明：{spec['intent']}")
    if review:
        lines.append(f"图片检查：{review}")
    return ("\n".join(lines) if lines else "设计方案已确认，详见效果图。")[:4000]


def _version_number(version) -> int:
    raw = (version.payload or {}).get("design_index") or 1
    try:
        number = int(raw)
    except (TypeError, ValueError):
        number = 1
    return number if number >= 1 else 1


def _loads(value):
    if not value:
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return None


def project_cms(project) -> dict:
    package = _loads(getattr(project, "cms_package_snapshot", None))
    responses = _loads(getattr(project, "cms_responses", None))
    if not isinstance(responses, list):
        responses = []
    ids: list[str] = []
    latest: dict = {}
    for item in responses:
        if not isinstance(item, dict):
            continue
        if item.get("design_version_id"):
            latest = item
        for rid in item.get("response_ids") or []:
            if isinstance(rid, str) and rid and rid not in ids:
                ids.append(rid)
    return {
        "package_id": getattr(project, "cms_package_id", None),
        "package": package if isinstance(package, dict) else None,
        "design_version_id": latest.get("design_version_id"),
        "response_ids": ids,
        "response_status": latest.get("response_status"),
        "cms_status": latest.get("cms_status"),
    }


def _extract_ids(body: dict) -> list[str]:
    ids: list[str] = []

    def add(value):
        if isinstance(value, str) and value and value not in ids:
            ids.append(value)

    for key in ("id", "response_id", "design_response_id", "cms_response_id"):
        add(body.get(key))
    for key in ("ids", "response_ids"):
        value = body.get(key)
        if isinstance(value, list):
            for item in value:
                add(item)
    nested = body.get("data")
    if isinstance(nested, dict):
        for item in _extract_ids(nested):
            add(item)
    return ids


def _request(method: str, path: str, *, json_body: dict | None = None, not_found: str, list_response: bool = False) -> dict | list:
    cfg = get_config()
    if not cfg.cms_api_key:
        raise AgentError("CMS_NOT_CONFIGURED", NOT_CONFIGURED, 503)
    url = f"{cfg.cms_base_url}{path}"
    headers = {"X-API-Key": cfg.cms_api_key, "Accept": "application/json"}
    try:
        with httpx.Client(timeout=cfg.cms_timeout) as client:
            response = client.request(method, url, headers=headers, json=json_body)
    except httpx.TimeoutException as exc:
        raise AgentError("CMS_TIMEOUT", TIMEOUT_MESSAGE, 504) from exc
    except httpx.HTTPError as exc:
        raise AgentError("CMS_UNAVAILABLE", UNAVAILABLE, 502) from exc
    try:
        body = response.json()
    except ValueError:
        body = None
    if response.status_code == 403:
        raise AgentError("CMS_NOT_APPROVED", NOT_APPROVED, 403)
    if response.status_code == 404:
        raise AgentError("CMS_NOT_FOUND", not_found, 404)
    if response.status_code >= 400:
        message = "CMS 没有接受这次请求，请稍后重试。"
        if isinstance(body, dict) and isinstance(body.get("error"), dict):
            remote = body["error"].get("message")
            if isinstance(remote, str) and remote.strip():
                message = remote.strip()
        status = 502 if response.status_code >= 500 else 400
        raise AgentError("CMS_REJECTED", message, status)
    if not isinstance(body, list if list_response else dict):
        raise AgentError("CMS_BAD_RESPONSE", "CMS 返回格式异常。", 502)
    return body


def fetch_packages(offset: int = 0) -> dict:
    body = _request("GET", f"/api/packages?status=approved&limit=21&offset={offset}",
                    not_found="CMS 暂不支持自动读取，请联系维护者更新服务。", list_response=True)
    if any(not isinstance(item, dict) or item.get("status") != "approved"
           or not _PACKAGE_ID.fullmatch(_text(item.get("package_id"))) for item in body):
        raise AgentError("CMS_BAD_RESPONSE", "CMS 返回了无效的已批准证据包，请稍后重试。", 502)
    return {"items": [normalize_package(item) for item in body[:20]],
            "next_offset": offset + 20 if len(body) > 20 else None}


def fetch_package(package_id: str) -> dict:
    package_id = package_id.strip()
    if not _PACKAGE_ID.fullmatch(package_id):
        raise AgentError("INVALID_INPUT", "证据包编号格式不正确。", 422)
    body = _request("GET", f"/api/packages/{package_id}", not_found=PACKAGE_MISSING)
    status = body.get("status")
    if status is not None and status != "approved":
        raise AgentError("CMS_NOT_APPROVED", NOT_APPROVED, 403)
    return normalize_package(body, package_id)


def _merge_responses(existing: str | None, entry: dict) -> str:
    items = _loads(existing)
    if not isinstance(items, list):
        items = []
    merged = False
    for index, item in enumerate(items):
        if isinstance(item, dict) and item.get("design_version_id") == entry["design_version_id"]:
            ids: list[str] = []
            for rid in [*(item.get("response_ids") or []), *(entry.get("response_ids") or [])]:
                if isinstance(rid, str) and rid and rid not in ids:
                    ids.append(rid)
            items[index] = {**item, **entry, "response_ids": ids}
            merged = True
            break
    if not merged:
        items.append(entry)
    return json.dumps(items, ensure_ascii=False)


def submit_response(pid: int, data: CmsResponseIn) -> dict:
    with transaction() as db:
        project = db.get(Project, pid)
        if project is None:
            raise AgentError("NOT_FOUND", "项目不存在", 404)
        if not project.cms_package_id:
            raise AgentError("CMS_PACKAGE_REQUIRED", "这个项目还没有关联证据包，请先从 CMS 导入。", 409)
        version = require(db, data.version_id, "version", pid)
        if version.status != "confirmed":
            raise AgentError("CONFIRM_REQUIRED", "请先确认这款设计，再回传 CMS。", 409)
        if not (version.payload or {}).get("image"):
            raise AgentError("IMAGE_REQUIRED", "这款设计还没有可回传的图片。", 409)
        spec = require(db, version.payload["spec_id"], "spec", pid).payload.get("spec") or {}
        package = _loads(project.cms_package_snapshot)
        if not isinstance(package, dict):
            package = {}
        note = data.design_note.strip() or build_design_note(package, spec, version)
        design_vid = design_version_public_id(version)
        version_number = _version_number(version)
        package_id = project.cms_package_id
        payload = {
            "design_version_id": design_vid,
            "source_agent": SOURCE_AGENT,
            "package_id": package_id,
            "design_image": [f"{get_config().public_base_url}/api/design-versions/{version.id}/image"],
            "design_note": note,
            "response_status": data.response_status,
            "version": f"v{version_number}",
        }
    body = _request("POST", "/api/design-responses", json_body=payload, not_found=RESPONSE_MISSING)
    entry = {
        "design_version_id": design_vid,
        "local_version_id": data.version_id,
        "response_ids": _extract_ids(body),
        "response_status": data.response_status,
        "cms_status": "draft",
        "version": version_number,
    }
    with transaction() as db:
        project = db.get(Project, pid)
        project.cms_responses = _merge_responses(project.cms_responses, entry)
        stored = json.loads(project.cms_responses)
    matched = next(item for item in stored if item.get("design_version_id") == design_vid)
    return {
        "design_version_id": design_vid,
        "response_status": data.response_status,
        "cms_status": "draft",
        "response_ids": matched.get("response_ids") or [],
        "package_id": package_id,
        "version": version_number,
        "design_note": note,
    }
