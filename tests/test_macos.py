import platform
import queue
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from PIL import Image

from shinclipboard import clipboard_images, hotkeys, macos
from shinclipboard.capture_overlay import HINT_MARGIN, RegionSelector
from shinclipboard.app import ShinClipboardApp


class PortabilityTests(unittest.TestCase):
    """Every entry point is called unconditionally, so none may raise elsewhere."""

    def test_nothing_is_attempted_off_macos(self):
        with patch.object(macos, "IS_MAC", False):
            self.assertTrue(macos.accessibility_trusted(), "an unenforced permission counts as granted")
            self.assertTrue(macos.screen_recording_allowed())
            self.assertTrue(macos.request_accessibility())
            self.assertTrue(macos.request_screen_recording())
            self.assertEqual(macos.menu_bar_height(), 0)
            self.assertFalse(macos.send_paste_keystroke())
            self.assertFalse(macos.activate_app())
            self.assertFalse(macos.hide_app())
            self.assertFalse(macos.open_settings_pane(macos.ACCESSIBILITY_PANE))
            macos.warm_up_input_monitoring_api()  # nothing to warm up, and no error

    def test_the_paste_key_is_the_v_position_on_any_layout(self):
        # kVK_ANSI_V. A layout-dependent lookup is what crashed the old paste.
        self.assertEqual(macos.PASTE_KEY_CODE, 9)


class HintPlacementTests(unittest.TestCase):
    def _hint_y(self, origin: tuple[int, int], inset: int) -> int:
        selector = RegionSelector.__new__(RegionSelector)  # no Tk needed to place text
        selector._origin = origin
        with patch("shinclipboard.capture_overlay.menu_bar_height", return_value=inset):
            return selector._hint_y()

    def test_without_a_menu_bar_the_hint_sits_at_the_top(self):
        self.assertEqual(self._hint_y((0, 0), 0), HINT_MARGIN)
        self.assertEqual(self._hint_y((-1920, -1080), 0), HINT_MARGIN)

    def test_the_hint_clears_the_menu_bar_on_the_main_display(self):
        self.assertEqual(self._hint_y((0, 0), 25), 25 + HINT_MARGIN)

    def test_a_display_above_the_main_one_shifts_the_hint_down(self):
        # The overlay starts 1080px above the main display, whose top edge - and
        # so the menu bar - is at y=0 in screen coordinates.
        self.assertEqual(self._hint_y((0, -1080), 25), 1080 + 25 + HINT_MARGIN)


@unittest.skipUnless(platform.system() == "Darwin", "the positional listener is only built on macOS")
class HotkeyMatchingTests(unittest.TestCase):
    """Replays exactly what macOS reported for a real Cmd+Option+S press."""

    def _fire(self, spec, presses):
        from pynput import keyboard

        fired = []
        listener = hotkeys.positional_hotkeys_class()({spec: lambda: fired.append(spec)})
        for key in presses:
            listener._on_press(key, False)
        for key in presses:
            listener._on_release(key, False)
        return fired

    def test_option_chords_fire_even_though_the_letter_changes(self):
        from pynput import keyboard

        # What the event tap actually delivered: Option turned S into "ß", but
        # the key code stayed 0x01, the place S sits on the board.
        presses = [
            keyboard.Key.alt,
            keyboard.Key.cmd,
            keyboard.KeyCode.from_char("ß", vk=macos.KEY_CODES["s"]),
        ]
        self.assertEqual(self._fire("<cmd>+<alt>+s", presses), ["<cmd>+<alt>+s"])

    def test_a_plain_chord_still_fires(self):
        from pynput import keyboard

        presses = [
            keyboard.Key.cmd,
            keyboard.Key.shift,
            keyboard.KeyCode.from_char("F", vk=macos.KEY_CODES["f"]),
        ]
        self.assertEqual(self._fire("<cmd>+<shift>+f", presses), ["<cmd>+<shift>+f"])

    def test_keycode_only_events_match_and_release_the_shortcut(self):
        from pynput import keyboard

        fired = []
        listener = hotkeys.positional_hotkeys_class()({"<cmd>+v": lambda: fired.append("v")})
        listener._on_press(keyboard.Key.cmd, False)
        listener._on_press(keyboard.KeyCode.from_char("v", vk=9), False)
        listener._on_release(keyboard.KeyCode.from_vk(9), False)
        listener._on_press(keyboard.KeyCode.from_vk(9), False)
        self.assertEqual(fired, ["v", "v"])

    def test_a_different_key_in_the_same_chord_does_not_fire(self):
        from pynput import keyboard

        presses = [
            keyboard.Key.alt,
            keyboard.Key.cmd,
            keyboard.KeyCode.from_char("∂", vk=macos.KEY_CODES["d"]),
        ]
        self.assertEqual(self._fire("<cmd>+<alt>+s", presses), [])

    def test_our_own_replayed_paste_does_not_trigger_a_shortcut(self):
        from pynput import keyboard

        fired = []
        listener = hotkeys.positional_hotkeys_class()({"<cmd>+v": lambda: fired.append("v")})
        # send_paste_keystroke posts these; the FIFO hotkey is <cmd>+v, so
        # acting on them would consume a second entry on every paste.
        for key in (keyboard.Key.cmd, keyboard.KeyCode.from_char("v", vk=macos.PASTE_KEY_CODE)):
            listener._on_press(key, True)
        self.assertEqual(fired, [])


@unittest.skipUnless(platform.system() == "Darwin", "the event tap mask is a macOS concern")
class ListenerMaskTests(unittest.TestCase):
    """System-defined events took the whole process down on macOS 15."""

    def test_the_tap_never_asks_for_system_defined_events(self):
        from Quartz import CGEventMaskBit, kCGEventFlagsChanged, kCGEventKeyDown, kCGEventKeyUp
        from pynput.keyboard._darwin import NSSystemDefined

        mask = hotkeys.safe_listener_class()._EVENTS
        self.assertFalse(mask & CGEventMaskBit(NSSystemDefined), "media keys reach TSM off the main thread")
        for wanted in (kCGEventKeyDown, kCGEventKeyUp, kCGEventFlagsChanged):
            self.assertTrue(mask & CGEventMaskBit(wanted), f"event type {wanted} is still needed")

    def test_every_listener_the_app_starts_uses_the_safe_mask(self):
        from pynput import keyboard

        safe = hotkeys.safe_listener_class()
        self.assertTrue(issubclass(hotkeys.positional_hotkeys_class(), safe))
        self.assertLess(safe._EVENTS, keyboard.Listener._EVENTS, "the mask is pynput's minus the media keys")


@unittest.skipUnless(platform.system() == "Darwin", "event taps are a macOS concern")
class TapRecoveryTests(unittest.TestCase):
    """macOS switches a tap off when its callback is late, and never back on."""

    def _listener(self):
        listener = hotkeys.safe_listener_class().__new__(hotkeys.safe_listener_class())
        listener._tap = object()
        return listener

    def test_a_disabled_tap_is_switched_back_on(self):
        from Quartz import kCGEventTapDisabledByTimeout, kCGEventTapDisabledByUserInput

        for reason in (kCGEventTapDisabledByTimeout, kCGEventTapDisabledByUserInput):
            listener = self._listener()
            with patch.object(hotkeys, "accessibility_trusted", return_value=True):
                with patch.object(hotkeys, "reenable_event_tap") as reenable:
                    listener._handle_message(None, reason, None, None, False)
            reenable.assert_called_once_with(listener._tap)

    def test_an_unpermitted_tap_is_left_alone(self):
        from Quartz import kCGEventTapDisabledByUserInput

        # macOS switches it off again at the next event, so this would be a loop
        # that never wins. Getting the permission is what has to happen first.
        listener = self._listener()
        with patch.object(hotkeys, "accessibility_trusted", return_value=False):
            with patch.object(hotkeys, "reenable_event_tap") as reenable:
                listener._handle_message(None, kCGEventTapDisabledByUserInput, None, None, False)
        reenable.assert_not_called()

    def test_the_notification_is_not_read_as_a_key(self):
        from Quartz import kCGEventTapDisabledByTimeout

        # pynput would fall through to its modifier branch and report a release
        # for a key that was never pressed, leaving a shortcut half held down.
        listener = self._listener()
        listener.on_release = Mock(side_effect=AssertionError("not a key event"))
        with patch.object(hotkeys, "accessibility_trusted", return_value=True):
            with patch.object(hotkeys, "reenable_event_tap"):
                listener._handle_message(None, kCGEventTapDisabledByTimeout, None, None, False)

    def test_a_key_event_still_reaches_pynput(self):
        from Quartz import kCGEventKeyDown

        listener = self._listener()
        with patch.object(hotkeys, "reenable_event_tap") as reenable:
            with patch.object(hotkeys.safe_listener_class().__bases__[0], "_handle_message") as handled:
                listener._handle_message("proxy", kCGEventKeyDown, "event", "refcon", False)
        handled.assert_called_once_with("proxy", kCGEventKeyDown, "event", "refcon", False)
        reenable.assert_not_called()


class AccessibilityRecoveryTests(unittest.TestCase):
    """A listener started without the permission never receives a key again."""

    def _app(self):
        app = ShinClipboardApp.__new__(ShinClipboardApp)
        app._accessibility_ok = False
        app._next_permission_check = 0.0
        app.status_var = Mock()
        app._refresh_permissions = Mock()
        app._restart_hotkeys = Mock()
        return app

    def test_granting_the_permission_starts_the_shortcuts_without_a_restart(self):
        app = self._app()
        with patch("shinclipboard.app.IS_MAC", True), patch("shinclipboard.app.accessibility_trusted", return_value=True):
            app._watch_accessibility()
        app._restart_hotkeys.assert_called_once()
        self.assertTrue(app._accessibility_ok)

    def test_the_listeners_are_not_restarted_over_and_over(self):
        app = self._app()
        with patch("shinclipboard.app.IS_MAC", True), patch("shinclipboard.app.accessibility_trusted", return_value=True):
            app._watch_accessibility()
            app._next_permission_check = 0.0  # let the throttle through
            app._watch_accessibility()
        app._restart_hotkeys.assert_called_once()

    def test_the_check_is_throttled_between_polls(self):
        app = self._app()
        with patch("shinclipboard.app.IS_MAC", True), patch("shinclipboard.app.accessibility_trusted") as trusted:
            trusted.return_value = False
            app._watch_accessibility()
            app._watch_accessibility()
        trusted.assert_called_once()

    def test_losing_the_permission_is_reported_but_not_restarted(self):
        app = self._app()
        app._accessibility_ok = True
        with patch("shinclipboard.app.IS_MAC", True), patch("shinclipboard.app.accessibility_trusted", return_value=False):
            app._watch_accessibility()
        app._restart_hotkeys.assert_not_called()
        self.assertIn("ショートカットが使えません", app.status_var.set.call_args.args[0])

    def test_nothing_is_checked_off_macos(self):
        app = self._app()
        with patch("shinclipboard.app.IS_MAC", False), patch("shinclipboard.app.accessibility_trusted") as trusted:
            app._watch_accessibility()
        trusted.assert_not_called()


class ClipboardImageTests(unittest.TestCase):
    """Pillow's macOS reader spawns `osascript`, so it is a last resort."""

    def _read_on_mac(self, mac_answer, grabclipboard):
        with patch.object(clipboard_images.platform, "system", return_value="Darwin"):
            with patch.object(clipboard_images, "_read_clipboard_image_mac", return_value=mac_answer):
                with patch.object(clipboard_images.ImageGrab, "grabclipboard", grabclipboard):
                    return clipboard_images.read_clipboard_image()

    def test_the_pasteboards_own_no_is_not_second_guessed(self):
        # Every text copy takes this path, and a subprocess per copy would show.
        never = Mock(side_effect=AssertionError("Pillow must not be asked after a clear answer"))
        self.assertIsNone(self._read_on_mac(None, never))
        never.assert_not_called()

    def test_pillow_still_covers_a_machine_without_pyobjc(self):
        grab = Mock(return_value=Image.new("RGB", (4, 4), "red"))
        image = self._read_on_mac(clipboard_images.CANNOT_ASK, grab)
        self.assertIsNotNone(image)
        self.assertEqual(image.size, (4, 4))
        grab.assert_called_once()

    def test_unreadable_image_does_not_clear_the_clipboard(self):
        appkit = Mock()
        appkit.NSData.dataWithContentsOfFile_.return_value = None
        with patch.dict(sys.modules, {"AppKit": appkit}), patch.object(clipboard_images.platform, "system", return_value="Darwin"), patch.object(clipboard_images.os, "name", "posix"):
            with self.assertRaises(OSError):
                clipboard_images.write_clipboard_image(Path("missing.png"))
        appkit.NSPasteboard.generalPasteboard.assert_not_called()

    def test_rejected_image_write_is_reported(self):
        appkit = Mock()
        board = appkit.NSPasteboard.generalPasteboard.return_value
        board.setData_forType_.return_value = False
        with patch.dict(sys.modules, {"AppKit": appkit}), patch.object(clipboard_images.platform, "system", return_value="Darwin"), patch.object(clipboard_images.os, "name", "posix"):
            with self.assertRaises(OSError):
                clipboard_images.write_clipboard_image(Path("image.png"))
        board.changeCount.assert_not_called()


class PasteFailureTests(unittest.TestCase):
    def test_failed_keystroke_is_reported_to_the_ui_queue(self):
        app = ShinClipboardApp.__new__(ShinClipboardApp)
        app.events = queue.Queue()
        app.hide_window = Mock()
        app.hide_call_window = Mock(return_value=True)
        with patch("shinclipboard.app.IS_MAC", True), patch("shinclipboard.app.accessibility_trusted", return_value=True), patch("shinclipboard.app.hide_app"), patch("shinclipboard.app.send_paste_keystroke", return_value=False), patch("shinclipboard.app.time.sleep"), patch("shinclipboard.app.threading.Thread") as thread:
            app._send_paste()
            thread.call_args.kwargs["target"]()
        self.assertEqual(app.events.get_nowait(), ("paste_failed", None))


class PopupFocusTests(unittest.TestCase):
    """The popup belongs over the window the user is in, not in front of it."""

    def _app(self):
        app = ShinClipboardApp.__new__(ShinClipboardApp)
        app.call_window = Mock()
        app.hide_window = Mock()
        app.events = queue.Queue()
        app._previous_app = None
        return app

    def test_closing_the_popup_hands_the_keyboard_back(self):
        app = self._app()
        app._previous_app = "the editor the user was typing in"
        with patch("shinclipboard.app.activate_running_app", return_value=True) as activate:
            self.assertTrue(app.hide_call_window())
        activate.assert_called_once_with("the editor the user was typing in")
        app.call_window.withdraw.assert_called_once()
        self.assertIsNone(app._previous_app, "the next popup remembers its own")

    def test_the_popup_stops_floating_once_it_is_closed(self):
        # It is only above everything else while it is the thing being chosen from.
        app = self._app()
        with patch("shinclipboard.app.activate_running_app", return_value=False):
            self.assertFalse(app.hide_call_window())
        app.call_window.attributes.assert_called_with("-topmost", False)

    def _paste(self, app):
        with patch("shinclipboard.app.IS_MAC", True), patch("shinclipboard.app.accessibility_trusted", return_value=True), patch("shinclipboard.app.hide_app") as hidden, patch("shinclipboard.app.threading.Thread"):
            app._send_paste()
        return hidden

    def test_a_paste_from_the_popup_leaves_the_other_windows_where_they_were(self):
        app = self._app()
        app.hide_call_window = Mock(return_value=True)
        hidden = self._paste(app)
        app.hide_window.assert_not_called()
        hidden.assert_not_called()

    def test_a_paste_with_nothing_to_return_to_still_steps_aside(self):
        # The settings window pastes too, and there the app has to get out of the
        # way itself or the keystroke lands on nothing.
        app = self._app()
        app.hide_call_window = Mock(return_value=False)
        hidden = self._paste(app)
        app.hide_window.assert_called_once()
        hidden.assert_called_once()


@unittest.skipUnless(platform.system() == "Darwin", "these read real macOS state")
class LiveMacTests(unittest.TestCase):
    def test_the_accessibility_check_is_resolved_before_listener_threads_need_it(self):
        try:
            import HIServices
        except ImportError:
            self.skipTest("pyobjc is not installed")
        HIServices.__dict__.pop("AXIsProcessTrusted", None)
        macos.warm_up_input_monitoring_api()
        # pynput reads this name from every listener thread it starts. Resolving
        # it lazily from two of them at once is what used to kill the hotkeys, so
        # after the warm-up it has to be a plain attribute of the module.
        self.assertIn("AXIsProcessTrusted", HIServices.__dict__)
        self.assertIsInstance(macos.accessibility_trusted(), bool)

    def test_the_permission_checks_answer_with_a_boolean(self):
        self.assertIsInstance(macos.accessibility_trusted(), bool)
        self.assertIsInstance(macos.screen_recording_allowed(), bool)

    def test_the_menu_bar_leaves_room_but_not_the_whole_screen(self):
        height = macos.menu_bar_height()
        self.assertGreaterEqual(height, 0)
        self.assertLess(height, 200)


if __name__ == "__main__":
    unittest.main()
