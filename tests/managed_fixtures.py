"""Deterministic synthetic fixtures; these are not a live product or approval proof."""

import base64
import hashlib
import json

SOURCE_INSTANCE = "product-partner-synthetic-20261010"
TARGET_INSTANCE = "design-partner-synthetic-20261010"
SCOPE = "partner-synthetic-20261010"
TIME = "2026-10-10T08:00:00+00:00"


def json_bytes(value, *, pretty: bool = False) -> bytes:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2 if pretty else None,
                      separators=None if pretty else (",", ":")).encode("utf-8")


def sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def make_delivery(*, source_instance_id: str = SOURCE_INSTANCE, target_instance_id: str = TARGET_INSTANCE,
                  scope_id: str = SCOPE, version: str = "v1", long_prompt: bool = False,
                  package_overrides: dict | None = None, prompt_overrides: dict | None = None,
                  requirement_overrides: dict | None = None, content_suffix: bytes = b"",
                  pretty: bool = False) -> bytes:
    """Build a fully bound local synthetic package, optionally with frozen whitespace.

    Overrides are deliberately allowed to make invalid fixtures; digests and
    approval bindings are then rebuilt so tests exercise semantic validation.
    """
    dedup = sha(json_bytes(["styling-synthetic", scope_id, "DH-SYNTHETIC-1", 1]))
    signal_id = "SIG-DH-" + dedup
    requirement = {
        "title": "合成秋季通勤阔腿裤需求", "requirement_desc": "秋季通勤需要宽松阔腿裤",
        "design_object": "裤装", "constraints": ["单任务单设计对象"],
        "priority_proposal": "P1", "priority_reason": "合成技术链路验证",
    }
    requirement.update(requirement_overrides or {})
    refs = [{
        "source_ref_id": "REF-SYNTHETIC-1", "signal_id": signal_id, "source_payload_digest": sha(b"synthetic-dh"),
        "evidence_id": "EV-SYNTHETIC-1", "evidence_revision": 1, "field_path": "design_spec.category",
        "excerpt": "秋季通勤想要宽松阔腿裤。",
    }]
    positive = "为秋季通勤设计一款宽松阔腿裤，完整展示裤装轮廓和裤腿宽度。"
    if long_prompt:
        positive += "此段是明确标注的合成展示模板说明，保留完整需求与来源版本。" * 180
    prompt = {
        "positive_prompt": positive, "avoid_items": ["紧身轮廓"],
        "design_spec": {
            "category": "裤装", "season": "秋季", "scene": "通勤", "fit": "宽松", "silhouette": "阔腿",
            "color": None, "material": None, "craft": None, "knowledge_status": "pending_database",
            "display_requirements": [{"id": "DISPLAY-1", "text": "完整单品轮廓，简洁展示背景；合成模板要求",
                                      "origin": "display_template", "template_version": "synthetic-display/1"}],
        },
        "generation": {"mode": "offline_template", "template_version": "synthetic-template/1", "model": None,
                       "generated_at": TIME, "derived_from": None},
        "source_refs": refs,
        "human_additions": [{"field_path": "design_spec.display_requirements", "text": "简洁展示背景",
                             "reason": "合成展示模板要求，不声称消费者表达",
                             "author_subject": "synthetic-product-staff"}],
        "unknowns": [{"field_path": "design_spec.material", "reason": "材料数据库尚未提供证据", "blocking": False}],
        "conflicts": [],
    }
    prompt.update(prompt_overrides or {})
    requirement_raw, prompt_raw = json_bytes(requirement), json_bytes(prompt)
    group = f"VG-SYNTHETIC-{version}"
    package = {
        "package_schema": "pa-design-package/2", "package_id": f"PKG-WB-{signal_id}", "version": version,
        "source_product": "product_management", "target_product": "footwear_design",
        "source_instance_id": source_instance_id, "target_instance_id": target_instance_id,
        "authorized_scope": scope_id,
        "source_agent": "product", "status": "approved", "signal_ids": [signal_id], "source": "合成穿搭需求",
        "dedup_key": dedup, "dedup_rule": "一条已批准的合成需求集合只建立一个设计包",
        "data_origin": "synthetic", "environment": "synthetic_local",
        "sample_count": {"intent": None, "simulated_cart": None, "real_purchase": None},
        "evidence_counts": {"expressions": 1, "sessions": 1, "unit": "expressions_and_sessions"},
        "attribution": {"supply_gap": None, "image_gap": None, "style_demand": None},
        "requirement_desc": requirement["requirement_desc"], "constraints": requirement["constraints"],
        "source_refs": prompt["source_refs"],
        "provenance": {
            "signal_schema": "pa-styling-signal/2", "signal_id": signal_id, "mapping_version": "dh1.4-to-signal2/1",
            "source_payload_digest": sha(b"synthetic-dh"), "source_supplement_digest": sha(b"synthetic-evidence"),
            "source_approval_ref": {"approval_record_id": "SA-SYNTHETIC-1"},
            "source_approval_subject": "synthetic-styling-manager",
            "source_approved_at": TIME, "verification_id": "VERIFY-SYNTHETIC-1", "verified_at": TIME,
            "projection_digest": sha(b"synthetic-projection"), "provenance_status": "complete",
        },
        "version_group_id": group, "requirement_version": int(version[1:]), "prompt_version": int(version[1:]),
        "requirement_digest": sha(requirement_raw), "requirement_base64": b64(requirement_raw),
        "prompt_digest": sha(prompt_raw), "prompt_base64": b64(prompt_raw),
        "approval": {
            "approval_schema": "pa-product-approval/2", "decision_id": f"PD-SYNTHETIC-{version}",
            "approved_by": "synthetic-product-manager", "approved_at": TIME, "version_group_id": group,
            "requirement_digest": sha(requirement_raw), "prompt_digest": sha(prompt_raw), "final_priority": "P1",
            "priority_reason": "合成技术验收，不代表真实业务人审",
        },
    }
    package.update(package_overrides or {})
    content = json_bytes(package, pretty=pretty) + content_suffix
    delivery = {
        "delivery_schema": "pa-design-delivery/2", "source_product": "product_management",
        "target_product": "footwear_design", "source_instance_id": source_instance_id,
        "target_instance_id": target_instance_id, "authorized_scope": scope_id,
        "package_id": package["package_id"], "version": package["version"],
        "content_digest": sha(content), "content_base64": b64(content),
    }
    return json_bytes(delivery, pretty=pretty)


def make_event(*, kind: str = "received", delivery_raw: bytes | None = None) -> bytes:
    delivery = json.loads(delivery_raw or make_delivery())
    package = json.loads(base64.b64decode(delivery["content_base64"]))
    event = {
        "event_schema": "pa-design-event/2", "event_id": "DE-SYNTHETIC-1", "event_revision": 1, "kind": kind,
        "source_product": "footwear_design", "target_product": "product_management",
        "source_instance_id": delivery["target_instance_id"], "target_instance_id": delivery["source_instance_id"],
        "authorized_scope": delivery["authorized_scope"], "created_at": TIME, "package_id": delivery["package_id"],
        "version": delivery["version"], "package_content_digest": delivery["content_digest"],
        "design_receive_id": "DR-SYNTHETIC-1", "version_group_id": package["version_group_id"],
        "requirement_digest": package["requirement_digest"], "prompt_digest": package["prompt_digest"],
        "note": "合成技术事件，不是真实业务审批", "design_receipt": None, "clarification": None, "result": None,
    }
    if kind == "received":
        event["design_receipt"] = {
            "receipt_schema": "pa-design-receipt/2", "design_receive_id": event["design_receive_id"],
            "source_instance_id": delivery["source_instance_id"], "target_instance_id": delivery["target_instance_id"],
            "authorized_scope": delivery["authorized_scope"], "package_id": delivery["package_id"],
            "version": delivery["version"], "content_digest": delivery["content_digest"], "status": "received",
            "received_at": TIME,
        }
    elif kind == "clarification":
        event["clarification"] = {"decision_id": "DD-SYNTHETIC-1", "manager_subject": "synthetic-design-manager",
                                  "decided_at": TIME, "reason": "合成澄清：请补充展示要求"}
    elif kind == "design_approved":
        event["result"] = {
            "task_id": "DT-SYNTHETIC-1", "assignment_id": "DA-SYNTHETIC-1", "submission_id": "DS-SYNTHETIC-1",
            "design_version_id": "DV-SYNTHETIC-1", "design_version_digest": sha(b"synthetic-design-version"),
            "review": {"review_id": "REVIEW-SYNTHETIC-1", "manager_subject": "synthetic-design-manager",
                       "approved_at": TIME, "decision": "approved", "submission_id": "DS-SYNTHETIC-1",
                       "design_version_id": "DV-SYNTHETIC-1",
                       "design_version_digest": sha(b"synthetic-design-version")},
            "assets": [{"asset_id": "ASSET-SYNTHETIC-1", "sha256": sha(b"synthetic-image-bytes"),
                        "mime_type": "image/png", "width": 64, "height": 64, "origin": "synthetic_fixture"}],
            "summary": "合成素材用于权限与技术回传验证", "changes": "保存合成设计版本",
            "checks": [{"code": "fixture_only", "status": "passed", "note": "未执行真实模型生成"}],
            "unknowns": ["真实设计质量未经评审"], "generation_mode": "synthetic_fixture",
        }
        result = event["result"]
        projection = {
            "schema_version": "pa-design-result-version/2",
            **{key: result[key] for key in (
                "design_version_id", "task_id", "assignment_id", "assets", "summary", "changes", "checks",
                "unknowns", "generation_mode",
            )},
            **{key: event[key] for key in (
                "design_receive_id", "package_id", "package_content_digest", "version_group_id",
                "requirement_digest", "prompt_digest",
            )},
            "package_version": event["version"], "created_by": "synthetic-design-staff", "created_at": TIME,
        }
        projection_raw = json_bytes(projection)
        result["design_version_base64"] = b64(projection_raw)
        result["design_version_digest"] = sha(projection_raw)
        result["review"]["design_version_digest"] = sha(projection_raw)
    return json_bytes(event)


def make_event_receipt(event_raw: bytes | None = None) -> tuple[bytes, dict]:
    raw = event_raw or make_event()
    event = json.loads(raw)
    expected = {key: event[key] for key in (
        "event_id", "event_revision", "source_instance_id", "target_instance_id", "authorized_scope",
        "package_id", "version",
    )}
    expected["content_digest"] = sha(raw)
    return json_bytes({"receipt_schema": "pa-design-event-receipt/2", "event_receive_id": "ER-SYNTHETIC-1",
                       **expected, "status": "received", "received_at": TIME}), expected
