from __future__ import annotations

import ctypes
import os
from contextlib import contextmanager
from ctypes import wintypes
from dataclasses import dataclass
from typing import Iterator

from PIL import Image, ImageGrab


Rect = tuple[int, int, int, int]

SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79

# Windows 10 1607+. Windows created while this context is active report and
# accept real pixels instead of the DPI-virtualised ones.
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4

DWMWA_CLOAKED = 14
DWMWA_EXTENDED_FRAME_BOUNDS = 9

GA_ROOT = 2


@dataclass(frozen=True)
class ScreenCapture:
    """A frozen picture of every monitor, plus how to map screen coordinates onto it.

    `origin` and any rectangle passed to `crop_screen` are in the same coordinate
    space the selection overlay uses. When the overlay runs DPI aware that space is
    physical pixels and `scale` is 1.0; otherwise `scale` converts from the
    virtualised coordinates Tk reports into the picture's pixels.
    """

    image: Image.Image
    origin: tuple[int, int]
    scale: float = 1.0

    @property
    def bounds(self) -> Rect:
        left, top = self.origin
        return (left, top, left + round(self.image.width / self.scale), top + round(self.image.height / self.scale))

    def crop_screen(self, rect: Rect) -> Image.Image:
        left, top, right, bottom = normalize_rect(*rect)
        origin_x, origin_y = self.origin
        box = (
            int(round((left - origin_x) * self.scale)),
            int(round((top - origin_y) * self.scale)),
            int(round((right - origin_x) * self.scale)),
            int(round((bottom - origin_y) * self.scale)),
        )
        box = (
            max(0, min(box[0], self.image.width)),
            max(0, min(box[1], self.image.height)),
            max(0, min(box[2], self.image.width)),
            max(0, min(box[3], self.image.height)),
        )
        if box[2] <= box[0] or box[3] <= box[1]:
            return self.image.copy()
        return self.image.crop(box)


def normalize_rect(x0: float, y0: float, x1: float, y1: float) -> Rect:
    """Order a dragged rectangle so left <= right and top <= bottom."""
    left, right = sorted((int(round(x0)), int(round(x1))))
    top, bottom = sorted((int(round(y0)), int(round(y1))))
    return (left, top, right, bottom)


def rect_under_point(rects: list[Rect], x: float, y: float) -> Rect | None:
    """First rectangle containing the point. Callers pass them front-to-back."""
    for rect in rects:
        left, top, right, bottom = rect
        if left <= x < right and top <= y < bottom:
            return rect
    return None


@contextmanager
def dpi_aware_windows() -> Iterator[bool]:
    """Create per-monitor DPI aware windows inside this block.

    A window's DPI awareness is fixed when it is created, so the app's existing
    windows keep the scaling they were built with; only what we create here sees
    real pixels. Yields False when the platform cannot do it, and the caller then
    falls back to a single uniform scale factor.
    """
    user32 = _user32()
    if user32 is None or not hasattr(user32, "SetThreadDpiAwarenessContext"):
        yield False
        return
    user32.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
    user32.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
    try:
        previous = user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2))
    except OSError:
        yield False
        return
    if not previous:
        yield False
        return
    try:
        yield True
    finally:
        try:
            user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(previous))
        except OSError:
            pass


def virtual_bounds() -> Rect:
    """The rectangle covering every monitor, in the calling thread's DPI context."""
    user32 = _user32()
    if user32 is None:
        return (0, 0, 0, 0)
    left = user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
    top = user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
    return (left, top, left + user32.GetSystemMetrics(SM_CXVIRTUALSCREEN), top + user32.GetSystemMetrics(SM_CYVIRTUALSCREEN))


def grab_screen() -> Image.Image:
    """Every monitor as one picture. Pillow already grabs at full resolution."""
    try:
        return ImageGrab.grab(all_screens=True).convert("RGB")
    except TypeError:  # all_screens is Windows only
        return ImageGrab.grab().convert("RGB")


def capture_virtual_screen() -> ScreenCapture:
    """Grab every monitor and describe how overlay coordinates map onto the result."""
    bounds = virtual_bounds()
    image = grab_screen()
    width = bounds[2] - bounds[0]
    scale = image.width / width if width > 0 else 1.0
    return ScreenCapture(image=image, origin=(bounds[0], bounds[1]), scale=scale or 1.0)


def monitor_rects() -> list[Rect]:
    """One rectangle per monitor, in the calling thread's DPI context."""
    user32 = _user32()
    if user32 is None:
        return []
    rects: list[Rect] = []
    callback_type = ctypes.WINFUNCTYPE(
        ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(wintypes.RECT), ctypes.c_double
    )

    def callback(_monitor, _hdc, rect_pointer, _data) -> int:
        rect = rect_pointer.contents
        rects.append((rect.left, rect.top, rect.right, rect.bottom))
        return 1

    try:
        user32.EnumDisplayMonitors(None, None, callback_type(callback), 0)
    except OSError:
        return []
    return rects


def window_rects() -> list[Rect]:
    """Visible top-level window rectangles, front-most first."""
    user32 = _user32()
    if user32 is None:
        return []
    screen = virtual_bounds()
    rects: list[Rect] = []
    callback_type = ctypes.WINFUNCTYPE(ctypes.c_int, wintypes.HWND, wintypes.LPARAM)

    def callback(hwnd, _param) -> int:
        rect = _window_rect(hwnd)
        if rect is None:
            return 1
        left, top, right, bottom = rect
        if right - left < 8 or bottom - top < 8:
            return 1
        if right <= screen[0] or bottom <= screen[1] or left >= screen[2] or top >= screen[3]:
            return 1
        rects.append(rect)
        return 1

    try:
        user32.EnumWindows(callback_type(callback), 0)
    except OSError:
        return []
    return rects


# ----- internals ------------------------------------------------------------------


def _user32():
    if os.name != "nt":
        return None
    try:
        return ctypes.windll.user32
    except (AttributeError, OSError):
        return None


def _window_rect(hwnd) -> Rect | None:
    """The window's real frame, skipping hidden, cloaked and zero-size windows."""
    user32 = _user32()
    if user32 is None or not user32.IsWindowVisible(hwnd):
        return None
    if user32.IsIconic(hwnd):
        return None
    try:
        dwmapi = ctypes.windll.dwmapi
    except (AttributeError, OSError):
        dwmapi = None
    if dwmapi is not None:
        cloaked = ctypes.c_int(0)
        if dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, ctypes.byref(cloaked), ctypes.sizeof(cloaked)) == 0:
            if cloaked.value:
                return None  # on another virtual desktop, or a suspended store app
    rect = wintypes.RECT()
    # The DWM frame excludes the invisible resize border, so the highlight lines
    # up with what the user actually sees.
    if dwmapi is not None:
        if dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(rect), ctypes.sizeof(rect)) == 0:
            if rect.right > rect.left and rect.bottom > rect.top:
                return (rect.left, rect.top, rect.right, rect.bottom)
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None
    return (rect.left, rect.top, rect.right, rect.bottom)
