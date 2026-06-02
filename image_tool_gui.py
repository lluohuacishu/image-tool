from __future__ import annotations

import json
import logging
import math
import os
import sys
import threading
import tkinter as tk
import tkinter.font as tkfont
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageTk

from crop_rotate_editor import CropRotateEditor
from image_compression import COMPRESSION_LEVELS, compress_image_file
from image_conversion import (
    COE_PIXEL_FORMATS,
    SUPPORTED_EXPORT_FORMATS,
    convert_image_file,
    same_target_format_sources,
)
from image_tool_core import (
    MAX_IMPORTED_IMAGE_FILES,
    clamp,
    collect_image_paths,
    is_image_file,
    load_preview_image,
    middle_ellipsis,
    oriented_size,
    readable_error,
    resize_image_file,
)
from output_naming import OutputNaming, validate_output_template
from image_transparency import EDGE_CLEANUP_LEVELS, create_transparent_image_file


def application_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def fallback_data_dir() -> Path:
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / "ImageTool"
    return Path.home() / ".image_tool"


def writable_data_dir(preferred: Path) -> Path:
    try:
        preferred.mkdir(parents=True, exist_ok=True)
        test_file = preferred / ".write_test"
        test_file.write_text("ok", encoding="utf-8")
        test_file.unlink(missing_ok=True)
        return preferred
    except Exception:
        fallback = fallback_data_dir()
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


APP_DIR = application_dir()
APP_DATA_DIR = writable_data_dir(fallback_data_dir())
CONFIG_PATH = APP_DATA_DIR / "image_tool_settings.json"
LEGACY_CONFIG_PATH = APP_DIR / "image_tool_settings.json"
LOG_FILENAME = "image_tool_gui.log"
DEFAULT_LOG_DIR = APP_DATA_DIR
LOG_PATH = DEFAULT_LOG_DIR / LOG_FILENAME
PREVIEW_PADDING = 28
MIN_WINDOW_SIZE = (1220, 760)
MAX_START_SIZE = (1500, 900)
CHECKED_MARK = "☑"
UNCHECKED_MARK = "☐"

logger = logging.getLogger("image_tool_gui")
logger.setLevel(logging.INFO)
logger.propagate = False


def configure_logging(log_dir: Path) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / LOG_FILENAME
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    new_handler = logging.FileHandler(log_path, encoding="utf-8")
    new_handler.setFormatter(formatter)

    for handler in list(logger.handlers):
        handler.close()
        logger.removeHandler(handler)

    logger.addHandler(new_handler)
    return log_path


try:
    LOG_PATH = configure_logging(DEFAULT_LOG_DIR)
except Exception:
    LOG_PATH = DEFAULT_LOG_DIR / LOG_FILENAME


def ensure_directory_writable(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    test_file = directory / ".image_tool_write_test"
    test_file.write_text("ok", encoding="utf-8")
    test_file.unlink(missing_ok=True)


def same_path(left: Path, right: Path) -> bool:
    try:
        return left.resolve() == right.resolve()
    except OSError:
        return left.absolute() == right.absolute()


def should_migrate_legacy_log_dir(directory_text: object) -> bool:
    if not isinstance(directory_text, str) or not directory_text.strip():
        return True
    return same_path(Path(directory_text).expanduser(), APP_DIR)


def load_config() -> dict[str, object]:
    candidates = [CONFIG_PATH]
    if not same_path(LEGACY_CONFIG_PATH, CONFIG_PATH):
        candidates.append(LEGACY_CONFIG_PATH)

    for path in candidates:
        if not path.exists():
            continue
        try:
            config = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            logger.exception("读取设置失败：%s", path)
            continue
        if not isinstance(config, dict):
            logger.warning("忽略无效设置文件：%s", path)
            continue
        if should_migrate_legacy_log_dir(config.get("log_dir")):
            config["log_dir"] = str(DEFAULT_LOG_DIR)
        return config

    return {}


def save_config(config: dict[str, object]) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(
        json.dumps(config, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


class ImageToolApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("图片处理工具")
        self.configure_window_size()

        self.config = load_config()
        self.files: list[Path] = []
        self.checked_paths: set[Path] = set()
        self.source_roots: dict[Path, Path] = {}
        self.output_dir = tk.StringVar(value=str(self.config.get("output_dir", "")))
        self.naming_template = tk.StringVar(value=str(self.config.get("naming_template", "")))
        self.preserve_structure = tk.BooleanVar(
            value=bool(self.config.get("preserve_structure", False))
        )
        self.keep_metadata = tk.BooleanVar(value=bool(self.config.get("keep_metadata", False)))
        self.percent = tk.IntVar(value=50)
        self.include_subfolders = tk.BooleanVar(
            value=bool(self.config.get("include_subfolders", False))
        )
        self.log_dir = tk.StringVar(
            value=str(self.config.get("log_dir", str(DEFAULT_LOG_DIR)))
        )
        self.operation_mode = tk.StringVar(value=str(self.config.get("operation_mode", "resize")))
        if self.operation_mode.get() not in {"resize", "compress", "convert", "transform", "transparent"}:
            self.operation_mode.set("resize")
        self.compression_level = tk.StringVar(value=str(self.config.get("compression_level", "中度")))
        if self.compression_level.get() not in COMPRESSION_LEVELS:
            self.compression_level.set("中度")
        try:
            target_size_kb = int(self.config.get("target_size_kb", 0))
        except (TypeError, ValueError):
            target_size_kb = 0
        self.target_size_kb = tk.IntVar(value=max(0, min(target_size_kb, 1_000_000)))
        try:
            transparency_tolerance = int(self.config.get("transparency_tolerance", 10))
        except (TypeError, ValueError):
            transparency_tolerance = 10
        transparency_tolerance = clamp(transparency_tolerance, 1, 100)
        self.transparency_tolerance = tk.IntVar(value=transparency_tolerance)
        self.transparency_edge_cleanup = tk.StringVar(
            value=str(self.config.get("transparency_edge_cleanup", "中度"))
        )
        if self.transparency_edge_cleanup.get() not in EDGE_CLEANUP_LEVELS:
            self.transparency_edge_cleanup.set("中度")
        self.target_format = tk.StringVar(value=str(self.config.get("target_format", "png")))
        if self.target_format.get() not in SUPPORTED_EXPORT_FORMATS:
            self.target_format.set("png")
        self.coe_pixel_format = tk.StringVar(
            value=str(self.config.get("coe_pixel_format", COE_PIXEL_FORMATS[0]))
        )
        if self.coe_pixel_format.get() not in COE_PIXEL_FORMATS:
            self.coe_pixel_format.set(COE_PIXEL_FORMATS[0])
        self.select_all_var = tk.BooleanVar(value=False)
        self.status_text = tk.StringVar(value="请选择图片开始")
        self.preview_image: ImageTk.PhotoImage | None = None
        self.preview_after_id: str | None = None
        self.selected_image_size: tuple[int, int] | None = None
        self.processing = False
        self.scanning = False
        self.closing = False
        self.cancel_requested = threading.Event()
        self.updating_select_all = False

        self.colors = {
            "bg": "#f5f1ea",
            "panel": "#fffaf2",
            "panel_alt": "#f7efe4",
            "text": "#223236",
            "muted": "#697579",
            "accent": "#1f8a84",
            "accent_dark": "#14645f",
            "line": "#dccfbd",
            "warn": "#c77532",
            "white": "#ffffff",
        }

        self.setup_style()
        if not self.apply_log_directory(self.log_dir.get(), announce=False):
            self.log_dir.set(str(DEFAULT_LOG_DIR))
            self.apply_log_directory(self.log_dir.get(), announce=False)
        self.build_layout()
        self.refresh_operation_ui()
        self.bind_events()
        self.output_dir.trace_add("write", lambda *_args: self.update_output_summary())
        self.operation_mode.trace_add("write", lambda *_args: self.refresh_operation_ui())
        self.target_format.trace_add("write", lambda *_args: self.refresh_operation_ui())
        self.compression_level.trace_add("write", lambda *_args: self.refresh_operation_ui())
        self.transparency_tolerance.trace_add("write", lambda *_args: self.on_transparency_tolerance_changed())
        self.root.report_callback_exception = self.report_callback_exception
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.write_log(f"日志文件：{LOG_PATH}")

    def configure_window_size(self) -> None:
        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()
        available_width = max(760, screen_width - 80)
        available_height = max(560, screen_height - 100)

        width = min(
            clamp(int(screen_width * 0.88), MIN_WINDOW_SIZE[0], MAX_START_SIZE[0]),
            available_width,
        )
        height = min(
            clamp(int(screen_height * 0.84), MIN_WINDOW_SIZE[1], MAX_START_SIZE[1]),
            available_height,
        )
        x = max(0, (screen_width - width) // 2)
        y = max(0, (screen_height - height) // 2)

        self.root.geometry(f"{width}x{height}+{x}+{y}")
        self.root.minsize(min(MIN_WINDOW_SIZE[0], available_width), min(MIN_WINDOW_SIZE[1], available_height))

    def setup_style(self) -> None:
        style = ttk.Style(self.root)
        style.theme_use("clam")

        self.font_family = self.choose_font_family()
        font_main = (self.font_family, 10)
        font_title = (self.font_family, 20, "bold")
        font_section = (self.font_family, 12, "bold")

        self.root.configure(bg=self.colors["bg"])
        self.root.option_add("*Font", font_main)

        style.configure(".", font=font_main)
        style.configure("App.TFrame", background=self.colors["bg"])
        style.configure("Panel.TFrame", background=self.colors["panel"])
        style.configure("AltPanel.TFrame", background=self.colors["panel_alt"])
        style.configure(
            "Title.TLabel",
            background=self.colors["bg"],
            foreground=self.colors["text"],
            font=font_title,
        )
        style.configure(
            "Subtitle.TLabel",
            background=self.colors["bg"],
            foreground=self.colors["muted"],
        )
        style.configure(
            "PanelTitle.TLabel",
            background=self.colors["panel"],
            foreground=self.colors["text"],
            font=font_section,
        )
        style.configure(
            "PanelText.TLabel",
            background=self.colors["panel"],
            foreground=self.colors["text"],
        )
        style.configure(
            "Muted.TLabel",
            background=self.colors["panel"],
            foreground=self.colors["muted"],
        )
        style.configure(
            "AltMuted.TLabel",
            background=self.colors["panel_alt"],
            foreground=self.colors["muted"],
        )
        style.configure(
            "Accent.TButton",
            background=self.colors["accent"],
            foreground=self.colors["white"],
            borderwidth=0,
            focusthickness=0,
            padding=(14, 8),
        )
        style.map(
            "Accent.TButton",
            background=[("active", self.colors["accent_dark"]), ("disabled", "#8eb9b6")],
        )
        style.configure(
            "Soft.TButton",
            background=self.colors["panel_alt"],
            foreground=self.colors["text"],
            borderwidth=1,
            relief="flat",
            padding=(12, 7),
        )
        style.map("Soft.TButton", background=[("active", "#eadcca")])
        style.configure(
            "TCheckbutton",
            background=self.colors["panel"],
            foreground=self.colors["text"],
        )
        style.configure(
            "Horizontal.TProgressbar",
            troughcolor="#eadfce",
            background=self.colors["accent"],
            bordercolor="#eadfce",
            lightcolor=self.colors["accent"],
            darkcolor=self.colors["accent"],
        )
        style.configure(
            "Horizontal.TScale",
            background=self.colors["panel"],
            troughcolor="#e5d9c8",
        )
        style.configure(
            "File.Treeview",
            background=self.colors["white"],
            fieldbackground=self.colors["white"],
            foreground=self.colors["text"],
            rowheight=30,
            borderwidth=0,
        )
        style.configure(
            "File.Treeview.Heading",
            background=self.colors["panel_alt"],
            foreground=self.colors["text"],
            font=(self.font_family, 10, "bold"),
        )
        style.map(
            "File.Treeview",
            background=[("selected", self.colors["accent"])],
            foreground=[("selected", self.colors["white"])],
        )

    def choose_font_family(self) -> str:
        preferred = (
            "Microsoft YaHei UI",
            "Microsoft YaHei",
            "SimHei",
            "Segoe UI",
            "Arial",
            "Tahoma",
            "TkDefaultFont",
        )
        try:
            available = set(tkfont.families(self.root))
        except tk.TclError:
            return "TkDefaultFont"

        for family in preferred:
            if family in available:
                return family
        return "TkDefaultFont"

    def build_layout(self) -> None:
        outer = ttk.Frame(self.root, style="App.TFrame", padding=22)
        outer.pack(fill=tk.BOTH, expand=True)
        outer.columnconfigure(0, weight=9, minsize=560)
        outer.columnconfigure(1, weight=8, minsize=520)
        outer.rowconfigure(1, weight=1)

        header = ttk.Frame(outer, style="App.TFrame")
        header.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 16))
        header.columnconfigure(0, weight=1)

        ttk.Label(header, text="图片处理工具", style="Title.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        ttk.Button(
            header,
            text="⚙ 设置",
            style="Soft.TButton",
            command=self.open_settings,
        ).grid(row=0, column=1, sticky="e", rowspan=2)
        ttk.Label(
            header,
            text="支持缩小像素、压缩体积、格式转换、COE 导出、裁切/旋转，以及实验性透明背景。",
            style="Subtitle.TLabel",
        ).grid(row=1, column=0, sticky="w", pady=(4, 0))

        left = ttk.Frame(outer, style="Panel.TFrame", padding=18)
        left.grid(row=1, column=0, sticky="nsew", padx=(0, 16))
        left.columnconfigure(0, weight=1)
        left.rowconfigure(2, weight=1)

        self.build_source_section(left)
        self.build_scale_section(left)
        self.build_file_list(left)
        self.build_actions(left)

        right = ttk.Frame(outer, style="Panel.TFrame", padding=18)
        right.grid(row=1, column=1, sticky="nsew")
        right.columnconfigure(0, weight=1)
        right.rowconfigure(1, weight=4, minsize=340)
        right.rowconfigure(4, weight=2, minsize=180)

        self.build_preview(right)
        self.build_log(right)

    def build_source_section(self, parent: ttk.Frame) -> None:
        top = ttk.Frame(parent, style="Panel.TFrame")
        top.grid(row=0, column=0, sticky="ew")
        top.columnconfigure(0, weight=1)

        ttk.Label(top, text="图片来源", style="PanelTitle.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        self.file_count_label = ttk.Label(top, text="0 张图片", style="Muted.TLabel")
        self.file_count_label.grid(row=0, column=1, sticky="e")

        buttons = ttk.Frame(parent, style="Panel.TFrame")
        buttons.grid(row=1, column=0, sticky="ew", pady=(12, 14))
        for index in range(3):
            buttons.columnconfigure(index, weight=1)

        ttk.Button(
            buttons,
            text="添加图片",
            style="Accent.TButton",
            command=self.add_images,
        ).grid(row=0, column=0, sticky="ew", padx=(0, 8))
        ttk.Button(
            buttons,
            text="添加文件夹",
            style="Soft.TButton",
            command=self.add_folder,
        ).grid(row=0, column=1, sticky="ew", padx=8)
        ttk.Button(
            buttons,
            text="清空",
            style="Soft.TButton",
            command=self.clear_files,
        ).grid(row=0, column=2, sticky="ew", padx=(8, 0))

    def build_scale_section(self, parent: ttk.Frame) -> None:
        scale_box = ttk.Frame(parent, style="AltPanel.TFrame", padding=14)
        scale_box.grid(row=3, column=0, sticky="ew", pady=(14, 0))
        scale_box.columnconfigure(0, weight=1)

        scale_header = ttk.Frame(scale_box, style="AltPanel.TFrame")
        scale_header.grid(row=0, column=0, sticky="ew")
        scale_header.columnconfigure(0, weight=1)

        ttk.Label(
            scale_header,
            text="处理功能",
            style="AltMuted.TLabel",
            font=(self.font_family, 11, "bold"),
        ).grid(row=0, column=0, sticky="w")

        mode_row = ttk.Frame(scale_box, style="AltPanel.TFrame")
        mode_row.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        mode_row.columnconfigure(0, weight=1)
        mode_row.columnconfigure(1, weight=1)
        mode_row.columnconfigure(2, weight=1)
        mode_row.columnconfigure(3, weight=1)
        mode_row.columnconfigure(4, weight=1)

        ttk.Radiobutton(
            mode_row,
            text="像素压缩",
            value="resize",
            variable=self.operation_mode,
        ).grid(row=0, column=0, sticky="w")
        ttk.Radiobutton(
            mode_row,
            text="格式转换",
            value="convert",
            variable=self.operation_mode,
        ).grid(row=0, column=1, sticky="w")
        ttk.Radiobutton(
            mode_row,
            text="体积压缩",
            value="compress",
            variable=self.operation_mode,
        ).grid(row=0, column=2, sticky="w")
        ttk.Radiobutton(
            mode_row,
            text="裁切旋转",
            value="transform",
            variable=self.operation_mode,
        ).grid(row=0, column=3, sticky="w")
        ttk.Radiobutton(
            mode_row,
            text="透明背景",
            value="transparent",
            variable=self.operation_mode,
        ).grid(row=0, column=4, sticky="w")

        self.resize_controls = ttk.Frame(scale_box, style="AltPanel.TFrame")
        self.resize_controls.grid(row=2, column=0, sticky="ew", pady=(12, 0))
        self.resize_controls.columnconfigure(0, weight=1)

        resize_header = ttk.Frame(self.resize_controls, style="AltPanel.TFrame")
        resize_header.grid(row=0, column=0, sticky="ew")
        resize_header.columnconfigure(0, weight=1)
        ttk.Label(
            resize_header,
            text="缩放比例",
            style="AltMuted.TLabel",
            font=(self.font_family, 10, "bold"),
        ).grid(row=0, column=0, sticky="w")
        self.percent_label = ttk.Label(
            resize_header,
            text="50%",
            style="AltMuted.TLabel",
            font=(self.font_family, 18, "bold"),
        )
        self.percent_label.grid(row=0, column=1, sticky="e")

        scale_row = ttk.Frame(self.resize_controls, style="AltPanel.TFrame")
        scale_row.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        scale_row.columnconfigure(0, weight=1)

        self.scale = ttk.Scale(
            scale_row,
            from_=1,
            to=100,
            orient=tk.HORIZONTAL,
            command=self.on_scale_changed,
        )
        self.scale.grid(row=0, column=0, sticky="ew", padx=(0, 12))

        self.percent_spin = ttk.Spinbox(
            scale_row,
            from_=1,
            to=100,
            width=6,
            textvariable=self.percent,
            command=self.on_percent_changed,
            justify="center",
        )
        self.percent_spin.grid(row=0, column=1, sticky="e")

        self.estimated_label = ttk.Label(
            self.resize_controls,
            text="选择图片后显示预计尺寸",
            style="AltMuted.TLabel",
        )
        self.estimated_label.grid(row=2, column=0, sticky="w", pady=(10, 0))
        self.scale.set(self.percent.get())

        self.convert_controls = ttk.Frame(scale_box, style="AltPanel.TFrame")
        self.convert_controls.grid(row=3, column=0, sticky="ew", pady=(12, 0))
        self.convert_controls.columnconfigure(1, weight=1)

        ttk.Label(
            self.convert_controls,
            text="输出格式",
            style="AltMuted.TLabel",
            font=(self.font_family, 10, "bold"),
        ).grid(row=0, column=0, sticky="w", padx=(0, 12))
        self.target_format_box = ttk.Combobox(
            self.convert_controls,
            textvariable=self.target_format,
            values=SUPPORTED_EXPORT_FORMATS,
            state="readonly",
            width=14,
        )
        self.target_format_box.grid(row=0, column=1, sticky="w")

        self.coe_format_row = ttk.Frame(self.convert_controls, style="AltPanel.TFrame")
        self.coe_format_row.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        ttk.Label(
            self.coe_format_row,
            text="COE 像素格式",
            style="AltMuted.TLabel",
        ).grid(row=0, column=0, sticky="w", padx=(0, 12))
        ttk.Combobox(
            self.coe_format_row,
            textvariable=self.coe_pixel_format,
            values=COE_PIXEL_FORMATS,
            state="readonly",
            width=14,
        ).grid(row=0, column=1, sticky="w")
        self.coe_note_label = ttk.Label(
            self.convert_controls,
            text="COE 将导出为 Xilinx 初始化文本。",
            style="AltMuted.TLabel",
        )
        self.coe_note_label.grid(row=2, column=0, columnspan=2, sticky="w", pady=(10, 0))

        self.compress_controls = ttk.Frame(scale_box, style="AltPanel.TFrame")
        self.compress_controls.grid(row=4, column=0, sticky="ew", pady=(12, 0))
        self.compress_controls.columnconfigure(1, weight=1)
        ttk.Label(
            self.compress_controls,
            text="压缩档位",
            style="AltMuted.TLabel",
            font=(self.font_family, 10, "bold"),
        ).grid(row=0, column=0, sticky="w", padx=(0, 12))
        ttk.Combobox(
            self.compress_controls,
            textvariable=self.compression_level,
            values=COMPRESSION_LEVELS,
            state="readonly",
            width=14,
        ).grid(row=0, column=1, sticky="w")

        target_size_row = ttk.Frame(self.compress_controls, style="AltPanel.TFrame")
        target_size_row.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        target_size_row.columnconfigure(1, weight=1)
        ttk.Label(
            target_size_row,
            text="目标体积 KB",
            style="AltMuted.TLabel",
            font=(self.font_family, 10, "bold"),
        ).grid(row=0, column=0, sticky="w", padx=(0, 12))
        self.target_size_spin = ttk.Spinbox(
            target_size_row,
            from_=0,
            to=1000000,
            width=12,
            textvariable=self.target_size_kb,
            justify="center",
        )
        self.target_size_spin.grid(row=0, column=1, sticky="w")
        self.compression_note_label = ttk.Label(
            self.compress_controls,
            text="目标体积填 0 时按档位压缩；填入 KB 后，JPEG/WebP 会自动调整质量，其他格式会尽量无损优化。",
            style="AltMuted.TLabel",
        )
        self.compression_note_label.grid(row=2, column=0, columnspan=2, sticky="w", pady=(10, 0))

        self.transform_controls = ttk.Frame(scale_box, style="AltPanel.TFrame")
        self.transform_controls.grid(row=5, column=0, sticky="ew", pady=(12, 0))
        ttk.Label(
            self.transform_controls,
            text="打开编辑器后，用鼠标拖框裁切，或直接旋转后保存。只编辑当前选中的一张图片。",
            style="AltMuted.TLabel",
        ).grid(row=0, column=0, sticky="w")

        self.transparent_controls = ttk.Frame(scale_box, style="AltPanel.TFrame")
        self.transparent_controls.grid(row=6, column=0, sticky="ew", pady=(12, 0))
        self.transparent_controls.columnconfigure(0, weight=1)

        transparent_header = ttk.Frame(self.transparent_controls, style="AltPanel.TFrame")
        transparent_header.grid(row=0, column=0, sticky="ew")
        transparent_header.columnconfigure(0, weight=1)
        ttk.Label(
            transparent_header,
            text="背景容差",
            style="AltMuted.TLabel",
            font=(self.font_family, 10, "bold"),
        ).grid(row=0, column=0, sticky="w")
        self.transparency_label = ttk.Label(
            transparent_header,
            text=f"{self.transparency_tolerance.get()}",
            style="AltMuted.TLabel",
            font=(self.font_family, 18, "bold"),
        )
        self.transparency_label.grid(row=0, column=1, sticky="e")

        transparent_row = ttk.Frame(self.transparent_controls, style="AltPanel.TFrame")
        transparent_row.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        transparent_row.columnconfigure(0, weight=1)
        self.transparency_scale = ttk.Scale(
            transparent_row,
            from_=1,
            to=100,
            orient=tk.HORIZONTAL,
            command=self.on_transparency_scale_changed,
        )
        self.transparency_scale.grid(row=0, column=0, sticky="ew", padx=(0, 12))
        self.transparency_spin = ttk.Spinbox(
            transparent_row,
            from_=1,
            to=100,
            width=6,
            textvariable=self.transparency_tolerance,
            command=self.on_transparency_tolerance_changed,
            justify="center",
        )
        self.transparency_spin.grid(row=0, column=1, sticky="e")
        self.transparency_scale.set(self.transparency_tolerance.get())

        cleanup_row = ttk.Frame(self.transparent_controls, style="AltPanel.TFrame")
        cleanup_row.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        cleanup_row.columnconfigure(1, weight=1)
        ttk.Label(
            cleanup_row,
            text="边缘净化",
            style="AltMuted.TLabel",
            font=(self.font_family, 10, "bold"),
        ).grid(row=0, column=0, sticky="w", padx=(0, 12))
        ttk.Combobox(
            cleanup_row,
            textvariable=self.transparency_edge_cleanup,
            values=EDGE_CLEANUP_LEVELS,
            state="readonly",
            width=12,
        ).grid(row=0, column=1, sticky="w")

        ttk.Label(
            self.transparent_controls,
            text="实验性：从图片边缘识别多种连通背景色，并做小区域清理、可调去毛刺和边缘去白边；强档会轻微收缩外轮廓，主体本身接近背景色时可能吃掉浅色边缘；输出为 PNG。",
            style="AltMuted.TLabel",
            wraplength=720,
        ).grid(row=3, column=0, sticky="w", pady=(10, 0))

        self.refresh_operation_ui()

    def build_file_list(self, parent: ttk.Frame) -> None:
        list_frame = ttk.Frame(parent, style="Panel.TFrame")
        list_frame.grid(row=2, column=0, sticky="nsew")
        list_frame.columnconfigure(0, weight=1)
        list_frame.rowconfigure(1, weight=1)

        select_bar = ttk.Frame(list_frame, style="Panel.TFrame")
        select_bar.grid(row=0, column=0, sticky="ew", columnspan=2, pady=(0, 6))
        select_bar.columnconfigure(0, minsize=64)
        select_bar.columnconfigure(1, weight=1)
        ttk.Checkbutton(
            select_bar,
            text="全选",
            variable=self.select_all_var,
            command=self.on_select_all_changed,
        ).grid(row=0, column=0, sticky="w")
        self.drop_hint_label = ttk.Label(
            select_bar,
            text="通过“添加图片”或“添加文件夹”导入",
            style="Muted.TLabel",
        )
        self.drop_hint_label.grid(row=0, column=1, sticky="e")

        self.file_list = ttk.Treeview(
            list_frame,
            columns=("checked", "name", "folder"),
            show="headings",
            selectmode="browse",
            style="File.Treeview",
        )
        self.file_list.heading("checked", text="勾选")
        self.file_list.heading("name", text="文件名")
        self.file_list.heading("folder", text="所在文件夹")
        self.file_list.column("checked", width=64, minwidth=58, stretch=False, anchor=tk.CENTER)
        self.file_list.column("name", width=520, minwidth=360, stretch=False, anchor=tk.W)
        self.file_list.column("folder", width=440, minwidth=280, stretch=True, anchor=tk.W)
        self.file_list.grid(row=1, column=0, sticky="nsew")

        scroll_y = ttk.Scrollbar(list_frame, orient=tk.VERTICAL, command=self.file_list.yview)
        scroll_y.grid(row=1, column=1, sticky="ns")
        scroll_x = ttk.Scrollbar(list_frame, orient=tk.HORIZONTAL, command=self.file_list.xview)
        scroll_x.grid(row=2, column=0, sticky="ew")
        self.file_list.configure(yscrollcommand=scroll_y.set, xscrollcommand=scroll_x.set)

        self.file_menu = tk.Menu(self.root, tearoff=False)
        self.file_menu.add_command(label="勾选", command=self.context_toggle_file)
        self.file_menu.add_separator()
        self.file_menu.add_command(label="删除", command=self.context_delete_file)
        self.context_iid: str | None = None

    def build_actions(self, parent: ttk.Frame) -> None:
        action_bar = ttk.Frame(parent, style="Panel.TFrame")
        action_bar.grid(row=4, column=0, sticky="ew", pady=(16, 0))
        action_bar.columnconfigure(0, weight=1)
        action_bar.columnconfigure(1, weight=1)
        action_bar.columnconfigure(2, weight=1)

        self.output_summary_label = ttk.Label(
            action_bar,
            text="输出：未设置",
            style="Muted.TLabel",
        )
        self.output_summary_label.grid(row=0, column=0, sticky="w", columnspan=3, pady=(0, 8))

        self.progress = ttk.Progressbar(
            action_bar,
            orient=tk.HORIZONTAL,
            mode="determinate",
            style="Horizontal.TProgressbar",
        )
        self.progress.grid(row=1, column=0, sticky="ew", columnspan=3, pady=(0, 12))

        self.start_button = ttk.Button(
            action_bar,
            text="开始压缩",
            style="Accent.TButton",
            command=self.start_processing,
        )
        self.start_button.grid(row=2, column=0, sticky="ew", padx=(0, 8))

        self.cancel_button = ttk.Button(
            action_bar,
            text="取消任务",
            style="Soft.TButton",
            command=self.cancel_processing,
            state=tk.DISABLED,
        )
        self.cancel_button.grid(row=2, column=1, sticky="ew", padx=8)

        ttk.Button(
            action_bar,
            text="打开输出目录",
            style="Soft.TButton",
            command=self.open_output_dir,
        ).grid(row=2, column=2, sticky="ew", padx=(8, 0))
        self.update_output_summary()

    def build_preview(self, parent: ttk.Frame) -> None:
        preview_top = ttk.Frame(parent, style="Panel.TFrame")
        preview_top.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        preview_top.columnconfigure(0, weight=1)

        ttk.Label(preview_top, text="预览", style="PanelTitle.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        ttk.Label(preview_top, textvariable=self.status_text, style="Muted.TLabel").grid(
            row=1, column=0, sticky="w", pady=(4, 0)
        )

        self.preview_canvas = tk.Canvas(
            parent,
            height=360,
            bd=0,
            highlightthickness=1,
            highlightbackground=self.colors["line"],
            bg=self.colors["white"],
        )
        self.preview_canvas.grid(row=1, column=0, sticky="nsew")

        self.preview_info = ttk.Label(
            parent,
            text="暂无图片",
            style="Muted.TLabel",
            justify=tk.LEFT,
        )
        self.preview_info.grid(row=2, column=0, sticky="ew", pady=(12, 16))

    def build_log(self, parent: ttk.Frame) -> None:
        ttk.Label(parent, text="处理记录", style="PanelTitle.TLabel").grid(
            row=3, column=0, sticky="w"
        )

        log_frame = ttk.Frame(parent, style="Panel.TFrame")
        log_frame.grid(row=4, column=0, sticky="nsew", pady=(10, 0))
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)

        self.log = tk.Text(
            log_frame,
            height=9,
            wrap=tk.WORD,
            bd=0,
            highlightthickness=1,
            highlightbackground=self.colors["line"],
            bg=self.colors["white"],
            fg=self.colors["text"],
            insertbackground=self.colors["text"],
        )
        self.log.grid(row=0, column=0, sticky="nsew")

        scroll = ttk.Scrollbar(log_frame, orient=tk.VERTICAL, command=self.log.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.log.configure(yscrollcommand=scroll.set)

    def bind_events(self) -> None:
        self.file_list.bind("<<TreeviewSelect>>", lambda _event: self.schedule_preview_update())
        self.file_list.bind("<Button-1>", self.on_file_list_click)
        self.file_list.bind("<Button-3>", self.show_file_context_menu)
        self.preview_canvas.bind("<Configure>", lambda _event: self.schedule_preview_update(140))
        self.percent_spin.bind("<KeyRelease>", lambda _event: self.on_percent_changed())

    def on_file_list_click(self, event: tk.Event) -> str | None:
        iid = self.file_list.identify_row(event.y)
        column = self.file_list.identify_column(event.x)
        if not iid:
            return None

        self.file_list.selection_set(iid)
        self.file_list.focus(iid)
        if column == "#1":
            self.toggle_file_check(iid)
            self.schedule_preview_update()
            return "break"
        return None

    def show_file_context_menu(self, event: tk.Event) -> str | None:
        iid = self.file_list.identify_row(event.y)
        if not iid:
            return None

        self.context_iid = iid
        self.file_list.selection_set(iid)
        self.file_list.focus(iid)
        path = self.path_from_iid(iid)
        if path is None:
            return None

        label = "取消勾选" if path in self.checked_paths else "勾选"
        self.file_menu.entryconfigure(0, label=label)
        try:
            self.file_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.file_menu.grab_release()
        return "break"

    def context_toggle_file(self) -> None:
        if self.context_iid is not None:
            self.toggle_file_check(self.context_iid)

    def context_delete_file(self) -> None:
        if self.context_iid is not None:
            self.delete_file_iid(self.context_iid)

    def add_images(self) -> None:
        paths = filedialog.askopenfilenames(
            title="选择要处理的图片",
            filetypes=[
                (
                    "图片文件",
                    "*.jpg *.jpeg *.jfif *.png *.webp *.bmp *.dib *.tif *.tiff "
                    "*.ico *.gif *.ppm *.pgm *.pbm *.pnm *.tga *.pcx *.dds",
                ),
                ("所有文件", "*.*"),
            ],
        )
        if paths:
            self.add_paths([Path(path) for path in paths])

    def add_folder(self) -> None:
        folder = filedialog.askdirectory(title="选择图片文件夹")
        if not folder:
            return

        self.add_import_paths([Path(folder)], source="文件夹")

    def add_import_paths(self, paths: list[Path], source: str = "导入") -> None:
        if self.scanning:
            messagebox.showinfo("正在扫描", "正在扫描文件夹，请稍等。")
            return
        if not paths:
            return

        include_subfolders = self.include_subfolders.get()
        self.scanning = True
        self.status_text.set("正在扫描图片...")
        self.write_log(f"开始扫描{source}。")

        thread = threading.Thread(
            target=self.process_import_paths,
            args=(paths, include_subfolders, source),
            daemon=True,
        )
        try:
            thread.start()
        except Exception as exc:
            self.scanning = False
            self.status_text.set("扫描启动失败")
            self.write_log(f"扫描启动失败：{readable_error(exc)}")
            logger.exception("扫描启动失败")
            messagebox.showerror("扫描启动失败", readable_error(exc))

    def process_import_paths(self, paths: list[Path], include_subfolders: bool, source: str) -> None:
        image_paths: list[Path] = []
        root_map: dict[Path, Path] = {}
        error_text: str | None = None
        try:
            image_paths = collect_image_paths(
                paths,
                include_subfolders,
                max_images=MAX_IMPORTED_IMAGE_FILES,
            )
            root_map = self.build_source_root_map(image_paths, paths)
        except Exception as exc:
            error_text = readable_error(exc)
            logger.exception("扫描图片失败")

        self.run_on_ui_thread(self.on_import_scan_finished, image_paths, source, root_map, error_text)

    def build_source_root_map(self, image_paths: list[Path], import_paths: list[Path]) -> dict[Path, Path]:
        roots: list[Path] = []
        for path in import_paths:
            try:
                roots.append(path.resolve() if path.is_dir() else path.resolve().parent)
            except OSError:
                continue

        root_map: dict[Path, Path] = {}
        for image_path in image_paths:
            try:
                resolved = image_path.resolve()
            except OSError:
                continue
            for root in roots:
                try:
                    resolved.relative_to(root)
                except ValueError:
                    continue
                root_map[resolved] = root
                break
            else:
                root_map[resolved] = resolved.parent
        return root_map

    def on_import_scan_finished(
        self,
        image_paths: list[Path],
        source: str,
        root_map: dict[Path, Path] | None = None,
        error_text: str | None = None,
    ) -> None:
        self.scanning = False
        if error_text:
            self.status_text.set("扫描失败")
            self.write_log(f"{source}扫描失败：{error_text}")
            messagebox.showerror("扫描失败", error_text)
            return

        if not image_paths:
            self.write_log(f"{source}未找到可支持的图片文件。")
            self.status_text.set("未找到可支持的图片")
            messagebox.showinfo("没有找到图片", "没有找到可支持的图片文件。")
            return

        before = len(self.files)
        self.add_paths(image_paths, root_map)
        added = len(self.files) - before
        skipped = len(image_paths) - added
        self.status_text.set(f"扫描完成：新增 {added} 张")
        if skipped > 0:
            self.write_log(f"{source}完成：新增 {added} 张，跳过重复 {skipped} 张。")

    def add_paths(self, paths: list[Path], root_map: dict[Path, Path] | None = None) -> None:
        before = len(self.files)
        known = {path.resolve() for path in self.files}
        add_as_checked = self.select_all_var.get()
        for path in paths:
            try:
                if not is_image_file(path):
                    continue
                resolved = path.resolve()
                if resolved in known:
                    continue
                self.files.append(resolved)
                if root_map and resolved in root_map:
                    self.source_roots[resolved] = root_map[resolved]
                else:
                    self.source_roots[resolved] = resolved.parent
                if add_as_checked:
                    self.checked_paths.add(resolved)
                known.add(resolved)
            except OSError:
                logger.warning("添加图片失败：%s", path, exc_info=True)

        if self.files and not self.output_dir.get():
            self.output_dir.set(str(self.files[0].parent / "compressed_output"))

        added = len(self.files) - before
        self.refresh_file_list()
        self.write_log(f"已添加 {added} 张图片，请勾选后再处理。")
        self.schedule_preview_update()

    def clear_files(self) -> None:
        self.cancel_preview_update()
        self.files.clear()
        self.checked_paths.clear()
        self.source_roots.clear()
        self.set_select_all_value(False)
        self.selected_image_size = None
        self.preview_image = None
        self.refresh_file_list()
        self.preview_canvas.delete("all")
        self.preview_info.configure(text="暂无图片")
        self.estimated_label.configure(text="选择图片后显示预计尺寸")
        self.status_text.set("请选择图片开始")
        self.write_log("已清空图片列表。")

    def refresh_file_list(self) -> None:
        selected_path = self.selected_preview_path()
        children = self.file_list.get_children()
        if children:
            self.file_list.delete(*children)

        for index, path in enumerate(self.files):
            checked = CHECKED_MARK if path in self.checked_paths else UNCHECKED_MARK
            self.file_list.insert(
                "",
                tk.END,
                iid=str(index),
                values=(checked, path.name, str(path.parent)),
            )

        checked_count = len(self.selected_files())
        total = len(self.files)
        self.file_count_label.configure(text=f"{checked_count}/{total} 已勾选")
        self.sync_select_all_checkbox()

        if not self.files:
            return

        selected_index = self.files.index(selected_path) if selected_path in self.files else 0
        iid = str(selected_index)
        self.file_list.selection_set(iid)
        self.file_list.focus(iid)

    def path_from_iid(self, iid: str) -> Path | None:
        try:
            index = int(iid)
        except ValueError:
            return None
        if index < 0 or index >= len(self.files):
            return None
        return self.files[index]

    def selected_files(self) -> list[Path]:
        return [path for path in self.files if path in self.checked_paths]

    def output_naming_for(self, source: Path, sequence: int | None = None) -> OutputNaming:
        return OutputNaming(
            template=self.naming_template.get().strip(),
            preserve_structure=self.preserve_structure.get(),
            source_root=self.source_roots.get(source, source.parent),
            sequence=sequence,
        )

    def toggle_file_check(self, iid: str) -> None:
        path = self.path_from_iid(iid)
        if path is None:
            return

        if path in self.checked_paths:
            self.checked_paths.remove(path)
        else:
            self.checked_paths.add(path)

        self.file_list.set(
            iid,
            "checked",
            CHECKED_MARK if path in self.checked_paths else UNCHECKED_MARK,
        )
        self.file_count_label.configure(text=f"{len(self.selected_files())}/{len(self.files)} 已勾选")
        self.sync_select_all_checkbox()

    def delete_file_iid(self, iid: str) -> None:
        path = self.path_from_iid(iid)
        if path is None:
            return

        self.files.remove(path)
        self.checked_paths.discard(path)
        self.source_roots.pop(path, None)
        self.selected_image_size = None
        self.refresh_file_list()
        self.write_log(f"已从列表删除：{path.name}")

        if self.files:
            self.schedule_preview_update()
        else:
            self.preview_canvas.delete("all")
            self.preview_info.configure(text="暂无图片")
            self.status_text.set("请选择图片开始")

    def on_select_all_changed(self) -> None:
        if self.updating_select_all:
            return

        if self.select_all_var.get():
            self.checked_paths = set(self.files)
        else:
            self.checked_paths.clear()
        self.refresh_file_list()

    def set_select_all_value(self, value: bool) -> None:
        self.updating_select_all = True
        self.select_all_var.set(value)
        self.updating_select_all = False

    def sync_select_all_checkbox(self) -> None:
        self.set_select_all_value(bool(self.files) and len(self.checked_paths) == len(self.files))

    def update_output_summary(self) -> None:
        if not hasattr(self, "output_summary_label"):
            return

        directory = self.output_dir.get().strip()
        text = f"输出：{middle_ellipsis(directory, 76)}" if directory else "输出：未设置"
        self.output_summary_label.configure(text=text)

    def validated_output_dir(self) -> Path | None:
        directory_text = self.output_dir.get().strip()
        if not directory_text:
            messagebox.showwarning("缺少输出目录", "请选择输出目录。")
            return None

        directory = Path(directory_text).expanduser()
        try:
            ensure_directory_writable(directory)
        except Exception as exc:
            logger.exception("输出目录不可用：%s", directory)
            messagebox.showerror(
                "输出目录不可用",
                f"无法写入输出目录：\n{directory}\n\n{readable_error(exc)}",
            )
            return None
        return directory

    def save_current_config(self, announce: bool = False) -> bool:
        try:
            save_config(
                {
                    "output_dir": self.output_dir.get().strip(),
                    "naming_template": self.naming_template.get().strip(),
                    "preserve_structure": self.preserve_structure.get(),
                    "keep_metadata": self.keep_metadata.get(),
                    "include_subfolders": self.include_subfolders.get(),
                    "log_dir": self.log_dir.get().strip(),
                    "operation_mode": self.operation_mode.get(),
                    "compression_level": self.compression_level.get(),
                    "target_size_kb": self.target_size_kb.get(),
                    "transparency_tolerance": self.transparency_tolerance.get(),
                    "transparency_edge_cleanup": self.transparency_edge_cleanup.get(),
                    "target_format": self.target_format.get(),
                    "coe_pixel_format": self.coe_pixel_format.get(),
                }
            )
        except Exception as exc:
            logger.exception("保存设置失败：%s", CONFIG_PATH)
            if announce:
                messagebox.showerror("保存设置失败", readable_error(exc))
            return False
        return True

    def apply_log_directory(self, directory_text: str, announce: bool = True) -> bool:
        global LOG_PATH

        directory = Path(directory_text.strip() or DEFAULT_LOG_DIR).expanduser()
        try:
            LOG_PATH = configure_logging(directory)
        except Exception as exc:
            logger.exception("设置日志目录失败：%s", directory)
            if announce:
                messagebox.showerror("日志目录不可用", str(exc))
            return False

        normalized = str(LOG_PATH.parent)
        if self.log_dir.get() != normalized:
            self.log_dir.set(normalized)
        if announce:
            self.write_log(f"日志目录已设置：{LOG_PATH.parent}")
        return True

    def open_settings(self) -> None:
        dialog = tk.Toplevel(self.root)
        dialog.title("设置")
        dialog.transient(self.root)
        dialog.resizable(False, False)
        dialog.configure(bg=self.colors["bg"])

        output_var = tk.StringVar(value=self.output_dir.get())
        template_var = tk.StringVar(value=self.naming_template.get())
        preserve_var = tk.BooleanVar(value=self.preserve_structure.get())
        metadata_var = tk.BooleanVar(value=self.keep_metadata.get())
        include_var = tk.BooleanVar(value=self.include_subfolders.get())
        log_dir_var = tk.StringVar(value=self.log_dir.get())

        body = ttk.Frame(dialog, style="Panel.TFrame", padding=18)
        body.grid(row=0, column=0, sticky="nsew")
        body.columnconfigure(1, weight=1)

        ttk.Label(body, text="输出目录", style="PanelText.TLabel").grid(
            row=0, column=0, sticky="w", pady=(0, 8)
        )
        ttk.Entry(body, textvariable=output_var, width=58).grid(
            row=1, column=0, columnspan=2, sticky="ew", padx=(0, 10)
        )
        ttk.Button(
            body,
            text="选择",
            style="Soft.TButton",
            command=lambda: self.choose_directory_for(output_var, "选择输出目录", dialog),
        ).grid(row=1, column=2, sticky="e")

        ttk.Checkbutton(
            body,
            text="添加文件夹时包含子文件夹",
            variable=include_var,
        ).grid(row=2, column=0, columnspan=3, sticky="w", pady=(16, 0))
        ttk.Label(
            body,
            text="开启后，添加文件夹会同时扫描它下面所有子文件夹中的图片。",
            style="Muted.TLabel",
        ).grid(row=3, column=0, columnspan=3, sticky="w", pady=(6, 0))

        ttk.Label(body, text="输出命名模板", style="PanelText.TLabel").grid(
            row=4, column=0, sticky="w", pady=(18, 8)
        )
        ttk.Entry(body, textvariable=template_var, width=58).grid(
            row=5, column=0, columnspan=3, sticky="ew", padx=(0, 10)
        )
        ttk.Label(
            body,
            text="留空沿用默认命名；可用 {name}、{operation}、{width}、{height}、{format}、{date}、{time}、{index}。",
            style="Muted.TLabel",
        ).grid(row=6, column=0, columnspan=3, sticky="w", pady=(6, 0))

        ttk.Checkbutton(
            body,
            text="保留导入文件夹的子目录结构",
            variable=preserve_var,
        ).grid(row=7, column=0, columnspan=3, sticky="w", pady=(16, 0))

        ttk.Checkbutton(
            body,
            text="保留图片元数据（EXIF/ICC/DPI）",
            variable=metadata_var,
        ).grid(row=8, column=0, columnspan=3, sticky="w", pady=(10, 0))

        ttk.Label(body, text="日志目录", style="PanelText.TLabel").grid(
            row=9, column=0, sticky="w", pady=(18, 8)
        )
        ttk.Entry(body, textvariable=log_dir_var, width=58).grid(
            row=10, column=0, columnspan=2, sticky="ew", padx=(0, 10)
        )
        ttk.Button(
            body,
            text="选择",
            style="Soft.TButton",
            command=lambda: self.choose_directory_for(log_dir_var, "选择日志目录", dialog),
        ).grid(row=10, column=2, sticky="e")

        ttk.Label(
            body,
            text=f"当前日志文件：{middle_ellipsis(str(LOG_PATH), 78)}",
            style="Muted.TLabel",
        ).grid(row=11, column=0, columnspan=3, sticky="w", pady=(8, 0))

        ttk.Label(
            body,
            text=f"配置文件：{middle_ellipsis(str(CONFIG_PATH), 78)}",
            style="Muted.TLabel",
        ).grid(row=12, column=0, columnspan=3, sticky="w", pady=(6, 0))

        buttons = ttk.Frame(body, style="Panel.TFrame")
        buttons.grid(row=13, column=0, columnspan=3, sticky="ew", pady=(20, 0))
        buttons.columnconfigure(0, weight=1)

        ttk.Button(
            buttons,
            text="取消",
            style="Soft.TButton",
            command=dialog.destroy,
        ).grid(row=0, column=1, sticky="e", padx=(0, 10))

        def save_settings() -> None:
            try:
                validate_output_template(template_var.get())
            except ValueError as exc:
                messagebox.showerror("命名模板不可用", readable_error(exc), parent=dialog)
                return
            if not self.apply_log_directory(log_dir_var.get()):
                return
            self.output_dir.set(output_var.get().strip())
            self.naming_template.set(template_var.get().strip())
            self.preserve_structure.set(preserve_var.get())
            self.keep_metadata.set(metadata_var.get())
            self.include_subfolders.set(include_var.get())
            if not self.save_current_config(announce=True):
                return
            self.write_log("设置已保存。")
            dialog.destroy()

        ttk.Button(
            buttons,
            text="保存",
            style="Accent.TButton",
            command=save_settings,
        ).grid(row=0, column=2, sticky="e")

        dialog.update_idletasks()
        x = self.root.winfo_x() + (self.root.winfo_width() - dialog.winfo_width()) // 2
        y = self.root.winfo_y() + (self.root.winfo_height() - dialog.winfo_height()) // 2
        dialog.geometry(f"+{max(0, x)}+{max(0, y)}")
        dialog.grab_set()

    def choose_directory_for(self, variable: tk.StringVar, title: str, parent: tk.Toplevel) -> None:
        directory = filedialog.askdirectory(title=title, parent=parent)
        if directory:
            variable.set(directory)

    def on_scale_changed(self, value: str) -> None:
        percent = min(100, max(1, int(round(float(value)))))
        if self.percent.get() != percent:
            self.percent.set(percent)
        self.update_percent_ui(percent)

    def on_percent_changed(self, _value: str | None = None) -> None:
        try:
            value = int(self.percent.get())
        except (tk.TclError, ValueError):
            return
        value = min(100, max(1, value))
        if self.percent.get() != value:
            self.percent.set(value)
        self.scale.set(value)
        self.update_percent_ui(value)

    def update_percent_ui(self, value: int) -> None:
        self.percent_label.configure(text=f"{value}%")
        if hasattr(self, "estimated_label"):
            self.update_size_estimate()

    def on_transparency_scale_changed(self, value: str) -> None:
        tolerance = min(100, max(1, int(round(float(value)))))
        if self.transparency_tolerance.get() != tolerance:
            self.transparency_tolerance.set(tolerance)
        self.update_transparency_ui(tolerance)

    def on_transparency_tolerance_changed(self, _value: str | None = None) -> None:
        try:
            value = int(self.transparency_tolerance.get())
        except (tk.TclError, ValueError):
            return
        value = min(100, max(1, value))
        if self.transparency_tolerance.get() != value:
            self.transparency_tolerance.set(value)
        if hasattr(self, "transparency_scale"):
            self.transparency_scale.set(value)
        self.update_transparency_ui(value)

    def update_transparency_ui(self, value: int) -> None:
        if hasattr(self, "transparency_label"):
            self.transparency_label.configure(text=f"{value}")

    def refresh_operation_ui(self) -> None:
        if not hasattr(self, "resize_controls"):
            return

        if self.operation_mode.get() == "convert":
            self.resize_controls.grid_remove()
            self.compress_controls.grid_remove()
            self.transform_controls.grid_remove()
            self.transparent_controls.grid_remove()
            self.convert_controls.grid()
            if hasattr(self, "start_button"):
                self.start_button.configure(text="开始转换")
            self.status_text.set("请选择图片并设置输出格式")
        elif self.operation_mode.get() == "compress":
            self.resize_controls.grid_remove()
            self.convert_controls.grid_remove()
            self.transform_controls.grid_remove()
            self.transparent_controls.grid_remove()
            self.compress_controls.grid()
            if hasattr(self, "start_button"):
                self.start_button.configure(text="开始体积压缩")
            self.status_text.set("请选择图片并设置压缩档位")
        elif self.operation_mode.get() == "transform":
            self.resize_controls.grid_remove()
            self.convert_controls.grid_remove()
            self.compress_controls.grid_remove()
            self.transparent_controls.grid_remove()
            self.transform_controls.grid()
            if hasattr(self, "start_button"):
                self.start_button.configure(text="打开裁切/旋转")
            self.status_text.set("请选择一张图片打开编辑器")
        elif self.operation_mode.get() == "transparent":
            self.resize_controls.grid_remove()
            self.convert_controls.grid_remove()
            self.compress_controls.grid_remove()
            self.transform_controls.grid_remove()
            self.transparent_controls.grid()
            if hasattr(self, "start_button"):
                self.start_button.configure(text="生成透明图片")
            self.status_text.set("实验性功能：请选择图片并设置背景容差")
        else:
            self.convert_controls.grid_remove()
            self.compress_controls.grid_remove()
            self.transform_controls.grid_remove()
            self.transparent_controls.grid_remove()
            self.resize_controls.grid()
            if hasattr(self, "start_button"):
                self.start_button.configure(text="开始压缩")
            self.status_text.set("请选择图片并设置缩放比例")

        if self.target_format.get() == "coe":
            self.coe_format_row.grid()
            self.coe_note_label.grid()
        else:
            self.coe_format_row.grid_remove()
            self.coe_note_label.grid_remove()

    def selected_preview_path(self) -> Path | None:
        if not self.files:
            return None
        selection = self.file_list.selection()
        iid = selection[0] if selection else "0"
        return self.path_from_iid(iid)

    def cancel_preview_update(self) -> None:
        if self.preview_after_id is None:
            return
        try:
            self.root.after_cancel(self.preview_after_id)
        except tk.TclError:
            pass
        self.preview_after_id = None

    def schedule_preview_update(self, delay_ms: int = 60) -> None:
        if self.closing or not self.files:
            return
        self.cancel_preview_update()
        try:
            self.preview_after_id = self.root.after(delay_ms, self.update_preview)
        except tk.TclError:
            logger.debug("界面已关闭，跳过预览刷新", exc_info=True)

    def update_preview(self) -> None:
        self.preview_after_id = None
        if self.closing:
            return
        path = self.selected_preview_path()
        self.preview_canvas.delete("all")
        if path is None:
            return

        try:
            max_size = self.preview_target_size()
            self.preview_info.configure(wraplength=max_size[0])
            preview, original_size = load_preview_image(path, max_size)
            self.selected_image_size = original_size
            self.draw_preview(preview)
            self.update_size_estimate(original_size)
            display_name = middle_ellipsis(path.name, 78)
            display_parent = middle_ellipsis(str(path.parent), 78)
            self.preview_info.configure(
                text=(
                    f"{display_name}\n"
                    f"原始尺寸：{original_size[0]} x {original_size[1]} 像素\n"
                    f"预览尺寸：{preview.width} x {preview.height} 像素\n"
                    f"文件位置：{display_parent}"
                )
            )
            self.status_text.set("已载入预览")
        except Exception as exc:
            self.preview_info.configure(text=f"无法预览：{path.name}")
            self.status_text.set("预览失败")
            self.write_log(f"预览失败 {path}: {readable_error(exc)}")
            logger.exception("预览失败：%s", path)

    def preview_target_size(self) -> tuple[int, int]:
        width = max(self.preview_canvas.winfo_width(), 300)
        height = max(self.preview_canvas.winfo_height(), 220)
        return max(1, width - PREVIEW_PADDING), max(1, height - PREVIEW_PADDING)

    def draw_preview(self, image: Image.Image) -> None:
        width = max(self.preview_canvas.winfo_width(), 300)
        height = max(self.preview_canvas.winfo_height(), 220)
        self.preview_image = ImageTk.PhotoImage(image)

        x = width // 2
        y = height // 2
        self.preview_canvas.create_image(x, y, image=self.preview_image)

    def update_size_estimate(self, size: tuple[int, int] | None = None) -> None:
        if size is None:
            size = self.selected_image_size
        if size is None:
            path = self.selected_preview_path()
            if path is None:
                return
            try:
                with Image.open(path) as image:
                    size = oriented_size(image)
                    self.selected_image_size = size
            except Exception as exc:
                self.write_log(f"读取图片尺寸失败 {path}: {readable_error(exc)}")
                logger.exception("读取图片尺寸失败：%s", path)
                return

        percent = self.percent.get()
        new_width = max(1, math.floor(size[0] * percent / 100))
        new_height = max(1, math.floor(size[1] * percent / 100))
        self.estimated_label.configure(
            text=f"当前图片预计输出：{new_width} x {new_height} 像素"
        )

    def run_on_ui_thread(self, callback, *args) -> None:
        if self.closing:
            return
        try:
            self.root.after(0, self.invoke_ui_callback, callback, *args)
        except tk.TclError:
            logger.debug("界面已关闭，跳过回调：%s", getattr(callback, "__name__", callback), exc_info=True)

    def invoke_ui_callback(self, callback, *args) -> None:
        if self.closing:
            return
        try:
            callback(*args)
        except tk.TclError:
            logger.debug("界面回调已跳过：%s", getattr(callback, "__name__", callback), exc_info=True)
        except Exception as exc:
            logger.exception("界面回调失败：%s", getattr(callback, "__name__", callback))
            if not self.closing:
                try:
                    self.status_text.set("界面回调失败，详情已写入日志")
                    self.write_log(f"界面回调失败：{readable_error(exc)}")
                except tk.TclError:
                    pass

    def validate_before_processing(self) -> bool:
        if self.processing:
            return False
        if self.scanning:
            messagebox.showinfo("正在扫描", "正在扫描图片，请稍等。")
            return False
        if not self.files:
            messagebox.showwarning("还没有图片", "请先添加要处理的图片。")
            return False
        if self.operation_mode.get() != "transform" and not self.selected_files():
            messagebox.showwarning("还没有勾选图片", "请先勾选要处理的图片。")
            return False
        if self.validated_output_dir() is None:
            return False
        try:
            validate_output_template(self.naming_template.get())
        except ValueError as exc:
            messagebox.showwarning("命名模板不正确", readable_error(exc))
            return False
        if self.operation_mode.get() == "convert":
            if self.target_format.get() not in SUPPORTED_EXPORT_FORMATS:
                messagebox.showwarning("格式不正确", "请选择要转换到的输出格式。")
                return False
            if self.target_format.get() == "coe" and self.coe_pixel_format.get() not in COE_PIXEL_FORMATS:
                messagebox.showwarning("COE 设置不正确", "请选择 COE 像素格式。")
                return False
            return True
        if self.operation_mode.get() == "compress":
            if self.compression_level.get() not in COMPRESSION_LEVELS:
                messagebox.showwarning("档位不正确", "请选择轻度、中度或重度压缩。")
                return False
            try:
                target_size_kb = int(self.target_size_kb.get())
            except (tk.TclError, ValueError):
                messagebox.showwarning("目标体积不正确", "目标体积需要是 0 或正整数 KB。")
                return False
            if target_size_kb < 0:
                messagebox.showwarning("目标体积不正确", "目标体积需要是 0 或正整数 KB。")
                return False
            return True
        if self.operation_mode.get() == "transparent":
            try:
                tolerance = int(self.transparency_tolerance.get())
            except (tk.TclError, ValueError):
                messagebox.showwarning("容差不正确", "透明背景容差需要在 1 到 100 之间。")
                return False
            if tolerance < 1 or tolerance > 100:
                messagebox.showwarning("容差不正确", "透明背景容差需要在 1 到 100 之间。")
                return False
            if self.transparency_edge_cleanup.get() not in EDGE_CLEANUP_LEVELS:
                messagebox.showwarning("边缘净化不正确", "请选择轻度、中度或强。")
                return False
            return True
        if self.operation_mode.get() == "transform":
            if self.selected_preview_path() is None:
                messagebox.showwarning("还没有选中图片", "请先在列表中选中一张要编辑的图片。")
                return False
            return True
        try:
            percent = int(self.percent.get())
        except (tk.TclError, ValueError):
            messagebox.showwarning("比例不正确", "缩放比例需要在 1 到 100 之间。")
            return False
        if percent < 1 or percent > 100:
            messagebox.showwarning("比例不正确", "缩放比例需要在 1 到 100 之间。")
            return False
        return True

    def cancel_processing(self) -> None:
        if not self.processing or self.cancel_requested.is_set():
            return
        self.cancel_requested.set()
        self.status_text.set("正在取消，当前图片处理完成后停止...")
        self.write_log("已请求取消任务，当前图片处理完成后停止。")
        self.cancel_button.configure(state=tk.DISABLED)

    def start_processing(self) -> None:
        if not self.validate_before_processing():
            return

        files = self.selected_files()
        output_dir = Path(self.output_dir.get().strip()).expanduser()
        target_format = self.target_format.get()
        coe_pixel_format = self.coe_pixel_format.get()
        compression_level = self.compression_level.get()
        mode = self.operation_mode.get()
        target_size_kb = int(self.target_size_kb.get()) if mode == "compress" else 0
        target_bytes = target_size_kb * 1024 if target_size_kb > 0 else None
        keep_metadata = self.keep_metadata.get()
        transparency_tolerance = self.transparency_tolerance.get()
        transparency_edge_cleanup = self.transparency_edge_cleanup.get()
        percent = int(self.percent.get()) if mode == "resize" else 0

        if mode == "transform":
            path = self.selected_preview_path()
            if path is not None:
                try:
                    CropRotateEditor(
                        self,
                        path,
                        output_dir,
                        naming=self.output_naming_for(path),
                        keep_metadata=keep_metadata,
                    )
                except Exception as exc:
                    logger.exception("打开裁切/旋转编辑器失败：%s", path)
                    self.write_log(f"打开裁切/旋转失败 {path}: {readable_error(exc)}")
                    messagebox.showerror("无法打开编辑器", readable_error(exc))
            return

        if mode == "convert" and not self.confirm_same_format_conversion(files, target_format):
            return

        self.processing = True
        self.cancel_requested.clear()
        self.start_button.configure(state=tk.DISABLED)
        self.cancel_button.configure(state=tk.NORMAL)
        self.progress.configure(maximum=len(files), value=0)
        self.status_text.set("正在处理...")
        if mode == "convert":
            self.write_log(f"开始转换图片为 {target_format.upper()}。")
            thread = threading.Thread(
                target=self.process_conversion_files,
                args=(files, output_dir, target_format, coe_pixel_format, keep_metadata),
                daemon=True,
            )
        elif mode == "compress":
            self.write_log(f"开始体积压缩图片，档位：{compression_level}。")
            thread = threading.Thread(
                target=self.process_compression_files,
                args=(files, output_dir, compression_level, target_bytes, keep_metadata),
                daemon=True,
            )
        elif mode == "transparent":
            self.write_log(
                f"开始生成透明背景图片（实验性），容差：{transparency_tolerance}，"
                f"边缘净化：{transparency_edge_cleanup}。"
            )
            thread = threading.Thread(
                target=self.process_transparency_files,
                args=(files, output_dir, transparency_tolerance, transparency_edge_cleanup, keep_metadata),
                daemon=True,
            )
        else:
            self.write_log("开始压缩图片。")
            thread = threading.Thread(
                target=self.process_files,
                args=(files, output_dir, percent, keep_metadata),
                daemon=True,
            )

        try:
            thread.start()
        except Exception as exc:
            self.processing = False
            self.start_button.configure(state=tk.NORMAL)
            self.cancel_button.configure(state=tk.DISABLED)
            self.status_text.set("任务启动失败")
            self.write_log(f"任务启动失败：{readable_error(exc)}")
            logger.exception("任务启动失败")
            messagebox.showerror("任务启动失败", readable_error(exc))

    def confirm_same_format_conversion(self, files: list[Path], target_format: str) -> bool:
        same_format_files = same_target_format_sources(files, target_format)
        if not same_format_files:
            return True

        sample_names = "\n".join(f"- {path.name}" for path in same_format_files[:5])
        if len(same_format_files) > 5:
            sample_names += f"\n- 另外 {len(same_format_files) - 5} 张..."

        confirmed = messagebox.askyesno(
            "目标格式相同",
            (
                f"已勾选 {len(files)} 张图片，其中 {len(same_format_files)} 张已经是 "
                f"{target_format.upper()} 格式。\n\n"
                "继续转换会另存为新文件，是否继续？\n\n"
                f"{sample_names}"
            ),
        )
        if not confirmed:
            self.write_log(
                f"已取消转换：{len(same_format_files)} 张图片与目标格式 {target_format.upper()} 一致。"
            )
        return confirmed

    def process_files(
        self,
        files: list[Path],
        output_dir: Path,
        percent: int,
        keep_metadata: bool,
    ) -> None:
        success = 0
        failed = 0
        cancelled = False

        for index, source in enumerate(files, start=1):
            if self.closing:
                return
            if self.cancel_requested.is_set():
                cancelled = True
                break
            try:
                result = resize_image_file(
                    source,
                    output_dir,
                    percent,
                    naming=self.output_naming_for(source, index),
                    keep_metadata=keep_metadata,
                )
                success += 1
                message = (
                    f"完成 {source.name}: "
                    f"{result.original_size[0]}x{result.original_size[1]} -> "
                    f"{result.new_size[0]}x{result.new_size[1]}，保存到 {result.target.name}"
                )
            except Exception as exc:
                failed += 1
                message = f"失败 {source.name}: {readable_error(exc)}"
                logger.exception("压缩失败：%s", source)

            self.run_on_ui_thread(self.on_file_processed, index, message)

        cancelled = cancelled or self.cancel_requested.is_set()
        self.run_on_ui_thread(self.on_processing_finished, success, failed, "压缩", cancelled)

    def process_conversion_files(
        self,
        files: list[Path],
        output_dir: Path,
        target_format: str,
        coe_pixel_format: str,
        keep_metadata: bool,
    ) -> None:
        success = 0
        failed = 0
        cancelled = False

        for index, source in enumerate(files, start=1):
            if self.closing:
                return
            if self.cancel_requested.is_set():
                cancelled = True
                break
            try:
                result = convert_image_file(
                    source,
                    output_dir,
                    target_format,
                    coe_pixel_format,
                    naming=self.output_naming_for(source, index),
                    keep_metadata=keep_metadata,
                )
                success += 1
                message = (
                    f"完成 {source.name}: 转换为 {result.target_format.upper()}，"
                    f"保存到 {result.target.name}"
                )
            except Exception as exc:
                failed += 1
                message = f"失败 {source.name}: {readable_error(exc)}"
                logger.exception("转换失败：%s", source)

            self.run_on_ui_thread(self.on_file_processed, index, message)

        cancelled = cancelled or self.cancel_requested.is_set()
        self.run_on_ui_thread(self.on_processing_finished, success, failed, "转换", cancelled)

    def process_compression_files(
        self,
        files: list[Path],
        output_dir: Path,
        level: str,
        target_bytes: int | None,
        keep_metadata: bool,
    ) -> None:
        success = 0
        failed = 0
        cancelled = False

        for index, source in enumerate(files, start=1):
            if self.closing:
                return
            if self.cancel_requested.is_set():
                cancelled = True
                break
            try:
                result = compress_image_file(
                    source,
                    output_dir,
                    level,
                    target_bytes=target_bytes,
                    naming=self.output_naming_for(source, index),
                    keep_metadata=keep_metadata,
                )
                success += 1
                if result.target_bytes is not None:
                    if result.target_size_reached:
                        change_text = f"已达目标 {result.target_bytes / 1024:.0f}KB"
                    else:
                        change_text = f"未能低于目标 {result.target_bytes / 1024:.0f}KB，已输出可达到的最小结果"
                elif result.used_original_copy:
                    change_text = "原图已是更优体积，已复制原图"
                else:
                    change_text = f"节省 {result.saved_percent:.1f}%"
                message = (
                    f"完成 {source.name}: {result.original_bytes / 1024:.1f}KB -> "
                    f"{result.output_bytes / 1024:.1f}KB，{change_text}，保存到 {result.target.name}"
                )
            except Exception as exc:
                failed += 1
                message = f"失败 {source.name}: {readable_error(exc)}"
                logger.exception("体积压缩失败：%s", source)

            self.run_on_ui_thread(self.on_file_processed, index, message)

        cancelled = cancelled or self.cancel_requested.is_set()
        self.run_on_ui_thread(self.on_processing_finished, success, failed, "体积压缩", cancelled)

    def process_transparency_files(
        self,
        files: list[Path],
        output_dir: Path,
        tolerance: int,
        edge_cleanup: str,
        keep_metadata: bool,
    ) -> None:
        success = 0
        failed = 0
        cancelled = False

        for index, source in enumerate(files, start=1):
            if self.closing:
                return
            if self.cancel_requested.is_set():
                cancelled = True
                break
            try:
                result = create_transparent_image_file(
                    source,
                    output_dir,
                    tolerance,
                    edge_cleanup,
                    naming=self.output_naming_for(source, index),
                    keep_metadata=keep_metadata,
                )
                success += 1
                red, green, blue = result.background_color
                message = (
                    f"完成 {source.name}: 实验性透明背景，"
                    f"背景色 #{red:02X}{green:02X}{blue:02X}，"
                    f"容差 {result.tolerance}，边缘净化 {result.edge_cleanup}，"
                    f"保存到 {result.target.name}"
                )
            except Exception as exc:
                failed += 1
                message = f"失败 {source.name}: {readable_error(exc)}"
                logger.exception("透明背景处理失败：%s", source)

            self.run_on_ui_thread(self.on_file_processed, index, message)

        cancelled = cancelled or self.cancel_requested.is_set()
        self.run_on_ui_thread(self.on_processing_finished, success, failed, "生成透明图片", cancelled)

    def on_file_processed(self, progress_value: int, message: str) -> None:
        if self.closing:
            return
        self.progress.configure(value=progress_value)
        self.write_log(message)

    def on_processing_finished(
        self,
        success: int,
        failed: int,
        operation: str = "压缩",
        cancelled: bool = False,
    ) -> None:
        if self.closing:
            return
        self.processing = False
        self.start_button.configure(state=tk.NORMAL)
        self.cancel_button.configure(state=tk.DISABLED)
        self.cancel_requested.clear()
        if cancelled:
            self.status_text.set(f"已取消：成功 {success}，失败 {failed}")
            self.write_log(f"任务已取消：成功 {success} 张，失败 {failed} 张。")
            return
        self.status_text.set(f"完成：成功 {success}，失败 {failed}")
        self.write_log(f"处理结束：成功 {success} 张，失败 {failed} 张。")
        if success:
            messagebox.showinfo("处理完成", f"成功{operation} {success} 张图片。")

    def write_log(self, text: str) -> None:
        logger.info(text)
        try:
            self.log.insert(tk.END, text + "\n")
            self.log.see(tk.END)
        except tk.TclError:
            logger.exception("写入界面日志失败")

    def report_callback_exception(self, exc_type: type[BaseException], exc: BaseException, tb) -> None:
        logger.error("Tk 回调异常", exc_info=(exc_type, exc, tb))
        try:
            self.status_text.set("捕获到界面异常，详情已写入日志")
            self.write_log(f"界面异常：{exc}（详情见 {LOG_PATH.name}）")
        except tk.TclError:
            pass

    def on_close(self) -> None:
        if self.processing or self.scanning:
            confirmed = messagebox.askyesno(
                "任务仍在运行",
                "当前还有任务没有结束，直接关闭可能中断正在输出的文件。确定关闭吗？",
            )
            if not confirmed:
                return

        self.closing = True
        try:
            self.save_current_config()
            self.cancel_preview_update()
        finally:
            self.root.destroy()

    def open_output_dir(self) -> None:
        directory_text = self.output_dir.get().strip()
        if not directory_text:
            messagebox.showwarning("缺少输出目录", "请先选择输出目录。")
            return

        directory = Path(directory_text).expanduser()
        try:
            ensure_directory_writable(directory)
            import os

            os.startfile(directory)
        except Exception as exc:
            messagebox.showerror("无法打开目录", str(exc))


def create_root() -> tk.Tk:
    return tk.Tk()


def main() -> None:
    root = create_root()
    try:
        ImageToolApp(root)
    except Exception as exc:
        logger.exception("程序启动失败")
        try:
            messagebox.showerror("程序启动失败", readable_error(exc))
        finally:
            root.destroy()
        return
    root.mainloop()


if __name__ == "__main__":
    main()
