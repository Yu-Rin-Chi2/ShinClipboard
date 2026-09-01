from __future__ import annotations

import platform
import time
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


class DoubleTapDetector:
    def __init__(self, interval: float = 0.35):
        self.interval = interval
        self.last_press = 0.0
        self.is_down = False

    def press(self, now: float | None = None) -> bool:
        if self.is_down:
            return False
        self.is_down = True
        current = time.monotonic() if now is None else now
        if current - self.last_press <= self.interval:
            self.last_press = 0.0
            return True
        self.last_press = current
        return False

    def release(self) -> None:
        self.is_down = False


class GlobalHotkeyService:
    def __init__(self, mappings: dict[str, Callable[[], None]], on_double_ctrl: Callable[[], None] | None = None):
        self.mappings = mappings
        self.on_double_ctrl = on_double_ctrl
        self.listener = None
        self.double_ctrl_listener = None
        self._double_ctrl = DoubleTapDetector()

    def start(self) -> None:
        from pynput import keyboard

        converted: dict[str, Callable[[], None]] = {}
        for hotkey, callback in self.mappings.items():
            if hotkey.strip():
                converted[portable_to_pynput(hotkey)] = callback
        self.listener = keyboard.GlobalHotKeys(converted)
        self.listener.start()
        if self.on_double_ctrl:
            ctrl_keys = {keyboard.Key.ctrl, keyboard.Key.ctrl_l, keyboard.Key.ctrl_r}

            def on_press(key) -> None:
                if key not in ctrl_keys:
                    return
                if self._double_ctrl.press():
                    self.on_double_ctrl()

            def on_release(key) -> None:
                if key in ctrl_keys:
                    self._double_ctrl.release()

            self.double_ctrl_listener = keyboard.Listener(on_press=on_press, on_release=on_release)
            self.double_ctrl_listener.start()

    def stop(self) -> None:
        if self.listener:
            self.listener.stop()
            self.listener = None
        if self.double_ctrl_listener:
            self.double_ctrl_listener.stop()
            self.double_ctrl_listener = None
