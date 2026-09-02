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
