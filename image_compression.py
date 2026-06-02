from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps

from image_conversion import metadata_save_kwargs, prepare_for_jpeg, source_format_group
from image_tool_core import MAX_SAFE_IMAGE_PIXELS, ensure_image_pixel_limit, oriented_size
from output_naming import OutputNaming, build_output_path
from output_safety import commit_temporary_output, remove_file_silently, temporary_output_path


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
    target_bytes: int | None = None
    target_size_reached: bool = True

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


def save_compressed_image(
    image: Image.Image,
    target: Path,
    source_group: str | None,
    level: str,
    keep_metadata: bool = False,
) -> None:
    settings = PRESET_SETTINGS[level]

    if source_group == "jpeg":
        save_kwargs = metadata_save_kwargs(image, keep_metadata, "jpeg")
        image = prepare_for_jpeg(image)
        image.save(
            target,
            format="JPEG",
            quality=settings["jpeg_quality"],
            optimize=True,
            progressive=True,
            subsampling=settings["jpeg_subsampling"],
            **save_kwargs,
        )
    elif source_group == "webp":
        save_kwargs = metadata_save_kwargs(image, keep_metadata, "webp")
        image.save(
            target,
            format="WEBP",
            quality=settings["webp_quality"],
            method=6,
            **save_kwargs,
        )
    elif source_group == "png":
        save_kwargs = metadata_save_kwargs(image, keep_metadata, "png")
        if image.mode not in {"RGB", "RGBA", "L", "LA", "P"}:
            image = image.convert("RGBA")
        image.save(
            target,
            format="PNG",
            optimize=True,
            compress_level=settings["png_level"],
            **save_kwargs,
        )
    elif source_group == "tiff":
        save_kwargs = metadata_save_kwargs(image, keep_metadata, "tiff")
        image.save(target, format="TIFF", compression="tiff_lzw", **save_kwargs)
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


def save_image_to_target_size(
    image: Image.Image,
    target: Path,
    source_group: str | None,
    level: str,
    target_bytes: int,
    keep_metadata: bool = False,
) -> bool:
    if source_group == "jpeg":
        save_kwargs = metadata_save_kwargs(image, keep_metadata, "jpeg")
        prepared = prepare_for_jpeg(image)
        best_reached = False
        for quality in range(95, 19, -5):
            prepared.save(
                target,
                format="JPEG",
                quality=quality,
                optimize=True,
                progressive=True,
                subsampling=2 if quality <= 85 else 1,
                **save_kwargs,
            )
            if target.stat().st_size <= target_bytes:
                best_reached = True
                break
        return best_reached

    if source_group == "webp":
        save_kwargs = metadata_save_kwargs(image, keep_metadata, "webp")
        best_reached = False
        for quality in range(95, 14, -5):
            image.save(
                target,
                format="WEBP",
                quality=quality,
                method=6,
                **save_kwargs,
            )
            if target.stat().st_size <= target_bytes:
                best_reached = True
                break
        return best_reached

    save_compressed_image(image, target, source_group, level, keep_metadata)
    return target.stat().st_size <= target_bytes


def compress_image_file(
    source: Path,
    output_dir: Path,
    level: str,
    target_bytes: int | None = None,
    naming: OutputNaming | None = None,
    keep_metadata: bool = False,
) -> CompressionResult:
    if level not in COMPRESSION_LEVELS:
        raise ValueError("压缩档位需要是：轻度、中度或重度")
    if target_bytes is not None and target_bytes <= 0:
        raise ValueError("目标体积需要大于 0 KB")

    target = output_dir / f"{source.stem}{source.suffix.lower() or '.png'}"
    temp_target: Path | None = None
    original_bytes = source.stat().st_size
    source_group = source_format_group(source)
    original_size = (0, 0)
    used_original_copy = False
    target_size_reached = True

    try:
        with Image.open(source) as opened:
            original_size = oriented_size(opened)
            ensure_image_pixel_limit(original_size, MAX_SAFE_IMAGE_PIXELS, "体积压缩")
            image = ImageOps.exif_transpose(opened)
            original_size = image.size
            suffix = source.suffix.lower() or ".png"
            if target_bytes is None:
                operation = f"compressed_{level}"
                legacy_stem = f"{source.stem}_compressed_{level}"
            else:
                operation = f"target_{max(1, round(target_bytes / 1024))}KB"
                legacy_stem = f"{source.stem}_{operation}"
            target = build_output_path(
                output_dir,
                source,
                suffix,
                operation,
                suffix.lstrip("."),
                image.size,
                naming,
                legacy_stem,
            )
            temp_target = temporary_output_path(target)
            if target_bytes is None:
                save_compressed_image(image, temp_target, source_group, level, keep_metadata)
            else:
                target_size_reached = save_image_to_target_size(
                    image,
                    temp_target,
                    source_group,
                    level,
                    target_bytes,
                    keep_metadata,
                )

        if target_bytes is None and temp_target.stat().st_size > original_bytes:
            shutil.copy2(source, temp_target)
            used_original_copy = True

        target = commit_temporary_output(temp_target, target)
    except Exception:
        if temp_target is not None:
            remove_file_silently(temp_target)
        raise

    return CompressionResult(
        source=source,
        target=target,
        level=level,
        original_size=original_size,
        original_bytes=original_bytes,
        output_bytes=target.stat().st_size,
        used_original_copy=used_original_copy,
        target_bytes=target_bytes,
        target_size_reached=target_size_reached,
    )
