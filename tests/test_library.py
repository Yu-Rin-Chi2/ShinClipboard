"""Colour recognition, and the colour / image libraries."""

import tempfile
import unittest
import unittest.mock
from pathlib import Path

from PIL import Image

from shinclipboard.colors import is_hex_color, normalize_hex_color, parse_hex_color, swatch_image
from shinclipboard.core import ClipboardHistory
from shinclipboard.storage import JsonStore, library_image_paths
from shinclipboard.transfer import create_backup, export_settings_package, import_settings_package, restore_backup


class ColorParsingTests(unittest.TestCase):
    def test_hash_forms_are_colours(self):
        self.assertEqual(parse_hex_color("#f00"), (255, 0, 0, 255))
        self.assertEqual(parse_hex_color("#f008"), (255, 0, 0, 136))
        self.assertEqual(parse_hex_color("#075DCC"), (7, 93, 204, 255))
        self.assertEqual(parse_hex_color("  #00000080\n"), (0, 0, 0, 128))

    def test_bare_digits_and_prefixes_are_not_colours(self):
        for text in ("075dcc", "0x075dcc", "#12345", "#gggggg", "#", "", "#075dcc extra"):
            self.assertIsNone(parse_hex_color(text), text)
            self.assertFalse(is_hex_color(text), text)

    def test_normalized_spelling_drops_an_opaque_alpha(self):
        self.assertEqual(normalize_hex_color("#F00"), "#ff0000")
        self.assertEqual(normalize_hex_color("#ff0000ff"), "#ff0000")
        self.assertEqual(normalize_hex_color("#ff000080"), "#ff000080")
        self.assertIsNone(normalize_hex_color("red"))

    def test_swatch_is_the_colour_with_a_border_and_a_checkerboard_when_translucent(self):
        opaque = swatch_image((255, 0, 0, 255), (28, 16))
        self.assertEqual(opaque.size, (28, 16))
        self.assertEqual(opaque.getpixel((14, 8)), (255, 0, 0, 255))
        self.assertNotEqual(opaque.getpixel((0, 0)), (255, 0, 0, 255), "the edge is a border, not the colour")

        translucent = swatch_image((0, 0, 0, 0), (28, 16))
        self.assertNotEqual(translucent.getpixel((2, 2)), translucent.getpixel((6, 2)), "checkerboard shows through")


class ColorHistoryTests(unittest.TestCase):
    def test_colours_answer_to_a_search_for_their_kind(self):
        history = ClipboardHistory(limit=10)
        history.add("#ff0000")
        history.add("plain text")
        self.assertEqual([item.text for item in history.search("色")], ["#ff0000"])
        self.assertEqual([item.text for item in history.search("color")], ["#ff0000"])
        self.assertEqual([item.text for item in history.search("ff00")], ["#ff0000"])


class LibraryStorageTests(unittest.TestCase):
    def test_a_new_config_has_a_sample_colour_group_and_no_image_groups(self):
        with tempfile.TemporaryDirectory() as folder:
            config = JsonStore(Path(folder)).load_config()
            self.assertEqual(config["color_groups"][0]["name"], "サンプル")
            self.assertTrue(all(is_hex_color(color["value"]) for color in config["color_groups"][0]["colors"]))
            self.assertEqual(config["image_groups"], [])

    def test_an_older_config_gains_the_library_keys_and_item_defaults(self):
        with tempfile.TemporaryDirectory() as folder:
            store = JsonStore(Path(folder))
            config = store.load_config()
            del config["color_groups"]
            config["image_groups"] = [{"id": "g", "name": "ロゴ", "images": [{"id": "i", "title": "logo", "image_path": "library/x.png"}]}]
            store.save_config(config)
            loaded = store.load_config()
            self.assertEqual(loaded["color_groups"], [])
            entry = loaded["image_groups"][0]["images"][0]
            self.assertEqual((entry["memo"], entry["hotkey"]), ("", ""))

    def test_library_pictures_live_apart_from_the_history_and_survive_its_cleanup(self):
        with tempfile.TemporaryDirectory() as folder:
            store = JsonStore(Path(folder))
            relative, width, height = store.save_library_image(Image.new("RGB", (6, 4), "blue"))
            self.assertTrue(relative.startswith("library/"))
            self.assertEqual((width, height), (6, 4))
            self.assertTrue(store.image_path(relative).exists())

            store.save_history(ClipboardHistory(limit=10))  # runs cleanup_images on an empty history
            self.assertTrue(store.image_path(relative).exists(), "history cleanup must not touch the library")

    def test_library_cleanup_keeps_only_referenced_pictures(self):
        with tempfile.TemporaryDirectory() as folder:
            store = JsonStore(Path(folder))
            kept, _, _ = store.save_library_image(Image.new("RGB", (2, 2), "red"))
            dropped, _, _ = store.save_library_image(Image.new("RGB", (2, 2), "green"))
            config = store.load_config()
            config["image_groups"] = [{"id": "g", "name": "g", "images": [{"id": "i", "title": "t", "image_path": kept, "width": 2, "height": 2}]}]
            self.assertEqual(library_image_paths(config), {kept})

            store.cleanup_library(config)
            self.assertTrue(store.image_path(kept).exists())
            self.assertFalse(store.image_path(dropped).exists())

    def test_a_plain_config_save_never_deletes_library_pictures(self):
        """Importing someone else's config must not wipe the local pictures."""
        with tempfile.TemporaryDirectory() as folder:
            store = JsonStore(Path(folder))
            orphan, _, _ = store.save_library_image(Image.new("RGB", (2, 2), "red"))
            store.save_config(store.load_config())
            self.assertTrue(store.image_path(orphan).exists())

    def test_backups_carry_the_library(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            store = JsonStore(base / "source")
            store.load_config()
            relative, _, _ = store.save_library_image(Image.new("RGB", (3, 3), "red"))
            store.save_history(ClipboardHistory(limit=10))
            backup = base / "all.zip"
            create_backup(store.config_path, store.history_path, backup)

            restored_config = base / "restored" / "config.json"
            restored_history = base / "restored" / "history.json"
            restored_config.parent.mkdir()
            restore_backup(backup, restored_config, restored_history)
            self.assertTrue((restored_history.parent / relative).exists())


class SettingsPackageTests(unittest.TestCase):
    """The ZIP that carries definitions, colours, pictures and rules to another machine."""

    def _source(self, base: Path) -> tuple[JsonStore, dict, str]:
        store = JsonStore(base / "source")
        config = store.load_config()
        config["settings"]["screenshot_save_dir"] = r"D:\only-here"
        config["settings"]["theme"] = "dark"
        config["groups"][0]["snippets"].append({"id": "s2", "title": "署名", "text": "-- 太郎", "memo": "", "hotkey": "primary+alt+2"})
        relative, width, height = store.save_library_image(Image.new("RGB", (3, 2), "red"))
        config["image_groups"] = [{"id": "ig", "name": "ロゴ", "images": [
            {"id": "i1", "title": "logo", "image_path": relative, "width": width, "height": height, "memo": "", "hotkey": ""},
        ]}]
        store.save_config(config)
        return store, config, relative

    def test_replace_brings_everything_portable_and_leaves_the_machine_settings(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            source, config, relative = self._source(base)
            package = base / "settings.zip"
            self.assertEqual(export_settings_package(config, source.data_dir, package), 1)

            target = JsonStore(base / "target")
            local = target.load_config()
            local["settings"]["screenshot_save_dir"] = r"E:\mine"
            local["groups"] = [{"id": "old", "name": "消える", "snippets": []}]
            summary = import_settings_package(local, target.data_dir, package, "replace")
            self.assertEqual(summary["groups"], 3, "definition, colour and image groups")
            self.assertEqual([group["name"] for group in local["groups"]], ["サンプル"])
            self.assertEqual(local["groups"][0]["snippets"][1]["hotkey"], "primary+alt+2")
            self.assertEqual(local["settings"]["theme"], "dark")
            self.assertEqual(local["settings"]["screenshot_save_dir"], r"E:\mine", "a folder path is not portable")
            self.assertTrue(target.image_path(relative).exists(), "the picture travelled with the package")
            self.assertEqual(local["image_groups"][0]["images"][0]["image_path"], relative)

    def test_merge_adds_what_is_missing_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            source, config, relative = self._source(base)
            package = base / "settings.zip"
            export_settings_package(config, source.data_dir, package)

            target = JsonStore(base / "target")
            local = target.load_config()
            local["settings"]["theme"] = "green"
            local["groups"][0]["snippets"].append({"id": "mine", "title": "ローカル", "text": "ここだけ", "memo": "", "hotkey": ""})
            local["color_groups"][0]["colors"][0]["title"] = "ローカルの青"  # same value, different title
            summary = import_settings_package(local, target.data_dir, package, "merge")
            self.assertEqual(summary, {"groups": 1, "items": 2, "updated": 1}, "one image group with its picture, one snippet, one renamed colour")
            sample = local["groups"][0]
            self.assertEqual([snippet["text"] for snippet in sample["snippets"]], ["お世話になっております。", "ここだけ", "-- 太郎"])
            self.assertEqual(local["color_groups"][0]["colors"][0]["title"], "ブルー", "same colour: the package's fields win")
            self.assertEqual(local["settings"]["theme"], "green", "merge leaves the local settings alone")
            self.assertEqual(local["image_groups"][0]["name"], "ロゴ")
            self.assertNotEqual(local["image_groups"][0]["id"], "ig", "added groups get fresh ids")
            self.assertTrue(target.image_path(relative).exists())

            again = import_settings_package(local, target.data_dir, package, "merge")
            self.assertEqual(again, {"groups": 0, "items": 0, "updated": 0})
            self.assertEqual(len(sample["snippets"]), 3)

    def test_a_foreign_zip_and_an_unknown_mode_are_rejected(self):
        import zipfile

        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            store = JsonStore(base / "t")
            config = store.load_config()
            bogus = base / "bogus.zip"
            with zipfile.ZipFile(bogus, "w") as archive:
                archive.writestr("readme.txt", "hi")
            with self.assertRaises(ValueError):
                import_settings_package(config, store.data_dir, bogus, "merge")
            with self.assertRaises(ValueError):
                import_settings_package(config, store.data_dir, bogus, "sideways")


def _destroy_root(root) -> None:
    try:
        for timer in root.tk.splitlist(root.tk.call("after", "info")):
            root.tk.call("after", "cancel", timer)
    except Exception:
        pass
    root.destroy()


class LibraryAppTests(unittest.TestCase):
    """GUI tests. They never map or focus a window, so they do not steal keyboard input."""

    def _root(self):
        import tkinter as tk

        try:
            root = tk.Tk()
        except tk.TclError as error:
            self.skipTest(f"Tk is unavailable: {error}")
        root.withdraw()
        return root

    def _app(self, root, folder, **overrides):
        from shinclipboard.app import ShinClipboardApp

        namespace = {"_start_tray": lambda self: None, "_restart_hotkeys": lambda self: None}
        namespace.update(overrides)
        headless = type("HeadlessApp", (ShinClipboardApp,), namespace)
        return headless(root, JsonStore(Path(folder)))

    @staticmethod
    def _key(char: str, state: int = 0):
        return type("Event", (), {"char": char, "state": state})()

    def test_the_popup_has_colour_and_image_tabs_that_cycle_through_their_own_groups(self):
        root = self._root()
        try:
            with tempfile.TemporaryDirectory() as folder:
                app = self._app(root, folder)
                app.config["color_groups"].append({"id": "second", "name": "二番目", "colors": []})
                app._refresh_call_window()
                self.assertEqual([app.call_tabs.tab(tab, "text") for tab in app.call_tabs.tabs()], ["履歴", "定型文", "色", "画像"])

                first_group = app.call_pages[2].group_id
                app.call_tabs.select(app.call_tabs.tabs()[2])
                self.assertEqual(app._move_call_group(1), "break")
                self.assertEqual(app.call_pages[2].group_id, "second")
                app._move_call_group(1)
                self.assertEqual(app.call_pages[2].group_id, first_group, "wraps around")
                self.assertEqual(app.call_group_id, app.call_pages[1].group_id, "the definitions page is untouched")

                app.call_tabs.select(app.call_tabs.tabs()[3])
                self.assertEqual(app._move_call_group(1), "break", "no image groups yet: nothing to move to")
                self.assertIsNone(app.call_pages[3].group_id)
        finally:
            _destroy_root(root)

    def test_colour_rows_carry_a_swatch_and_a_quick_key_pastes_the_registered_spelling(self):
        pasted: list[str] = []
        root = self._root()
        try:
            with tempfile.TemporaryDirectory() as folder:
                app = self._app(root, folder, _paste_text=lambda self, text: pasted.append(text))
                app.config["color_groups"][0]["colors"] = [
                    {"id": "a", "title": "メイン", "value": "#075DCC", "memo": "", "hotkey": ""},
                    {"id": "b", "title": "壊れた値", "value": "red", "memo": "", "hotkey": ""},
                ]
                app._refresh_call_window()
                rows = app.call_pages[2].listbox
                self.assertEqual(rows.size(), 2)
                self.assertTrue(rows.row_has_image(0))
                self.assertFalse(rows.row_has_image(1), "an unparseable value falls back to text")
                self.assertIn("#075DCC", rows.get("1.0", "1.end"))

                app.call_tabs.select(app.call_tabs.tabs()[2])
                self.assertEqual(app._quick_select(self._key("1")), "break")
                self.assertEqual(pasted, ["#075DCC"], "the spelling the user registered, untouched")
                self.assertIsNone(app._quick_select(self._key("3")), "no third row")
        finally:
            _destroy_root(root)

    def test_image_rows_show_thumbnails_and_paste_through_the_image_clipboard(self):
        from shinclipboard import app as app_module

        root = self._root()
        try:
            with tempfile.TemporaryDirectory() as folder:
                app = self._app(root, folder)
                relative, width, height = app.store.save_library_image(Image.new("RGB", (400, 100), "red"))
                app.config["image_groups"] = [{"id": "g", "name": "ロゴ", "images": [
                    {"id": "i", "title": "logo", "image_path": relative, "width": width, "height": height, "memo": "", "hotkey": ""},
                    {"id": "m", "title": "missing", "image_path": "library/missing.png", "width": 1, "height": 1, "memo": "", "hotkey": ""},
                ]}]
                app._refresh_call_window()
                rows = app.call_pages[3].listbox
                self.assertEqual(rows.size(), 2)
                self.assertTrue(rows.row_has_image(0))
                self.assertFalse(rows.row_has_image(1), "a missing file falls back to the text label")
                self.assertIn("400×100", rows.get("1.0", "1.end"))
                self.assertEqual((app._thumbnails[relative].width(), app._thumbnails[relative].height()), (200, 50))

                app.call_tabs.select(app.call_tabs.tabs()[3])
                app.config["settings"]["auto_paste"] = False
                with unittest.mock.patch.object(app_module, "write_clipboard_image", return_value=5) as write:
                    self.assertEqual(app._quick_select(self._key("1")), "break")
                self.assertEqual(Path(write.call_args[0][0]), app.store.image_path(relative))
                self.assertEqual(app.last_clipboard_token, 5, "our own write must not come back as a copy")

                app._refresh_call_history()
                self.assertIn(relative, app._thumbnails, "library thumbnails survive the history refresh")
        finally:
            _destroy_root(root)

    def test_copied_colour_codes_get_a_swatch_in_the_popup_and_a_tag_in_the_settings_list(self):
        root = self._root()
        try:
            with tempfile.TemporaryDirectory() as folder:
                app = self._app(root, folder)
                app.history.add("plain")
                app.history.add("#ff0000")  # newest first: row 0
                app._refresh_history()
                rows = app.call_history_list
                self.assertTrue(rows.row_has_image(0))
                self.assertFalse(rows.row_has_image(1))
                self.assertIn("#ff0000", rows.get("1.0", "1.end"))
                self.assertEqual(app.history_list.get(0), "[色] #ff0000")
                self.assertEqual(app.history_list.get(1), "plain")
        finally:
            _destroy_root(root)

    def test_library_hotkeys_paste_text_or_images(self):
        root = self._root()
        try:
            with tempfile.TemporaryDirectory() as folder:
                app = self._app(root, folder)
                app.config["color_groups"][0]["colors"][0]["hotkey"] = "primary+alt+7"
                app.config["image_groups"] = [{"id": "g", "name": "g", "images": [
                    {"id": "i", "title": "t", "image_path": "library/a.png", "width": 1, "height": 1, "memo": "", "hotkey": "primary+alt+8"},
                ]}]
                mappings = app._hotkey_mappings()
                mappings["primary+alt+7"]()
                mappings["primary+alt+8"]()
                self.assertEqual(app.events.get_nowait(), ("paste_text", app.config["color_groups"][0]["colors"][0]["value"]))
                self.assertEqual(app.events.get_nowait(), ("paste_image", "library/a.png"))
        finally:
            _destroy_root(root)

    def test_registering_and_deleting_an_image_manages_its_file(self):
        root = self._root()
        try:
            with tempfile.TemporaryDirectory() as folder:
                app = self._app(root, folder)
                # Thumbnail rows are 60px tall; ten of them would push the button
                # bar - and with it "削除" - out of the window.
                root.update_idletasks()
                self.assertLess(app.image_tree.winfo_reqheight(), 420)
                self.assertTrue(app.image_tree.bind("<Delete>"))
                self.assertTrue(app.color_tree.bind("<Delete>"))
                app.config["image_groups"] = [{"id": "g", "name": "g", "images": []}]
                app.image_groups.refresh(0)
                picture = Image.new("RGB", (5, 5), "blue")
                entry = {"id": "i", "title": "t", "memo": "", "hotkey": "", "image_path": "", "width": 0, "height": 0, "image": picture}
                with unittest.mock.patch.object(app, "_image_dialog", return_value=entry):
                    app._add_image()
                stored = app.config["image_groups"][0]["images"][0]
                self.assertNotIn("image", stored, "the picture is a file now, not part of the config")
                self.assertTrue(stored["image_path"].startswith("library/"))
                self.assertTrue(app.store.image_path(stored["image_path"]).exists())
                self.assertEqual(app.image_tree.get_children(), ("i",))

                app.image_tree.selection_set("i")
                with unittest.mock.patch("tkinter.messagebox.askyesno", return_value=True):
                    app._delete_image()
                self.assertEqual(app.config["image_groups"][0]["images"], [])
                self.assertFalse(app.store.image_path(stored["image_path"]).exists(), "an unreferenced file is collected")
        finally:
            _destroy_root(root)

    def test_dropped_picture_files_are_registered_and_other_files_are_skipped(self):
        root = self._root()
        try:
            with tempfile.TemporaryDirectory() as folder:
                app = self._app(root, folder)
                app.config["image_groups"] = [{"id": "g", "name": "g", "images": []}, {"id": "h", "name": "h", "images": []}]
                app.image_groups.refresh(0)
                app._refresh_call_window()
                base = Path(folder)
                Image.new("RGB", (9, 7), "red").save(base / "my logo.png")
                (base / "notes.txt").write_text("not a picture", encoding="utf-8")
                # tkdnd hands the paths over as a Tcl list with forward slashes
                # even on Windows; a path containing a space arrives in braces.
                event = type("Event", (), {"data": (
                    f"{{{(base / 'my logo.png').as_posix()}}} {(base / 'notes.txt').as_posix()} {(base / 'absent.png').as_posix()}"
                )})()

                self.assertEqual(app._drop_images(event), "copy")
                entries = app.config["image_groups"][0]["images"]
                self.assertEqual([(e["title"], e["width"], e["height"]) for e in entries], [("my logo", 9, 7)])
                self.assertTrue(app.store.image_path(entries[0]["image_path"]).exists())
                self.assertIn("1件", app.status_var.get())
                self.assertIn("1件は飛ばし", app.status_var.get())
                self.assertEqual(app.image_tree.get_children(), (entries[0]["id"],))

                # The popup adds to the group it is showing, which need not be the settings tab's.
                page = app.call_pages[3]
                page.group_id = "h"
                app._refresh_call_page(page)
                self.assertEqual(app._drop_images(event, page), "copy")
                self.assertEqual(len(app.config["image_groups"][1]["images"]), 1)
                self.assertEqual(page.listbox.size(), 1)

                app._drag_active = True
                self.assertEqual(app._drop_images(event, page), "refuse_drop", "our own drag let go over the list")
        finally:
            _destroy_root(root)

    def test_a_history_colour_can_be_registered_into_the_selected_group(self):
        root = self._root()
        try:
            with tempfile.TemporaryDirectory() as folder:
                app = self._app(root, folder)
                app.history.add(" #ABCDEF ")
                item = app.history.search()[0]
                seen: list[str] = []

                def dialog(color=None, initial_value="", parent=None):
                    seen.append(initial_value)
                    return {"id": "new", "title": "登録", "value": initial_value, "memo": "", "hotkey": ""}

                with unittest.mock.patch.object(app, "_color_dialog", dialog):
                    app._register_history_color(item)
                self.assertEqual(seen, ["#ABCDEF"])
                self.assertEqual(app.config["color_groups"][0]["colors"][-1]["value"], "#ABCDEF")
                self.assertEqual(app.tabs.index("current"), app.tabs.index(app.color_tab))
        finally:
            _destroy_root(root)


if __name__ == "__main__":
    unittest.main()
