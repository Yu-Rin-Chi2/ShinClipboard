import tempfile
import threading
import unittest
import unittest.mock
from pathlib import Path
from tkinter import ttk

from PIL import Image

from shinclipboard.core import ClipboardHistory, FifoQueue
from shinclipboard.hotkeys import DoubleTapDetector, portable_to_pynput
from shinclipboard.single_instance import SingleInstance
from shinclipboard import storage
from shinclipboard.storage import JsonStore, migrate_legacy_data_dir
from shinclipboard.transfer import create_backup, export_snippets_csv, import_snippets_csv, restore_backup
from shinclipboard.transforms import apply_transform


class HistoryTests(unittest.TestCase):
    def test_newest_first_deduplicated_and_limited(self):
        history = ClipboardHistory(limit=2)
        history.add("a", "1")
        history.add("b", "2")
        history.add("a", "3")
        history.add("c", "4")
        self.assertEqual([item.text for item in history.search()], ["c", "a"])

    def test_search_is_case_insensitive(self):
        history = ClipboardHistory(limit=10)
        history.add("Hello World")
        self.assertEqual(history.search("hello")[0].text, "Hello World")

    def test_image_history_is_deduplicated(self):
        history = ClipboardHistory(limit=10)
        history.add_image("images/a.png", 320, 200, "1")
        history.add_image("images/a.png", 320, 200, "2")
        self.assertEqual(len(history.search()), 1)
        self.assertEqual(history.search("画像")[0].kind, "image")


class FifoTests(unittest.TestCase):
    def test_fifo_and_undo(self):
        queue = FifoQueue(["a", "b"])
        self.assertEqual(queue.pop(), "a")
        queue.undo("a")
        self.assertEqual(queue.values(), ["a", "b"])

    def test_lifo_and_queue_editing(self):
        queue = FifoQueue(["a", "b"])
        queue.insert(1, "x")
        queue.edit(0, "A")
        self.assertEqual(queue.pop("lifo"), "b")
        self.assertEqual(queue.remove(1), "x")
        self.assertEqual(queue.joined(), "A")


class LegacyMigrationTests(unittest.TestCase):
    """The rename from NewClipboard must not strand existing user data."""

    def _legacy_dir(self, base: Path) -> Path:
        legacy = base / "NewClipboard"
        store = JsonStore(legacy)
        config = store.load_config()
        config["groups"][0]["name"] = "移行前グループ"
        store.save_config(config)
        history = ClipboardHistory(limit=10)
        history.add("移行前の履歴")
        store.save_history(history)
        return legacy

    def test_legacy_data_is_copied_and_original_is_kept(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            legacy = self._legacy_dir(base)
            target = base / "ShinClipboard"
            with unittest.mock.patch.object(storage, "legacy_data_dir", return_value=legacy):
                self.assertEqual(migrate_legacy_data_dir(target), target)

            migrated = JsonStore(target)
            self.assertEqual(migrated.load_config()["groups"][0]["name"], "移行前グループ")
            self.assertEqual([item.text for item in migrated.load_history(10).search()], ["移行前の履歴"])
            self.assertTrue((legacy / "config.json").exists())

    def test_existing_data_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            legacy = self._legacy_dir(base)
            target = base / "ShinClipboard"
            current = JsonStore(target)
            config = current.load_config()
            config["groups"][0]["name"] = "移行後グループ"
            current.save_config(config)

            with unittest.mock.patch.object(storage, "legacy_data_dir", return_value=legacy):
                self.assertIsNone(migrate_legacy_data_dir(target))
            self.assertEqual(JsonStore(target).load_config()["groups"][0]["name"], "移行後グループ")

    def test_missing_legacy_directory_is_a_no_op(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            target = base / "ShinClipboard"
            with unittest.mock.patch.object(storage, "legacy_data_dir", return_value=base / "absent"):
                self.assertIsNone(migrate_legacy_data_dir(target))
            self.assertFalse(target.exists())


class StorageTests(unittest.TestCase):
    def test_config_and_history_are_separate(self):
        with tempfile.TemporaryDirectory() as folder:
            store = JsonStore(Path(folder))
            config = store.load_config()
            history = ClipboardHistory(limit=10)
            history.add("secret")
            store.save_history(history)
            self.assertNotIn("secret", store.config_path.read_text(encoding="utf-8"))
            self.assertEqual(config["schema_version"], 1)

    def test_import_preserves_previous_config_as_backup(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            store = JsonStore(base / "local")
            store.load_config()
            source_store = JsonStore(base / "other")
            imported = source_store.load_config()
            imported["groups"][0]["name"] = "共有グループ"
            source_store.save_config(imported)
            loaded = store.import_config(source_store.config_path)
            self.assertEqual(loaded["groups"][0]["name"], "共有グループ")
            self.assertTrue(store.config_path.with_suffix(".json.bak").exists())

    def test_image_is_saved_by_content_hash_and_cleaned(self):
        with tempfile.TemporaryDirectory() as folder:
            store = JsonStore(Path(folder))
            relative, width, height = store.save_image(Image.new("RGB", (8, 6), "red"))
            self.assertEqual((width, height), (8, 6))
            self.assertTrue(store.image_path(relative).exists())
            history = ClipboardHistory(limit=10)
            history.add_image(relative, width, height)
            store.save_history(history)
            history.clear()
            store.save_history(history)
            self.assertFalse(store.image_path(relative).exists())

    def test_image_path_cannot_escape_data_directory(self):
        with tempfile.TemporaryDirectory() as folder:
            store = JsonStore(Path(folder))
            with self.assertRaises(ValueError):
                store.image_path("../outside.png")

    def test_popup_hotkey_defaults_to_none_and_legacy_ctrl_space_is_disabled(self):
        with tempfile.TemporaryDirectory() as folder:
            store = JsonStore(Path(folder))
            self.assertEqual(store.load_config()["settings"]["popup_hotkey"], "")
            config = store.load_config()
            config["settings"]["popup_hotkey"] = "Ctrl+Space"
            store.save_config(config)
            self.assertEqual(store.load_config()["settings"]["popup_hotkey"], "")
            self.assertIn('"popup_hotkey": ""', store.config_path.read_text(encoding="utf-8"))
            config["settings"]["popup_hotkey"] = "primary+shift+space"
            store.save_config(config)
            self.assertEqual(store.load_config()["settings"]["popup_hotkey"], "primary+shift+space")


class HotkeyTests(unittest.TestCase):
    def test_portable_primary_hotkey(self):
        translated = portable_to_pynput("primary+shift+space")
        self.assertIn(translated, {"<ctrl>+<shift>+<space>", "<cmd>+<shift>+<space>"})
        self.assertEqual(portable_to_pynput("ctrl+space"), "<ctrl>+<space>")

    def test_single_key_is_rejected(self):
        with self.assertRaises(ValueError):
            portable_to_pynput("space")

    def test_double_tap_requires_release_and_short_interval(self):
        detector = DoubleTapDetector(interval=0.35)
        self.assertFalse(detector.press(1.0))
        self.assertFalse(detector.press(1.1))
        detector.release()
        self.assertTrue(detector.press(1.2))
        detector.release()
        self.assertFalse(detector.press(2.0))

    def test_double_tap_is_cancelled_when_ctrl_is_used_as_a_modifier(self):
        detector = DoubleTapDetector(interval=0.35)
        self.assertFalse(detector.press(1.0))  # Ctrl down
        detector.cancel()  # e.g. "c" pressed while Ctrl is held
        detector.release()
        self.assertFalse(detector.press(1.1))  # Ctrl down again quickly: no popup
        detector.release()
        self.assertTrue(detector.press(1.2))  # two bare taps still work


def _destroy_root(root) -> None:
    """Destroy a Tk root without leaving `after` timers that would fire in the next interpreter.

    Only the Tcl timers are cancelled here; `destroy()` still deletes the Python
    callbacks, so every widget's command bookkeeping stays consistent.
    """
    try:
        for timer in root.tk.splitlist(root.tk.call("after", "info")):
            root.tk.call("after", "cancel", timer)
    except Exception:
        pass
    root.destroy()


class SettingsTabTests(unittest.TestCase):
    def test_the_settings_tab_can_be_scrolled_to_its_last_row(self):
        """The settings rows are taller than the window and grow with the OS.

        macOS lays the same widgets out taller and adds the permissions box, so
        without a scroller the last sections - screenshots, CSV and backup -
        cannot be reached at all.
        """
        import tkinter as tk

        from shinclipboard.app import ShinClipboardApp

        class HeadlessApp(ShinClipboardApp):
            def _start_tray(self) -> None:
                pass

            def _restart_hotkeys(self) -> None:
                pass

            def _check_macos_permissions(self) -> None:
                pass

        try:
            root = tk.Tk()
        except tk.TclError as error:
            self.skipTest(f"Tk is unavailable: {error}")
        root.withdraw()
        try:
            with tempfile.TemporaryDirectory() as folder:
                app = HeadlessApp(root, JsonStore(Path(folder)))
                scroller = root.nametowidget(app.tabs.tabs()[-1])
                self.assertIs(app.settings_tab.master, scroller._canvas)
                root.update_idletasks()

                wanted = app.settings_tab.winfo_reqheight()
                self.assertGreater(wanted, 0)
                region = [int(value) for value in scroller._canvas.cget("scrollregion").split()]
                self.assertEqual(region[3], wanted, "the scrollregion covers every row")

                scroller._canvas.yview_moveto(1.0)
                self.assertAlmostEqual(scroller._canvas.yview()[1], 1.0, places=3)
        finally:
            _destroy_root(root)


class WheelScrollTests(unittest.TestCase):
    def test_wheel_units_handle_both_platforms(self):
        from shinclipboard.widgets import WHEEL_STEP, wheel_units

        self.assertEqual(wheel_units(-120), WHEEL_STEP, "Windows: one notch down")
        self.assertEqual(wheel_units(240), -2 * WHEEL_STEP, "Windows: two notches up")
        self.assertEqual(wheel_units(-1), WHEEL_STEP, "macOS: one tick down")
        self.assertEqual(wheel_units(2), -2 * WHEEL_STEP, "macOS: two ticks up")
        self.assertEqual(wheel_units(0), 0)

    def test_image_listbox_scrolls_on_a_macos_wheel_tick(self):
        import tkinter as tk

        from shinclipboard.widgets import ImageListbox

        try:
            root = tk.Tk()
        except tk.TclError as error:
            self.skipTest(f"Tk is unavailable: {error}")
        root.withdraw()
        try:
            listbox = ImageListbox(root, height=5)
            listbox.pack()
            for index in range(100):
                listbox.insert("end", f"row {index}")
            root.update_idletasks()
            self.assertEqual(listbox.yview()[0], 0.0)

            listbox.event_generate("<MouseWheel>", delta=-1)  # macOS reports small counts, not multiples of 120
            root.update()
            self.assertGreater(listbox.yview()[0], 0.0, "a single macOS tick has to move the list")

            listbox.event_generate("<MouseWheel>", delta=1)
            root.update()
            self.assertEqual(listbox.yview()[0], 0.0, "and the opposite tick brings it back")
        finally:
            _destroy_root(root)


class CallWindowTests(unittest.TestCase):
    """GUI tests. They never map or focus a window, so they do not steal keyboard input."""

    def test_keyboard_switches_tabs_and_snippet_groups(self):
        import tkinter as tk

        from shinclipboard.app import ShinClipboardApp

        class HeadlessApp(ShinClipboardApp):
            def _start_tray(self) -> None:
                pass

            def _restart_hotkeys(self) -> None:
                pass

        try:
            root = tk.Tk()
        except tk.TclError as error:
            self.skipTest(f"Tk is unavailable: {error}")
        root.withdraw()
        try:
            with tempfile.TemporaryDirectory() as folder:
                store = JsonStore(Path(folder))
                config = store.load_config()
                config["groups"].append({"id": "second", "name": "二番目", "snippets": []})
                store.save_config(config)
                app = HeadlessApp(root, store)
                first_group = app.call_group_id

                self.assertEqual(app.call_tabs.index("current"), 0)
                self.assertEqual(app._move_call_group(1), "break")
                self.assertEqual(app.call_group_id, first_group)  # arrows are ignored on the history tab

                app._move_call_tab(1)
                self.assertEqual(app.call_tabs.index("current"), 1)
                app._move_call_group(1)
                self.assertEqual(app.call_group_id, "second")
                app._move_call_group(1)
                self.assertEqual(app.call_group_id, first_group)  # wraps around
                app._move_call_group(-1)
                self.assertEqual(app.call_group_id, "second")

                app._move_call_tab(-1)
                self.assertEqual(app.call_tabs.index("current"), 0)
                app._move_call_tab(1)
                self.assertEqual(app.call_tabs.index("current"), 1)

                for sequence in ("<Tab>", "<Shift-Tab>", "<Left>", "<Right>", "<Control-Left>", "<Control-Right>"):
                    self.assertTrue(app.call_window.bind(sequence), sequence)
        finally:
            _destroy_root(root)

    def test_only_quick_keys_paste_from_the_popup(self):
        import tkinter as tk

        from shinclipboard.app import ShinClipboardApp

        pasted: list[str] = []

        class HeadlessApp(ShinClipboardApp):
            def _start_tray(self) -> None:
                pass

            def _restart_hotkeys(self) -> None:
                pass

            def _paste_text(self, text: str) -> None:
                pasted.append(text)

        try:
            root = tk.Tk()
        except tk.TclError as error:
            self.skipTest(f"Tk is unavailable: {error}")
        root.withdraw()
        try:
            with tempfile.TemporaryDirectory() as folder:
                store = JsonStore(Path(folder))
                app = HeadlessApp(root, store)
                app.history.add("first")
                app.history.add("second")  # newest first: "second" is row 0, "first" is row 1
                app._refresh_call_history()

                def key(char: str, state: int = 0):
                    return type("Event", (), {"char": char, "state": state})()

                # Arrows, Home, Shift... arrive with an empty char; Enter, Space, Tab, Esc with a control char.
                for char in ("", "\r", " ", "\t", "\x1b", "\x08"):
                    self.assertIsNone(app._quick_select(key(char)), repr(char))
                self.assertEqual(pasted, [], "keys without a quick-key character must not paste")
                self.assertIsNone(app._quick_select(key("2", state=0x4)), "Ctrl+2 is a shortcut, not a quick key")
                self.assertEqual(pasted, [])

                self.assertEqual(app._quick_select(key("2")), "break")
                self.assertEqual(pasted, ["first"])

                rows = app.call_history_list
                self.assertNotIn("Text", rows.bindtags(), "Text class bindings (caret, editing) are disabled")
                for sequence in ("<Up>", "<Down>", "<Home>", "<End>", "<Prior>", "<Next>", "<Return>"):
                    self.assertTrue(rows.bind(sequence), sequence)
        finally:
            _destroy_root(root)

    def test_history_popup_shows_image_thumbnails(self):
        import tkinter as tk

        from shinclipboard.app import ShinClipboardApp

        class HeadlessApp(ShinClipboardApp):
            def _start_tray(self) -> None:
                pass

            def _restart_hotkeys(self) -> None:
                pass

        try:
            root = tk.Tk()
        except tk.TclError as error:
            self.skipTest(f"Tk is unavailable: {error}")
        root.withdraw()
        try:
            with tempfile.TemporaryDirectory() as folder:
                store = JsonStore(Path(folder))
                app = HeadlessApp(root, store)
                relative, width, height = store.save_image(Image.new("RGB", (400, 100), "red"))
                app.history.add_image(relative, width, height)
                app.history.add("text")
                app.history.add_image("images/missing.png", 10, 10)  # newest first: this is row 0
                app._refresh_call_history()
                rows = app.call_history_list

                self.assertEqual(rows.size(), 3)
                self.assertFalse(rows.row_has_image(0), "an unreadable image falls back to the text label")
                self.assertFalse(rows.row_has_image(1))
                self.assertTrue(rows.row_has_image(2))
                self.assertEqual(len(rows.image_names()), 1)
                thumbnail = app._thumbnails[relative]
                self.assertEqual((thumbnail.width(), thumbnail.height()), (200, 50))
                self.assertIn("400×100", rows.get("3.0", "3.end"))

                rows.selection_set(2)
                self.assertEqual(rows.curselection(), (2,))
                rows._move(-1)
                self.assertEqual(rows.curselection(), (1,))
                rows._move(5)
                self.assertEqual(rows.curselection(), (2,), "movement is clamped to the last row")

                app.history.clear()
                app._refresh_call_history()
                self.assertEqual(rows.size(), 0)
                self.assertEqual(app._thumbnails, {}, "thumbnails of removed items are evicted")
        finally:
            _destroy_root(root)

    def test_history_rows_can_be_dragged_to_other_apps(self):
        import tkinter as tk

        from shinclipboard.app import ShinClipboardApp

        try:
            import tkinterdnd2
        except ImportError:
            self.skipTest("tkinterdnd2 is not installed")

        pasted: list[bool] = []
        hidden: list[bool] = []

        class HeadlessApp(ShinClipboardApp):
            def _start_tray(self) -> None:
                pass

            def _restart_hotkeys(self) -> None:
                pass

            def _paste_call_history(self) -> None:
                pasted.append(True)

            def hide_call_window(self) -> None:
                hidden.append(True)
                super().hide_call_window()

        try:
            root = tk.Tk()
        except tk.TclError as error:
            self.skipTest(f"Tk is unavailable: {error}")
        root.withdraw()
        try:
            tkinterdnd2.TkinterDnD._require(root)
        except (RuntimeError, tk.TclError) as error:
            # tkinterdnd2 ships one tkdnd build per OS, architecture and Tcl
            # major version, and not every combination exists: Intel macOS on
            # Tcl 9 has none. Dropping the feature with a message is exactly
            # what the app is meant to do there, so there is nothing to assert.
            root.destroy()
            self.skipTest(f"tkdnd has no build for this Tcl/architecture: {error}")
        try:
            with tempfile.TemporaryDirectory() as folder:
                store = JsonStore(Path(folder))
                app = HeadlessApp(root, store)
                self.assertTrue(app.drag_enabled, app.status_var.get())
                rows = app.call_history_list
                self.assertIn("TkDND_Drag1", rows.bindtags())
                self.assertTrue(rows.bind("<Button-1>"), "the row-selecting click handler survives registration")

                relative, width, height = store.save_image(Image.new("RGB", (8, 8), "blue"))
                app.history.add_image(relative, width, height)
                app.history.add("dragged text")
                app._refresh_call_history()

                rows.selection_clear()
                self.assertEqual(app._drag_init(), ("refuse_drop",))
                self.assertFalse(app._drag_active)
                rows.selection_set(0)
                self.assertEqual(app._drag_init(), ("copy", "DND_Text", "dragged text"))
                rows.selection_clear()
                rows.selection_set(1)
                action, kind, files = app._drag_init()
                self.assertEqual((action, kind), ("copy", "DND_Files"))
                self.assertEqual(Path(files[0]), store.image_path(relative))
                self.assertTrue(app._drag_active)

                # A button release that arrives while a drag is in flight must not paste.
                app._clicked_list_row = lambda widget, y: 1  # hit-testing needs a mapped window; stub it
                click = type("Event", (), {"y": 10})()
                app._call_history_click(click)
                root.update()
                self.assertEqual(pasted, [])

                app._drag_end(type("Event", (), {"action": "refuse_drop"})())
                self.assertEqual(hidden, [], "a cancelled drag keeps the popup open")
                app._drag_end(type("Event", (), {"action": "copy"})())
                self.assertEqual(hidden, [True], "the popup closes after a drop")
                app._finish_drag()
                self.assertFalse(app._drag_active)

                app._call_history_click(click)
                root.update()
                self.assertEqual(pasted, [True], "ordinary clicks paste again once the drag is over")
        finally:
            _destroy_root(root)


class SingleInstanceTests(unittest.TestCase):
    def test_second_instance_notifies_first(self):
        with tempfile.TemporaryDirectory() as folder:
            first = SingleInstance(Path(folder))
            second = SingleInstance(Path(folder))
            notified = threading.Event()
            self.assertTrue(first.acquire())
            try:
                first.start_listener(notified.set)
                self.assertFalse(second.acquire())
                self.assertTrue(second.notify_existing())
                self.assertTrue(notified.wait(1.0))
            finally:
                first.close()

            third = SingleInstance(Path(folder))
            self.assertTrue(third.acquire())
            third.close()


class TransformTests(unittest.TestCase):
    def test_number_and_regex_transform(self):
        numbered = apply_transform("a\nb", {"type": "number_lines", "params": {"start": 1, "width": 2, "separator": ":"}})
        self.assertEqual(numbered, "01:a\n02:b")
        replaced = apply_transform("A1 B2", {"type": "regex", "params": {"pattern": r"\d", "replacement": "#"}})
        self.assertEqual(replaced, "A# B#")
        groups = apply_transform("name: value", {"type": "regex", "params": {"pattern": r"(\w+): (\w+)", "replacement": "$2=$1"}})
        self.assertEqual(groups, "value=name")


class TransferTests(unittest.TestCase):
    def test_csv_merge_and_full_backup(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            source_store = JsonStore(base / "source")
            config = source_store.load_config()
            config["groups"][0]["snippets"][0]["memo"] = "memo"
            source_store.save_config(config)
            history = ClipboardHistory(limit=10)
            relative, width, height = source_store.save_image(Image.new("RGB", (5, 4), "blue"))
            history.add_image(relative, width, height)
            source_store.save_history(history)
            csv_path = base / "snippets.csv"
            export_snippets_csv(config, csv_path)
            target = source_store.load_config()
            added, updated = import_snippets_csv(target, csv_path)
            self.assertEqual((added, updated), (0, 1))
            backup = base / "all.zip"
            create_backup(source_store.config_path, source_store.history_path, backup)
            restored_config = base / "restored" / "config.json"
            restored_history = base / "restored" / "history.json"
            restored_config.parent.mkdir()
            restore_backup(backup, restored_config, restored_history)
            self.assertTrue(restored_config.exists())
            self.assertTrue(restored_history.exists())
            self.assertTrue((restored_history.parent / relative).exists())


class ScreenshotStorageTests(unittest.TestCase):
    def test_drag_and_drop_scratch_files_survive_the_image_cleanup(self):
        """`cleanup_images` deletes unreferenced pictures, so exports live outside images/."""
        from shinclipboard.editor import EXPORT_DIR, cleanup_exports

        with tempfile.TemporaryDirectory() as folder:
            store = JsonStore(Path(folder))
            history = ClipboardHistory(limit=10)
            orphan = store.data_dir / "images" / "orphan.png"
            orphan.parent.mkdir(parents=True, exist_ok=True)
            Image.new("RGB", (4, 4), "red").save(orphan)
            scratch = store.data_dir / EXPORT_DIR / "shinclipboard-20260908-120000.png"
            scratch.parent.mkdir(parents=True, exist_ok=True)
            Image.new("RGB", (4, 4), "blue").save(scratch)

            store.save_history(history)
            self.assertFalse(orphan.exists(), "unreferenced history images are still collected")
            self.assertTrue(scratch.exists(), "a pending drag payload must not be deleted underneath it")

            self.assertEqual(cleanup_exports(store.data_dir, max_age=0), 1)
            self.assertFalse(scratch.exists())

    def test_a_config_without_screenshot_keys_gains_the_defaults(self):
        with tempfile.TemporaryDirectory() as folder:
            store = JsonStore(Path(folder))
            config = store.load_config()
            for key in ("screenshot_hotkey", "screenshot_format", "annotation_color"):
                del config["settings"][key]
            store.save_config(config)
            settings = store.load_config()["settings"]
            self.assertEqual(settings["screenshot_hotkey"], "primary+alt+s")
            self.assertEqual(settings["screenshot_format"], "png")
            self.assertEqual(settings["annotation_color"], "#e02424")


class AppearanceTests(unittest.TestCase):
    def _app(self, root, folder):
        from shinclipboard.app import ShinClipboardApp

        namespace = {"_start_tray": lambda self: None, "_restart_hotkeys": lambda self: None}
        return type("HeadlessApp", (ShinClipboardApp,), namespace)(root, JsonStore(Path(folder)))

    def test_the_system_theme_resolves_by_appearance_and_named_themes_do_not(self):
        from shinclipboard.app import THEME_NAMES, THEMES, theme_colors

        self.assertEqual(theme_colors("system", dark=True), THEMES["dark"])
        self.assertEqual(theme_colors("system", dark=False), THEMES["blue"])
        self.assertEqual(theme_colors("blue", dark=True), THEMES["blue"])
        self.assertEqual(theme_colors("green", dark=False), THEMES["green"])
        self.assertEqual(theme_colors("no such theme", dark=True), THEMES["blue"])
        self.assertEqual(THEME_NAMES[0], "system", "offered first in the settings, as the default")

    def test_the_window_takes_the_os_colour_on_macos_and_the_theme_colour_elsewhere(self):
        import platform
        import tkinter as tk

        from shinclipboard.app import THEMES

        try:
            root = tk.Tk()
        except tk.TclError as error:
            self.skipTest(f"Tk is unavailable: {error}")
        root.withdraw()
        try:
            with tempfile.TemporaryDirectory() as folder:
                # ttk frames are painted in the OS window colour there, and a
                # white root showed through as a band around every one of them.
                if platform.system() == "Darwin":
                    app = self._app(root, folder)
                    self.assertEqual(str(app.root.cget("background")), "systemWindowBackgroundColor")
                    self.assertEqual(str(app.call_window.cget("background")), "systemWindowBackgroundColor")
                    # The default theme follows the OS: dark lists in dark mode, blue otherwise.
                    expected = THEMES["dark" if app._dark_appearance() else "blue"]["background"]
                    self.assertEqual(str(app.history_list.cget("background")), expected)
                    app.config["settings"]["theme"] = "blue"
                    app._apply_appearance()
                    self.assertEqual(str(app.history_list.cget("background")), "#ffffff", "a named theme is fixed")
                    self.assertEqual(str(app.root.cget("background")), "systemWindowBackgroundColor")
                with unittest.mock.patch("shinclipboard.app.IS_MAC", False):
                    app = self._app(root, folder)
                    self.assertEqual(str(app.root.cget("background")), "#ffffff", "system means blue off macOS")
                    self.assertEqual(str(app.history_list.cget("background")), "#ffffff")
        finally:
            _destroy_root(root)

    def test_the_status_line_outlives_tabs_that_ask_for_too_much_height(self):
        import tkinter as tk

        try:
            root = tk.Tk()
        except tk.TclError as error:
            self.skipTest(f"Tk is unavailable: {error}")
        root.geometry("+3000+3000")
        try:
            with tempfile.TemporaryDirectory() as folder:
                app = self._app(root, folder)
                root.geometry("700x300")  # far shorter than the tabs want
                root.update()
                status = next(child for child in root.winfo_children() if isinstance(child, ttk.Label))
                self.assertTrue(status.winfo_ismapped(), "pack gives way from the last slave in, so it is packed first")
                self.assertTrue(app.tabs.winfo_ismapped())
        finally:
            _destroy_root(root)


class ScreenshotAppTests(unittest.TestCase):
    """GUI tests. Like the popup tests they never map or focus a window."""

    def _app(self, root, folder, **overrides):
        from shinclipboard.app import ShinClipboardApp

        namespace = {"_start_tray": lambda self: None, "_restart_hotkeys": lambda self: None}
        namespace.update(overrides)
        headless = type("HeadlessApp", (ShinClipboardApp,), namespace)
        return headless(root, JsonStore(Path(folder)))

    def _root(self):
        import tkinter as tk

        try:
            root = tk.Tk()
        except tk.TclError as error:
            self.skipTest(f"Tk is unavailable: {error}")
        root.withdraw()
        return root

    def test_writing_an_image_to_the_clipboard_does_not_re_add_it_to_history(self):
        from shinclipboard import app as app_module

        root = self._root()
        try:
            with tempfile.TemporaryDirectory() as folder:
                app = self._app(root, folder)
                relative, width, height = app.store.save_image(Image.new("RGB", (8, 8), "blue"))
                app.history.add_image(relative, width, height)
                before = len(app.history.search())

                # The write reports its own token, and the clipboard still holds
                # exactly that when the next poll looks.
                with unittest.mock.patch.object(app_module, "write_clipboard_image", return_value=99), \
                        unittest.mock.patch.object(app_module, "clipboard_change_token", return_value=99), \
                        unittest.mock.patch.object(
                            app_module, "read_clipboard_image", return_value=Image.new("RGB", (8, 8), "green")
                        ):
                    app._set_clipboard_image(app.store.image_path(relative))
                    self.assertEqual(app.last_clipboard_token, 99)
                    app._poll()
                self.assertEqual(len(app.history.search()), before, "our own write must not come back as a copy")
        finally:
            _destroy_root(root)

    def test_an_image_copied_by_another_app_right_after_our_write_is_still_recorded(self):
        from shinclipboard import app as app_module

        root = self._root()
        try:
            with tempfile.TemporaryDirectory() as folder:
                app = self._app(root, folder)
                relative, width, height = app.store.save_image(Image.new("RGB", (8, 8), "blue"))
                app.history.add_image(relative, width, height)
                before = len(app.history.search())

                with unittest.mock.patch.object(app_module, "write_clipboard_image", return_value=99), \
                        unittest.mock.patch.object(app_module, "clipboard_change_token", return_value=100), \
                        unittest.mock.patch.object(
                            app_module, "read_clipboard_image", return_value=Image.new("RGB", (9, 9), "green")
                        ):
                    app._set_clipboard_image(app.store.image_path(relative))
                    app._poll()
                self.assertEqual(len(app.history.search()), before + 1, "someone else's copy is not swallowed")
        finally:
            _destroy_root(root)

    def test_a_history_image_opens_an_editor_that_can_save_a_new_entry(self):
        root = self._root()
        try:
            with tempfile.TemporaryDirectory() as folder:
                app = self._app(root, folder)
                relative, width, height = app.store.save_image(Image.new("RGB", (40, 30), "blue"))
                app.history.add_image(relative, width, height)
                item = app.history.search()[0]

                app._edit_image_item(item)
                root.update()
                self.assertEqual(len(app.editors), 1)
                editor = next(iter(app.editors))
                self.assertEqual(editor.base.size, (40, 30))

                from shinclipboard.annotations import Shape

                editor.document.shapes.append(Shape("rect", [(2, 2), (20, 20)], color="#ff0000", filled=True))
                editor.save_to_history()
                self.assertEqual(len(app.history.search()), 2, "the edit is a new entry, the original stays")
                self.assertTrue(app.store.image_path(app.history.search()[0].image_path).exists())

                editor.close()
                self.assertEqual(app.editors, set())
        finally:
            _destroy_root(root)

    def test_a_text_history_row_is_refused_by_the_image_editor(self):
        opened: list[str] = []
        root = self._root()
        try:
            with tempfile.TemporaryDirectory() as folder:
                app = self._app(root, folder)
                app.history.add("just text")
                with unittest.mock.patch("tkinter.messagebox.showinfo", lambda *a, **k: opened.append("info")):
                    app._edit_image_item(app.history.search()[0])
                self.assertEqual(opened, ["info"])
                self.assertEqual(app.editors, set())
        finally:
            _destroy_root(root)

    def test_capture_hides_our_windows_and_always_restores_them(self):
        root = self._root()
        try:
            with tempfile.TemporaryDirectory() as folder:
                app = self._app(root, folder)
                # A failing selector stands in for any error during the grab; the
                # app must never be left in capture mode with hidden windows.
                with unittest.mock.patch("shinclipboard.app.RegionSelector") as selector:
                    selector.return_value.start.side_effect = RuntimeError("no screen")
                    app.start_capture()
                    self.assertTrue(app.capturing)
                    root.update()
                    app._run_selector()
                self.assertFalse(app.capturing)
                self.assertEqual(app._hidden_for_capture, [])
                self.assertIn("撮影できません", app.status_var.get())
        finally:
            _destroy_root(root)

    def test_a_finished_capture_is_copied_before_the_editor_opens(self):
        from shinclipboard import app as app_module

        root = self._root()
        try:
            with tempfile.TemporaryDirectory() as folder:
                app = self._app(root, folder)
                app.capturing = True
                with unittest.mock.patch.object(app_module, "write_clipboard_image", return_value=7) as write:
                    app._capture_done(Image.new("RGB", (32, 16), "red"))
                self.assertEqual(write.call_count, 1)
                self.assertTrue(Path(write.call_args[0][0]).exists(), "the clipboard is fed a real PNG")
                self.assertEqual(app.last_clipboard_token, 7)
                self.assertEqual(len(app.history.search()), 1, "the raw capture lands in the history")
                self.assertEqual(len(app.editors), 1, "and the editor still opens")
                self.assertFalse(app.capturing)
                next(iter(app.editors)).close()
        finally:
            _destroy_root(root)

    def test_a_delayed_capture_counts_down_then_captures(self):
        root = self._root()
        try:
            with tempfile.TemporaryDirectory() as folder:
                app = self._app(root, folder)
                app.config["settings"]["screenshot_delay_seconds"] = 2
                with unittest.mock.patch("shinclipboard.app.RegionSelector") as selector:
                    app.start_delayed_capture()
                    self.assertTrue(app.capturing)
                    self.assertIsNotNone(app._countdown_window, "a visible countdown tells the user what is happening")
                    self.assertIn("2 秒後", app._countdown_var.get())
                    selector.assert_not_called()
                    # Drive the ticks by hand instead of waiting real seconds.
                    root.after_cancel(app._countdown_job)
                    app._tick_countdown()
                    root.after_cancel(app._countdown_job)
                    self.assertIn("1 秒後", app._countdown_var.get())
                    app._tick_countdown()
                    self.assertIsNone(app._countdown_window, "the countdown must not end up in the picture")
                    root.update()
                    for _ in range(20):
                        if selector.called:
                            break
                        root.after(20, root.quit)
                        root.mainloop()
                    selector.return_value.start.assert_called_once()
                app._capture_done(None)
        finally:
            _destroy_root(root)

    def test_pressing_the_hotkey_again_cancels_the_countdown(self):
        root = self._root()
        try:
            with tempfile.TemporaryDirectory() as folder:
                app = self._app(root, folder)
                with unittest.mock.patch("shinclipboard.app.RegionSelector") as selector:
                    app.start_capture(delay_seconds=5)
                    self.assertIsNotNone(app._countdown_window)
                    app.start_capture()  # either hotkey while counting down aborts
                    self.assertIsNone(app._countdown_window)
                    self.assertFalse(app.capturing)
                    self.assertIn("中止", app.status_var.get())
                    root.after(50, root.quit)
                    root.mainloop()
                    selector.assert_not_called()
        finally:
            _destroy_root(root)

    def test_a_successful_capture_shows_only_the_editor(self):
        from shinclipboard import app as app_module

        root = self._root()
        try:
            with tempfile.TemporaryDirectory() as folder:
                app = self._app(root, folder)
                shown: list[str] = []
                app.root.deiconify = lambda: shown.append("settings")
                app.call_window.deiconify = lambda: shown.append("popup")
                app.capturing = True
                app._hidden_for_capture = [app.root, app.call_window]
                with unittest.mock.patch.object(app_module, "write_clipboard_image", return_value=1):
                    app._capture_done(Image.new("RGB", (8, 8), "red"))
                self.assertEqual(shown, [], "the windows hidden for the shot stay hidden")
                self.assertEqual(len(app.editors), 1)
                next(iter(app.editors)).close()

                app.capturing = True
                app._hidden_for_capture = [app.root, app.call_window]
                app._capture_done(None)
                self.assertEqual(shown, ["settings", "popup"], "an aborted shot restores what was open")
        finally:
            _destroy_root(root)

    def test_a_new_shot_replaces_the_previous_screenshot_editor_only(self):
        from shinclipboard import app as app_module

        root = self._root()
        try:
            with tempfile.TemporaryDirectory() as folder:
                app = self._app(root, folder)
                with unittest.mock.patch.object(app_module, "write_clipboard_image", return_value=1):
                    app.capturing = True
                    app._capture_done(Image.new("RGB", (8, 8), "red"))
                    first = next(iter(app.editors))
                    self.assertTrue(first.from_screenshot)
                    # An editor opened from the history is not a shot and is left alone.
                    relative, width, height = app.store.save_image(Image.new("RGB", (4, 4), "blue"))
                    app.history.add_image(relative, width, height)
                    app._edit_image_item(app.history.search()[0])
                    kept = next(editor for editor in app.editors if editor is not first)
                    self.assertFalse(kept.from_screenshot)

                    # The old editor would otherwise be in the picture, so the
                    # capture hides it along with the app's own windows.
                    hidden: list[str] = []
                    first.window.winfo_viewable = lambda: True
                    first.window.withdraw = lambda: hidden.append("editor")
                    kept.window.winfo_viewable = lambda: True
                    kept.window.withdraw = lambda: hidden.append("history editor")
                    with unittest.mock.patch("shinclipboard.app.RegionSelector"):
                        app._begin_capture()
                    self.assertEqual(hidden, ["editor"])
                    self.assertIn(first.window, app._hidden_for_capture)

                    app._capture_done(Image.new("RGB", (16, 16), "green"))
                self.assertNotIn(first, app.editors, "the previous shot's editor is closed")
                self.assertIn(kept, app.editors)
                shots = [editor for editor in app.editors if editor.from_screenshot]
                self.assertEqual([editor.base.size for editor in shots], [(16, 16)])
                for editor in list(app.editors):
                    editor.close()
        finally:
            _destroy_root(root)

    def test_an_aborted_shot_brings_the_previous_screenshot_editor_back(self):
        from shinclipboard import app as app_module

        root = self._root()
        try:
            with tempfile.TemporaryDirectory() as folder:
                app = self._app(root, folder)
                with unittest.mock.patch.object(app_module, "write_clipboard_image", return_value=1):
                    app.capturing = True
                    app._capture_done(Image.new("RGB", (8, 8), "red"))
                editor = next(iter(app.editors))
                restored: list[str] = []
                editor.window.deiconify = lambda: restored.append("editor")
                app.capturing = True
                app._hidden_for_capture = [editor.window]
                app._capture_done(None)
                self.assertEqual(restored, ["editor"])
                self.assertEqual(app.editors, {editor}, "nothing is closed when no shot was taken")
                editor.close()
        finally:
            _destroy_root(root)

    def test_the_editor_answers_to_the_command_key_on_macos(self):
        from shinclipboard import editor as editor_module

        root = self._root()
        try:
            with tempfile.TemporaryDirectory() as folder:
                app = self._app(root, folder)
                with unittest.mock.patch.object(editor_module, "IS_MAC", True):
                    editor = app.open_editor(Image.new("RGB", (8, 8), "red"))
                for key in ("z", "y", "c", "s", "Key-0"):
                    self.assertTrue(editor.window.bind(f"<Command-{key}>"), key)
                    self.assertTrue(editor.window.bind(f"<Control-{key}>"), "Ctrl keeps working too")
                self.assertTrue(editor.canvas.bind("<Command-MouseWheel>"))
                editor.close()

                with unittest.mock.patch.object(editor_module, "IS_MAC", False):
                    editor = app.open_editor(Image.new("RGB", (8, 8), "red"))
                self.assertFalse(editor.window.bind("<Command-z>"), "Windows has no Command key")
                editor.close()
        finally:
            _destroy_root(root)

    def test_copy_and_undo_keys_leave_an_open_text_box_alone(self):
        root = self._root()
        try:
            with tempfile.TemporaryDirectory() as folder:
                app = self._app(root, folder)
                editor = app.open_editor(Image.new("RGB", (80, 40), "red"))
                copied: list[str] = []
                editor.copy_to_clipboard = lambda: copied.append("image") or "break"
                editor._open_text_box(5, 5)
                editor._text_box.insert("1.0", "キー")
                self.assertIsNone(editor._on_copy_key(), "Cmd+C while typing copies the text, not the picture")
                self.assertEqual(copied, [])
                self.assertEqual(editor.redo(), "break")
                self.assertIsNotNone(editor._text_box, "and the box is still open")
                editor._commit_text_box()
                self.assertEqual([shape.text for shape in editor.document.shapes], ["キー"])
                self.assertEqual(editor._on_copy_key(), "break")
                self.assertEqual(copied, ["image"])
                editor.close()
        finally:
            _destroy_root(root)

    def test_the_preview_is_drawn_with_the_face_the_export_uses(self):
        from shinclipboard import editor as editor_module
        from shinclipboard.annotations import resolve_font
        from shinclipboard.platform_support import UI_FONT_FAMILY

        root = self._root()
        try:
            with tempfile.TemporaryDirectory() as folder:
                app = self._app(root, folder)
                editor = app.open_editor(Image.new("RGB", (8, 8), "red"))
                exported = resolve_font(24).getname()[0]
                self.assertEqual(editor._font_family, exported, "Tk knows the exported face by this name")
                self.assertEqual(editor._font(24)[0], exported)
                editor.close()

                fake = unittest.mock.Mock()
                fake.getname.return_value = ("No Such Face 1234", "Regular")
                with unittest.mock.patch.object(editor_module, "resolve_font", return_value=fake):
                    self.assertEqual(
                        editor_module.preview_font_family(root), UI_FONT_FAMILY,
                        "a face Tk cannot find falls back to the UI font instead of Tk's silent substitute",
                    )
        finally:
            _destroy_root(root)

    def test_the_preview_places_text_where_the_export_draws_it(self):
        from shinclipboard.annotations import Shape, resolve_font, text_line_height

        root = self._root()
        try:
            with tempfile.TemporaryDirectory() as folder:
                app = self._app(root, folder)
                editor = app.open_editor(Image.new("RGB", (400, 200), "white"))
                editor.set_zoom(1.0)
                editor.document.shapes.append(Shape("text", [(20, 30)], text="キー\n2行目", color="#e02424", font_size=32))
                editor.refresh()
                items = editor.canvas.find_withtag(editor.document.shapes[0].id)
                self.assertEqual(len(items), 2, "one canvas item per line")
                # PIL puts the baseline `ascent` below the point it is given; a
                # canvas item is placed so that its own baseline lands there too,
                # whatever Tk pads above the ascender for this face.
                pil_ascent = resolve_font(32).getmetrics()[0]
                tk_ascent = editor._tk_font(32).metrics("ascent")
                first_top = editor.canvas.coords(items[0])[1] - editor._offset[1]
                self.assertEqual(first_top + tk_ascent, 30 + pil_ascent)
                second_top = editor.canvas.coords(items[1])[1] - editor._offset[1]
                self.assertEqual(second_top - first_top, text_line_height(32), "lines keep the export's pitch")
                editor.close()
        finally:
            _destroy_root(root)

    def test_changing_the_font_size_or_colour_while_typing_changes_the_text(self):
        from shinclipboard.annotations import Shape

        root = self._root()
        try:
            with tempfile.TemporaryDirectory() as folder:
                app = self._app(root, folder)
                editor = app.open_editor(Image.new("RGB", (200, 100), "white"))
                editor.set_zoom(1.0)
                editor.font_size_var.set("24")
                editor._open_text_box(5, 5)
                editor._text_box.insert("1.0", "キー")
                editor.font_size_var.set("48")
                editor._pick_color("#2563eb")
                box_pixels = lambda: int(root.tk.splitlist(editor._text_box.cget("font"))[1])  # noqa: E731
                self.assertEqual(box_pixels(), -48, "the box is redrawn at the new size")
                self.assertEqual(editor._text_box.cget("foreground"), "#2563eb")
                editor.font_size_var.set("")  # half-typed in the spinbox
                self.assertEqual(box_pixels(), -48, "an unreadable size keeps the last one")
                editor._commit_text_box()
                shape = editor.document.shapes[0]
                self.assertEqual((shape.text, shape.font_size, shape.color), ("キー", 48, "#2563eb"))

                # Reopening an existing text shows its own size and colour in the
                # toolbar, so a nudge of the spinbox grows *this* text.
                editor.font_size_var.set("24")
                editor._pick_color("#e02424")
                editor._open_text_box(*shape.points[0], shape=shape)
                self.assertEqual(editor.font_size_var.get(), "48")
                self.assertEqual(editor.color_var.get(), "#2563eb")
                editor.font_size_var.set("50")
                editor._commit_text_box()
                self.assertEqual([(s.text, s.font_size, s.color) for s in editor.document.shapes], [("キー", 50, "#2563eb")])
                editor.close()
        finally:
            _destroy_root(root)

    def test_the_text_box_is_wide_enough_for_japanese(self):
        root = self._root()
        try:
            with tempfile.TemporaryDirectory() as folder:
                app = self._app(root, folder)
                editor = app.open_editor(Image.new("RGB", (200, 100), "white"))
                editor.set_zoom(1.0)
                editor._open_text_box(5, 5)
                text = "キーボードショートカット一覧です"
                editor._text_box.insert("1.0", text)
                editor._grow_text_box()
                font = editor._tk_font(editor._pixels(editor._text_box_size))
                needed = font.measure(text) / font.measure("0")
                self.assertGreaterEqual(int(editor._text_box.cget("width")), needed, "a kana is about two '0's wide")
                editor._cancel_text_box()
                editor.close()
        finally:
            _destroy_root(root)

    def test_the_font_menu_offers_the_catalog_and_the_choice_is_kept(self):
        from shinclipboard import editor as editor_module
        from shinclipboard.annotations import Shape
        from shinclipboard.fonts import FontCatalog, FontFace

        mincho = Path("/System/Library/Fonts/ヒラギノ明朝 ProN.ttc")
        if not mincho.exists():
            self.skipTest("uses the fonts that ship with macOS")
        catalog = FontCatalog([FontFace("Hiragino Mincho ProN", "W3", str(mincho))])
        root = self._root()
        try:
            with tempfile.TemporaryDirectory() as folder, \
                    unittest.mock.patch.object(editor_module, "CATALOG", catalog), \
                    unittest.mock.patch("shinclipboard.annotations.CATALOG", catalog):
                app = self._app(root, folder)
                editor = app.open_editor(Image.new("RGB", (200, 100), "white"))
                editor.set_zoom(1.0)
                self.assertEqual(editor.font_var.get(), "（標準）")
                self.assertEqual(list(editor.font_combo["values"]), ["（標準）", "Hiragino Mincho ProN"])

                # Picking a family while typing restyles the box and the committed text.
                editor._open_text_box(5, 5)
                editor._text_box.insert("1.0", "注釈")
                editor.font_var.set("Hiragino Mincho ProN")
                editor._font_chosen()
                self.assertEqual(root.tk.splitlist(editor._text_box.cget("font"))[0], "Hiragino Mincho ProN")
                editor._commit_text_box()
                shape = editor.document.shapes[0]
                self.assertEqual(shape.font, "Hiragino Mincho ProN")
                self.assertEqual(editor._font(24, shape.font)[0], "Hiragino Mincho ProN", "the preview uses it too")
                self.assertEqual(app.config["settings"]["annotation_font"], "Hiragino Mincho ProN")
                self.assertEqual(
                    app.store.load_config()["settings"]["annotation_font"], "Hiragino Mincho ProN", "and it is saved"
                )

                # Reopening the text shows its family; a fresh editor starts from the saved one.
                editor.font_var.set("（標準）")
                editor._open_text_box(*shape.points[0], shape=shape)
                self.assertEqual(editor.font_var.get(), "Hiragino Mincho ProN")
                editor._cancel_text_box()
                self.assertEqual(shape.font, "Hiragino Mincho ProN", "cancelling changes nothing")
                editor.close()
                second = app.open_editor(Image.new("RGB", (8, 8), "white"))
                self.assertEqual(second.font_var.get(), "Hiragino Mincho ProN")
                self.assertEqual(second._new_shape("text", [(0, 0)]).font, "Hiragino Mincho ProN")

                # With the select tool, a pick restyles the selected text and can be undone.
                second.document.shapes.append(Shape("text", [(1, 1)], text="x", font_size=12))
                second.selected_id = second.document.shapes[0].id
                second.font_var.set("（標準）")
                second._font_chosen()
                self.assertEqual(second.document.shapes[0].font, "")
                second.font_var.set("Hiragino Mincho ProN")
                second._font_chosen()
                self.assertEqual(second.document.shapes[0].font, "Hiragino Mincho ProN")
                second.undo()
                self.assertEqual(second.document.shapes[0].font, "")
                second.close()
        finally:
            _destroy_root(root)

    def test_a_family_tk_does_not_know_previews_in_the_default_face(self):
        from shinclipboard import editor as editor_module
        from shinclipboard.fonts import FontCatalog, FontFace

        catalog = FontCatalog([FontFace("No Such Face 1234", "Regular", "/nowhere.ttf")])
        root = self._root()
        try:
            with tempfile.TemporaryDirectory() as folder, unittest.mock.patch.object(editor_module, "CATALOG", catalog):
                app = self._app(root, folder)
                editor = app.open_editor(Image.new("RGB", (8, 8), "white"))
                self.assertEqual(editor._font(24, "No Such Face 1234")[0], editor._font_family)
                self.assertEqual(editor._font(24, "")[0], editor._font_family)
                editor.close()
        finally:
            _destroy_root(root)

    def test_the_font_menu_fills_itself_once_the_background_scan_is_done(self):
        from shinclipboard import editor as editor_module
        from shinclipboard.fonts import FontCatalog, FontFace

        catalog = FontCatalog()  # not scanned yet
        root = self._root()
        try:
            with tempfile.TemporaryDirectory() as folder, unittest.mock.patch.object(editor_module, "CATALOG", catalog):
                app = self._app(root, folder)
                editor = app.open_editor(Image.new("RGB", (8, 8), "white"))
                self.assertEqual(list(editor.font_combo["values"]), ["（標準）"])
                self.assertIsNotNone(editor._font_menu_job, "the editor keeps looking")
                catalog._install([FontFace("Fake Family", "Regular", "/nowhere.ttf")])
                editor.window.after_cancel(editor._font_menu_job)
                editor._watch_font_catalog()
                self.assertEqual(list(editor.font_combo["values"]), ["（標準）", "Fake Family"])
                self.assertIsNone(editor._font_menu_job, "and stops once the list is in")
                editor.close()
        finally:
            _destroy_root(root)

    def test_a_second_capture_request_is_ignored_while_one_is_running(self):
        root = self._root()
        try:
            with tempfile.TemporaryDirectory() as folder:
                app = self._app(root, folder)
                app.capturing = True
                with unittest.mock.patch("shinclipboard.app.RegionSelector") as selector:
                    app.start_capture()
                    selector.assert_not_called()
        finally:
            _destroy_root(root)

    def test_the_popup_binds_the_image_editor_without_stealing_the_quick_key(self):
        root = self._root()
        try:
            with tempfile.TemporaryDirectory() as folder:
                app = self._app(root, folder)
                self.assertTrue(app.call_window.bind("<Control-e>"))
                self.assertTrue(app.call_history_list.bind("<Button-3>"))

                app.history.add("first")
                app._refresh_call_history()
                pasted: list[str] = []
                app._paste_text = lambda text: pasted.append(text)
                event = type("Event", (), {"char": "e", "state": 0})()
                self.assertIsNone(app._quick_select(event), "a bare 'e' is a quick key, not the editor")
                ctrl_event = type("Event", (), {"char": "e", "state": 0x4})()
                self.assertIsNone(app._quick_select(ctrl_event), "Ctrl+E is left to the editor binding")
        finally:
            _destroy_root(root)


if __name__ == "__main__":
    unittest.main()
