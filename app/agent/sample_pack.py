"""Deterministic first-sample handoff, never a production pattern or model output."""

import io
import json
import re
from pathlib import Path
from xml.sax.saxutils import escape

from PIL import Image
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

FIELD_LABELS = (
    ("面料与辅料", "material"),
    ("颜色与色号", "color"),
    ("尺码与关键尺寸", "measurements"),
    ("图案位置", "graphic_placement"),
    ("图案尺寸", "graphic_dimensions"),
    ("制作工艺", "construction"),
)
NOTICE = "首版打样沟通资料；结构图仅为示意，不是纸样。未填写内容须与版师、打样方核对。"
FONT_NAME = "SampleCJK"


def _font_name() -> str:
    if FONT_NAME in pdfmetrics.getRegisteredFontNames():
        return FONT_NAME
    candidates = (
        "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttf",
        "/usr/share/fonts/truetype/arphic/uming.ttc",
    )
    for path in candidates:
        if Path(path).is_file():
            try:
                pdfmetrics.registerFont(TTFont(FONT_NAME, path))
                return FONT_NAME
            except Exception:
                continue
    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    return "STSong-Light"


def is_tshirt(spec: dict) -> bool:
    text = spec.get("intent", "")
    return bool(re.search(r"T\s*恤|T[- ]?shirt", text, re.IGNORECASE))


def brief_data(version_id: str, spec: dict, fields: dict, sheet_id: str | None) -> dict:
    requirements = [c["text"] for c in spec.get("constraints", []) if c.get("kind") == "must_keep"]
    return {
        "version_id": version_id,
        "sheet_id": sheet_id,
        "intent": spec.get("intent", ""),
        "confirmed_requirements": requirements,
        "fields": {key: (fields.get(key) or "").strip() for _, key in FIELD_LABELS},
        "notes": (fields.get("notes") or "").strip(),
        "unknown_fields": [label for label, key in FIELD_LABELS if not (fields.get(key) or "").strip()],
        "flat_type": "T恤基础结构示意" if is_tshirt(spec) else "该品类暂无可靠结构模板",
        "notice": NOTICE,
    }


def flat_svg(side: str, spec: dict) -> bytes:
    if side not in {"front", "back"}:
        raise ValueError("invalid flat side")
    if not is_tshirt(spec):
        raise ValueError("no reliable flat template for this category")
    front = side == "front"
    label = "正面" if front else "背面"
    if is_tshirt(spec):
        neck = "Q 420 216 470 158" if front else "Q 420 177 470 158"
        graphic = (
            '<rect x="353" y="322" width="134" height="145" rx="8" fill="none" '
            'stroke="#a68450" stroke-width="2" stroke-dasharray="8 7"/>'
            '<text x="420" y="390" text-anchor="middle" class="note">胸前图案区域</text>'
            '<text x="420" y="420" text-anchor="middle" class="small">具体位置、尺寸待核对</text>'
        ) if front else '<text x="420" y="412" text-anchor="middle" class="note">背面细节待核对</text>'
        drawing = f'''
          <path d="M 300 180 L 370 158 {neck} L 540 180 L 632 216
                   L 685 352 L 610 384 L 560 290 L 560 768 L 280 768 L 280 290
                   L 230 384 L 155 352 L 208 216 Z" class="seam"/>
          <path d="M 370 167 {neck.replace('158', '167')}" class="detail"/>
          <path d="M 280 290 L 230 384 M 560 290 L 610 384 M 280 737 L 560 737" class="detail"/>
          <path d="M 177 344 L 238 371 M 602 371 L 663 344" class="detail"/>
          {graphic}
        '''
        category_note = "T恤基础轮廓，比例仅供沟通；肩、袖、领、下摆需实物和尺寸确认。"
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="840" height="960"
      viewBox="0 0 840 960" role="img" aria-label="{label}结构示意">
      <style>
        .seam{{fill:#fbfaf8;stroke:#493f32;stroke-width:3;stroke-linejoin:round;stroke-linecap:round}}
        .detail{{fill:none;stroke:#8e806e;stroke-width:2;stroke-linecap:round}}
        .title{{font:32px 'PingFang SC','Noto Sans CJK SC',sans-serif;fill:#302b25}}
        .note{{font:22px 'PingFang SC','Noto Sans CJK SC',sans-serif;fill:#6e5940}}
        .small{{font:18px 'PingFang SC','Noto Sans CJK SC',sans-serif;fill:#796f63}}
      </style>
      <rect width="840" height="960" fill="#fff"/>
      <text x="48" y="72" class="title">{label} · 首版结构示意</text>
      <path d="M 48 96 H 792" stroke="#ded6c8"/>
      {drawing}
      <path d="M 48 822 H 792" stroke="#ded6c8"/>
      <text x="48" y="864" class="small">{escape(category_note)}</text>
      <text x="48" y="904" class="small">示意图 · 不可直接裁剪或生产</text>
    </svg>'''
    return svg.encode("utf-8")


def _wrapped(text: str, max_width: float, font_size: float, max_lines: int, font: str) -> list[str]:
    text = " ".join(str(text).split())
    lines, current = [], ""
    for char in text:
        candidate = current + char
        if pdfmetrics.stringWidth(candidate, font, font_size) > max_width and current:
            lines.append(current)
            current = char
        else:
            current = candidate
        if len(lines) >= max_lines:
            return lines[:-1] + [lines[-1][:-1] + "…"] if lines else ["…"]
    if current:
        lines.append(current)
    return lines or ["待核对"]


def brief_pdf(data: dict, image_bytes: bytes) -> bytes:
    """One-page A4 communication sheet, with unknowns visibly separated."""
    font = _font_name()
    out = io.BytesIO()
    page_w, page_h = 595.28, 841.89
    pdf = canvas.Canvas(out, pagesize=(page_w, page_h), pageCompression=1)
    pdf.setTitle("首版打样沟通单")
    pdf.setFillColorRGB(.18, .16, .13)
    pdf.setFont(font, 20)
    pdf.drawString(42, 790, "首版打样沟通单")
    pdf.setFont(font, 9)
    pdf.setFillColorRGB(.48, .43, .37)
    pdf.drawRightString(553, 794, f"设计版本  {data['version_id'][:8]}")
    pdf.setStrokeColorRGB(.75, .67, .54)
    pdf.line(42, 776, 553, 776)

    with Image.open(io.BytesIO(image_bytes)) as image:
        image.thumbnail((190, 225))
        width, height = image.size
        x = 42 + (200 - width) / 2
        y = 525 + (225 - height) / 2
        pdf.drawImage(ImageReader(image), x, y, width, height, preserveAspectRatio=True)
    pdf.setFont(font, 9)
    pdf.drawString(42, 515, "已选效果图（仅作外观参考）")
    pdf.setFont(font, 12)
    pdf.setFillColorRGB(.18, .16, .13)
    pdf.drawString(260, 746, "已确认的设计方向")
    y = 723
    for line in _wrapped(data["intent"], 293, 10, 4, font):
        pdf.setFont(font, 10)
        pdf.drawString(260, y, line)
        y -= 16
    y -= 10
    for item in data["confirmed_requirements"][:3]:
        for line in _wrapped("• " + item, 293, 9, 2, font):
            pdf.setFont(font, 9)
            pdf.drawString(260, y, line)
            y -= 14
        y -= 4
    if len(data["confirmed_requirements"]) > 3:
        pdf.setFont(font, 8)
        pdf.drawString(260, y, "其余已确认要求见 JSON。")

    pdf.setStrokeColorRGB(.84, .82, .78)
    pdf.line(42, 495, 553, 495)
    pdf.setFont(font, 12)
    pdf.drawString(42, 472, "首版沟通要点")
    y = 450
    for label, key in FIELD_LABELS:
        value = data["fields"][key] or "待核对"
        pdf.setFont(font, 9)
        pdf.setFillColorRGB(.48, .43, .37)
        pdf.drawString(42, y, label)
        pdf.setFillColorRGB(.18, .16, .13)
        for index, line in enumerate(_wrapped(value, 384, 9, 2, font)):
            pdf.drawString(168, y - index * 13, line)
        y -= 45
    pdf.setFillColorRGB(.96, .94, .90)
    pdf.rect(42, 91, 511, 58, fill=1, stroke=0)
    pdf.setFillColorRGB(.35, .29, .21)
    pdf.setFont(font, 9)
    pdf.drawString(54, 127, "交接前核对")
    pending = "、".join(data["unknown_fields"]) or "已填写项目仍需实物核对"
    for index, line in enumerate(_wrapped(pending, 480, 9, 2, font)):
        pdf.drawString(54, 110 - index * 12, line)
    pdf.setFillColorRGB(.48, .43, .37)
    pdf.setFont(font, 8)
    pdf.drawString(42, 60, NOTICE)
    pdf.drawString(42, 44, "完整要求与字段见 JSON；结构示意仅在有可靠模板时附带。")
    pdf.showPage()
    pdf.save()
    return out.getvalue()


def manifest_json(data: dict) -> bytes:
    return json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
