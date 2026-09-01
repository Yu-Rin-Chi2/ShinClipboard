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


def write_clipboard_image(path: Path) -> None:
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
        finally:
            win32clipboard.CloseClipboard()
        return
    if platform.system() == "Darwin":
        from AppKit import NSData, NSPasteboard, NSPasteboardTypePNG

        pasteboard = NSPasteboard.generalPasteboard()
        pasteboard.clearContents()
        data = NSData.dataWithContentsOfFile_(str(path))
        pasteboard.setData_forType_(data, NSPasteboardTypePNG)
        return
    raise RuntimeError("このOSでは画像クリップボードへの書き戻しに対応していません。")
