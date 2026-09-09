"""Screen geometry and grabbing on macOS, through Quartz.

Rectangles here are points with the origin at the top-left of the main display.
That is the same space Tk measures `geometry()` in, so the selection overlay can
use these values unchanged. The grab itself comes back at the display's backing
resolution, and `ScreenCapture.scale` bridges the two.

Every entry point degrades to "nothing to offer" rather than raising when pyobjc
is missing or a call fails, so the caller can fall back to Pillow's plain grab.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

from PIL import Image


Rect = tuple[int, int, int, int]

MAX_DISPLAYS = 16
# Ordinary application windows sit on layer 0. Everything above it is menu bar
# extras, the Dock and other system furniture: already painted into the frozen
# screen, so offering them as click targets would only get in the way.
NORMAL_WINDOW_LAYER = 0
MIN_WINDOW_SIDE = 8
SCREENCAPTURE_TIMEOUT = 20


def _quartz():
    try:
        import Quartz
    except ImportError:
        return None
    return Quartz


def monitor_rects() -> list[Rect]:
    """One rectangle per active display."""
    quartz = _quartz()
    if quartz is None:
        return []
    try:
        error, display_ids, _count = quartz.CGGetActiveDisplayList(MAX_DISPLAYS, None, None)
    except (AttributeError, ValueError):
        return []
    if error or not display_ids:
        return []
    rects: list[Rect] = []
    for display in display_ids:
        bounds = quartz.CGDisplayBounds(display)
        left, top = int(round(bounds.origin.x)), int(round(bounds.origin.y))
        rects.append((left, top, left + int(round(bounds.size.width)), top + int(round(bounds.size.height))))
    return rects


def virtual_bounds() -> Rect:
    """The rectangle covering every display."""
    rects = monitor_rects()
    if not rects:
        return (0, 0, 0, 0)
    return (
        min(rect[0] for rect in rects),
        min(rect[1] for rect in rects),
        max(rect[2] for rect in rects),
        max(rect[3] for rect in rects),
    )


def window_rects() -> list[Rect]:
    """Visible ordinary window frames, front-most first.

    The window server already returns them in front-to-back order, which is what
    `rect_under_point` expects. Our own windows are skipped: the app hides them
    before the grab, so a rectangle for one would highlight empty desktop.
    """
    quartz = _quartz()
    if quartz is None:
        return []
    try:
        entries = quartz.CGWindowListCopyWindowInfo(
            quartz.kCGWindowListOptionOnScreenOnly | quartz.kCGWindowListExcludeDesktopElements,
            quartz.kCGNullWindowID,
        )
    except (AttributeError, ValueError):
        return []
    if not entries:
        return []
    own_pid = os.getpid()
    rects: list[Rect] = []
    for entry in entries:
        if entry.get("kCGWindowLayer") != NORMAL_WINDOW_LAYER:
            continue
        if entry.get("kCGWindowOwnerPID") == own_pid:
            continue
        if float(entry.get("kCGWindowAlpha", 1.0)) <= 0.0:
            continue
        bounds = entry.get("kCGWindowBounds")
        if not bounds:
            continue
        left, top = int(round(bounds["X"])), int(round(bounds["Y"]))
        width, height = int(round(bounds["Width"])), int(round(bounds["Height"]))
        if width < MIN_WINDOW_SIDE or height < MIN_WINDOW_SIDE:
            continue
        rects.append((left, top, left + width, top + height))
    return rects


def grab_screen(bounds: Rect | None = None) -> Image.Image | None:
    """Every display as one picture, or None when macOS refuses to hand it over.

    Returning None rather than raising lets the caller fall back to Pillow, which
    manages the main display on its own.
    """
    image = _grab_quartz()
    if image is not None:
        return image
    return _grab_screencapture(bounds or virtual_bounds())


# ----- internals ------------------------------------------------------------------


def _grab_quartz() -> Image.Image | None:
    """Read the whole desktop straight out of the window server.

    `CGWindowListCreateImage` is deprecated as of macOS 14 but still the only
    call that returns every display in one shot without spawning a process, so
    it is tried first and the `screencapture` fallback covers its removal.
    """
    quartz = _quartz()
    if quartz is None or not hasattr(quartz, "CGWindowListCreateImage"):
        return None
    try:
        image_ref = quartz.CGWindowListCreateImage(
            quartz.CGRectInfinite,
            quartz.kCGWindowListOptionOnScreenOnly,
            quartz.kCGNullWindowID,
            quartz.kCGWindowImageBestResolution,
        )
    except (AttributeError, ValueError):
        return None
    if image_ref is None:
        return None
    try:
        return _cgimage_to_pil(quartz, image_ref)
    except (AttributeError, ValueError, TypeError):
        return None


def _cgimage_to_pil(quartz, image_ref) -> Image.Image | None:
    """Copy a CGImage's pixels into a Pillow image without re-encoding them.

    Screen images come back as 32 bits per pixel with the alpha byte unused. Its
    position and the channel order both follow the bitmap info, so that is read
    rather than assumed; anything else is left to the caller's fallback.
    """
    width = int(quartz.CGImageGetWidth(image_ref))
    height = int(quartz.CGImageGetHeight(image_ref))
    if width <= 0 or height <= 0:
        return None
    if int(quartz.CGImageGetBitsPerPixel(image_ref)) != 32:
        return None
    raw_mode = _raw_mode(quartz, image_ref)
    if raw_mode is None:
        return None
    provider = quartz.CGImageGetDataProvider(image_ref)
    if provider is None:
        return None
    data = quartz.CGDataProviderCopyData(provider)
    if data is None:
        return None
    stride = int(quartz.CGImageGetBytesPerRow(image_ref))
    buffer = bytes(data)
    if len(buffer) < stride * height:
        return None
    return Image.frombuffer("RGB", (width, height), buffer, "raw", raw_mode, stride, 1)


def _raw_mode(quartz, image_ref) -> str | None:
    """The Pillow raw mode matching this CGImage's byte layout."""
    info = int(quartz.CGImageGetBitmapInfo(image_ref))
    order = info & quartz.kCGBitmapByteOrderMask
    alpha = info & quartz.kCGBitmapAlphaInfoMask
    alpha_first = alpha in (quartz.kCGImageAlphaPremultipliedFirst, quartz.kCGImageAlphaFirst, quartz.kCGImageAlphaNoneSkipFirst)
    if order == quartz.kCGBitmapByteOrder32Little:
        # Little-endian words reverse the component order in memory, so an
        # "alpha first" pixel is stored B, G, R, A.
        return "BGRX" if alpha_first else "XBGR"
    if order in (quartz.kCGBitmapByteOrder32Big, 0):
        return "XRGB" if alpha_first else "RGBX"
    return None


def _grab_screencapture(bounds: Rect) -> Image.Image | None:
    """Fallback grab through the `screencapture` tool.

    It covers every display in one file when given an explicit rectangle, which
    Pillow's own macOS grab does not do.
    """
    left, top, right, bottom = bounds
    width, height = right - left, bottom - top
    if width <= 0 or height <= 0:
        return None
    handle, name = tempfile.mkstemp(suffix=".png")
    os.close(handle)
    path = Path(name)
    try:
        result = subprocess.run(
            ["screencapture", "-x", "-o", "-R", f"{left},{top},{width},{height}", str(path)],
            capture_output=True,
            timeout=SCREENCAPTURE_TIMEOUT,
        )
        if result.returncode != 0 or not path.exists() or path.stat().st_size == 0:
            return None
        with Image.open(path) as opened:
            return opened.convert("RGB")
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    finally:
        path.unlink(missing_ok=True)
