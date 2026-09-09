from __future__ import annotations

import platform
import time
from collections.abc import Callable
from functools import lru_cache

from .macos import KEY_CODES, accessibility_trusted, warm_up_input_monitoring_api
from .platform_support import IS_MAC


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
    """Detects two bare taps of the same key within `interval` seconds.

    A tap only counts when the key was pressed on its own; pressing any other
    key (for example `Ctrl+C`) cancels the pending tap so that quick shortcut
    sequences never open the popup by accident.
    """

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

    def cancel(self) -> None:
        """Forget the pending tap because another key was pressed."""
        self.last_press = 0.0

    def release(self) -> None:
        self.is_down = False


def build_listener(mappings: dict[str, Callable[[], None]]):
    """A listener that fires `mappings` when their key combinations are pressed.

    On macOS this is not pynput's own `GlobalHotKeys`. That one compares the
    character a key produced, and macOS runs every key through the layout before
    reporting it: holding Option turns S into "ß", which never equals the "s" the
    shortcut was written with, so no combination containing Option ever fires.
    The class below compares where the key sits instead, which Option does not
    change. Everywhere else pynput's own version is already right.
    """
    from pynput import keyboard

    if IS_MAC:
        return positional_hotkeys_class()(mappings)
    return keyboard.GlobalHotKeys(mappings)


def reenable_event_tap(tap) -> None:
    """Switch an event tap macOS turned off back on.

    Kept out of the listener so the recovery can be watched from a test without
    a real tap, and so importing this module still never imports Quartz.
    """
    from Quartz import CGEventTapEnable

    CGEventTapEnable(tap, True)


@lru_cache(maxsize=1)
def safe_listener_class():
    """pynput's macOS listener with the system-defined events masked out.

    pynput asks the event tap for `NSSystemDefined` events so it can report media
    keys, and turns each one into an `NSEvent` on its own listener thread. On
    macOS 15 the Text Services Manager runs inside that conversion and asserts
    that it is on the main queue, which kills the whole process with SIGILL the
    moment a volume key, the Globe key or an input-source double-tap comes
    through - it looks like the app simply vanished. Nothing here wants media
    keys, so they are left out of the tap: what never arrives is never converted.
    Built on first use so importing this module never imports pynput or Quartz.
    """
    from pynput import keyboard
    from Quartz import (
        CGEventMaskBit,
        kCGEventFlagsChanged,
        kCGEventKeyDown,
        kCGEventKeyUp,
        kCGEventTapDisabledByTimeout,
        kCGEventTapDisabledByUserInput,
    )

    class _SafeListener(keyboard.Listener):
        _EVENTS = CGEventMaskBit(kCGEventKeyDown) | CGEventMaskBit(kCGEventKeyUp) | CGEventMaskBit(kCGEventFlagsChanged)

        #: macOS reports these through the tap's own callback instead of a key.
        _TAP_DISABLED = (kCGEventTapDisabledByTimeout, kCGEventTapDisabledByUserInput)

        _tap = None

        def _create_event_tap(self):
            """Keep the tap, which pynput drops into a local of its run loop."""
            self._tap = super()._create_event_tap()
            return self._tap

        def _handle_message(self, proxy, event_type, event, refcon, injected):
            """Take the tap back after macOS switches it off.

            A callback that misses the deadline - one busy moment on the UI
            thread is enough, since it holds the interpreter lock the tap thread
            needs - costs the tap every event from then on, and nothing turns it
            back on by itself. pynput would also read this notification as a key
            and report a phantom release, which leaves a shortcut half pressed.
            """
            if event_type in self._TAP_DISABLED:
                # Without the permission macOS switches the tap off again at the
                # next event, so asking for it back is a loop that never wins;
                # the app watches for the permission and starts a fresh listener
                # when it arrives instead.
                if self._tap is not None and accessibility_trusted():
                    reenable_event_tap(self._tap)
                return
            super()._handle_message(proxy, event_type, event, refcon, injected)

    return _SafeListener


@lru_cache(maxsize=1)
def positional_hotkeys_class():
    """Built on first use, so importing this module never imports pynput."""
    from pynput import keyboard

    class _PositionalHotKeys(safe_listener_class()):
        """Global hotkeys matched by key position rather than typed character."""

        def __init__(self, mappings: dict[str, Callable[[], None]]):
            self._hotkeys: list = []
            super().__init__(on_press=self._on_press, on_release=self._on_release)
            self._hotkeys = [
                keyboard.HotKey([self._positional(key) for key in keyboard.HotKey.parse(spec)], callback)
                for spec, callback in mappings.items()
            ]

        def _positional(self, key):
            """Reduce a key to the code for its place on the board.

            Incoming keys already carry that code next to the character they
            produced; a key parsed out of a shortcut carries only the character,
            so the layout-independent table supplies it. Anything not in the
            table - a modifier, a named key, a character from a layout this does
            not describe - falls back to pynput's own normalisation.
            """
            char = getattr(key, "char", None)
            code = getattr(key, "vk", None)
            if code is None and char is not None:
                code = KEY_CODES.get(char.lower())
            if code is not None:
                return keyboard.KeyCode.from_vk(code)
            return self.canonical(key)

        def _on_press(self, key, injected) -> None:
            if injected:
                return  # our own replayed Cmd+V must not trigger a shortcut
            for hotkey in self._hotkeys:
                hotkey.press(self._positional(key))

        def _on_release(self, key, injected) -> None:
            if injected:
                return
            for hotkey in self._hotkeys:
                hotkey.release(self._positional(key))

    return _PositionalHotKeys


class GlobalHotkeyService:
    def __init__(
        self,
        mappings: dict[str, Callable[[], None]],
        on_double_ctrl: Callable[[], None] | None = None,
    ):
        self.mappings = mappings
        self.on_double_ctrl = on_double_ctrl
        self.listener = None
        self.double_ctrl_listener = None
        self._double_ctrl = DoubleTapDetector()

    def start(self) -> None:
        from pynput import keyboard

        # Both listeners below check the Accessibility permission as they come
        # up. Doing that from two threads at once trips a lazy-import race in
        # pyobjc, so the lookup is settled here first, on the caller's thread.
        warm_up_input_monitoring_api()
        converted: dict[str, Callable[[], None]] = {}
        for hotkey, callback in self.mappings.items():
            if hotkey.strip():
                converted[portable_to_pynput(hotkey)] = callback
        self.listener = build_listener(converted)
        self.listener.start()
        if self.on_double_ctrl:
            ctrl_keys = {keyboard.Key.ctrl, keyboard.Key.ctrl_l, keyboard.Key.ctrl_r}

            def on_press(key) -> None:
                if key not in ctrl_keys:
                    self._double_ctrl.cancel()
                    return
                if self._double_ctrl.press():
                    self.on_double_ctrl()

            def on_release(key) -> None:
                if key in ctrl_keys:
                    self._double_ctrl.release()

            listener_class = safe_listener_class() if IS_MAC else keyboard.Listener
            self.double_ctrl_listener = listener_class(on_press=on_press, on_release=on_release)
            self.double_ctrl_listener.start()

    def stop(self) -> None:
        if self.listener:
            self.listener.stop()
            self.listener = None
        if self.double_ctrl_listener:
            self.double_ctrl_listener.stop()
            self.double_ctrl_listener = None
