from __future__ import annotations

import ctypes
import io
import os
import platform
from pathlib import Path

from PIL import Image, ImageGrab


# "the pasteboard could not be asked", as opposed to "it holds no image".
CANNOT_ASK = object()


def clipboard_change_token() -> int | None:
    if os.name == "nt":
        return int(ctypes.windll.user32.GetClipboardSequenceNumber())
    if platform.system() == "Darwin":
        try:
            from AppKit import NSPasteboard

            return int(NSPasteboard.generalPasteboard().changeCount())
        except ImportError:
            return None
    return None


def read_clipboard_image() -> Image.Image | None:
    if platform.system() == "Darwin":
        answer = _read_clipboard_image_mac()
        if answer is not CANNOT_ASK:
            return answer
    try:
        value = ImageGrab.grabclipboard()
    except (OSError, NotImplementedError):
        return None
    return value.copy() if isinstance(value, Image.Image) else None


def _read_clipboard_image_mac() -> Image.Image | None | object:
    """Read an image straight off the pasteboard, or `CANNOT_ASK` without pyobjc.

    Pillow's macOS clipboard reader shells out to `osascript`, which costs a few
    hundred milliseconds and is spent on every copy - text included, since being
    asked is the only way it can report "no image". Asking the pasteboard which
    types it is holding is instant, and a plain no for text. That makes its "no"
    worth trusting, so it is told apart from "there was no way to ask".
    """
    try:
        from AppKit import NSPasteboard, NSPasteboardTypePNG, NSPasteboardTypeTIFF

        pasteboard = NSPasteboard.generalPasteboard()
        wanted = pasteboard.availableTypeFromArray_([NSPasteboardTypePNG, NSPasteboardTypeTIFF])
        if wanted is None:
            return None
        data = pasteboard.dataForType_(wanted)
        if data is None:
            return None
        with Image.open(io.BytesIO(bytes(data))) as opened:
            opened.load()
            return opened.copy()
    except ImportError:
        return CANNOT_ASK
    except (AttributeError, OSError, ValueError):
        return None


def write_clipboard_image(path: Path) -> int | None:
    """Put an image on the clipboard and return the token for *this* write.

    The token is read while the clipboard is still locked, so it identifies our
    own change and nothing else. Callers that suppress their own writes must use
    this value rather than re-reading the token afterwards: between the write and
    a later read another application can copy something, and remembering that
    token would silently swallow the other application's copy.
    """
    if os.name == "nt":
        import win32clipboard

        image = Image.open(path).convert("RGB")
        buffer = io.BytesIO()
        image.save(buffer, "BMP")
        dib = buffer.getvalue()[14:]
        win32clipboard.OpenClipboard()
        try:
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardData(win32clipboard.CF_DIB, dib)
            return clipboard_change_token()
        finally:
            win32clipboard.CloseClipboard()
    if platform.system() == "Darwin":
        from AppKit import NSData, NSPasteboard, NSPasteboardTypePNG

        data = NSData.dataWithContentsOfFile_(str(path))
        if data is None:
            raise OSError(f"画像ファイルを読み込めません: {path}")
        pasteboard = NSPasteboard.generalPasteboard()
        pasteboard.clearContents()
        if not pasteboard.setData_forType_(data, NSPasteboardTypePNG):
            raise OSError("画像をクリップボードへ書き込めませんでした。")
        return int(pasteboard.changeCount())
    raise RuntimeError("このOSでは画像クリップボードへの書き戻しに対応していません。")
