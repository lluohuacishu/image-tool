from __future__ import annotations

import logging
import math
import warnings
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps

from image_conversion import prepare_for_jpeg


MAX_SAFE_IMAGE_PIXELS = 500_000_000
MAX_PREVIEW_IMAGE_PIXELS = 160_000_000
MAX_INTERACTIVE_IMAGE_PIXELS = 120_000_000
IMAGE_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".jfif",
    ".png",
    ".webp",
    ".bmp",
    ".dib",
    ".tif",
    ".tiff",
    ".ico",
    ".gif",
    ".ppm",
    ".pgm",
    ".pbm",
    ".pnm",
    ".tga",
    ".pcx",
    ".dds",
}

Image.MAX_IMAGE_PIXELS = MAX_SAFE_IMAGE_PIXELS
warnings.simplefilter("error", Image.DecompressionBombWarning)

logger = logging.getLogger("image_tool_gui")


@dataclass(frozen=True)
class ResizeResult:
    source: Path
    target: Path
    original_size: tuple[int, int]
    new_size: tuple[int, int]


def is_image_file(path: Path) -> bool:
    try:
        return path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS
    except OSError:
        logger.debug("无法访问路径：%s", path, exc_info=True)
        return False


def collect_image_paths(paths: list[Path], include_subfolders: bool) -> list[Path]:
    images: list[Path] = []
    for path in paths:
        try:
            if path.is_dir():
                iterator = path.rglob("*") if include_subfolders else path.iterdir()
                for candidate in iterator:
                    if is_image_file(candidate):
                        images.append(candidate)
            elif is_image_file(path):
                images.append(path)
        except OSError:
            logger.warning("扫描路径失败：%s", path, exc_info=True)
    return images


def clamp(value: int, minimum: int, maximum: int) -> int:
    return max(minimum, min(value, maximum))


def middle_ellipsis(text: str, max_chars: int = 72) -> str:
    if len(text) <= max_chars:
        return text

    keep = max_chars - 3
    front = keep // 2
    back = keep - front
    return f"{text[:front]}...{text[-back:]}"


def readable_error(exc: BaseException) -> str:
    if isinstance(exc, (Image.DecompressionBombError, Image.DecompressionBombWarning)):
        return f"图片像素过大，超过安全上限 {MAX_SAFE_IMAGE_PIXELS:,} 像素"
    return str(exc)


def pixel_count(size: tuple[int, int]) -> int:
    return max(0, size[0]) * max(0, size[1])


def ensure_image_pixel_limit(
    size: tuple[int, int],
    limit: int,
    purpose: str,
) -> None:
    count = pixel_count(size)
    if count > limit:
        raise ValueError(
            f"{purpose}图片像素过大：{size[0]} x {size[1]}，"
            f"超过当前安全上限 {limit:,} 像素"
        )


def unique_output_path(output_dir: Path, source: Path, percent: int) -> Path:
    suffix = source.suffix.lower()
    base_name = f"{source.stem}_{percent}pct"
    candidate = output_dir / f"{base_name}{suffix}"
    index = 2
    while candidate.exists():
        candidate = output_dir / f"{base_name}_{index}{suffix}"
        index += 1
    return candidate


def oriented_size(image: Image.Image) -> tuple[int, int]:
    width, height = image.size
    orientation = image.getexif().get(274)
    if orientation in {5, 6, 7, 8}:
        return height, width
    return width, height


def load_preview_image(path: Path, max_size: tuple[int, int]) -> tuple[Image.Image, tuple[int, int]]:
    with Image.open(path) as opened:
        original_size = oriented_size(opened)
        ensure_image_pixel_limit(original_size, MAX_PREVIEW_IMAGE_PIXELS, "预览")
        try:
            opened.draft("RGB", max_size)
        except Exception:
            logger.debug("当前格式不支持 draft 预缩放：%s", path, exc_info=True)

        preview = ImageOps.exif_transpose(opened)
        preview.thumbnail(max_size, Image.Resampling.LANCZOS)

        if preview.mode not in {"RGB", "RGBA"}:
            preview = preview.convert("RGBA")

        return preview.copy(), original_size


def resize_image_file(source: Path, output_dir: Path, percent: int) -> ResizeResult:
    if percent < 1 or percent > 100:
        raise ValueError("缩放比例需要在 1 到 100 之间")

    output_dir.mkdir(parents=True, exist_ok=True)
    target = unique_output_path(output_dir, source, percent)

    with Image.open(source) as opened:
        original_size = oriented_size(opened)
        ensure_image_pixel_limit(original_size, MAX_SAFE_IMAGE_PIXELS, "处理")
        original = ImageOps.exif_transpose(opened)
        original_size = original.size
        new_size = (
            max(1, math.floor(original.width * percent / 100)),
            max(1, math.floor(original.height * percent / 100)),
        )
        resized = original.resize(new_size, Image.Resampling.LANCZOS)

        save_kwargs: dict[str, object] = {}
        suffix = target.suffix.lower()
        if suffix in {".jpg", ".jpeg"}:
            resized = prepare_for_jpeg(resized)
            save_kwargs.update({"quality": 92, "optimize": True, "progressive": True})
        elif suffix == ".png":
            save_kwargs.update({"optimize": True})
        elif suffix == ".webp":
            save_kwargs.update({"quality": 92, "method": 6})

        resized.save(target, **save_kwargs)

    return ResizeResult(source, target, original_size, new_size)
