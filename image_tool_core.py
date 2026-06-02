from __future__ import annotations

import logging
import math
import warnings
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps

from image_conversion import copy_metadata_info, prepare_for_format, save_regular_image
from output_naming import OutputNaming, build_output_path
from output_safety import commit_temporary_output, remove_file_silently, temporary_output_path


MAX_SAFE_IMAGE_PIXELS = 150_000_000
MAX_PREVIEW_IMAGE_PIXELS = 80_000_000
MAX_INTERACTIVE_IMAGE_PIXELS = 80_000_000
MAX_IMPORTED_IMAGE_FILES = 10_000
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

RESIZE_FORMAT_ALIASES = {
    "jfif": "jpg",
    "dib": "bmp",
    "pgm": "ppm",
    "pbm": "ppm",
    "pnm": "ppm",
}

RESIZE_EXPORT_FORMATS = {
    "png",
    "jpg",
    "jpeg",
    "webp",
    "bmp",
    "tif",
    "tiff",
    "ico",
    "gif",
    "ppm",
    "tga",
}


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


def collect_image_paths(
    paths: list[Path],
    include_subfolders: bool,
    max_images: int | None = None,
) -> list[Path]:
    images: list[Path] = []

    def append_image(image_path: Path) -> None:
        if max_images is not None and len(images) >= max_images:
            raise ValueError(
                f"一次最多导入 {max_images:,} 张图片，请缩小文件夹范围或关闭子文件夹扫描。"
            )
        images.append(image_path)

    for path in paths:
        try:
            if path.is_dir():
                iterator = path.rglob("*") if include_subfolders else path.iterdir()
                for candidate in iterator:
                    if is_image_file(candidate):
                        append_image(candidate)
            elif is_image_file(path):
                append_image(path)
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


def resize_target_format_from_source(source: Path) -> str:
    suffix = source.suffix.lower().lstrip(".")
    target_format = RESIZE_FORMAT_ALIASES.get(suffix, suffix)
    if target_format not in RESIZE_EXPORT_FORMATS:
        return "png"
    return target_format


def resize_target_suffix(source: Path) -> str:
    target_format = resize_target_format_from_source(source)
    source_suffix = source.suffix.lower()
    if target_format == source_suffix.lstrip("."):
        return source_suffix
    return f".{target_format}"


def unique_output_path(output_dir: Path, source: Path, percent: int) -> Path:
    suffix = resize_target_suffix(source)
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


def resize_image_file(
    source: Path,
    output_dir: Path,
    percent: int,
    naming: OutputNaming | None = None,
    keep_metadata: bool = False,
) -> ResizeResult:
    if percent < 1 or percent > 100:
        raise ValueError("缩放比例需要在 1 到 100 之间")

    target = output_dir / f"{source.stem}{resize_target_suffix(source)}"
    temp_target: Path | None = None

    try:
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

            target_format = resize_target_format_from_source(source)
            suffix = resize_target_suffix(source)
            target = build_output_path(
                output_dir,
                source,
                suffix,
                f"{percent}pct",
                target_format,
                new_size,
                naming,
                f"{source.stem}_{percent}pct",
            )
            temp_target = temporary_output_path(target)
            resized = prepare_for_format(resized, target_format)
            copy_metadata_info(resized, original)
            save_regular_image(resized, temp_target, target_format, keep_metadata)

        target = commit_temporary_output(temp_target, target)
    except Exception:
        if temp_target is not None:
            remove_file_silently(temp_target)
        raise

    return ResizeResult(source, target, original_size, new_size)
