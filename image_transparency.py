from __future__ import annotations

from collections import Counter, deque
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps

from output_naming import OutputNaming, build_output_path
from output_safety import commit_temporary_output, remove_file_silently, temporary_output_path


MAX_BACKGROUND_COLORS = 4
BACKGROUND_GROUP_MIN_SHARE = 0.05
MAX_TRANSPARENCY_IMAGE_PIXELS = 60_000_000
MIN_BACKGROUND_COMPONENT_AREA = 8
MAX_OPAQUE_ISLAND_AREA = 32
DEFAULT_EDGE_CLEANUP = "中度"


@dataclass(frozen=True)
class EdgeCleanupSettings:
    choke_radius: int
    fringe_threshold_multiplier: float
    fringe_threshold_extra: int
    smooth_passes: int
    smooth_color_neighbors: int
    smooth_always_neighbors: int
    edge_erosion_passes: int
    feather_radius: int
    feather_opacity_base: float
    feather_opacity_span: float
    decontamination_threshold_extra: int
    decontamination_min_alpha: float


EDGE_CLEANUP_SETTINGS = {
    "轻度": EdgeCleanupSettings(
        choke_radius=1,
        fringe_threshold_multiplier=1.45,
        fringe_threshold_extra=18,
        smooth_passes=0,
        smooth_color_neighbors=6,
        smooth_always_neighbors=8,
        edge_erosion_passes=0,
        feather_radius=1,
        feather_opacity_base=0.68,
        feather_opacity_span=0.24,
        decontamination_threshold_extra=85,
        decontamination_min_alpha=0.45,
    ),
    "中度": EdgeCleanupSettings(
        choke_radius=1,
        fringe_threshold_multiplier=1.8,
        fringe_threshold_extra=28,
        smooth_passes=1,
        smooth_color_neighbors=5,
        smooth_always_neighbors=7,
        edge_erosion_passes=0,
        feather_radius=2,
        feather_opacity_base=0.56,
        feather_opacity_span=0.38,
        decontamination_threshold_extra=115,
        decontamination_min_alpha=0.35,
    ),
    "强": EdgeCleanupSettings(
        choke_radius=2,
        fringe_threshold_multiplier=2.2,
        fringe_threshold_extra=42,
        smooth_passes=2,
        smooth_color_neighbors=4,
        smooth_always_neighbors=6,
        edge_erosion_passes=1,
        feather_radius=3,
        feather_opacity_base=0.42,
        feather_opacity_span=0.48,
        decontamination_threshold_extra=150,
        decontamination_min_alpha=0.28,
    ),
}
EDGE_CLEANUP_LEVELS = tuple(EDGE_CLEANUP_SETTINGS)


@dataclass(frozen=True)
class TransparencyResult:
    source: Path
    target: Path
    original_size: tuple[int, int]
    output_size: tuple[int, int]
    background_color: tuple[int, int, int]
    tolerance: int
    edge_cleanup: str


def resolve_edge_cleanup_settings(edge_cleanup: str) -> tuple[str, EdgeCleanupSettings]:
    if edge_cleanup in EDGE_CLEANUP_SETTINGS:
        return edge_cleanup, EDGE_CLEANUP_SETTINGS[edge_cleanup]
    return DEFAULT_EDGE_CLEANUP, EDGE_CLEANUP_SETTINGS[DEFAULT_EDGE_CLEANUP]


def unique_transparent_path(output_dir: Path, source: Path) -> Path:
    candidate = output_dir / f"{source.stem}_transparent.png"
    index = 2
    while candidate.exists():
        candidate = output_dir / f"{source.stem}_transparent_{index}.png"
        index += 1
    return candidate


def quantize_color(color: tuple[int, int, int], step: int = 16) -> tuple[int, int, int]:
    return tuple(min(255, max(0, round(channel / step) * step)) for channel in color)


def sampled_edge_pixels(image: Image.Image) -> list[tuple[int, int, int]]:
    rgba = image.convert("RGBA")
    width, height = rgba.size
    if width == 0 or height == 0:
        return []

    step = max(1, max(width, height) // 240)
    samples: list[tuple[int, int, int]] = []
    pixels = rgba.load()

    for x in range(0, width, step):
        for y in (0, height - 1):
            red, green, blue, alpha = pixels[x, y]
            if alpha > 16:
                samples.append((red, green, blue))

    for y in range(0, height, step):
        for x in (0, width - 1):
            red, green, blue, alpha = pixels[x, y]
            if alpha > 16:
                samples.append((red, green, blue))

    return samples


def estimate_background_color(image: Image.Image) -> tuple[int, int, int]:
    return estimate_background_colors(image)[0]


def estimate_background_colors(image: Image.Image) -> list[tuple[int, int, int]]:
    samples = sampled_edge_pixels(image)
    if not samples:
        return [(255, 255, 255)]

    groups = Counter(quantize_color(sample) for sample in samples)
    minimum_count = max(3, round(len(samples) * BACKGROUND_GROUP_MIN_SHARE))
    colors: list[tuple[int, int, int]] = []

    for group, count in groups.most_common(MAX_BACKGROUND_COLORS * 2):
        if count < minimum_count and colors:
            continue
        members = [sample for sample in samples if quantize_color(sample) == group]
        red = round(sum(sample[0] for sample in members) / len(members))
        green = round(sum(sample[1] for sample in members) / len(members))
        blue = round(sum(sample[2] for sample in members) / len(members))
        color = (red, green, blue)
        if any(color_distance(color, existing) < 10 for existing in colors):
            continue
        colors.append(color)
        if len(colors) >= MAX_BACKGROUND_COLORS:
            break

    return colors or [(255, 255, 255)]


def color_distance(left: tuple[int, int, int], right: tuple[int, int, int]) -> float:
    return sum((left[index] - right[index]) ** 2 for index in range(3)) ** 0.5


def ensure_transparency_pixel_limit(size: tuple[int, int]) -> None:
    count = max(0, size[0]) * max(0, size[1])
    if count > MAX_TRANSPARENCY_IMAGE_PIXELS:
        raise ValueError(
            f"透明背景图片像素过大：{size[0]} x {size[1]}，"
            f"超过当前安全上限 {MAX_TRANSPARENCY_IMAGE_PIXELS:,} 像素"
        )


def nearest_background_color(
    color: tuple[int, int, int],
    backgrounds: list[tuple[int, int, int]],
) -> tuple[int, int, int]:
    return min(backgrounds, key=lambda background: color_distance(color, background))


def background_threshold(tolerance: int) -> float:
    tolerance = max(1, min(tolerance, 100))
    return tolerance * 3.0


def is_connected_background_pixel(
    pixel: tuple[int, int, int, int],
    backgrounds: list[tuple[int, int, int]],
    threshold: float,
) -> bool:
    red, green, blue, alpha = pixel
    if alpha <= 16:
        return True
    color = (red, green, blue)
    return any(color_distance(color, background) <= threshold for background in backgrounds)


def edge_points(width: int, height: int) -> list[tuple[int, int]]:
    if width <= 0 or height <= 0:
        return []

    points: list[tuple[int, int]] = []
    for x in range(width):
        points.append((x, 0))
        if height > 1:
            points.append((x, height - 1))
    for y in range(1, max(1, height - 1)):
        points.append((0, y))
        if width > 1:
            points.append((width - 1, y))
    return points


def connected_background_mask(
    image: Image.Image,
    backgrounds: list[tuple[int, int, int]],
    tolerance: int,
) -> bytearray:
    width, height = image.size
    mask = bytearray(width * height)
    if width <= 0 or height <= 0:
        return mask

    threshold = background_threshold(tolerance)
    pixels = image.load()
    queue: deque[tuple[int, int]] = deque()

    def try_enqueue(x: int, y: int) -> None:
        index = y * width + x
        if mask[index]:
            return
        if is_connected_background_pixel(pixels[x, y], backgrounds, threshold):
            mask[index] = 1
            queue.append((x, y))

    for x, y in edge_points(width, height):
        try_enqueue(x, y)

    while queue:
        x, y = queue.popleft()
        if x > 0:
            try_enqueue(x - 1, y)
        if x < width - 1:
            try_enqueue(x + 1, y)
        if y > 0:
            try_enqueue(x, y - 1)
        if y < height - 1:
            try_enqueue(x, y + 1)

    return mask


def neighbor_indices(index: int, width: int, height: int) -> tuple[int, ...]:
    x = index % width
    y = index // width
    neighbors: list[int] = []
    if x > 0:
        neighbors.append(index - 1)
    if x < width - 1:
        neighbors.append(index + 1)
    if y > 0:
        neighbors.append(index - width)
    if y < height - 1:
        neighbors.append(index + width)
    return tuple(neighbors)


def neighbor_indices8(index: int, width: int, height: int) -> tuple[int, ...]:
    x = index % width
    y = index // width
    neighbors: list[int] = []
    for dy in (-1, 0, 1):
        ny = y + dy
        if ny < 0 or ny >= height:
            continue
        for dx in (-1, 0, 1):
            if dx == 0 and dy == 0:
                continue
            nx = x + dx
            if 0 <= nx < width:
                neighbors.append(ny * width + nx)
    return tuple(neighbors)


def is_edge_index(index: int, width: int, height: int) -> bool:
    x = index % width
    y = index // width
    return x == 0 or y == 0 or x == width - 1 or y == height - 1


def cleanup_small_background_components(mask: bytearray, width: int, height: int) -> None:
    total = width * height
    min_area = max(MIN_BACKGROUND_COMPONENT_AREA, total // 60_000)
    visited = bytearray(total)

    for start in range(total):
        if visited[start] or not mask[start]:
            continue

        queue: deque[int] = deque([start])
        visited[start] = 1
        component: list[int] = []
        keep_component = False

        while queue:
            index = queue.popleft()
            if not keep_component:
                component.append(index)
                if len(component) > min_area:
                    keep_component = True
                    component.clear()
            for neighbor in neighbor_indices(index, width, height):
                if not visited[neighbor] and mask[neighbor]:
                    visited[neighbor] = 1
                    queue.append(neighbor)

        if not keep_component:
            for index in component:
                mask[index] = 0


def fill_small_opaque_islands(mask: bytearray, width: int, height: int) -> None:
    total = width * height
    max_area = max(MAX_OPAQUE_ISLAND_AREA, total // 30_000)
    visited = bytearray(total)

    for start in range(total):
        if visited[start] or mask[start]:
            continue

        queue: deque[int] = deque([start])
        visited[start] = 1
        component: list[int] = []
        touches_edge = False
        too_large = False

        while queue:
            index = queue.popleft()
            touches_edge = touches_edge or is_edge_index(index, width, height)
            if not too_large:
                component.append(index)
                if len(component) > max_area:
                    too_large = True
                    component.clear()
            for neighbor in neighbor_indices(index, width, height):
                if not visited[neighbor] and not mask[neighbor]:
                    visited[neighbor] = 1
                    queue.append(neighbor)

        if not touches_edge and not too_large:
            for index in component:
                mask[index] = 1


def cleanup_mask(mask: bytearray, width: int, height: int) -> None:
    cleanup_small_background_components(mask, width, height)
    fill_small_opaque_islands(mask, width, height)


def is_background_fringe_pixel(
    pixel: tuple[int, int, int, int],
    backgrounds: list[tuple[int, int, int]],
    threshold: float,
) -> bool:
    red, green, blue, alpha = pixel
    if alpha <= 16:
        return True
    color = (red, green, blue)
    return any(color_distance(color, background) <= threshold for background in backgrounds)


def choke_background_fringe(
    mask: bytearray,
    image: Image.Image,
    backgrounds: list[tuple[int, int, int]],
    tolerance: int,
    settings: EdgeCleanupSettings,
) -> None:
    if settings.choke_radius <= 0:
        return

    width, height = image.size
    total = width * height
    pixels = image.load()
    threshold = (
        background_threshold(tolerance) * settings.fringe_threshold_multiplier
        + settings.fringe_threshold_extra
    )

    for _step in range(settings.choke_radius):
        additions = bytearray(total)
        changed = False
        for index in range(total):
            if mask[index]:
                continue
            if not any(mask[neighbor] for neighbor in neighbor_indices8(index, width, height)):
                continue
            x = index % width
            y = index // width
            if is_background_fringe_pixel(pixels[x, y], backgrounds, threshold):
                additions[index] = 1
                changed = True

        if not changed:
            break
        for index, value in enumerate(additions):
            if value:
                mask[index] = 1


def smooth_background_edges(
    mask: bytearray,
    image: Image.Image,
    backgrounds: list[tuple[int, int, int]],
    tolerance: int,
    settings: EdgeCleanupSettings,
) -> None:
    if settings.smooth_passes <= 0:
        return

    width, height = image.size
    total = width * height
    pixels = image.load()
    threshold = (
        background_threshold(tolerance) * settings.fringe_threshold_multiplier
        + settings.fringe_threshold_extra
    )

    for _step in range(settings.smooth_passes):
        additions = bytearray(total)
        changed = False
        for index in range(total):
            if mask[index]:
                continue
            background_neighbors = sum(mask[neighbor] for neighbor in neighbor_indices8(index, width, height))
            if background_neighbors >= settings.smooth_always_neighbors:
                additions[index] = 1
                changed = True
                continue
            if background_neighbors < settings.smooth_color_neighbors:
                continue
            x = index % width
            y = index // width
            if is_background_fringe_pixel(pixels[x, y], backgrounds, threshold):
                additions[index] = 1
                changed = True

        if not changed:
            break
        for index, value in enumerate(additions):
            if value:
                mask[index] = 1


def erode_foreground_edge(mask: bytearray, width: int, height: int, passes: int) -> None:
    if passes <= 0:
        return

    total = width * height
    for _step in range(passes):
        additions = bytearray(total)
        changed = False
        for index in range(total):
            if mask[index]:
                continue
            if any(mask[neighbor] for neighbor in neighbor_indices8(index, width, height)):
                additions[index] = 1
                changed = True

        if not changed:
            break
        for index, value in enumerate(additions):
            if value:
                mask[index] = 1


def feather_distances(mask: bytearray, width: int, height: int, radius: int) -> bytearray:
    distances = bytearray(width * height)
    queue: deque[int] = deque()

    for index, value in enumerate(mask):
        if value:
            continue
        if any(mask[neighbor] for neighbor in neighbor_indices8(index, width, height)):
            distances[index] = 1
            queue.append(index)

    while queue:
        index = queue.popleft()
        if distances[index] >= radius:
            continue
        for neighbor in neighbor_indices8(index, width, height):
            if mask[neighbor] or distances[neighbor]:
                continue
            distances[neighbor] = distances[index] + 1
            queue.append(neighbor)

    return distances


def feathered_alpha(original_alpha: int, distance: int, settings: EdgeCleanupSettings) -> int:
    if distance <= 0:
        return original_alpha
    opacity = settings.feather_opacity_base + settings.feather_opacity_span * (
        distance / (settings.feather_radius + 1)
    )
    return min(original_alpha, round(original_alpha * opacity))


def decontaminate_edge_color(
    color: tuple[int, int, int],
    background: tuple[int, int, int],
    alpha: int,
    threshold: float,
    settings: EdgeCleanupSettings,
) -> tuple[int, int, int]:
    distance = color_distance(color, background)
    if distance > threshold:
        return color

    normalized_alpha = max(settings.decontamination_min_alpha, alpha / 255)
    strength = min(1.0, max(0.0, (threshold - distance) / threshold))
    corrected: list[int] = []

    for channel, background_channel in zip(color, background):
        foreground = (channel - background_channel * (1 - normalized_alpha)) / normalized_alpha
        foreground = min(255, max(0, round(foreground)))
        corrected.append(round(channel * (1 - strength) + foreground * strength))

    return tuple(corrected)


def make_background_transparent(
    image: Image.Image,
    tolerance: int,
    edge_cleanup: str = DEFAULT_EDGE_CLEANUP,
) -> tuple[Image.Image, tuple[int, int, int]]:
    ensure_transparency_pixel_limit(image.size)
    rgba = image.convert("RGBA")
    _cleanup_name, settings = resolve_edge_cleanup_settings(edge_cleanup)
    backgrounds = estimate_background_colors(rgba)
    mask = connected_background_mask(rgba, backgrounds, tolerance)

    result = rgba.copy()
    pixels = result.load()
    width, height = result.size
    cleanup_mask(mask, width, height)
    choke_background_fringe(mask, rgba, backgrounds, tolerance, settings)
    smooth_background_edges(mask, rgba, backgrounds, tolerance, settings)
    erode_foreground_edge(mask, width, height, settings.edge_erosion_passes)
    cleanup_mask(mask, width, height)
    distances = feather_distances(mask, width, height, settings.feather_radius)
    decontamination_threshold = (
        background_threshold(tolerance) + settings.decontamination_threshold_extra
    )

    for y in range(height):
        for x in range(width):
            index = y * width + x
            if mask[index]:
                red, green, blue, _alpha = pixels[x, y]
                pixels[x, y] = (red, green, blue, 0)
            elif distances[index]:
                red, green, blue, alpha = pixels[x, y]
                new_alpha = feathered_alpha(alpha, distances[index], settings)
                color = (red, green, blue)
                background = nearest_background_color(color, backgrounds)
                color = decontaminate_edge_color(
                    color,
                    background,
                    new_alpha,
                    decontamination_threshold,
                    settings,
                )
                pixels[x, y] = (
                    color[0],
                    color[1],
                    color[2],
                    new_alpha,
                )
    return result, backgrounds[0]


def create_transparent_image_file(
    source: Path,
    output_dir: Path,
    tolerance: int,
    edge_cleanup: str = DEFAULT_EDGE_CLEANUP,
    naming: OutputNaming | None = None,
    keep_metadata: bool = False,
) -> TransparencyResult:
    if tolerance < 1 or tolerance > 100:
        raise ValueError("透明背景容差需要在 1 到 100 之间")

    target = output_dir / f"{source.stem}_transparent.png"
    temp_target: Path | None = None
    cleanup_name, _settings = resolve_edge_cleanup_settings(edge_cleanup)

    try:
        with Image.open(source) as opened:
            ensure_transparency_pixel_limit(opened.size)
            image = ImageOps.exif_transpose(opened)
            original_size = image.size
            transparent, background = make_background_transparent(image, tolerance, cleanup_name)
            target = build_output_path(
                output_dir,
                source,
                ".png",
                "transparent",
                "png",
                transparent.size,
                naming,
                f"{source.stem}_transparent",
            )
            temp_target = temporary_output_path(target)
            save_kwargs: dict[str, object] = {"optimize": True}
            if keep_metadata:
                exif = image.getexif()
                if exif:
                    save_kwargs["exif"] = exif.tobytes()
                for key in ("icc_profile", "dpi"):
                    if key in image.info:
                        save_kwargs[key] = image.info[key]
            transparent.save(temp_target, format="PNG", **save_kwargs)

        target = commit_temporary_output(temp_target, target)
    except Exception:
        if temp_target is not None:
            remove_file_silently(temp_target)
        raise

    return TransparencyResult(
        source=source,
        target=target,
        original_size=original_size,
        output_size=transparent.size,
        background_color=background,
        tolerance=tolerance,
        edge_cleanup=cleanup_name,
    )
