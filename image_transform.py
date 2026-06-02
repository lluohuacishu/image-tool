from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps

from image_conversion import SUPPORTED_EXPORT_FORMATS, copy_metadata_info, prepare_for_format, save_regular_image
from image_tool_core import MAX_SAFE_IMAGE_PIXELS, ensure_image_pixel_limit, oriented_size
from output_naming import OutputNaming, build_output_path
from output_safety import commit_temporary_output, remove_file_silently, temporary_output_path


SAVE_FORMAT_ALIASES = {
    "jfif": "jpg",
    "dib": "bmp",
    "pgm": "ppm",
    "pbm": "ppm",
    "pnm": "ppm",
}


@dataclass(frozen=True)
class TransformResult:
    source: Path
    target: Path
    original_size: tuple[int, int]
    output_size: tuple[int, int]
    rotation_degrees: int
    crop_box: tuple[int, int, int, int] | None


def target_format_from_source(source: Path) -> str:
    suffix = source.suffix.lower().lstrip(".") or "png"
    target_format = SAVE_FORMAT_ALIASES.get(suffix, suffix)
    if target_format not in SUPPORTED_EXPORT_FORMATS or target_format == "coe":
        return "png"
    return target_format


def unique_transform_path(output_dir: Path, source: Path) -> Path:
    target_format = target_format_from_source(source)
    suffix = source.suffix.lower() if target_format == source.suffix.lower().lstrip(".") else f".{target_format}"
    base_name = f"{source.stem}_edited"
    candidate = output_dir / f"{base_name}{suffix}"
    index = 2
    while candidate.exists():
        candidate = output_dir / f"{base_name}_{index}{suffix}"
        index += 1
    return candidate


def save_transformed_image(
    source: Path,
    output_dir: Path,
    rotation_degrees: int = 0,
    crop_box: tuple[int, int, int, int] | None = None,
    naming: OutputNaming | None = None,
    keep_metadata: bool = False,
) -> TransformResult:
    target = output_dir / f"{source.stem}{source.suffix.lower() or '.png'}"
    temp_target: Path | None = None
    target_format = target_format_from_source(source)

    try:
        with Image.open(source) as opened:
            original_size = oriented_size(opened)
            ensure_image_pixel_limit(original_size, MAX_SAFE_IMAGE_PIXELS, "裁切/旋转")
            image = ImageOps.exif_transpose(opened)
            original_size = image.size

            rotation_degrees %= 360
            if rotation_degrees:
                image = image.rotate(-rotation_degrees, expand=True)

            normalized_crop: tuple[int, int, int, int] | None = None
            if crop_box is not None:
                left, upper, right, lower = crop_box
                left = max(0, min(left, image.width - 1))
                upper = max(0, min(upper, image.height - 1))
                right = max(left + 1, min(right, image.width))
                lower = max(upper + 1, min(lower, image.height))
                normalized_crop = (left, upper, right, lower)
                image = image.crop(normalized_crop)

            prepared = prepare_for_format(image, target_format)
            copy_metadata_info(prepared, image)
            suffix = source.suffix.lower() if target_format == source.suffix.lower().lstrip(".") else f".{target_format}"
            target = build_output_path(
                output_dir,
                source,
                suffix,
                "edited",
                target_format,
                image.size,
                naming,
                f"{source.stem}_edited",
            )
            temp_target = temporary_output_path(target)
            save_regular_image(prepared, temp_target, target_format, keep_metadata)

        target = commit_temporary_output(temp_target, target)
    except Exception:
        if temp_target is not None:
            remove_file_silently(temp_target)
        raise

    return TransformResult(
        source=source,
        target=target,
        original_size=original_size,
        output_size=image.size,
        rotation_degrees=rotation_degrees,
        crop_box=normalized_crop,
    )
