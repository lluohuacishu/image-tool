from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps


MAX_CONVERSION_IMAGE_PIXELS = 500_000_000
MAX_COE_IMAGE_PIXELS = 4_000_000

SUPPORTED_EXPORT_FORMATS = (
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
    "coe",
)

COE_PIXEL_FORMATS = ("rgb565", "rgb888")

TARGET_FORMAT_GROUPS = {
    "jpg": "jpeg",
    "jpeg": "jpeg",
    "png": "png",
    "webp": "webp",
    "bmp": "bmp",
    "tif": "tiff",
    "tiff": "tiff",
    "ico": "ico",
    "gif": "gif",
    "ppm": "ppm",
    "tga": "tga",
}

PIL_FORMAT_GROUPS = {
    "JPEG": "jpeg",
    "PNG": "png",
    "WEBP": "webp",
    "BMP": "bmp",
    "DIB": "bmp",
    "TIFF": "tiff",
    "ICO": "ico",
    "GIF": "gif",
    "PPM": "ppm",
    "TGA": "tga",
}

EXTENSION_FORMAT_GROUPS = {
    ".jpg": "jpeg",
    ".jpeg": "jpeg",
    ".jfif": "jpeg",
    ".png": "png",
    ".webp": "webp",
    ".bmp": "bmp",
    ".dib": "bmp",
    ".tif": "tiff",
    ".tiff": "tiff",
    ".ico": "ico",
    ".gif": "gif",
    ".ppm": "ppm",
    ".pgm": "ppm",
    ".pbm": "ppm",
    ".pnm": "ppm",
    ".tga": "tga",
}


@dataclass(frozen=True)
class ConvertResult:
    source: Path
    target: Path
    original_size: tuple[int, int]
    target_format: str


def source_format_group(source: Path) -> str | None:
    try:
        with Image.open(source) as image:
            if image.format:
                return PIL_FORMAT_GROUPS.get(image.format.upper())
    except Exception:
        return EXTENSION_FORMAT_GROUPS.get(source.suffix.lower())

    return EXTENSION_FORMAT_GROUPS.get(source.suffix.lower())


def is_same_target_format(source: Path, target_format: str) -> bool:
    target_format = target_format.lower()
    if target_format == "coe":
        return False

    source_group = source_format_group(source)
    target_group = TARGET_FORMAT_GROUPS.get(target_format)
    return source_group is not None and source_group == target_group


def same_target_format_sources(sources: list[Path], target_format: str) -> list[Path]:
    return [source for source in sources if is_same_target_format(source, target_format)]


def image_pixel_count(size: tuple[int, int]) -> int:
    return max(0, size[0]) * max(0, size[1])


def ensure_pixel_limit(size: tuple[int, int], limit: int, purpose: str) -> None:
    count = image_pixel_count(size)
    if count > limit:
        raise ValueError(
            f"{purpose}图片像素过大：{size[0]} x {size[1]}，"
            f"超过当前安全上限 {limit:,} 像素"
        )


def unique_conversion_path(output_dir: Path, source: Path, target_format: str) -> Path:
    suffix = f".{target_format.lower()}"
    if target_format.lower() == "jpg":
        suffix = ".jpg"

    source_suffix = source.suffix.lower()
    base_name = source.stem if source_suffix != suffix else f"{source.stem}_converted"
    candidate = output_dir / f"{base_name}{suffix}"
    index = 2
    while candidate.exists():
        candidate = output_dir / f"{base_name}_{index}{suffix}"
        index += 1
    return candidate


def prepare_for_jpeg(image: Image.Image) -> Image.Image:
    if image.mode in {"RGB", "L"}:
        return image.convert("RGB")

    background = Image.new("RGB", image.size, (255, 255, 255))
    if image.mode in {"RGBA", "LA"}:
        alpha = image.getchannel("A")
        background.paste(image.convert("RGBA"), mask=alpha)
        return background

    return image.convert("RGB")


def prepare_for_format(image: Image.Image, target_format: str) -> Image.Image:
    target_format = target_format.lower()
    if target_format in {"jpg", "jpeg"}:
        return prepare_for_jpeg(image)
    if target_format == "gif":
        return image.convert("P", palette=Image.Palette.ADAPTIVE)
    if target_format in {"png", "webp", "ico", "tif", "tiff"}:
        if image.mode in {"RGB", "RGBA", "L", "LA", "P"}:
            return image.copy()
        return image.convert("RGBA")
    if target_format in {"bmp", "ppm", "tga"}:
        if image.mode == "RGBA" and target_format == "tga":
            return image.copy()
        return prepare_for_jpeg(image)
    return image.copy()


def save_regular_image(image: Image.Image, target: Path, target_format: str) -> None:
    target_format = target_format.lower()
    save_kwargs: dict[str, object] = {}

    if target_format in {"jpg", "jpeg"}:
        save_format = "JPEG"
        save_kwargs.update({"quality": 92, "optimize": True, "progressive": True})
    elif target_format == "png":
        save_format = "PNG"
        save_kwargs.update({"optimize": True})
    elif target_format == "webp":
        save_format = "WEBP"
        save_kwargs.update({"quality": 92, "method": 6})
    elif target_format == "ico":
        save_format = "ICO"
        save_kwargs.update({"sizes": [(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)]})
    elif target_format in {"tif", "tiff"}:
        save_format = "TIFF"
    elif target_format == "bmp":
        save_format = "BMP"
    elif target_format == "gif":
        save_format = "GIF"
    elif target_format == "ppm":
        save_format = "PPM"
    elif target_format == "tga":
        save_format = "TGA"
    else:
        raise ValueError(f"暂不支持输出格式：{target_format}")

    image.save(target, format=save_format, **save_kwargs)


def rgb_to_rgb565_hex(red: int, green: int, blue: int) -> str:
    value = ((red & 0xF8) << 8) | ((green & 0xFC) << 3) | (blue >> 3)
    return f"{value:04X}"


def rgb_to_rgb888_hex(red: int, green: int, blue: int) -> str:
    return f"{red:02X}{green:02X}{blue:02X}"


def save_coe(image: Image.Image, target: Path, pixel_format: str) -> None:
    pixel_format = pixel_format.lower()
    if pixel_format not in COE_PIXEL_FORMATS:
        raise ValueError(f"暂不支持 COE 像素格式：{pixel_format}")
    ensure_pixel_limit(image.size, MAX_COE_IMAGE_PIXELS, "COE 导出")

    rgb = image.convert("RGB")
    converter = rgb_to_rgb565_hex if pixel_format == "rgb565" else rgb_to_rgb888_hex
    total_pixels = rgb.width * rgb.height

    with target.open("w", encoding="utf-8") as handle:
        handle.write("; Generated by image_tool_gui.py\n")
        handle.write(f"; width={rgb.width}\n")
        handle.write(f"; height={rgb.height}\n")
        handle.write(f"; pixel_format={pixel_format}\n")
        handle.write("memory_initialization_radix=16;\n")
        handle.write("memory_initialization_vector=\n")

        for index, (red, green, blue) in enumerate(rgb.getdata(), start=1):
            end = ";" if index == total_pixels else ","
            handle.write(f"{converter(red, green, blue)}{end}\n")


def convert_image_file(
    source: Path,
    output_dir: Path,
    target_format: str,
    coe_pixel_format: str = "rgb565",
) -> ConvertResult:
    target_format = target_format.lower()
    if target_format not in SUPPORTED_EXPORT_FORMATS:
        raise ValueError(f"暂不支持输出格式：{target_format}")

    output_dir.mkdir(parents=True, exist_ok=True)
    target = unique_conversion_path(output_dir, source, target_format)

    with Image.open(source) as opened:
        ensure_pixel_limit(opened.size, MAX_CONVERSION_IMAGE_PIXELS, "转换")
        image = ImageOps.exif_transpose(opened)
        original_size = image.size

        if target_format == "coe":
            save_coe(image, target, coe_pixel_format)
        else:
            prepared = prepare_for_format(image, target_format)
            save_regular_image(prepared, target, target_format)

    return ConvertResult(source, target, original_size, target_format)
