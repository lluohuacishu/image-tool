from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps

from image_conversion import prepare_for_jpeg, source_format_group
from image_tool_core import MAX_SAFE_IMAGE_PIXELS, ensure_image_pixel_limit, oriented_size


COMPRESSION_LEVELS = ("轻度", "中度", "重度")

PRESET_SETTINGS = {
    "轻度": {
        "jpeg_quality": 92,
        "jpeg_subsampling": 0,
        "webp_quality": 88,
        "png_level": 6,
    },
    "中度": {
        "jpeg_quality": 84,
        "jpeg_subsampling": 1,
        "webp_quality": 80,
        "png_level": 8,
    },
    "重度": {
        "jpeg_quality": 74,
        "jpeg_subsampling": 2,
        "webp_quality": 70,
        "png_level": 9,
    },
}


@dataclass(frozen=True)
class CompressionResult:
    source: Path
    target: Path
    level: str
    original_size: tuple[int, int]
    original_bytes: int
    output_bytes: int
    used_original_copy: bool

    @property
    def saved_percent(self) -> float:
        if self.original_bytes <= 0:
            return 0.0
        return (1 - self.output_bytes / self.original_bytes) * 100


def unique_compressed_path(output_dir: Path, source: Path, level: str) -> Path:
    suffix = source.suffix.lower() or ".png"
    base_name = f"{source.stem}_compressed_{level}"
    candidate = output_dir / f"{base_name}{suffix}"
    index = 2
    while candidate.exists():
        candidate = output_dir / f"{base_name}_{index}{suffix}"
        index += 1
    return candidate


def save_compressed_image(image: Image.Image, target: Path, source_group: str | None, level: str) -> None:
    settings = PRESET_SETTINGS[level]

    if source_group == "jpeg":
        image = prepare_for_jpeg(image)
        image.save(
            target,
            format="JPEG",
            quality=settings["jpeg_quality"],
            optimize=True,
            progressive=True,
            subsampling=settings["jpeg_subsampling"],
        )
    elif source_group == "webp":
        image.save(
            target,
            format="WEBP",
            quality=settings["webp_quality"],
            method=6,
        )
    elif source_group == "png":
        if image.mode not in {"RGB", "RGBA", "L", "LA", "P"}:
            image = image.convert("RGBA")
        image.save(
            target,
            format="PNG",
            optimize=True,
            compress_level=settings["png_level"],
        )
    elif source_group == "tiff":
        image.save(target, format="TIFF", compression="tiff_lzw")
    elif source_group == "gif":
        image.save(target, format="GIF", optimize=True)
    elif source_group == "ico":
        if image.mode not in {"RGB", "RGBA"}:
            image = image.convert("RGBA")
        image.save(
            target,
            format="ICO",
            sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)],
        )
    elif source_group == "bmp":
        image = prepare_for_jpeg(image)
        image.save(target, format="BMP")
    elif source_group == "ppm":
        image = prepare_for_jpeg(image)
        image.save(target, format="PPM")
    elif source_group == "tga":
        if image.mode not in {"RGB", "RGBA"}:
            image = image.convert("RGBA")
        image.save(target, format="TGA")
    else:
        raise ValueError("暂不支持该图片格式的体积压缩")


def compress_image_file(source: Path, output_dir: Path, level: str) -> CompressionResult:
    if level not in COMPRESSION_LEVELS:
        raise ValueError("压缩档位需要是：轻度、中度或重度")

    output_dir.mkdir(parents=True, exist_ok=True)
    target = unique_compressed_path(output_dir, source, level)
    original_bytes = source.stat().st_size
    source_group = source_format_group(source)

    with Image.open(source) as opened:
        original_size = oriented_size(opened)
        ensure_image_pixel_limit(original_size, MAX_SAFE_IMAGE_PIXELS, "体积压缩")
        image = ImageOps.exif_transpose(opened)
        original_size = image.size
        save_compressed_image(image, target, source_group, level)

    used_original_copy = False
    if target.stat().st_size > original_bytes:
        shutil.copy2(source, target)
        used_original_copy = True

    return CompressionResult(
        source=source,
        target=target,
        level=level,
        original_size=original_size,
        original_bytes=original_bytes,
        output_bytes=target.stat().st_size,
        used_original_copy=used_original_copy,
    )
