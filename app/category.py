"""品类判定（鞋类 / 靴类 / 服装）。

**所有"按品类分叉"的逻辑都必须走这里**（尺寸表、用料、导出、迭代文案），
避免各处各写一份关键词表导致口径不一致（凉鞋出现"腰围/裙长"就是这么来的）。
"""

from __future__ import annotations

SHOE_WORDS = ("鞋", "靴", "跟", "凉", "拖")
BOOT_WORDS = ("靴",)


def text_of(*values: object) -> str:
    """把品类、版型名、企划文本等拼成一段用于判定的文本。"""
    return " ".join(str(v) for v in values if v)


def is_shoe(*values: object) -> bool:
    """是否鞋类（鞋/靴/高跟/凉鞋/拖鞋…）。"""
    text = text_of(*values)
    return any(word in text for word in SHOE_WORDS)


def is_boot(*values: object) -> bool:
    """是否靴类（只有靴类才有"筒高"）。"""
    text = text_of(*values)
    return any(word in text for word in BOOT_WORDS)
