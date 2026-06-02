from __future__ import annotations

from pathlib import Path
from uuid import uuid4


def temporary_output_path(target: Path) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    suffix = target.suffix or ".tmp"
    for _attempt in range(100):
        candidate = target.parent / f".{target.stem}.{uuid4().hex}.tmp{suffix}"
        if not candidate.exists():
            return candidate
    raise RuntimeError("无法创建临时输出文件名")


def available_output_path(target: Path) -> Path:
    if not target.exists():
        return target

    base_name = target.stem
    suffix = target.suffix
    for index in range(2, 10_000):
        candidate = target.with_name(f"{base_name}_{index}{suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError("无法创建不重名的输出文件名")


def commit_temporary_output(temp_path: Path, target: Path) -> Path:
    final_path = available_output_path(target)
    temp_path.replace(final_path)
    return final_path


def remove_file_silently(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
