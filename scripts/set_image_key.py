#!/usr/bin/env python
"""交互式填写火山方舟出图配置（**输入不回显、不写日志、不进仓库**）。

用法：
    cd projects/鞋服agent开发文档
    .venv/bin/python scripts/set_image_key.py

它会问你三件事：API Key（隐藏输入）、图像模型 ID、服务地址（可回车用默认），
然后只把结果写进本项目的 `.env`（已 gitignore），并在屏幕上只显示"有没有填上 / 长度"，不显示内容。
"""

from __future__ import annotations

import getpass
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
ENV = ROOT / ".env"
DEFAULT_BASE = "https://ark.cn-beijing.volces.com/api/v3"


def set_var(text: str, key: str, value: str) -> str:
    if re.search(rf"^{key}=.*$", text, flags=re.M):
        return re.sub(rf"^{key}=.*$", lambda _: f"{key}={value}", text, flags=re.M)
    return text.rstrip() + f"\n{key}={value}\n"


def check_key(key: str) -> str:
    """检查 Key 的形状；返回一句人话问题，形状正常则返回空串。

    2026-09-21 实测：产品经理第一次粘贴进来 326 个字符（把控制台页面上的说明文字一起复制的），
    接口直接报 401 "The API key format is incorrect"。所以在这里当场拦住，不让来回试。
    """
    if not key:
        return "没有输入 Key。"
    if any(ch.isspace() for ch in key):
        return "里面有空格或换行，请只复制那一串字符。"
    if re.search(r"[^A-Za-z0-9._\-]", key):
        return "里面有中文或符号，多半是把页面上的说明文字也复制进来了。"
    if key.lower().startswith("bearer"):
        return "不要带 Bearer 前缀，只要那串字符。"
    if len(key) > 120:
        return f"太长了（{len(key)} 个字符）。正常是 30–80 个字符，请确认只选中了 Key 那一串。"
    if len(key) < 20:
        return f"太短了（{len(key)} 个字符），看起来没复制全。"
    return ""


def main() -> None:
    print("填写火山方舟（即梦底层模型）出图配置 —— 内容不会回显、不会进仓库")
    key = getpass.getpass("① 粘贴 API Key（输入时不显示，回车确认）：").strip()
    problem = check_key(key)
    if problem:
        print(f"❌ {problem}\n   请重新运行本命令，只复制 Key 那一串（不要带引号/空格/Bearer/页面文字）。")
        return
    model = input("② 粘贴图像模型 ID（控制台「模型广场」里开通的那个）：").strip()
    if model and (len(model) > 80 or any(ch.isspace() for ch in model)):
        print(f"❌ 模型 ID 看起来不对（{len(model)} 个字符）。它通常很短，形如 doubao-seedream-3-0-t2i-…")
        return
    base = input(f"③ 服务地址（直接回车用默认 {DEFAULT_BASE}）：").strip() or DEFAULT_BASE

    text = ENV.read_text(encoding="utf-8") if ENV.exists() else ""
    text = set_var(text, "IMAGE_API_KEY", key)
    text = set_var(text, "IMAGE_BASE_URL", base)
    if model:
        text = set_var(text, "IMAGE_MODEL", model)
    ENV.write_text(text, encoding="utf-8")
    ENV.chmod(0o600)

    print("\n已写入 .env（只显示有没有、多长，不显示内容）：")
    for line in ENV.read_text(encoding="utf-8").splitlines():
        if line.startswith("IMAGE_"):
            name, _, value = line.partition("=")
            value = re.sub(r"\s+#.*$", "", value).strip()
            print(f"  {name:18s} {'已填（' + str(len(value)) + ' 字符）' if value else '（空）'}")
    print("\n下一步：回一句「填好了」，我会真实出一张图（约几分到一毛多）。")


if __name__ == "__main__":
    main()
