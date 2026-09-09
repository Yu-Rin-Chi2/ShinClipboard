from __future__ import annotations

import tkinter as tk
from typing import Callable

from PIL import Image, ImageTk

from .macos import menu_bar_height
from .platform_support import UI_FONT_FAMILY
from .screenshot import (
    ScreenCapture,
    capture_virtual_screen,
    dpi_aware_windows,
    monitor_rects,
    normalize_rect,
    rect_under_point,
    window_rects,
)


Rect = tuple[int, int, int, int]

HINT = "ドラッグ: 範囲   クリック: ウィンドウ   F: このモニタ   A: 全画面   Esc/右クリック: 中止"
# A full-screen borderless window that will not close would lock the desktop, so
# the selector always arms a watchdog on top of the explicit cancel keys.
WATCHDOG_MS = 120_000
HINT_MARGIN = 28  # how far below the top of the usable area the hint sits
DIM_FACTOR = 0.4
_DIM_LUT = [int(value * DIM_FACTOR) for value in range(256)] * 3


class RegionSelector:
    """Frozen-screen overlay for picking a screen region.

    The overlay window is created per-monitor DPI aware, so its Tk coordinates
    are real pixels and line up 1:1 with the captured picture. Result delivery is
    a callback rather than `wait_window`, because the app's clipboard poll keeps
    running underneath and re-entering the Tk event loop here makes that racy.

    Painting is arranged so that a mouse move only touches a small area: the
    whole screen is shown once, already darkened, and the selection is a bright
    crop pasted on top. Dimming with translucent canvas rectangles instead would
    make Tk repaint the entire (very large) canvas on every movement.
    """

    def __init__(self, root: tk.Tk, on_done: Callable[[Image.Image | None], None]):
        self.root = root
        self.on_done = on_done
        self.capture: ScreenCapture | None = None
        self.window: tk.Toplevel | None = None
        self._monitors: list[Rect] = []
        self._windows: list[Rect] = []
        self._origin = (0, 0)
        self._start: tuple[int, int] | None = None
        self._last_pointer = (0, 0)
        self._dragging = False
        self._backdrop_image: Image.Image | None = None
        self._photo: ImageTk.PhotoImage | None = None
        self._bright_photo: ImageTk.PhotoImage | None = None
        self._pending: Rect | None = None
        self._paint_job: str | None = None
        self._watchdog: str | None = None
        self._finished = False

    def start(self) -> None:
        """Grab the screen and show the overlay, or report failure through on_done.

        A borderless topmost window that outlives a half-finished setup would
        cover the desktop with nothing bound to close it, so everything from the
        grab to the last key binding runs under one guard and the window is only
        made visible once the exits are armed.
        """
        try:
            with dpi_aware_windows():
                self.capture = capture_virtual_screen()
                self._monitors = monitor_rects()
                self._windows = window_rects()
                self._build_window()
        except Exception:
            self._close(None)
            raise
        window = self.window
        if window is not None:
            window.deiconify()
            # Everything above was drawn while the window was still withdrawn.
            # macOS Tk keeps no drawing for a window in that state and does not
            # catch up when it is mapped, so the overlay would come up as an
            # empty grey sheet over the whole desktop. Flushing the idle work
            # paints it without re-entering the event loop, which the clipboard
            # poll running underneath depends on.
            window.update_idletasks()
            window.after(10, self._take_focus)

    # ----- construction -----------------------------------------------------------

    def _build_window(self) -> None:
        assert self.capture is not None
        left, top, right, bottom = self.capture.bounds
        self._origin = (left, top)
        self._last_pointer = (left, top)
        window = tk.Toplevel(self.root)
        self.window = window
        window.withdraw()  # shown by start() once every exit is wired up
        window.overrideredirect(True)
        window.attributes("-topmost", True)
        # Always use an explicit sign so a negative left edge is a position, not a flag.
        window.geometry(f"{right - left}x{bottom - top}+{left}+{top}")
        window.configure(background="#000000", cursor="crosshair")
        window.protocol("WM_DELETE_WINDOW", self.cancel)

        self.canvas = tk.Canvas(window, highlightthickness=0, borderwidth=0, cursor="crosshair")
        self.canvas.pack(fill="both", expand=True)
        self._backdrop_image = self._backdrop()
        self._photo = ImageTk.PhotoImage(self._backdrop_image.point(_DIM_LUT), master=window)
        self.canvas.create_image(0, 0, image=self._photo, anchor="nw")
        self.canvas.create_image(0, 0, anchor="nw", tags="bright", state="hidden")
        self.canvas.create_rectangle(0, 0, 0, 0, outline="#ffffff", width=2, dash=(5, 3), tags="selection", state="hidden")
        self.canvas.create_rectangle(0, 0, 0, 0, outline="#38bdf8", width=3, tags="hover", state="hidden")
        self.canvas.create_text(0, 0, text="", fill="#ffffff", anchor="nw", font=(UI_FONT_FAMILY, 16, "bold"), tags="size")
        self.canvas.create_text(
            (right - left) // 2, self._hint_y(), text=HINT, fill="#ffffff", font=(UI_FONT_FAMILY, 16), tags="hint"
        )

        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_motion)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<Motion>", self._on_hover)
        for sequence in ("<Button-3>", "<Escape>"):
            window.bind(sequence, lambda _: self.cancel())
        window.bind("<KeyPress>", self._on_key)
        self._watchdog = window.after(WATCHDOG_MS, self.cancel)

    def _hint_y(self) -> int:
        """Where the hint clears whatever the desktop draws on top of the overlay.

        On macOS that is the menu bar, which only exists along the top of the
        main display; the main display's top edge is y=0 in screen coordinates,
        so the overlay's own origin converts it into canvas coordinates.
        """
        inset = menu_bar_height()
        if not inset:
            return HINT_MARGIN
        return -self._origin[1] + inset + HINT_MARGIN

    def _backdrop(self) -> Image.Image:
        """The frozen screen at overlay size.

        With DPI awareness the picture is already 1:1. Without it the grab is
        still physical while the window is measured in virtualised units, so the
        picture has to shrink or the selection would not sit where it looks.
        """
        assert self.capture is not None
        left, top, right, bottom = self.capture.bounds
        size = (max(1, right - left), max(1, bottom - top))
        if size == self.capture.image.size:
            return self.capture.image
        return self.capture.image.resize(size, Image.Resampling.BILINEAR)

    def _take_focus(self) -> None:
        """Borderless windows are not given focus on Windows, so take it."""
        if self.window is None:
            return
        try:
            self.window.focus_force()
            self.window.grab_set()
        except tk.TclError:
            self.cancel()  # without focus there is no way to press Escape

    # ----- interaction ------------------------------------------------------------

    def _screen(self, event) -> tuple[int, int]:
        return (event.x + self._origin[0], event.y + self._origin[1])

    def _canvas_rect(self, rect: Rect) -> Rect:
        left, top, right, bottom = rect
        return (left - self._origin[0], top - self._origin[1], right - self._origin[0], bottom - self._origin[1])

    def _on_press(self, event) -> None:
        self._start = self._screen(event)
        self._dragging = False
        self.canvas.itemconfigure("hover", state="hidden")
        self.canvas.itemconfigure("hint", state="hidden")

    def _on_motion(self, event) -> None:
        if self._start is None:
            return
        x, y = self._screen(event)
        self._last_pointer = (x, y)
        if not self._dragging and (abs(x - self._start[0]) > 3 or abs(y - self._start[1]) > 3):
            self._dragging = True
        if self._dragging:
            self._show_selection(normalize_rect(*self._start, x, y))

    def _on_release(self, event) -> None:
        if self._start is None:
            return
        start, self._start = self._start, None
        if self._dragging:
            self._finish(normalize_rect(*start, *self._screen(event)))
            return
        # A click without a drag means "grab the window under the cursor".
        target = rect_under_point(self._windows, *self._screen(event))
        self._finish(target or self._monitor_at(*self._screen(event)))

    def _on_hover(self, event) -> None:
        self._last_pointer = self._screen(event)
        if self._start is not None:
            return
        target = rect_under_point(self._windows, *self._last_pointer)
        if target is None:
            self.canvas.itemconfigure("hover", state="hidden")
            return
        current = tuple(int(value) for value in self.canvas.coords("hover"))
        wanted = self._canvas_rect(target)
        if current != wanted:
            self.canvas.coords("hover", *wanted)
            self.canvas.itemconfigure("hover", state="normal")
            self._show_size(target)

    def _on_key(self, event) -> None:
        key = (event.char or "").lower()
        if key == "f":
            # Motion events on this window already report the overlay's own
            # coordinate space; winfo_pointerxy would answer in the main
            # thread's virtualised one, which differs under mixed DPI.
            self._finish(self._monitor_at(*self._last_pointer))
        elif key == "a":
            assert self.capture is not None
            self._finish(self.capture.bounds)

    def _monitor_at(self, x: int, y: int) -> Rect:
        assert self.capture is not None
        return rect_under_point(self._monitors, x, y) or self.capture.bounds

    # ----- painting ---------------------------------------------------------------

    def _show_selection(self, rect: Rect) -> None:
        """Queue a repaint; several mouse events between frames collapse into one."""
        self._pending = rect
        if self._paint_job is None and self.window is not None:
            self._paint_job = self.window.after_idle(self._paint_pending)

    def _paint_pending(self) -> None:
        self._paint_job = None
        rect, self._pending = self._pending, None
        if rect is None or self.window is None or self._backdrop_image is None:
            return
        left, top, right, bottom = self._canvas_rect(rect)
        if right - left < 1 or bottom - top < 1:
            self.canvas.itemconfigure("bright", state="hidden")
            self.canvas.itemconfigure("selection", state="hidden")
            return
        # Cropping the bright pixels is cheap even for a whole monitor, and
        # updating one image item only invalidates the area it covers.
        self._bright_photo = ImageTk.PhotoImage(self._backdrop_image.crop((left, top, right, bottom)), master=self.window)
        self.canvas.itemconfigure("bright", image=self._bright_photo, state="normal")
        self.canvas.coords("bright", left, top)
        self.canvas.coords("selection", left, top, right, bottom)
        self.canvas.itemconfigure("selection", state="normal")
        self._show_size(rect)

    def _show_size(self, rect: Rect) -> None:
        left, top, right, bottom = self._canvas_rect(rect)
        self.canvas.itemconfigure("size", text=f"{right - left} × {bottom - top}")
        self.canvas.coords("size", left, max(0, top - 26))
        self.canvas.tag_raise("size")

    # ----- teardown ---------------------------------------------------------------

    def _finish(self, rect: Rect) -> None:
        capture = self.capture
        if capture is None:
            self.cancel()
            return
        left, top, right, bottom = rect
        if right - left < 2 or bottom - top < 2:
            self.cancel()
            return
        try:
            image = capture.crop_screen(rect)
        except (OSError, ValueError):
            image = None  # a failed crop must still tear the overlay down
        self._close(image)

    def cancel(self) -> None:
        self._close(None)

    def _close(self, image: Image.Image | None) -> None:
        """Tear everything down exactly once and always report a result.

        Every step is independent: a failure to cancel the watchdog must not stop
        the window from being destroyed, and a failure to destroy the window must
        not stop the caller from learning that the capture is over and restoring
        its own windows.
        """
        if self._finished:
            return
        self._finished = True
        window, self.window = self.window, None
        watchdog, self._watchdog = self._watchdog, None
        paint_job, self._paint_job = self._paint_job, None
        if window is not None:
            for step in (
                lambda: window.after_cancel(watchdog) if watchdog else None,
                lambda: window.after_cancel(paint_job) if paint_job else None,
                window.grab_release,
                window.destroy,
            ):
                try:
                    step()
                except (tk.TclError, ValueError, RuntimeError):
                    pass
        self._photo = None
        self._bright_photo = None
        self._backdrop_image = None
        self.capture = None
        self.on_done(image)
