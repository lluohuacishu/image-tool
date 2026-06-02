from __future__ import annotations

import logging
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Any

from PIL import Image, ImageOps, ImageTk

from image_tool_core import (
    MAX_INTERACTIVE_IMAGE_PIXELS,
    ensure_image_pixel_limit,
    middle_ellipsis,
    oriented_size,
    readable_error,
)
from image_transform import save_transformed_image
from output_naming import OutputNaming


logger = logging.getLogger("image_tool_gui")


class CropRotateEditor:
    def __init__(
        self,
        app: Any,
        source: Path,
        output_dir: Path,
        naming: OutputNaming | None = None,
        keep_metadata: bool = False,
    ) -> None:
        self.app = app
        self.source = source
        self.output_dir = output_dir
        self.naming = naming
        self.keep_metadata = keep_metadata
        self.rotation_degrees = 0
        self.crop_box: tuple[int, int, int, int] | None = None
        self.drag_start: tuple[int, int] | None = None
        self.preview_image: ImageTk.PhotoImage | None = None
        self.display_size = (1, 1)
        self.display_origin = (0, 0)
        self.display_scale = 1.0
        self.image_item: int | None = None
        self.crop_rect_id: int | None = None

        with Image.open(source) as opened:
            ensure_image_pixel_limit(
                oriented_size(opened),
                MAX_INTERACTIVE_IMAGE_PIXELS,
                "裁切/旋转",
            )
            self.base_image = ImageOps.exif_transpose(opened).copy()
        self.current_image = self.base_image.copy()

        self.window = tk.Toplevel(app.root)
        self.window.title(f"裁切/旋转 - {source.name}")
        self.window.geometry("1040x760")
        self.window.minsize(860, 620)
        self.window.configure(bg=app.colors["bg"])
        self.window.transient(app.root)

        self.status = tk.StringVar(value="拖动鼠标框选裁切区域，或直接旋转后保存。")
        self.build_layout()
        self.bind_events()
        self.window.update_idletasks()
        self.render()

    def build_layout(self) -> None:
        outer = ttk.Frame(self.window, style="App.TFrame", padding=16)
        outer.pack(fill=tk.BOTH, expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(1, weight=1)

        toolbar = ttk.Frame(outer, style="App.TFrame")
        toolbar.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        toolbar.columnconfigure(7, weight=1)

        ttk.Button(
            toolbar,
            text="逆时针 90°",
            style="Soft.TButton",
            command=lambda: self.rotate(-90),
        ).grid(row=0, column=0, sticky="w", padx=(0, 8))
        ttk.Button(
            toolbar,
            text="顺时针 90°",
            style="Soft.TButton",
            command=lambda: self.rotate(90),
        ).grid(row=0, column=1, sticky="w", padx=(0, 8))
        ttk.Button(
            toolbar,
            text="旋转 180°",
            style="Soft.TButton",
            command=lambda: self.rotate(180),
        ).grid(row=0, column=2, sticky="w", padx=(0, 8))
        ttk.Button(
            toolbar,
            text="清除裁切框",
            style="Soft.TButton",
            command=self.clear_crop,
        ).grid(row=0, column=3, sticky="w", padx=(0, 8))
        ttk.Button(
            toolbar,
            text="保存",
            style="Accent.TButton",
            command=self.save,
        ).grid(row=0, column=4, sticky="w", padx=(10, 0))

        ttk.Label(toolbar, textvariable=self.status, style="Subtitle.TLabel").grid(
            row=1, column=0, columnspan=8, sticky="w", pady=(10, 0)
        )

        self.canvas = tk.Canvas(
            outer,
            bd=0,
            highlightthickness=1,
            highlightbackground=self.app.colors["line"],
            bg=self.app.colors["white"],
            cursor="crosshair",
        )
        self.canvas.grid(row=1, column=0, sticky="nsew")

        footer = ttk.Frame(outer, style="App.TFrame")
        footer.grid(row=2, column=0, sticky="ew", pady=(12, 0))
        footer.columnconfigure(0, weight=1)
        ttk.Label(
            footer,
            text=(
                f"原始尺寸：{self.base_image.width} x {self.base_image.height} 像素    "
                f"输出目录：{middle_ellipsis(str(self.output_dir), 86)}"
            ),
            style="Subtitle.TLabel",
        ).grid(row=0, column=0, sticky="w")

    def bind_events(self) -> None:
        self.canvas.bind("<Configure>", lambda _event: self.render())
        self.canvas.bind("<ButtonPress-1>", self.start_crop)
        self.canvas.bind("<B1-Motion>", self.update_crop)
        self.canvas.bind("<ButtonRelease-1>", self.finish_crop)

    def transformed_image(self) -> Image.Image:
        rotation = self.rotation_degrees % 360
        if rotation:
            return self.base_image.rotate(-rotation, expand=True)
        return self.base_image.copy()

    def render(self) -> None:
        image = self.transformed_image()
        canvas_width = max(self.canvas.winfo_width(), 720)
        canvas_height = max(self.canvas.winfo_height(), 480)
        max_width = max(1, canvas_width - 28)
        max_height = max(1, canvas_height - 28)
        scale = min(max_width / image.width, max_height / image.height)
        scale = max(0.01, min(scale, 4.0))
        preview_size = (
            max(1, int(image.width * scale)),
            max(1, int(image.height * scale)),
        )
        preview = image.resize(preview_size, Image.Resampling.LANCZOS)
        self.current_image = image
        self.preview_image = ImageTk.PhotoImage(preview)
        self.display_size = preview_size
        self.display_scale = scale
        self.display_origin = (
            (canvas_width - preview_size[0]) // 2,
            (canvas_height - preview_size[1]) // 2,
        )

        self.canvas.delete("all")
        self.crop_rect_id = None
        self.image_item = self.canvas.create_image(
            self.display_origin[0],
            self.display_origin[1],
            image=self.preview_image,
            anchor=tk.NW,
        )
        self.draw_crop_rect()

    def image_point_from_event(self, event: tk.Event) -> tuple[int, int]:
        x = int(round((event.x - self.display_origin[0]) / self.display_scale))
        y = int(round((event.y - self.display_origin[1]) / self.display_scale))
        x = max(0, min(x, self.current_image.width))
        y = max(0, min(y, self.current_image.height))
        return x, y

    def canvas_box_from_image_box(
        self,
        box: tuple[int, int, int, int],
    ) -> tuple[float, float, float, float]:
        left, upper, right, lower = box
        ox, oy = self.display_origin
        scale = self.display_scale
        return (
            ox + left * scale,
            oy + upper * scale,
            ox + right * scale,
            oy + lower * scale,
        )

    def normalized_box(
        self,
        start: tuple[int, int],
        end: tuple[int, int],
    ) -> tuple[int, int, int, int] | None:
        left = min(start[0], end[0])
        upper = min(start[1], end[1])
        right = max(start[0], end[0])
        lower = max(start[1], end[1])
        if right - left < 2 or lower - upper < 2:
            return None
        return left, upper, right, lower

    def draw_crop_rect(self) -> None:
        if self.crop_rect_id is not None:
            self.canvas.delete(self.crop_rect_id)
            self.crop_rect_id = None
        if self.crop_box is None:
            return
        self.crop_rect_id = self.canvas.create_rectangle(
            *self.canvas_box_from_image_box(self.crop_box),
            outline=self.app.colors["accent"],
            width=2,
            dash=(5, 3),
        )

    def start_crop(self, event: tk.Event) -> None:
        self.drag_start = self.image_point_from_event(event)
        self.crop_box = None
        self.draw_crop_rect()

    def update_crop(self, event: tk.Event) -> None:
        if self.drag_start is None:
            return
        end = self.image_point_from_event(event)
        self.crop_box = self.normalized_box(self.drag_start, end)
        self.draw_crop_rect()

    def finish_crop(self, event: tk.Event) -> None:
        if self.drag_start is None:
            return
        end = self.image_point_from_event(event)
        self.crop_box = self.normalized_box(self.drag_start, end)
        self.drag_start = None
        if self.crop_box is None:
            self.status.set("裁切框太小，已忽略。")
        else:
            left, upper, right, lower = self.crop_box
            self.status.set(f"裁切区域：{right - left} x {lower - upper} 像素。")
        self.draw_crop_rect()

    def rotate(self, degrees: int) -> None:
        self.rotation_degrees = (self.rotation_degrees + degrees) % 360
        self.crop_box = None
        self.status.set(f"已旋转 {self.rotation_degrees}°。旋转后请重新框选裁切区域。")
        self.render()

    def clear_crop(self) -> None:
        self.crop_box = None
        self.status.set("已清除裁切框。")
        self.render()

    def save(self) -> None:
        if self.crop_box is None and self.rotation_degrees % 360 == 0:
            if not messagebox.askyesno(
                "没有编辑操作",
                "当前没有裁切框，也没有旋转。是否仍然另存一份？",
                parent=self.window,
            ):
                return

        try:
            result = save_transformed_image(
                self.source,
                self.output_dir,
                rotation_degrees=self.rotation_degrees,
                crop_box=self.crop_box,
                naming=self.naming,
                keep_metadata=self.keep_metadata,
            )
        except Exception as exc:
            self.app.write_log(f"裁切/旋转失败 {self.source}: {readable_error(exc)}")
            logger.exception("裁切/旋转失败：%s", self.source)
            messagebox.showerror("保存失败", readable_error(exc), parent=self.window)
            return

        self.status.set(f"已保存：{result.target.name}")
        self.app.write_log(
            f"完成 {self.source.name}: 裁切/旋转输出 "
            f"{result.output_size[0]}x{result.output_size[1]}，保存到 {result.target.name}"
        )
        messagebox.showinfo("保存完成", f"已保存到：\n{result.target}", parent=self.window)
