import tempfile
import threading
import unittest
from pathlib import Path

from PIL import Image

from newclipboard.core import ClipboardHistory, FifoQueue
from newclipboard.hotkeys import DoubleTapDetector, portable_to_pynput
from newclipboard.single_instance import SingleInstance
from newclipboard.storage import JsonStore
from newclipboard.transfer import create_backup, export_snippets_csv, import_snippets_csv, restore_backup
from newclipboard.transforms import apply_transform


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


class CallWindowTests(unittest.TestCase):
    def test_keyboard_switches_tabs_and_snippet_groups(self):
        import tkinter as tk

        from newclipboard.app import NewClipboardApp

        class HeadlessApp(NewClipboardApp):
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
            root.destroy()

    def test_only_quick_keys_paste_from_the_popup(self):
        import tkinter as tk

        from newclipboard.app import NewClipboardApp

        pasted: list[str] = []

        class HeadlessApp(NewClipboardApp):
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
                app.history.add("second")  # newest first: "second" is item 1, "first" is item 2
                app.show_window()
                root.update()
                if app.call_window.focus_get() is not app.call_history_list:
                    self.skipTest("Could not focus the popup window")

                for sequence in ("<Down>", "<Up>", "<Home>", "<End>", "<Shift_L>", "<Control_L>", "<space>"):
                    app.call_history_list.event_generate(sequence)
                    root.update()
                self.assertEqual(pasted, [], "keys without a quick-key character must not paste")

                app.call_history_list.event_generate("<KeyPress-2>")
                root.update()
                self.assertEqual(pasted, ["first"])
                app.call_history_list.selection_clear(0, "end")
                app.call_history_list.selection_set(0)
                app.call_history_list.event_generate("<Return>")
                root.update()
                self.assertEqual(pasted, ["first", "second"])
        finally:
            root.destroy()


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


if __name__ == "__main__":
    unittest.main()
