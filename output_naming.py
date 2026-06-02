from __future__ import annotations

import re
from string import Formatter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


DEFAULT_NAMING_TEMPLATE = "IMG_{date}_{time}_{index}_{operation}_{width}x{height}"
MAX_TEMPLATE_CHARS = 180
MAX_FILENAME_STEM_CHARS = 120
INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
RESERVED_WINDOWS_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}

TEMPLATE_FIELDS = (
    "name",
    "operation",
    "width",
    "height",
    "format",
    "ext",
    "date",
    "time",
    "index",
)
FORMATTER = Formatter()


@dataclass(frozen=True)
class OutputNaming:
    template: str = ""
    preserve_structure: bool = False
    source_root: Path | None = None
    sequence: int | None = None


def sanitize_filename_stem(value: str) -> str:
    cleaned = INVALID_FILENAME_CHARS.sub("_", value).strip(" .")
    cleaned = cleaned[:MAX_FILENAME_STEM_CHARS].rstrip(" .")
    if not cleaned:
        return "image"
    if cleaned.upper() in RESERVED_WINDOWS_NAMES:
        return f"{cleaned}_file"
    return cleaned


def output_directory_for_source(output_dir: Path, source: Path, naming: OutputNaming) -> Path:
    if not naming.preserve_structure or naming.source_root is None:
        return output_dir

    try:
        relative_parent = source.resolve().parent.relative_to(naming.source_root.resolve())
    except (OSError, ValueError):
        return output_dir

    if str(relative_parent) == ".":
        return output_dir
    return output_dir / relative_parent


def render_output_stem(
    source: Path,
    operation: str,
    target_format: str,
    size: tuple[int, int],
    naming: OutputNaming,
    legacy_stem: str,
) -> str:
    template = normalized_template(naming.template)
    if not template:
        return sanitize_filename_stem(legacy_stem)

    now = datetime.now()
    values = {
        "name": source.stem,
        "operation": operation,
        "width": str(size[0]),
        "height": str(size[1]),
        "format": target_format.lower(),
        "ext": source.suffix.lower().lstrip("."),
        "date": now.strftime("%Y%m%d"),
        "time": now.strftime("%H%M%S"),
        "index": "" if naming.sequence is None else str(naming.sequence),
    }
    validate_template_fields(template)
    try:
        rendered = template.format(**values)
    except ValueError as exc:
        raise ValueError(f"输出命名模板格式不正确：{exc}") from exc
    return sanitize_filename_stem(rendered)


def build_output_path(
    output_dir: Path,
    source: Path,
    suffix: str,
    operation: str,
    target_format: str,
    size: tuple[int, int],
    naming: OutputNaming | None,
    legacy_stem: str,
) -> Path:
    naming = naming or OutputNaming()
    directory = output_directory_for_source(output_dir, source, naming)
    stem = render_output_stem(source, operation, target_format, size, naming, legacy_stem)
    return directory / f"{stem}{suffix}"


def validate_output_template(template: str) -> None:
    template = normalized_template(template, allow_empty=True)
    if not template:
        return
    validate_template_fields(template)
    sample = OutputNaming(template=template, sequence=1)
    render_output_stem(
        Path("sample.jpg"),
        "50pct",
        "jpg",
        (800, 600),
        sample,
        "sample_50pct",
    )


def normalized_template(template: str, allow_empty: bool = True) -> str:
    template = str(template).strip()
    if not template and allow_empty:
        return ""
    if len(template) > MAX_TEMPLATE_CHARS:
        raise ValueError(f"输出命名模板过长，最多 {MAX_TEMPLATE_CHARS} 个字符")
    return template


def validate_template_fields(template: str) -> None:
    valid = "、".join(f"{{{field}}}" for field in TEMPLATE_FIELDS)
    try:
        parsed = list(FORMATTER.parse(template))
    except ValueError as exc:
        raise ValueError(f"输出命名模板格式不正确：{exc}") from exc

    for _literal, field_name, format_spec, conversion in parsed:
        if field_name is None:
            continue
        if field_name not in TEMPLATE_FIELDS:
            raise ValueError(f"输出命名模板包含未知占位符：{{{field_name}}}。可用：{valid}")
        if format_spec or conversion:
            raise ValueError("输出命名模板不支持格式修饰或转换标记，请只使用普通占位符")


def default_template_from_config(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        return DEFAULT_NAMING_TEMPLATE
    try:
        template = normalized_template(value, allow_empty=False)
        validate_template_fields(template)
        return template
    except ValueError:
        return DEFAULT_NAMING_TEMPLATE
