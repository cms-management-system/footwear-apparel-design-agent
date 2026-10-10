"""Strict, byte-preserving partner v2 protocol validation.

This module validates data, not authority: deliveries must additionally be fetched
by the bridge from the configured authenticated product service.  No uploaded
``approved`` string can establish that provenance.
"""

import base64
import binascii
import hashlib
import json
import math
import re
from datetime import datetime
from typing import NoReturn

from .store import AgentError

CONTENT_LIMIT = 2 * 1024 * 1024
DELIVERY_LIMIT = 3 * 1024 * 1024
LIST_LIMIT = 16 * 1024 * 1024
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_VERSION = re.compile(r"v[1-9][0-9]*\Z")
_TIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})\Z")
_PRIVACY_TEXT = re.compile(
    r"(?:https?://|file://|data:image/|blob:|Bearer\s+[A-Za-z0-9._~-]+)"
    r"|(?:[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9-]+\.[A-Za-z]{2,})"
    r"|(?<![A-Za-z0-9])(?:\+?86[- ]?)?1[3-9][0-9]{9}(?![A-Za-z0-9])"
    r"|(?:api[_ -]?key|access[_ -]?token|session[_ -]?(?:id|ref)|password)\s*[:=]\s*\S+"
    r"|(?:身高|体重|胸围|腰围|臀围|身份证(?:号)?|手机号|真实姓名)\s*[:：=]?\s*[0-9０-９]+",
    re.IGNORECASE,
)

DELIVERY_FIELDS = {
    "delivery_schema", "source_product", "target_product", "source_instance_id",
    "target_instance_id", "authorized_scope", "package_id", "version", "content_digest", "content_base64",
}
PACKAGE_FIELDS = {
    "package_schema", "package_id", "version", "source_product", "target_product", "source_instance_id",
    "target_instance_id", "authorized_scope", "source_agent", "status", "signal_ids", "source", "dedup_key",
    "dedup_rule", "data_origin", "environment", "sample_count", "evidence_counts", "attribution",
    "requirement_desc", "constraints", "source_refs", "provenance", "version_group_id", "requirement_version",
    "prompt_version", "requirement_digest", "requirement_base64", "prompt_digest", "prompt_base64", "approval",
}
REQUIREMENT_FIELDS = {
    "title", "requirement_desc", "design_object", "constraints", "priority_proposal", "priority_reason",
}
PROMPT_FIELDS = {
    "positive_prompt", "avoid_items", "design_spec", "generation", "source_refs", "human_additions", "unknowns",
    "conflicts",
}
SPEC_FIELDS = {
    "category", "season", "scene", "fit", "silhouette", "color", "material", "craft", "knowledge_status",
    "display_requirements",
}
SOURCE_REF_FIELDS = {
    "source_ref_id", "signal_id", "source_payload_digest", "evidence_id", "evidence_revision", "field_path", "excerpt",
}
PROVENANCE_FIELDS = {
    "signal_schema", "signal_id", "mapping_version", "source_payload_digest", "source_supplement_digest",
    "source_approval_ref", "source_approval_subject", "source_approved_at", "verification_id", "verified_at",
    "projection_digest", "provenance_status",
}
APPROVAL_FIELDS = {
    "approval_schema", "decision_id", "approved_by", "approved_at", "version_group_id", "requirement_digest",
    "prompt_digest", "final_priority", "priority_reason",
}
DESIGN_RECEIPT_FIELDS = {
    "receipt_schema", "design_receive_id", "source_instance_id", "target_instance_id", "authorized_scope",
    "package_id", "version", "content_digest", "status", "received_at",
}
EVENT_FIELDS = {
    "event_schema", "event_id", "event_revision", "kind", "source_product", "target_product",
    "source_instance_id", "target_instance_id", "authorized_scope", "created_at", "package_id", "version",
    "package_content_digest", "design_receive_id", "version_group_id", "requirement_digest", "prompt_digest",
    "note", "design_receipt", "clarification", "result",
}
# The published v2 contract enumerates twelve fields, not thirteen.
RESULT_VERSION_FIELDS = {
    "schema_version", "design_version_id", "task_id", "assignment_id", "design_receive_id", "package_id",
    "package_version", "package_content_digest", "version_group_id", "requirement_digest", "prompt_digest",
    "assets", "summary", "changes", "checks", "unknowns", "generation_mode", "created_by", "created_at",
}
IMAGE_PROVENANCE_FIELDS = {
    "schema_version", "provider", "model", "response_model", "generation_task_id", "generation_attempt_id",
    "provider_request_id", "execution_prompt_id", "execution_prompt_digest", "execution_prompt_base64",
    "source_product_prompt_digest", "authorization_ref", "started_at", "completed_at", "actual_cost_fen",
}
EVENT_RECEIPT_FIELDS = {
    "receipt_schema", "event_receive_id", "event_id", "event_revision", "source_instance_id",
    "target_instance_id", "authorized_scope", "content_digest", "package_id", "version", "status", "received_at",
}


def _fail(message: str, code: str = "INVALID_INPUT", status: int = 422) -> NoReturn:
    # Never interpolate data into externally visible errors.
    raise AgentError(code, message, status)


def _object(value, fields: set[str], label: str) -> dict:
    if type(value) is not dict or set(value) != fields:
        _fail(f"{label}字段不完整或含未定义字段")
    return value


def _string(value, label: str, *, empty: bool = False, maximum: int | None = None) -> str:
    if not isinstance(value, str) or (not empty and not value.strip()):
        _fail(f"{label}必须为有效文本")
    if maximum is not None and len(value) > maximum:
        _fail(f"{label}文本超过允许长度")
    if any(0xD800 <= ord(c) <= 0xDFFF or ord(c) == 0 for c in value):
        _fail(f"{label}包含无效字符")
    return value


def _identifier(value, label: str = "标识") -> str:
    value = _string(value, label, maximum=180)
    if value != value.strip() or any(ord(c) < 32 or ord(c) == 127 for c in value):
        _fail(f"{label}包含无效字符")
    return value


def _integer(value, label: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        _fail(f"{label}必须为有效整数")
    return value


def _digest(value, label: str = "摘要") -> str:
    if not isinstance(value, str) or not _SHA.fullmatch(value):
        _fail(f"{label}必须为小写SHA256")
    return value


def _time(value, label: str = "时间") -> str:
    if not isinstance(value, str) or not _TIME.fullmatch(value):
        _fail(f"{label}必须为带时区ISO8601")
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if timestamp.utcoffset() is None:
            raise ValueError
    except ValueError:
        _fail(f"{label}不是有效时间")
    return value


def _version(value) -> str:
    if not isinstance(value, str) or not _VERSION.fullmatch(value):
        _fail("包版本必须为v1起的正整数版本")
    return value


def _enum(value, choices, label: str):
    if value not in choices or type(value) is not str:
        _fail(f"{label}不受支持")
    return value


def _schema(value, expected: str):
    if value != expected:
        _fail("交接协议版本不受支持", "UNSUPPORTED_SCHEMA")


def _same_json(left, right) -> bool:
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(_same_json(left[key], right[key]) for key in left)
    if isinstance(left, list):
        return len(left) == len(right) and all(_same_json(a, b) for a, b in zip(left, right, strict=True))
    return left == right


def _equal(left, right, label: str):
    if not _same_json(left, right):
        _fail(f"{label}绑定不一致")


def _array(value, label: str, *, minimum: int = 0) -> list:
    if type(value) is not list or len(value) < minimum:
        _fail(f"{label}必须为有效数组")
    return value


def _texts(value, label: str, *, minimum: int = 0):
    for item in _array(value, label, minimum=minimum):
        _string(item, label)


def _privacy(value):
    """Fail closed on forbidden fields and obvious private content, not an anonymizer."""
    if isinstance(value, dict):
        for key, item in value.items():
            if key.lower() in {
                "session_id", "session_ref", "photo", "photo_url", "image_url", "metadata", "source_snapshot",
                "consumer_profile", "body_profile", "height_cm", "weight_kg", "phone", "email", "password",
                "api_key", "access_token", "original_photo", "personal_outfit", "raw_identity", "raw_dh",
            }:
                _fail("交接内容包含禁止传播的资料")
            if key not in {"content_base64", "requirement_base64", "prompt_base64", "design_version_base64",
                           "execution_prompt_base64"}:
                _privacy(item)
    elif isinstance(value, list):
        for item in value:
            _privacy(item)
    elif isinstance(value, str) and _PRIVACY_TEXT.search(value):
        _fail("交接文本包含未脱敏资料或外部访问地址")


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _fail("JSON不允许重复字段")
        result[key] = value
    return result


def _constant(_value):
    _fail("JSON不允许NaN或Infinity")


_DECODER = json.JSONDecoder(object_pairs_hook=_pairs, parse_constant=_constant)


def _decode(raw: bytes, *, limit: int, label: str):
    if type(raw) is not bytes:
        _fail(f"{label}必须为原始字节")
    if len(raw) > limit:
        _fail(f"{label}超过大小限制", status=413)
    try:
        text = raw.decode("utf-8", errors="strict")
        value = _DECODER.decode(text)
        _unicode_tree(value)
        return value, text
    except (UnicodeError, ValueError, RecursionError, OverflowError) as exc:
        raise AgentError("INVALID_INPUT", f"{label}不是有效UTF8 JSON", 422) from exc


def _unicode_tree(value):
    if isinstance(value, str):
        _string(value, "JSON文本", empty=True)
    elif isinstance(value, list):
        for item in value:
            _unicode_tree(item)
    elif isinstance(value, dict):
        for key, item in value.items():
            _string(key, "JSON字段", empty=True)
            _unicode_tree(item)
    elif isinstance(value, float):
        # Includes exponent overflow (1e999), which parse_constant does not see.
        if not math.isfinite(value):
            _fail("JSON不允许非有限数值")


def _blob(value, digest, label: str) -> tuple[bytes, dict]:
    _digest(digest, f"{label}摘要")
    if not isinstance(value, str):
        _fail(f"{label}必须为Base64文本")
    if len(value) > ((CONTENT_LIMIT + 2) // 3) * 4:
        _fail(f"{label}超过大小限制", status=413)
    try:
        encoded = value.encode("ascii")
        raw = base64.b64decode(encoded, validate=True)
    except (UnicodeError, ValueError, binascii.Error) as exc:
        raise AgentError("INVALID_INPUT", f"{label}不是有效Base64", 422) from exc
    if base64.b64encode(raw) != encoded:
        _fail(f"{label}必须为规范Base64")
    if len(raw) > CONTENT_LIMIT:
        _fail(f"{label}超过大小限制", status=413)
    if hashlib.sha256(raw).hexdigest() != digest:
        _fail(f"{label}原字节摘要不一致")
    content, _ = _decode(raw, limit=CONTENT_LIMIT, label=label)
    if type(content) is not dict:
        _fail(f"{label}必须为JSON对象")
    return raw, content


def _source_refs(refs, signal_id: str, payload_digest: str) -> set[str]:
    identifiers = set()
    for ref in _array(refs, "来源引用", minimum=1):
        _object(ref, SOURCE_REF_FIELDS, "来源引用")
        ref_id = _identifier(ref["source_ref_id"], "来源引用标识")
        if ref_id in identifiers:
            _fail("来源引用标识重复")
        identifiers.add(ref_id)
        _equal(ref["signal_id"], signal_id, "来源信号")
        _equal(ref["source_payload_digest"], payload_digest, "来源摘要")
        _identifier(ref["evidence_id"], "证据标识")
        if ref["evidence_revision"] is not None:
            _integer(ref["evidence_revision"], "证据版本", minimum=1)
        _identifier(ref["field_path"], "来源字段路径")
        _string(ref["excerpt"], "脱敏来源原文", maximum=10000)
    return identifiers


def decode_delivery(raw: bytes, *, source_instance_id: str, target_instance_id: str, scope_id: str) -> dict:
    """Validate one authenticated-source delivery and preserve all three originals."""
    delivery, _ = _decode(raw, limit=DELIVERY_LIMIT, label="传输项")
    _object(delivery, DELIVERY_FIELDS, "传输项")
    _schema(delivery["delivery_schema"], "pa-design-delivery/2")
    for field, expected in {
        "source_product": "product_management", "target_product": "footwear_design",
        "source_instance_id": source_instance_id, "target_instance_id": target_instance_id,
        "authorized_scope": scope_id,
    }.items():
        _identifier(delivery[field], field)
        _equal(delivery[field], expected, field)
    _identifier(delivery["package_id"], "批准包标识")
    _version(delivery["version"])
    content_bytes, package = _blob(delivery["content_base64"], delivery["content_digest"], "批准包")
    _object(package, PACKAGE_FIELDS, "批准包")
    _schema(package["package_schema"], "pa-design-package/2")
    for field in DELIVERY_FIELDS - {"delivery_schema", "content_base64", "content_digest"}:
        _equal(package[field], delivery[field], field)
    _equal(package["source_agent"], "product", "来源Agent")
    _equal(package["status"], "approved", "批准状态")
    signal_ids = _array(package["signal_ids"], "来源信号", minimum=1)
    if len(signal_ids) != 1:
        _fail("每个批准包必须只绑定一个来源信号")
    signal_id = _identifier(signal_ids[0], "来源信号")
    _equal(package["package_id"], f"PKG-WB-{signal_id}", "批准包信号")
    _string(package["source"], "来源展示名")
    _digest(package["dedup_key"], "来源去重摘要")
    _string(package["dedup_rule"], "去重规则")
    _enum(package["data_origin"], ("synthetic", "runtime_demo", "mixed_demo"), "数据来源分类")
    _equal(package["environment"], "synthetic_local", "当前环境")
    _object(package["sample_count"], {"intent", "simulated_cart", "real_purchase"}, "样本统计")
    if any(value is not None for value in package["sample_count"].values()):
        _fail("未采集的样本统计必须为null")
    counts = _object(package["evidence_counts"], {"expressions", "sessions", "unit"}, "证据统计")
    _integer(counts["expressions"], "表达次数")
    _integer(counts["sessions"], "会话次数")
    _equal(counts["unit"], "expressions_and_sessions", "统计单位")
    attribution = _object(package["attribution"], {"supply_gap", "image_gap", "style_demand"}, "需求归因")
    if any(value is not None for value in attribution.values()):
        _fail("本来源适配的三项归因均须为null")
    _string(package["requirement_desc"], "需求正文")
    _texts(package["constraints"], "明确约束")
    provenance = _object(package["provenance"], PROVENANCE_FIELDS, "来源证明")
    _schema(provenance["signal_schema"], "pa-styling-signal/2")
    _equal(provenance["signal_id"], signal_id, "来源证明信号")
    _equal(provenance["mapping_version"], "dh1.4-to-signal2/1", "来源映射版本")
    for key in ("source_payload_digest", "projection_digest"):
        _digest(provenance[key], key)
    if provenance["source_supplement_digest"] is None:
        _fail("本轮设计包须有完整来源补充", "PROVENANCE_INCOMPLETE")
    _digest(provenance["source_supplement_digest"], "来源补充摘要")
    source_approval = _object(provenance["source_approval_ref"], {"approval_record_id"}, "来源批准引用")
    _identifier(source_approval["approval_record_id"], "来源批准记录标识")
    for key in ("source_approval_subject", "verification_id"):
        _identifier(provenance[key], key)
    for key in ("source_approved_at", "verified_at"):
        _time(provenance[key], key)
    if provenance["provenance_status"] != "complete":
        _fail("来源证据不完整，不能接收为设计包", "PROVENANCE_INCOMPLETE")
    ref_ids = _source_refs(package["source_refs"], signal_id, provenance["source_payload_digest"])
    _identifier(package["version_group_id"], "保存组标识")
    _integer(package["requirement_version"], "需求版本", minimum=1)
    _integer(package["prompt_version"], "提示词版本", minimum=1)
    requirement_bytes, requirement = _blob(
        package["requirement_base64"], package["requirement_digest"], "冻结需求",
    )
    prompt_bytes, prompt = _blob(package["prompt_base64"], package["prompt_digest"], "冻结提示词")
    _object(requirement, REQUIREMENT_FIELDS, "冻结需求")
    for key in REQUIREMENT_FIELDS - {"priority_proposal", "constraints"}:
        _string(requirement[key], key)
    _enum(requirement["priority_proposal"], ("P0", "P1", "P2", "P3"), "建议优先级")
    _equal(requirement["requirement_desc"], package["requirement_desc"], "需求正文")
    _texts(requirement["constraints"], "需求约束")
    _equal(requirement["constraints"], package["constraints"], "冻结需求约束")
    _object(prompt, PROMPT_FIELDS, "冻结提示词")
    _string(prompt["positive_prompt"], "完整提示词正文", maximum=10000)
    _texts(prompt["avoid_items"], "避免项")
    spec = _object(prompt["design_spec"], SPEC_FIELDS, "设计规格")
    _string(spec["category"], "设计品类")
    _equal(spec["category"], requirement["design_object"], "设计对象")
    for key in SPEC_FIELDS - {"category", "knowledge_status", "display_requirements"}:
        if spec[key] is not None:
            _string(spec[key], key)
    _equal(spec["knowledge_status"], "pending_database", "材料工艺知识状态")
    display_ids = set()
    for display in _array(spec["display_requirements"], "展示要求"):
        _object(display, {"id", "text", "origin", "template_version"}, "展示要求")
        display_id = _identifier(display["id"], "展示要求标识")
        if display_id in display_ids:
            _fail("展示要求标识重复")
        display_ids.add(display_id)
        _string(display["text"], "展示要求正文")
        _enum(display["origin"], ("display_template", "human_authored"), "展示要求来源")
        if display["origin"] == "display_template":
            _identifier(display["template_version"], "展示模板版本")
        elif display["template_version"] is not None:
            _fail("人工展示要求的模板版本必须为null")
    _equal(prompt["source_refs"], package["source_refs"], "冻结出处")
    for key in ("material", "craft"):
        has_evidence = any(ref["field_path"].split(".")[-1] == key for ref in prompt["source_refs"])
        if spec[key] is not None and not has_evidence:
            _fail("缺少来源证据的材料或工艺必须为null")
    generation = _object(prompt["generation"], {
        "mode", "template_version", "model", "generated_at", "derived_from",
    }, "生成记录")
    _enum(generation["mode"], ("human_authored", "offline_template", "text_model"), "提示词生成方式")
    if generation["mode"] == "text_model":
        _fail("本轮未授权真实文本模型生成", "CAPABILITY_UNAVAILABLE", 503)
    if generation["model"] is not None:
        _fail("人工和离线模板不能声明模型生成")
    if generation["mode"] == "offline_template":
        _identifier(generation["template_version"], "模板版本")
    elif generation["template_version"] is not None:
        _identifier(generation["template_version"], "模板版本")
    _time(generation["generated_at"], "提示词生成时间")
    if generation["derived_from"] is not None:
        derived = _object(generation["derived_from"], {"kind", "id", "prompt_digest"}, "提示词衍生来源")
        _enum(derived["kind"], ("candidate", "saved_prompt"), "提示词衍生类型")
        _identifier(derived["id"], "提示词衍生标识")
        _digest(derived["prompt_digest"], "衍生原提示词摘要")
    for addition in _array(prompt["human_additions"], "人工增补"):
        _object(addition, {"field_path", "text", "reason", "author_subject"}, "人工增补")
        for key in ("field_path", "author_subject"):
            _identifier(addition[key], key)
        for key in ("text", "reason"):
            _string(addition[key], key)
    for unknown in _array(prompt["unknowns"], "未知项"):
        _object(unknown, {"field_path", "reason", "blocking"}, "未知项")
        _identifier(unknown["field_path"], "未知字段")
        _string(unknown["reason"], "未知原因")
        if type(unknown["blocking"]) is not bool:
            _fail("未知项blocking必须为布尔值")
        if unknown["blocking"]:
            _fail("已批准设计包不能包含阻断未知项", "PROVENANCE_INCOMPLETE")
    for conflict in _array(prompt["conflicts"], "冲突项"):
        _object(conflict, {
            "field_path", "source_ref_ids", "description", "blocking", "resolution", "resolved_by",
        }, "冲突项")
        _identifier(conflict["field_path"], "冲突字段")
        _string(conflict["description"], "冲突说明")
        for ref_id in _array(conflict["source_ref_ids"], "冲突来源", minimum=1):
            _identifier(ref_id, "冲突来源标识")
            if ref_id not in ref_ids:
                _fail("冲突引用不属于冻结来源")
        if type(conflict["blocking"]) is not bool:
            _fail("冲突项blocking必须为布尔值")
        if conflict["resolution"] is None and conflict["resolved_by"] is None:
            if conflict["blocking"]:
                _fail("已批准设计包包含未解决关键冲突", "PROVENANCE_INCOMPLETE")
        elif conflict["resolution"] is None or conflict["resolved_by"] is None:
            _fail("冲突解决内容和负责人必须同时提供")
        else:
            _string(conflict["resolution"], "冲突处理")
            _identifier(conflict["resolved_by"], "冲突处理人")
    approval = _object(package["approval"], APPROVAL_FIELDS, "产品批准")
    _schema(approval["approval_schema"], "pa-product-approval/2")
    for key in ("decision_id", "approved_by"):
        _identifier(approval[key], key)
    _time(approval["approved_at"], "产品批准时间")
    for key in ("version_group_id", "requirement_digest", "prompt_digest"):
        _equal(approval[key], package[key], "批准版本")
    _enum(approval["final_priority"], ("P0", "P1", "P2", "P3"), "最终优先级")
    _string(approval["priority_reason"], "最终优先级原因")
    _privacy(package)
    _privacy(requirement)
    _privacy(prompt)
    return {
        "delivery": delivery, "package": package, "content_bytes": content_bytes,
        "requirement": requirement, "requirement_bytes": requirement_bytes,
        "prompt": prompt, "prompt_bytes": prompt_bytes,
    }


def parse_delivery_list(raw: bytes) -> tuple[list[bytes], str | None]:
    """Decode a strict list while slicing every Delivery from its original JSON."""
    value, text = _decode(raw, limit=LIST_LIMIT, label="交接列表")
    _object(value, {"delivery_schema", "items", "next_cursor"}, "交接列表")
    _schema(value["delivery_schema"], "pa-design-list/2")
    items = _array(value["items"], "交接列表项")
    if len(items) > 50:
        _fail("单页交接包不能超过50项", status=413)
    if value["next_cursor"] is not None:
        _identifier(value["next_cursor"], "分页游标")
    # The first decode checked the complete syntax and duplicate keys.  Walk only
    # top-level members with the same strict decoder to find the actual array,
    # even when nested text contains escaped copies of its name.
    def ws(position: int) -> int:
        while position < len(text) and text[position] in " \r\n\t":
            position += 1
        return position

    position = ws(0) + 1
    slices = []
    while True:
        position = ws(position)
        if text[position] == "}":
            break
        key, position = _DECODER.raw_decode(text, position)
        position = ws(position) + 1  # colon, already validated
        position = ws(position)
        if key == "items":
            position = ws(position + 1)
            while text[position] != "]":
                start = position
                item, end = _DECODER.raw_decode(text, position)
                if type(item) is not dict:
                    _fail("交接列表项必须为JSON对象")
                item_bytes = text[start:end].encode("utf-8")
                if len(item_bytes) > DELIVERY_LIMIT:
                    _fail("传输项超过大小限制", status=413)
                slices.append(item_bytes)
                position = ws(end)
                if text[position] == ",":
                    position = ws(position + 1)
            position += 1
        else:
            _, position = _DECODER.raw_decode(text, position)
        position = ws(position)
        if text[position] == ",":
            position += 1
    return slices, value["next_cursor"]


def _design_receipt(receipt: dict, event: dict):
    _object(receipt, DESIGN_RECEIPT_FIELDS, "设计接收回执")
    _schema(receipt["receipt_schema"], "pa-design-receipt/2")
    for key in ("design_receive_id", "authorized_scope", "package_id", "version"):
        _equal(receipt[key], event[key], "设计接收回执")
    # Receipt direction is product -> design; event direction is design -> product.
    _equal(receipt["source_instance_id"], event["target_instance_id"], "接收来源实例")
    _equal(receipt["target_instance_id"], event["source_instance_id"], "接收目标实例")
    _equal(receipt["content_digest"], event["package_content_digest"], "接收原件摘要")
    _equal(receipt["status"], "received", "接收状态")
    _time(receipt["received_at"], "接收时间")


def validate_event(raw: bytes) -> dict:
    """Validate a frozen event; service and stored-approval checks are external."""
    event, _ = _decode(raw, limit=CONTENT_LIMIT, label="设计事件")
    _object(event, EVENT_FIELDS, "设计事件")
    _schema(event["event_schema"], "pa-design-event/2")
    for key in (
        "event_id", "source_instance_id", "target_instance_id", "authorized_scope", "package_id",
        "design_receive_id", "version_group_id",
    ):
        _identifier(event[key], key)
    _integer(event["event_revision"], "事件版本", minimum=1)
    _equal(event["event_revision"], 1, "事件版本")
    _equal(event["source_product"], "footwear_design", "事件来源")
    _equal(event["target_product"], "product_management", "事件目标")
    _version(event["version"])
    _time(event["created_at"], "事件创建时间")
    for key in ("package_content_digest", "requirement_digest", "prompt_digest"):
        _digest(event[key], key)
    if event["note"] is not None:
        _string(event["note"], "事件说明", empty=True)
    _enum(event["kind"], ("received", "clarification", "design_approved"), "事件类型")
    active = {
        "received": "design_receipt", "clarification": "clarification", "design_approved": "result",
    }[event["kind"]]
    inactive = {"design_receipt", "clarification", "result"} - {active}
    if event[active] is None or any(event[key] is not None for key in inactive):
        _fail("设计事件必须只包含对应的非空分支")
    if active == "design_receipt":
        _design_receipt(event[active], event)
    elif active == "clarification":
        clarification = _object(event[active], {"decision_id", "manager_subject", "decided_at", "reason"}, "澄清决定")
        _identifier(clarification["decision_id"], "澄清决定标识")
        _identifier(clarification["manager_subject"], "澄清负责人")
        _time(clarification["decided_at"], "澄清时间")
        _string(clarification["reason"], "澄清原因")
    else:
        generated = isinstance(event[active], dict) and event[active].get("generation_mode") == "model_generated"
        extra = {"generation_provenance"} if generated else set()
        result = _object(event[active], {
            "task_id", "assignment_id", "submission_id", "design_version_id", "design_version_digest",
            "design_version_base64", "review",
            "assets", "summary", "changes", "checks", "unknowns", "generation_mode",
        } | extra, "审查结果")
        for key in ("task_id", "assignment_id", "submission_id", "design_version_id"):
            _identifier(result[key], key)
        _digest(result["design_version_digest"], "设计版本摘要")
        review = _object(result["review"], {
            "review_id", "manager_subject", "approved_at", "decision", "submission_id", "design_version_id",
            "design_version_digest",
        }, "审查记录")
        for key in ("review_id", "manager_subject"):
            _identifier(review[key], key)
        _time(review["approved_at"], "审查批准时间")
        _equal(review["decision"], "approved", "审查决定")
        for key in ("submission_id", "design_version_id", "design_version_digest"):
            _equal(review[key], result[key], "审查结果版本")
        seen_assets = set()
        for asset in _array(result["assets"], "结果素材", minimum=1):
            _object(asset, {"asset_id", "sha256", "mime_type", "width", "height", "origin"}, "结果素材")
            asset_id = _identifier(asset["asset_id"], "素材标识")
            if asset_id in seen_assets:
                _fail("结果素材标识重复")
            seen_assets.add(asset_id)
            _digest(asset["sha256"], "素材摘要")
            _enum(asset["mime_type"], ("image/png", "image/jpeg", "image/webp"), "素材格式")
            _integer(asset["width"], "素材宽度", minimum=1)
            _integer(asset["height"], "素材高度", minimum=1)
            _enum(asset["origin"], ("synthetic_fixture", "model_generated", "authorized_reference"), "素材来源")
            _equal(asset["origin"], "model_generated" if generated else "synthetic_fixture", "素材来源与生成模式")
        _string(result["summary"], "结果概述")
        _string(result["changes"], "设计修改", empty=True)
        for check in _array(result["checks"], "检查项"):
            _object(check, {"code", "status", "note"}, "检查项")
            _identifier(check["code"], "检查项标识")
            _enum(check["status"], ("passed", "failed", "not_checked"), "检查状态")
            _string(check["note"], "检查说明", empty=True)
        _texts(result["unknowns"], "未知项")
        _enum(result["generation_mode"], ("synthetic_fixture", "model_generated"), "生成方式")
        if generated:
            if len(result["assets"]) != 1:
                _fail("真实生成本轮必须恰好一张图")
            generation = _object(result["generation_provenance"], IMAGE_PROVENANCE_FIELDS, "真实生成记录")
            _schema(generation["schema_version"], "pa-image-generation/1")
            for key in ("provider", "model", "generation_task_id", "generation_attempt_id", "execution_prompt_id",
                        "authorization_ref"):
                _identifier(generation[key], key)
            for key in ("provider_request_id", "response_model"):
                if generation[key] is not None:
                    _identifier(generation[key], key)
            _digest(generation["source_product_prompt_digest"], "批准提示词摘要")
            _equal(generation["source_product_prompt_digest"], event["prompt_digest"], "批准提示词摘要")
            _digest(generation["execution_prompt_digest"], "实际提示词摘要")
            try:
                raw_prompt = base64.b64decode(generation["execution_prompt_base64"], validate=True)
                text = raw_prompt.decode("utf-8")
            except (ValueError, TypeError, UnicodeError):
                _fail("实际提示词原件无效")
            _string(text, "实际提示词", maximum=10000)
            _privacy(text)
            _equal(hashlib.sha256(raw_prompt).hexdigest(), generation["execution_prompt_digest"], "实际提示词原件摘要")
            for key in ("started_at", "completed_at"):
                _time(generation[key], key)
            if generation["actual_cost_fen"] is not None:
                _integer(generation["actual_cost_fen"], "真实费用")
        _, version = _blob(result["design_version_base64"], result["design_version_digest"], "冻结设计可交付版本")
        _object(version, RESULT_VERSION_FIELDS | extra, "冻结设计可交付版本")
        _schema(version["schema_version"], "pa-design-result-version/2")
        for key in (
            "design_version_id", "task_id", "assignment_id", "assets", "summary", "changes", "checks",
            "unknowns", "generation_mode",
        ):
            _equal(version[key], result[key], "冻结设计版本与结果")
        for key in (
            "design_receive_id", "package_id", "package_content_digest", "version_group_id",
            "requirement_digest", "prompt_digest",
        ):
            _equal(version[key], event[key], "冻结设计版本与批准包")
        _equal(version["package_version"], event["version"], "冻结设计版本与批准包版本")
        _identifier(version["created_by"], "设计版本作者")
        _time(version["created_at"], "设计版本创建时间")
        if generated:
            _equal(version["generation_provenance"], result["generation_provenance"], "实际生成原件一致")
            timeline = [generation["started_at"], generation["completed_at"], version["created_at"],
                        review["approved_at"], event["created_at"]]
            dates = [datetime.fromisoformat(value.replace("Z", "+00:00")) for value in timeline]
            if dates != sorted(dates):
                _fail("真实生成与审查时间次序不符")
        _privacy(version)
    _privacy(event)
    return event


def validate_event_receipt(raw: bytes, expected: dict) -> dict:
    """Validate the exact v2 receipt against persisted outbound envelope identity.

    ``expected`` contains event_id/event_revision/source_instance_id/
    target_instance_id/authorized_scope/content_digest/package_id/version.  It
    may be a larger frozen outbound identity dict; no values are inferred from
    the received receipt.
    """
    receipt, _ = _decode(raw, limit=DELIVERY_LIMIT, label="事件回执")
    _object(receipt, EVENT_RECEIPT_FIELDS, "事件回执")
    _schema(receipt["receipt_schema"], "pa-design-event-receipt/2")
    for key in (
        "event_receive_id", "event_id", "source_instance_id", "target_instance_id", "authorized_scope", "package_id",
    ):
        _identifier(receipt[key], key)
    _integer(receipt["event_revision"], "事件版本", minimum=1)
    _equal(receipt["event_revision"], 1, "事件版本")
    _version(receipt["version"])
    _digest(receipt["content_digest"], "事件原件摘要")
    _equal(receipt["status"], "received", "事件接收状态")
    _time(receipt["received_at"], "事件接收时间")
    for key in (
        "event_id", "event_revision", "source_instance_id", "target_instance_id", "authorized_scope",
        "content_digest", "package_id", "version",
    ):
        if key not in expected:
            _fail("缺少已冻结的事件回执核对依据")
        _equal(receipt[key], expected[key], "事件回执身份或原件摘要")
    _privacy(receipt)
    return receipt
