from __future__ import annotations

import json
import hashlib
import io
import os
from pathlib import Path
from shutil import copy2, copytree
from tempfile import NamedTemporaryFile
from typing import Any
from uuid import uuid4

from PIL import Image

from .core import ClipboardHistory, HistoryItem


SCHEMA_VERSION = 1
# `Ctrl+Space` used to be the default popup hotkey. It collides with IME
# toggling on Japanese Windows and was opened by accident too often, so it is
# no longer offered and existing configs are migrated to "no hotkey".
LEGACY_POPUP_HOTKEY = "ctrl+space"


def default_config() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "settings": {
            "history_limit": 1000,
            "popup_hotkey": "",
            "fifo_toggle_hotkey": "primary+shift+f",
            "lifo_toggle_hotkey": "primary+shift+l",
            "monitor_toggle_hotkey": "primary+shift+m",
            "double_ctrl_popup": True,
            "start_minimized": False,
            "auto_paste": True,
            "save_history": True,
            "clear_history_on_exit": False,
            "always_on_top": False,
            "font_size": 11,
            "theme": "blue",
            # `primary+shift+s` is "save as" in most editors, and pynput does not
            # suppress the keys it watches, so both would fire. Alt keeps clear.
            "screenshot_hotkey": "primary+alt+s",
            "screenshot_delay_hotkey": "primary+alt+d",
            "screenshot_delay_seconds": 3,
            "screenshot_save_dir": "",
            "screenshot_format": "png",
            "screenshot_copy_after_save": True,
            "screenshot_save_to_history": True,
            "annotation_color": "#e02424",
            "annotation_width": 4,
        },
        "transforms": [
            {"id": str(uuid4()), "name": "各行先頭に > を挿入", "type": "prefix_each_line", "params": {"prefix": "> "}, "hotkey": "", "auto": False, "enabled": True},
            {"id": str(uuid4()), "name": "各行先頭に // を挿入", "type": "prefix_each_line", "params": {"prefix": "// "}, "hotkey": "", "auto": False, "enabled": True},
            {"id": str(uuid4()), "name": "各行を引用符で囲む", "type": "surround_each_line", "params": {"before": "\"", "after": "\""}, "hotkey": "", "auto": False, "enabled": True},
            {"id": str(uuid4()), "name": "001: の連番を挿入", "type": "number_lines", "params": {"start": 1, "width": 3, "separator": ": "}, "hotkey": "", "auto": False, "enabled": True},
        ],
        "groups": [
            {
                "id": str(uuid4()),
                "name": "サンプル",
                "snippets": [
                    {
                        "id": str(uuid4()),
                        "title": "あいさつ",
                        "text": "お世話になっております。",
                        "hotkey": "",
                    }
                ],
            }
        ],
    }


class JsonStore:
    def __init__(self, data_dir: Path, config_path: Path | None = None):
        self.data_dir = Path(data_dir).expanduser().resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.config_path = Path(config_path).expanduser().resolve() if config_path else self.data_dir / "config.json"
        self.history_path = self.data_dir / "history.json"

    def load_config(self) -> dict[str, Any]:
        if not self.config_path.exists():
            config = default_config()
            self.save_config(config)
            return config
        data = self._read(self.config_path)
        if data.get("schema_version") != SCHEMA_VERSION:
            raise ValueError(f"未対応の設定形式です: schema_version={data.get('schema_version')}")
        data.setdefault("settings", {})
        data.setdefault("groups", [])
        data.setdefault("transforms", default_config()["transforms"])
        defaults = default_config()["settings"]
        for key, value in defaults.items():
            data["settings"].setdefault(key, value)
        for group in data["groups"]:
            group.setdefault("snippets", [])
            for snippet in group["snippets"]:
                snippet.setdefault("memo", "")
        if str(data["settings"].get("popup_hotkey", "")).strip().lower() == LEGACY_POPUP_HOTKEY:
            data["settings"]["popup_hotkey"] = ""
            self.save_config(data)
        return data

    def save_config(self, config: dict[str, Any]) -> None:
        config["schema_version"] = SCHEMA_VERSION
        self._atomic_write(self.config_path, config)

    def import_config(self, source: Path) -> dict[str, Any]:
        source = Path(source).expanduser().resolve()
        data = self._read(source)
        if data.get("schema_version") != SCHEMA_VERSION:
            raise ValueError("この設定ファイルのバージョンには対応していません。")
        backup = self.config_path.with_suffix(".json.bak")
        if self.config_path.exists():
            copy2(self.config_path, backup)
        self.save_config(data)
        return self.load_config()

    def export_config(self, destination: Path) -> None:
        destination = Path(destination).expanduser().resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        copy2(self.config_path, destination)

    def load_history(self, limit: int) -> ClipboardHistory:
        if not self.history_path.exists():
            return ClipboardHistory(limit=limit)
        data = self._read(self.history_path)
        return ClipboardHistory((HistoryItem.from_dict(item) for item in data.get("items", [])), limit=limit)

    def save_history(self, history: ClipboardHistory) -> None:
        self._atomic_write(self.history_path, {"schema_version": SCHEMA_VERSION, "items": history.to_list()})
        self.cleanup_images(history)

    def save_image(self, image: Image.Image) -> tuple[str, int, int]:
        normalized = image.convert("RGBA")
        buffer = io.BytesIO()
        normalized.save(buffer, format="PNG")
        data = buffer.getvalue()
        digest = hashlib.sha256(data).hexdigest()
        relative = Path("images") / f"{digest}.png"
        target = self.data_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            target.write_bytes(data)
        return relative.as_posix(), normalized.width, normalized.height

    def image_path(self, relative: str) -> Path:
        target = (self.data_dir / relative).resolve()
        try:
            target.relative_to(self.data_dir)
        except ValueError as error:
            raise ValueError("画像履歴のパスが保存先の外を指しています。") from error
        return target

    def cleanup_images(self, history: ClipboardHistory) -> None:
        image_dir = self.data_dir / "images"
        if not image_dir.exists():
            return
        referenced = {item.image_path for item in history.search() if item.kind == "image"}
        for path in image_dir.glob("*.png"):
            relative = path.relative_to(self.data_dir).as_posix()
            if relative not in referenced:
                path.unlink()

    @staticmethod
    def _read(path: Path) -> dict[str, Any]:
        with path.open("r", encoding="utf-8") as stream:
            data = json.load(stream)
        if not isinstance(data, dict):
            raise ValueError("JSONのルートはオブジェクトである必要があります。")
        return data

    @staticmethod
    def _atomic_write(path: Path, data: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary_name = ""
        try:
            with NamedTemporaryFile("w", encoding="utf-8", newline="\n", dir=path.parent, delete=False) as stream:
                json.dump(data, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                temporary_name = stream.name
            os.replace(temporary_name, path)
        finally:
            if temporary_name and os.path.exists(temporary_name):
                os.unlink(temporary_name)


def default_data_dir() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA", Path.home()))
        return base / "ShinClipboard"
    return Path.home() / ".shinclipboard"


def legacy_data_dir() -> Path:
    """Where the app stored its data before it was renamed to ShinClipboard."""
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA", Path.home()))
        return base / "NewClipboard"
    return Path.home() / ".newclipboard"


def migrate_legacy_data_dir(target: Path | None = None) -> Path | None:
    """Copy pre-rename data into the new directory and return it when copied.

    The old directory is left untouched so the previous build keeps working if
    the user rolls back. Clipboard history and snippets only live here, so a
    copy is preferred over a move even though it leaves data behind.
    """
    destination = Path(target).expanduser() if target else default_data_dir()
    source = legacy_data_dir()
    if destination.exists() or source == destination:
        return None
    if not (source / "config.json").exists():
        return None
    try:
        copytree(source, destination)
    except OSError:
        return None
    return destination
