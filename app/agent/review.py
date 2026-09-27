"""Bind model-visible image aliases to the exact images in one review call."""

import re
from copy import deepcopy

from .schemas import Review
from .store import AgentError

PREFIXES = {"asset_id", "base_version_id", "parent_version_id", "candidate_id"}


def prepare(spec, images):
    context = deepcopy(spec)
    mapping, identifiers, labeled = {}, {}, []
    for index, (label, image) in enumerate(images, 1):
        identity, separator, description = label.partition(";")
        field, equals, identifier = identity.partition("=")
        if field not in PREFIXES or not equals or not identifier or not separator:
            raise AgentError("INVALID_IMAGE_BINDING", "图片对应关系异常，已保留当前设计")
        alias = f"image_{index}"
        mapping[alias] = identifier
        identifiers[identifier] = alias
        labeled.append((f"{field}={alias};{description}", image))
    for ref in context["references"]:
        ref["asset_id"] = identifiers[ref["asset_id"]]
    if context.get("base_version_id"):
        context["base_version_id"] = identifiers[context["base_version_id"]]
    context["available_image_ids"] = list(mapping)
    return context, labeled, mapping


def resolve(result, mapping, spec):
    result = Review.model_validate(result).model_dump()
    expected = {c["id"] for c in spec["constraints"]}
    actual = [c["constraint_id"] for c in result["checks"]]
    if set(actual) != expected or len(actual) != len(expected):
        raise AgentError(
            "REVIEW_INVALID", "检查内容需要重新核对", details=[{"path": "checks", "type": "constraint_mismatch"}]
        )
    for index, check in enumerate(result["checks"]):
        resolved = []
        for reference in check["reference_ids"]:
            value = reference.strip()
            # Only exact aliases and unambiguous field prefixes are accepted. No fuzzy matching.
            match = re.fullmatch(
                r"(?:asset_id|base_version_id|parent_version_id|candidate_id|reference_id)\s*=\s*(image_[1-9][0-9]*)",
                value,
            )
            if match:
                value = match.group(1)
            if value not in mapping:
                raise AgentError(
                    "REVIEW_INVALID",
                    "检查内容需要重新核对",
                    details=[{"path": f"checks.{index}.reference_ids", "type": "unknown_image_alias"}],
                )
            if mapping[value] not in resolved:
                resolved.append(mapping[value])
        check["reference_ids"] = resolved
    return result


def incomplete(spec):
    message = "图片已生成并保存，但自动核验暂未完成。你可以先查看设计，核验完成前不会标为通过。"
    return {
        "goal": {"status": "unknown", "evidence": "自动核验结果未通过格式与图片对应关系校验"},
        "preservation": {"status": "unknown", "evidence": "尚无有效的自动核验结论"},
        "checks": [
            {
                "constraint_id": c["id"],
                "status": "unknown",
                "candidate_region": c["region"],
                "evidence": "该要求尚未完成核验",
                "reference_ids": [],
            }
            for c in spec["constraints"]
        ],
        "summary": message,
    }
