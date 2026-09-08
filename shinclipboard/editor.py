from __future__ import annotations

import time
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import colorchooser, filedialog, messagebox, ttk

from PIL import Image, ImageTk

from .annotations import (
    PALETTE,
    RASTER_KINDS,
    AnnotationDocument,
    AnnotationHistory,
    Shape,
    badge_radius,
    effective_pixel_block,
    hit_test,
    render,
    render_raster,
)
from .icons import tool_icon


# Tool id -> (button label, tooltip-ish hint shown in the status bar).
TOOLS: tuple[tuple[str, str, str], ...] = (
    ("select", "選択", "図形をクリックして選び、ドラッグで移動、Deleteで削除します"),
    ("arrow", "矢印", "ドラッグで矢印を引きます"),
    ("rect", "矩形", "ドラッグで四角を描きます"),
    ("ellipse", "楕円", "ドラッグで楕円を描きます"),
    ("freehand", "ペン", "ドラッグで自由に線を描きます"),
    ("text", "文字", "クリックした位置に文字を入れます"),
    ("number", "番号", "クリックするたびに連番のバッジを置きます"),
    ("highlight", "マーカー", "ドラッグした範囲を半透明で塗ります"),
    ("blur", "ぼかし", "見た目をぼかします。確実に隠すには「矩形」＋「塗りつぶし」を使ってください"),
    ("pixelate", "モザイク", "モザイクを掛けます。確実に隠すには「矩形」＋「塗りつぶし」を使ってください"),
    ("crop", "切り抜き", "ドラッグした範囲だけを残します。Escで解除できます"),
)

DRAG_TOOLS = {"arrow", "rect", "ellipse", "highlight", "blur", "pixelate", "crop"}
CLICK_TOOLS = {"text", "number"}

MIN_ZOOM = 0.1
MAX_ZOOM = 8.0
EXPORT_DIR = "exports"
EXPORT_MAX_AGE = 24 * 60 * 60
TOOL_SELECTED_BG = "#bfdbfe"
TEXTBOX_HINT = "Ctrl+Enter: 確定   Esc: 取りやめ   Enter: 改行"


class _Tooltip:
    """Small hover label for icon-only buttons."""

    def __init__(self, widget: tk.Misc, text: str, delay_ms: int = 450):
        self.widget = widget
        self.text = text
        self.delay_ms = delay_ms
        self._job: str | None = None
        self._tip: tk.Toplevel | None = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _event=None) -> None:
        self._cancel()
        self._job = self.widget.after(self.delay_ms, self._show)

    def _show(self) -> None:
        self._job = None
        if self._tip is not None:
            return
        tip = tk.Toplevel(self.widget)
        tip.overrideredirect(True)
        tip.attributes("-topmost", True)
        tk.Label(
            tip, text=self.text, background="#fefce8", foreground="#1f2937",
            relief="solid", borderwidth=1, padx=6, pady=3, font=("Yu Gothic UI", 9),
        ).pack()
        x = self.widget.winfo_rootx()
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        tip.geometry(f"+{x}+{y}")
        self._tip = tip

    def _cancel(self) -> None:
        if self._job is not None:
            try:
                self.widget.after_cancel(self._job)
            except tk.TclError:
                pass
            self._job = None

    def _hide(self, _event=None) -> None:
        self._cancel()
        if self._tip is not None:
            try:
                self._tip.destroy()
            except tk.TclError:
                pass
            self._tip = None


class ImageEditorWindow:
    """Monosnap-style annotation editor for one picture.

    The window keeps the untouched picture in `self.base` and every edit as a
    vector `Shape`, so annotations stay movable and the export is always redrawn
    at full resolution. The canvas shows a two-layer approximation: a bitmap that
    already has the crop and the pixel-rewriting effects baked in, and native
    canvas items for everything else. `annotations.render()` is the source of
    truth; the canvas is only a draft.
    """

    def __init__(self, app, base: Image.Image, title: str = "画像編集"):
        self.app = app
        self.base = base.convert("RGBA")
        self.document = AnnotationDocument()
        self.history = AnnotationHistory()
        self.selected_id: str | None = None

        self.window = tk.Toplevel(app.root)
        self.window.title(f"ShinClipboard - {title}")
        self.window.geometry("1080x760")
        self.window.minsize(640, 480)
        self.window.protocol("WM_DELETE_WINDOW", self.close)

        settings = app.config["settings"]
        self.tool_var = tk.StringVar(value="arrow")
        self.color_var = tk.StringVar(value=str(settings.get("annotation_color", PALETTE[0])))
        self.width_var = tk.StringVar(value=str(settings.get("annotation_width", 4)))
        self.font_size_var = tk.StringVar(value="24")
        self.filled_var = tk.BooleanVar(value=False)
        self.status_var = tk.StringVar(value="ドラッグで矢印を引きます")

        self.zoom = 1.0
        self._photo: ImageTk.PhotoImage | None = None
        self._photo_key: tuple[int, int] | None = None
        self._raster_cache: Image.Image | None = None
        self._drag_start: tuple[float, float] | None = None
        self._draft: Shape | None = None
        self._moving: Shape | None = None
        self._dnd_path: Path | None = None

        self._offset = (0, 0)
        self._fitted = False
        self._layout_job: str | None = None
        self._text_box: tk.Text | None = None
        self._text_box_item: int | None = None
        self._text_box_origin: tuple[float, float] = (0.0, 0.0)
        self._text_box_shape_id: str | None = None  # set when re-editing an existing text

        self._build_toolbar()
        self._build_canvas()
        self._build_actions()
        self._bind_keys()
        # The canvas reports 1x1 until the window manager has laid it out, so the
        # first fit has to wait for a real <Configure> rather than the idle loop.
        self.canvas.bind("<Configure>", self._on_canvas_configure)

    # ----- construction -----------------------------------------------------------

    def _build_toolbar(self) -> None:
        bar = ttk.Frame(self.window, padding=(10, 8, 10, 4))
        bar.pack(fill="x")
        tools = ttk.Frame(bar)
        tools.pack(side="left")
        self._icons: dict[str, ImageTk.PhotoImage] = {}  # Tk drops images nobody references
        for name, label, hint in TOOLS:
            self._icons[name] = ImageTk.PhotoImage(tool_icon(name), master=self.window)
            button = tk.Radiobutton(
                tools,
                image=self._icons[name],
                value=name,
                variable=self.tool_var,
                command=self._tool_changed,
                indicatoron=False,
                selectcolor=TOOL_SELECTED_BG,
                relief="flat",
                overrelief="raised",
                borderwidth=1,
                padx=5,
                pady=3,
                cursor="hand2",
            )
            button.pack(side="left", padx=1)
            _Tooltip(button, f"{label}　{hint}")

        options = ttk.Frame(self.window, padding=(10, 0, 10, 6))
        options.pack(fill="x")
        ttk.Label(options, text="色").pack(side="left", padx=(0, 4))
        self.swatches = ttk.Frame(options)
        self.swatches.pack(side="left")
        for color in PALETTE:
            swatch = tk.Button(
                self.swatches,
                background=color,
                activebackground=color,
                width=2,
                relief="flat",
                borderwidth=2,
                highlightthickness=2,
                command=lambda value=color: self._pick_color(value),
            )
            swatch.pack(side="left", padx=1)
        ttk.Button(options, text="…", width=3, command=self._choose_color).pack(side="left", padx=(4, 12))
        ttk.Label(options, text="太さ").pack(side="left")
        ttk.Spinbox(options, from_=1, to=40, width=4, textvariable=self.width_var).pack(side="left", padx=(4, 12))
        ttk.Label(options, text="文字サイズ").pack(side="left")
        ttk.Spinbox(options, from_=8, to=200, width=4, textvariable=self.font_size_var).pack(side="left", padx=(4, 12))
        ttk.Checkbutton(options, text="塗りつぶし", variable=self.filled_var).pack(side="left")
        ttk.Button(options, text="取り消し", command=self.undo).pack(side="right")
        ttk.Button(options, text="やり直し", command=self.redo).pack(side="right", padx=6)
        ttk.Button(options, text="切り抜き解除", command=self.reset_crop).pack(side="right", padx=(0, 12))
        zoom = ttk.Frame(options)
        zoom.pack(side="right", padx=(0, 12))
        ttk.Label(zoom, text="表示").pack(side="left", padx=(0, 4))
        ttk.Button(zoom, text="－", width=3, command=lambda: self.zoom_by(1 / 1.25)).pack(side="left")
        ttk.Button(zoom, text="＋", width=3, command=lambda: self.zoom_by(1.25)).pack(side="left", padx=2)
        ttk.Button(zoom, text="100%", width=5, command=lambda: self.set_zoom(1.0)).pack(side="left")
        ttk.Button(zoom, text="全体", width=5, command=self._zoom_to_fit).pack(side="left", padx=2)
        self._paint_swatches()

    def _build_canvas(self) -> None:
        frame = ttk.Frame(self.window)
        frame.pack(fill="both", expand=True, padx=10)
        self.canvas = tk.Canvas(frame, background="#3f3f46", highlightthickness=0, cursor="crosshair")
        vertical = ttk.Scrollbar(frame, orient="vertical", command=self.canvas.yview)
        horizontal = ttk.Scrollbar(frame, orient="horizontal", command=self.canvas.xview)
        self.canvas.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_motion)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<Double-Button-1>", self._on_double_click)
        self.canvas.bind("<Control-MouseWheel>", self._on_zoom_wheel)
        self.canvas.bind("<MouseWheel>", lambda event: self.canvas.yview_scroll(-int(event.delta / 120), "units"))

    def _build_actions(self) -> None:
        bar = ttk.Frame(self.window, padding=(10, 8))
        bar.pack(fill="x")
        ttk.Button(bar, text="クリップボードへコピー", command=self.copy_to_clipboard).pack(side="left")
        ttk.Button(bar, text="名前を付けて保存", command=self.save_as).pack(side="left", padx=6)
        ttk.Button(bar, text="履歴へ保存", command=self.save_to_history).pack(side="left")
        # Dragging on the canvas draws, so the drag-out handle needs its own widget.
        self.drag_handle = ttk.Label(
            bar, text="⇱ ドラッグして渡す", relief="ridge", padding=(10, 4), cursor="hand2"
        )
        self.drag_handle.pack(side="left", padx=16)
        self._setup_drag_source()
        ttk.Label(bar, textvariable=self.status_var, style="Muted.TLabel").pack(side="right")

    def _bind_keys(self) -> None:
        self.window.bind("<Control-z>", lambda _: self.undo())
        self.window.bind("<Control-y>", lambda _: self.redo())
        self.window.bind("<Control-Shift-Z>", lambda _: self.redo())
        self.window.bind("<Control-c>", lambda _: self.copy_to_clipboard())
        self.window.bind("<Control-s>", lambda _: self.save_as())
        self.window.bind("<Delete>", lambda _: self.delete_selected())
        self.window.bind("<BackSpace>", lambda _: self.delete_selected())
        self.window.bind("<Escape>", self._on_escape)
        self.window.bind("<Control-Key-0>", lambda _: self._zoom_to_fit())
        self.window.bind("<Control-Key-1>", lambda _: self.set_zoom(1.0))
        self.window.bind("<Control-plus>", lambda _: self.zoom_by(1.25))
        self.window.bind("<Control-minus>", lambda _: self.zoom_by(1 / 1.25))

    # ----- coordinate helpers -----------------------------------------------------

    def _origin(self) -> tuple[int, int]:
        """Top-left of the visible picture in source-image coordinates."""
        return (self.document.crop[0], self.document.crop[1]) if self.document.crop else (0, 0)

    def _to_image(self, x: float, y: float) -> tuple[float, float]:
        origin_x, origin_y = self._origin()
        offset_x, offset_y = self._offset
        return (
            (self.canvas.canvasx(x) - offset_x) / self.zoom + origin_x,
            (self.canvas.canvasy(y) - offset_y) / self.zoom + origin_y,
        )

    def _to_canvas(self, x: float, y: float) -> tuple[float, float]:
        origin_x, origin_y = self._origin()
        offset_x, offset_y = self._offset
        return ((x - origin_x) * self.zoom + offset_x, (y - origin_y) * self.zoom + offset_y)

    def _viewport(self) -> tuple[int, int]:
        return (max(1, self.canvas.winfo_width()), max(1, self.canvas.winfo_height()))

    def _on_canvas_configure(self, event) -> None:
        if event.width < 20 or event.height < 20:
            return  # not laid out yet
        if not self._fitted:
            self._fitted = True
            self._zoom_to_fit()
            return
        # Re-centre after a resize, but only once the flurry of events settles.
        if self._layout_job is None:
            self._layout_job = self.window.after_idle(self._relayout)

    def _relayout(self) -> None:
        self._layout_job = None
        self.refresh()

    def _zoom_to_fit(self) -> str:
        width, height = self._viewport()
        picture = self._visible_size()
        self.set_zoom(min(1.0, (width - 8) / picture[0], (height - 8) / picture[1]))
        return "break"

    def set_zoom(self, zoom: float) -> None:
        self.zoom = max(MIN_ZOOM, min(MAX_ZOOM, zoom))
        self.refresh()
        self._set_status(f"表示倍率 {self.zoom * 100:.0f}%")

    def zoom_by(self, factor: float) -> str:
        self.set_zoom(self.zoom * factor)
        return "break"

    def _visible_size(self) -> tuple[int, int]:
        if self.document.crop:
            left, top, right, bottom = self.document.crop
            return (max(1, right - left), max(1, bottom - top))
        return self.base.size

    # ----- rendering --------------------------------------------------------------

    def refresh(self, rebuild_raster: bool = False) -> None:
        if rebuild_raster or self._raster_cache is None:
            self._raster_cache = render_raster(self.base, self.document)
            self._photo = None
        width = max(1, int(self._raster_cache.width * self.zoom))
        height = max(1, int(self._raster_cache.height * self.zoom))
        # Rescaling a large screenshot costs more than every canvas item put
        # together, so the backdrop is only rebuilt when the pixels or the zoom
        # actually changed; dragging a shape reuses it.
        if self._photo is None or self._photo_key != (width, height):
            preview = self._raster_cache
            if (width, height) != preview.size:
                preview = preview.resize((width, height), Image.Resampling.LANCZOS)
            self._photo = ImageTk.PhotoImage(preview, master=self.window)
            self._photo_key = (width, height)
        # A picture smaller than the viewport sits in the middle instead of
        # hugging the top-left corner; every canvas item is placed through
        # `_to_canvas`, which applies the same offset.
        view_width, view_height = self._viewport()
        self._offset = (max(0, (view_width - width) // 2), max(0, (view_height - height) // 2))
        # Not delete("all"): an open text box is an embedded window item that must survive.
        self.canvas.delete("backdrop", "shape", "handle", "draft")
        self.canvas.create_image(*self._offset, image=self._photo, anchor="nw", tags="backdrop")
        self.canvas.tag_lower("backdrop")
        self.canvas.configure(scrollregion=(0, 0, max(width, view_width), max(height, view_height)))
        for shape in self.document.shapes:
            if shape.id != self._text_box_shape_id:  # the box stands in for it while editing
                self._draw_shape(shape)
        self._paint_selection()
        if self._text_box_item is not None:
            self.canvas.coords(self._text_box_item, *self._to_canvas(*self._text_box_origin))
            self.canvas.tag_raise("textbox")

    def _draw_shape(self, shape: Shape) -> None:
        if shape.kind in RASTER_KINDS:
            return  # already baked into the backdrop
        points = [self._to_canvas(x, y) for x, y in shape.points]
        width = max(1, int(shape.width * self.zoom))
        tags = ("shape", shape.id)
        if shape.kind == "arrow":
            self.canvas.create_line(
                *points[0], *points[-1], fill=shape.color, width=width, arrow="last",
                arrowshape=(width * 4, width * 5, width * 2), tags=tags,
            )
        elif shape.kind == "rect":
            self.canvas.create_rectangle(
                *points[0], *points[-1], outline=shape.color, width=width,
                fill=shape.color if shape.filled else "", tags=tags,
            )
        elif shape.kind == "ellipse":
            self.canvas.create_oval(
                *points[0], *points[-1], outline=shape.color, width=width,
                fill=shape.color if shape.filled else "", tags=tags,
            )
        elif shape.kind == "freehand" and len(points) >= 2:
            flat = [value for point in points for value in point]
            # PIL joins the samples with straight segments, so the preview must not
            # smooth them or the stroke would shift when it is exported.
            self.canvas.create_line(*flat, fill=shape.color, width=width, smooth=False, tags=tags)
        elif shape.kind == "text":
            self.canvas.create_text(
                *points[0], text=shape.text, fill=shape.color, anchor="nw",
                font=self._font(shape.font_size), tags=tags,
            )
        elif shape.kind == "number":
            radius = badge_radius(shape) * self.zoom
            x, y = points[0]
            self.canvas.create_oval(
                x - radius, y - radius, x + radius, y + radius, fill=shape.color, outline="", tags=tags
            )
            self.canvas.create_text(
                x, y, text=str(shape.number), fill="#ffffff", font=self._font(shape.font_size), tags=tags
            )

    def _font(self, size: int) -> tuple[str, int]:
        """A canvas font measured in pixels, the unit PIL uses.

        Tk reads a positive size as points and scales it by the screen DPI, which
        makes canvas text noticeably larger than the exported text; a negative
        size means pixels and matches `annotations.resolve_font`.
        """
        return ("Yu Gothic UI", -max(1, int(round(size * self.zoom))))

    def _paint_selection(self) -> None:
        self.canvas.delete("handle")
        if not self.selected_id:
            return
        shape = self.document.find(self.selected_id)
        if shape is None:
            self.selected_id = None
            return
        left, top, right, bottom = shape.bounds()
        pad = max(6.0, shape.width)
        x0, y0 = self._to_canvas(left - pad, top - pad)
        x1, y1 = self._to_canvas(right + pad, bottom + pad)
        self.canvas.create_rectangle(x0, y0, x1, y1, outline="#0ea5e9", dash=(4, 3), width=2, tags="handle")

    # ----- mouse ------------------------------------------------------------------

    def _on_press(self, event) -> None:
        if self._text_box is not None:
            # Clicking anywhere else finishes the text being typed.
            self._commit_text_box()
            if self.tool_var.get() == "text":
                return  # one click closes the box; the next one opens a new box
        self.window.focus_set()
        x, y = self._to_image(event.x, event.y)
        self._drag_start = (x, y)
        tool = self.tool_var.get()
        if tool == "select":
            shape = hit_test(self.document, x, y, tolerance=max(6.0, 6.0 / max(self.zoom, 0.01)))
            self.selected_id = shape.id if shape else None
            self._moving = shape
            if shape is not None:
                self.history.push(self.document)
            self._paint_selection()
            return
        if tool in CLICK_TOOLS:
            return
        self._draft = self._new_shape(tool, [(x, y), (x, y)])

    def _on_motion(self, event) -> None:
        if self._drag_start is None:
            return
        x, y = self._to_image(event.x, event.y)
        if self._moving is not None:
            dx, dy = x - self._drag_start[0], y - self._drag_start[1]
            self._drag_start = (x, y)
            self.document.replace(self._moving.moved(dx, dy))
            self._moving = self.document.find(self._moving.id)
            self.refresh(rebuild_raster=self._moving is not None and self._moving.kind in RASTER_KINDS)
            return
        if self._draft is None:
            return
        if self._draft.kind == "freehand":
            self._draft.points.append((x, y))
        else:
            self._draft.points = [self._draft.points[0], (x, y)]
        self.canvas.delete("draft")
        self._draw_draft()

    def _on_release(self, event) -> None:
        tool = self.tool_var.get()
        start, self._drag_start = self._drag_start, None
        if self._moving is not None:
            self._moving = None
            self.refresh(rebuild_raster=True)
            return
        if start is None:
            return
        x, y = self._to_image(event.x, event.y)
        if tool in CLICK_TOOLS:
            self._place_click_shape(tool, x, y)
            return
        draft, self._draft = self._draft, None
        self.canvas.delete("draft")
        if draft is None:
            return
        left, top, right, bottom = draft.bounds()
        if right - left < 3 and bottom - top < 3:
            self.refresh()
            return
        if tool == "crop":
            # Clamp here rather than at render time: `_origin()` places every
            # canvas item against this rectangle, so an out-of-bounds crop would
            # shift the preview away from the exported image.
            crop = (
                max(0, int(left)),
                max(0, int(top)),
                min(self.base.width, int(right)),
                min(self.base.height, int(bottom)),
            )
            if crop[2] - crop[0] < 2 or crop[3] - crop[1] < 2:
                self.refresh()
                return
            self.history.push(self.document)
            self.document.crop = crop
            self._zoom_to_fit()
            self._set_status(f"{crop[2] - crop[0]} × {crop[3] - crop[1]} で切り抜きました")
            return
        self.history.push(self.document)
        self.document.shapes.append(draft)
        self.refresh(rebuild_raster=draft.kind in RASTER_KINDS)

    def _draw_draft(self) -> None:
        if self._draft is None:
            return
        shape = self._draft
        points = [self._to_canvas(x, y) for x, y in shape.points]
        width = max(1, int(shape.width * self.zoom))
        if shape.kind == "freehand" and len(points) >= 2:
            flat = [value for point in points for value in point]
            self.canvas.create_line(*flat, fill=shape.color, width=width, smooth=True, tags="draft")
        elif shape.kind == "arrow":
            self.canvas.create_line(
                *points[0], *points[-1], fill=shape.color, width=width, arrow="last",
                arrowshape=(width * 4, width * 5, width * 2), tags="draft",
            )
        elif shape.kind in ("rect", "highlight", "blur", "pixelate", "crop"):
            dash = (5, 3) if shape.kind == "crop" else None
            self.canvas.create_rectangle(
                *points[0], *points[-1], outline=shape.color, width=width, dash=dash,
                fill=shape.color if shape.filled else "", tags="draft",
            )
        elif shape.kind == "ellipse":
            self.canvas.create_oval(
                *points[0], *points[-1], outline=shape.color, width=width,
                fill=shape.color if shape.filled else "", tags="draft",
            )

    def _place_click_shape(self, tool: str, x: float, y: float) -> None:
        if tool == "number":
            self.history.push(self.document)
            shape = self._new_shape("number", [(x, y)])
            shape.number = self.document.next_number()
            self.document.shapes.append(shape)
            self.refresh()
            return
        self._open_text_box(x, y)

    def _on_double_click(self, event) -> None:
        """Double-clicking a text annotation reopens it for editing."""
        if self.tool_var.get() not in ("select", "text"):
            return
        x, y = self._to_image(event.x, event.y)
        shape = hit_test(self.document, x, y, tolerance=max(6.0, 6.0 / max(self.zoom, 0.01)))
        if shape is not None and shape.kind == "text":
            self._drag_start = None
            self._draft = None
            self._moving = None
            self._open_text_box(*shape.points[0], shape=shape)

    # ----- inline text box --------------------------------------------------------

    def _open_text_box(self, x: float, y: float, shape: Shape | None = None) -> None:
        """Drop a real Text widget onto the canvas so typing (and the IME) works in place."""
        self._commit_text_box()
        font_size = shape.font_size if shape else self._int(self.font_size_var, 24, 8, 200)
        color = shape.color if shape else self.color_var.get()
        box = tk.Text(
            self.canvas,
            width=12,
            height=1,
            wrap="none",
            font=self._font(font_size),
            foreground=color,
            insertbackground=color,
            background="#ffffff",
            relief="solid",
            borderwidth=1,
            highlightthickness=1,
            highlightcolor="#0ea5e9",
            padx=2,
            pady=0,
            undo=True,
        )
        if shape is not None:
            box.insert("1.0", shape.text)
        box.bind("<Control-Return>", lambda _: self._commit_text_box() or "break")
        box.bind("<Escape>", lambda _: self._cancel_text_box() or "break")
        box.bind("<KeyRelease>", lambda _: self._grow_text_box())
        self._text_box = box
        self._text_box_origin = (x, y)
        self._text_box_shape_id = shape.id if shape else None
        self._text_box_item = self.canvas.create_window(
            *self._to_canvas(x, y), window=box, anchor="nw", tags="textbox"
        )
        self.selected_id = None
        self.refresh()
        self._grow_text_box()
        box.focus_set()
        self._set_status(TEXTBOX_HINT)

    def _grow_text_box(self) -> None:
        box = self._text_box
        if box is None:
            return
        lines = box.get("1.0", "end-1c").split("\n")
        box.configure(
            width=max(12, max(len(line) for line in lines) + 2),
            height=max(1, len(lines)),
        )

    def _commit_text_box(self) -> None:
        box, self._text_box = self._text_box, None
        if box is None:
            return
        text = box.get("1.0", "end-1c").rstrip("\n")
        shape_id, self._text_box_shape_id = self._text_box_shape_id, None
        self._remove_text_box(box)
        existing = self.document.find(shape_id) if shape_id else None
        if not text.strip():
            if existing is not None:
                self.history.push(self.document)
                self.document.remove(existing.id)
            self.refresh()
            return
        self.history.push(self.document)
        if existing is not None:
            existing.text = text
        else:
            shape = self._new_shape("text", [self._text_box_origin])
            shape.text = text
            self.document.shapes.append(shape)
        self.refresh()
        self.window.focus_set()

    def _cancel_text_box(self) -> None:
        box, self._text_box = self._text_box, None
        self._text_box_shape_id = None
        if box is not None:
            self._remove_text_box(box)
        self.refresh()
        self.window.focus_set()

    def _remove_text_box(self, box: tk.Text) -> None:
        if self._text_box_item is not None:
            try:
                self.canvas.delete(self._text_box_item)
            except tk.TclError:
                pass
            self._text_box_item = None
        try:
            box.destroy()
        except tk.TclError:
            pass

    def _new_shape(self, kind: str, points: list[tuple[float, float]]) -> Shape:
        return Shape(
            kind=kind,
            points=list(points),
            color=self.color_var.get(),
            width=self._int(self.width_var, 4, 1, 40),
            filled=self.filled_var.get(),
            font_size=self._int(self.font_size_var, 24, 8, 200),
            strength=max(effective_pixel_block(0), self._int(self.width_var, 4, 1, 40) * 3),
        )

    def _on_zoom_wheel(self, event) -> str:
        return self.zoom_by(1.1 if event.delta > 0 else 1 / 1.1)

    # ----- commands ---------------------------------------------------------------

    def _tool_changed(self) -> None:
        name = self.tool_var.get()
        self.selected_id = None
        self.canvas.configure(cursor="arrow" if name == "select" else "crosshair")
        self._set_status(next((hint for tool, _label, hint in TOOLS if tool == name), ""))
        self.refresh()

    def _pick_color(self, color: str) -> None:
        self.color_var.set(color)
        self._paint_swatches()
        shape = self.document.find(self.selected_id) if self.selected_id else None
        if shape is not None:
            self.history.push(self.document)
            shape.color = color
            self.refresh(rebuild_raster=shape.kind in RASTER_KINDS)

    def _choose_color(self) -> None:
        chosen = colorchooser.askcolor(color=self.color_var.get(), parent=self.window)[1]
        if chosen:
            self._pick_color(chosen)

    def _paint_swatches(self) -> None:
        current = self.color_var.get()
        for child, color in zip(self.swatches.winfo_children(), PALETTE):
            child.configure(highlightbackground="#111827" if color == current else color)

    def undo(self) -> str:
        if self._text_box is not None:
            return "break"  # let the Text widget's own undo handle it while typing
        restored = self.history.undo(self.document)
        if restored is not None:
            self.document = restored
            self.selected_id = None
            self.refresh(rebuild_raster=True)
            self._set_status("取り消しました")
        return "break"

    def redo(self) -> str:
        restored = self.history.redo(self.document)
        if restored is not None:
            self.document = restored
            self.selected_id = None
            self.refresh(rebuild_raster=True)
            self._set_status("やり直しました")
        return "break"

    def delete_selected(self) -> str:
        if self.selected_id:
            self.history.push(self.document)
            self.document.remove(self.selected_id)
            self.selected_id = None
            self.refresh(rebuild_raster=True)
        return "break"

    def reset_crop(self) -> None:
        if self.document.crop:
            self.history.push(self.document)
            self.document.crop = None
            self._zoom_to_fit()
            self._set_status("切り抜きを解除しました")

    def _on_escape(self, _event=None) -> None:
        if self._text_box is not None:
            self._cancel_text_box()
            return
        if self.selected_id:
            self.selected_id = None
            self._paint_selection()
            return
        self.close()

    def bring_to_front(self) -> None:
        """Same trick as the popup: a brief topmost flip beats Windows' focus-stealing rules."""
        self.window.deiconify()
        self.window.lift()
        self.window.attributes("-topmost", True)
        self.window.after(80, lambda: self.window.attributes("-topmost", False))
        self.window.focus_force()

    def close(self) -> None:
        self.app.editors.discard(self)
        self.window.destroy()

    # ----- output -----------------------------------------------------------------

    def result(self) -> Image.Image:
        self._commit_text_box()  # whatever is being typed belongs in the export
        return render(self.base, self.document)

    def _write_export(self) -> Path:
        return write_export(self.app.store.data_dir, self.result())

    def copy_to_clipboard(self) -> str:
        try:
            self.app._set_clipboard_image(self._write_export())
        except (ImportError, OSError, RuntimeError, ValueError) as error:
            messagebox.showerror("画像編集", str(error), parent=self.window)
            return "break"
        self._set_status("クリップボードへコピーしました")
        if self.app.config["settings"].get("screenshot_save_to_history", True):
            self.save_to_history(quiet=True)
        return "break"

    def save_as(self) -> str:
        settings = self.app.config["settings"]
        default = str(settings.get("screenshot_format", "png")).lower()
        folder = str(settings.get("screenshot_save_dir", "")) or str(_pictures_dir())
        Path(folder).mkdir(parents=True, exist_ok=True)
        path = filedialog.asksaveasfilename(
            parent=self.window,
            title="画像を保存",
            initialdir=folder,
            initialfile=datetime.now().strftime("shinclipboard-%Y%m%d-%H%M%S"),
            defaultextension=f".{'jpg' if default == 'jpeg' else 'png'}",
            filetypes=[("PNG", "*.png"), ("JPEG", "*.jpg *.jpeg")],
        )
        if not path:
            return "break"
        image = self.result()
        try:
            if Path(path).suffix.lower() in (".jpg", ".jpeg"):
                flattened = Image.new("RGB", image.size, "white")
                flattened.paste(image, mask=image.split()[-1] if image.mode == "RGBA" else None)
                flattened.save(path, "JPEG", quality=92)
            else:
                image.save(path, "PNG")
        except (OSError, ValueError) as error:
            messagebox.showerror("画像編集", str(error), parent=self.window)
            return "break"
        self._set_status(f"保存しました: {path}")
        if settings.get("screenshot_copy_after_save", True):
            self.copy_to_clipboard()
        return "break"

    def save_to_history(self, quiet: bool = False) -> None:
        relative, width, height = self.app.store.save_image(self.result())
        self.app.history.add_image(relative, width, height)
        if self.app.config["settings"].get("save_history", True):
            self.app.store.save_history(self.app.history)
        self.app._refresh_history()
        if not quiet:
            self._set_status(f"履歴へ保存しました（{width}×{height}）")

    def _setup_drag_source(self) -> None:
        if not getattr(self.app, "drag_enabled", False):
            return
        try:
            self.drag_handle.drag_source_register(1, "DND_Files")
            self.drag_handle.dnd_bind("<<DragInitCmd>>", self._drag_init)
        except (AttributeError, tk.TclError):
            self.drag_handle.configure(text="（ドラッグは利用できません）")

    def _drag_init(self, _event=None):
        """tkdnd <<DragInitCmd>>: hand the rendered picture over as a real file."""
        try:
            self._dnd_path = self._write_export()
        except (OSError, ValueError):
            return ("refuse_drop",)
        return ("copy", "DND_Files", (str(self._dnd_path),))

    # ----- misc -------------------------------------------------------------------

    def _set_status(self, message: str) -> None:
        self.status_var.set(message)

    @staticmethod
    def _int(variable: tk.StringVar, fallback: int, low: int, high: int) -> int:
        try:
            return max(low, min(high, int(variable.get())))
        except (TypeError, ValueError):
            return fallback


def _pictures_dir() -> Path:
    return Path.home() / "Pictures" / "ShinClipboard"


def write_export(data_dir: Path, image: Image.Image) -> Path:
    """Save a picture as a scratch PNG the clipboard and drag & drop can hand over."""
    folder = Path(data_dir) / EXPORT_DIR
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    path = folder / f"shinclipboard-{stamp}.png"
    image.save(path, "PNG")
    return path


def cleanup_exports(data_dir: Path, max_age: float = EXPORT_MAX_AGE) -> int:
    """Drop drag-and-drop scratch files left behind by earlier sessions."""
    folder = Path(data_dir) / EXPORT_DIR
    if not folder.exists():
        return 0
    cutoff = time.time() - max_age
    removed = 0
    for path in folder.glob("shinclipboard-*"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError:
            continue
    return removed
