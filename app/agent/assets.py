import base64
import hashlib
import io
import warnings

from PIL import Image, ImageOps, UnidentifiedImageError

from ..config import get_config
from .store import AgentError, uid

MAX_BYTES = 10 * 1024 * 1024
MAX_PIXELS = 20_000_000


def image_bytes(raw: bytes):
    if not raw or len(raw) > MAX_BYTES:
        raise AgentError("INVALID_IMAGE", "每张图片须小于 10 MB", 422)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as source:
                if source.format not in {"JPEG", "PNG", "WEBP"} or getattr(source, "is_animated", False):
                    raise ValueError()
                if source.width * source.height > MAX_PIXELS or min(source.size) < 64:
                    raise ValueError()
                source.load()
                clean = ImageOps.exif_transpose(source).convert("RGB")
                out = io.BytesIO()
                clean.save(out, format="PNG")
                if out.tell() > MAX_BYTES:
                    raise ValueError()
                return out.getvalue(), clean.width, clean.height
    except (
        UnidentifiedImageError,
        ValueError,
        OSError,
        Image.DecompressionBombWarning,
        Image.DecompressionBombError,
    ) as exc:
        raise AgentError(
            "INVALID_IMAGE", "请选择清晰的静态 PNG/JPEG/WebP；至少 64 像素且不超过 2000 万像素", 422
        ) from exc


def save_image(raw: bytes):
    clean, width, height = image_bytes(raw)
    name = f"{uid()}.png"
    root = get_config().assets_dir / "design-agent"
    root.mkdir(parents=True, exist_ok=True)
    path = root / name
    path.write_bytes(clean)
    return {"file": name, "sha256": hashlib.sha256(clean).hexdigest(), "width": width, "height": height}


def file_path(payload):
    name = payload["file"]
    if name != name.split("/")[-1] or not name.endswith(".png") or len(name) != 36:
        raise AgentError("INVALID_ASSET", "素材路径无效")
    path = get_config().assets_dir / "design-agent" / name
    if not path.is_file():
        raise AgentError("ASSET_MISSING", "素材文件缺失，请重新上传", 404)
    return path


def data_url(payload):
    return "data:image/png;base64," + base64.b64encode(file_path(payload).read_bytes()).decode()
