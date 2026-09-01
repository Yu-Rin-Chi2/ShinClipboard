from __future__ import annotations

import platform
from collections.abc import Callable


def portable_to_pynput(value: str) -> str:
    """Translate portable `primary+...` notation to pynput notation."""
    parts = [part.strip().lower() for part in value.split("+") if part.strip()]
    translated: list[str] = []
    special = {
        "primary": "cmd" if platform.system() == "Darwin" else "ctrl",
        "control": "ctrl",
        "option": "alt",
        "return": "enter",
    }
    named = {"ctrl", "cmd", "shift", "alt", "enter", "space", "tab", "esc", "delete", "backspace"}
    for part in parts:
        part = special.get(part, part)
        translated.append(f"<{part}>" if part in named or part.startswith("f") and part[1:].isdigit() else part)
    if len(translated) < 2:
        raise ValueError("ホットキーは修飾キーを含む2キー以上で指定してください。")
    return "+".join(translated)


class GlobalHotkeyService:
    def __init__(self, mappings: dict[str, Callable[[], None]]):
        self.mappings = mappings
        self.listener = None

    def start(self) -> None:
        from pynput import keyboard

        converted: dict[str, Callable[[], None]] = {}
        for hotkey, callback in self.mappings.items():
            if hotkey.strip():
                converted[portable_to_pynput(hotkey)] = callback
        self.listener = keyboard.GlobalHotKeys(converted)
        self.listener.start()

    def stop(self) -> None:
        if self.listener:
            self.listener.stop()
            self.listener = None
