from __future__ import annotations

import ctypes
import io
import os
import platform
from pathlib import Path

from PIL import Image, ImageGrab


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
    try:
        value = ImageGrab.grabclipboard()
    except (OSError, NotImplementedError):
        return None
    return value.copy() if isinstance(value, Image.Image) else None


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

        pasteboard = NSPasteboard.generalPasteboard()
        pasteboard.clearContents()
        data = NSData.dataWithContentsOfFile_(str(path))
        pasteboard.setData_forType_(data, NSPasteboardTypePNG)
        return int(pasteboard.changeCount())
    raise RuntimeError("このOSでは画像クリップボードへの書き戻しに対応していません。")
