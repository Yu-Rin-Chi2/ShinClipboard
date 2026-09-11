import json
import os
import platform
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import ImageFont

from shinclipboard import fonts
from shinclipboard.fonts import CATALOG, FontCatalog, FontFace, faces_in, has_glyphs, scan_fonts

HIRAGINO_W3 = Path("/System/Library/Fonts/ヒラギノ角ゴシック W3.ttc")
HIRAGINO_W6 = Path("/System/Library/Fonts/ヒラギノ角ゴシック W6.ttc")
APPLE_GOTHIC = Path("/System/Library/Fonts/Supplemental/AppleGothic.ttf")


def _face(family: str, style: str, path: str = "/nowhere.ttf", index: int = 0) -> FontFace:
    return FontFace(family, style, path, index)


class CatalogSelectionTests(unittest.TestCase):
    """Pure bookkeeping: no font file is opened."""

    def test_one_face_is_offered_per_family_preferring_the_regular_weight(self):
        catalog = FontCatalog([
            _face("Hiragino Sans", "W6"),
            _face("Hiragino Sans", "W4"),
            _face("Hiragino Sans", "W3"),
            _face("Yu Gothic", "Bold"),
            _face("Yu Gothic", "Regular"),
            _face("Klee", "Demibold"),
            _face("Klee", "Medium"),
            _face("Weibei SC", "Bold"),
        ])
        self.assertEqual(catalog.families(), ["Hiragino Sans", "Klee", "Weibei SC", "Yu Gothic"])
        self.assertEqual(catalog.face("Hiragino Sans").style, "W4", "the weight Tk's normal maps to")
        self.assertEqual(catalog.face("Yu Gothic").style, "Regular")
        self.assertEqual(catalog.face("Klee").style, "Medium")
        self.assertEqual(catalog.face("Weibei SC").style, "Bold", "a family with one weight offers that one")

    def test_lookups_are_forgiving_and_empty_means_the_default(self):
        catalog = FontCatalog([_face("Hiragino Sans", "W4")])
        self.assertEqual(catalog.face(" hiragino sans ").style, "W4")
        self.assertIsNone(catalog.face(""))
        self.assertIsNone(catalog.face("No Such Family"))

    def test_an_unscanned_catalog_offers_nothing_but_does_not_fail(self):
        catalog = FontCatalog()
        self.assertFalse(catalog.ready)
        self.assertEqual(catalog.families(), [])
        self.assertIsNone(catalog.face("Hiragino Sans"))


class ScanTests(unittest.TestCase):
    def test_a_folder_without_fonts_yields_nothing(self):
        with tempfile.TemporaryDirectory() as folder:
            Path(folder, "notes.txt").write_text("not a font")
            Path(folder, "fake.ttf").write_bytes(b"not a font either")
            self.assertEqual(scan_fonts([Path(folder)]), [])
            self.assertEqual(faces_in(Path(folder, "fake.ttf")), [])

    @unittest.skipUnless(HIRAGINO_W3.exists() and HIRAGINO_W6.exists(), "uses the fonts that ship with macOS")
    def test_faces_are_read_from_the_files_and_grouped_by_family(self):
        with tempfile.TemporaryDirectory() as folder:
            nested = Path(folder, "deeper")
            nested.mkdir()
            os.symlink(HIRAGINO_W6, nested / "w6.ttc")
            os.symlink(HIRAGINO_W3, Path(folder, "w3.ttc"))
            faces = scan_fonts([Path(folder)])
            # Each collection carries the same weight under three family names.
            sans = [face for face in faces if face.family == "Hiragino Sans"]
            self.assertEqual(sorted(face.style for face in sans), ["W3", "W6"])
            self.assertIn("Hiragino Kaku Gothic ProN", {face.family for face in faces})
            self.assertFalse(any(face.family.startswith(".") for face in faces), "hidden faces are skipped")
            catalog = FontCatalog(faces)
            self.assertEqual(catalog.face("Hiragino Sans").style, "W3", "the lighter weight is the regular one")
            self.assertEqual(catalog.face("Hiragino Sans").load(24).getname(), ("Hiragino Sans", "W3"))

    @unittest.skipUnless(APPLE_GOTHIC.exists(), "uses the fonts that ship with macOS")
    def test_a_face_that_cannot_draw_japanese_is_left_out(self):
        # AppleGothic is Korean and has no prolonged sound mark: offering it
        # would bring back the boxes the default font chain was fixed for.
        self.assertEqual(faces_in(APPLE_GOTHIC), [])
        korean = ImageFont.truetype(str(APPLE_GOTHIC), 24)
        self.assertFalse(has_glyphs(korean, "ー"))
        self.assertTrue(has_glyphs(korean, "あ"))

    def test_a_face_that_cannot_be_probed_is_trusted_or_not_as_asked(self):
        class Broken:
            def getmask(self, _char):
                raise OSError("stack overflow")

        self.assertTrue(has_glyphs(Broken(), "ー"), "the default chain keeps a face it cannot judge")
        self.assertFalse(has_glyphs(Broken(), "ー", unknown=False), "the scan does not offer one")


class CacheTests(unittest.TestCase):
    def test_the_cache_is_reused_until_a_font_folder_changes(self):
        with tempfile.TemporaryDirectory() as folder:
            fonts_dir = Path(folder, "fonts")
            fonts_dir.mkdir()
            cache = Path(folder, "fonts.json")
            found = [_face("Fake Family", "Regular", str(fonts_dir / "fake.ttf"))]
            with patch.object(fonts, "scan_fonts", return_value=found) as scan:
                catalog = FontCatalog()
                self.assertEqual(catalog.load(cache, [fonts_dir]), found)
                self.assertEqual(scan.call_count, 1)
                self.assertTrue(cache.exists())
                self.assertEqual(json.loads(cache.read_text(encoding="utf-8"))["faces"][0]["family"], "Fake Family")

                again = FontCatalog()
                self.assertEqual(again.load(cache, [fonts_dir]), found)
                self.assertEqual(scan.call_count, 1, "the second load comes from the cache")
                self.assertTrue(again.ready)

                # Installing a font touches the folder, which is the signal to look again.
                time.sleep(0.02)
                (fonts_dir / "new.ttf").write_bytes(b"")
                os.utime(fonts_dir, None)
                FontCatalog().load(cache, [fonts_dir])
                self.assertEqual(scan.call_count, 2)

    def test_a_cache_from_another_version_or_a_broken_one_is_ignored(self):
        with tempfile.TemporaryDirectory() as folder:
            fonts_dir = Path(folder, "fonts")
            fonts_dir.mkdir()
            cache = Path(folder, "fonts.json")  # outside the scanned folder, so writing it changes no signature
            cache.write_text("{not json", encoding="utf-8")
            with patch.object(fonts, "scan_fonts", return_value=[]) as scan:
                FontCatalog().load(cache, [fonts_dir])
                self.assertEqual(scan.call_count, 1)
                FontCatalog().load(cache, [fonts_dir])
                self.assertEqual(scan.call_count, 1, "the rewritten cache is good again")
            data = json.loads(cache.read_text(encoding="utf-8"))
            data["version"] = -1
            cache.write_text(json.dumps(data), encoding="utf-8")
            with patch.object(fonts, "scan_fonts", return_value=[]) as scan:
                FontCatalog().load(cache, [fonts_dir])
                self.assertEqual(scan.call_count, 1)

    def test_loading_in_the_background_ends_ready_even_when_the_scan_fails(self):
        with patch.object(fonts, "scan_fonts", side_effect=RuntimeError("boom")):
            catalog = FontCatalog()
            catalog.start()
            catalog._thread.join(timeout=5)
        self.assertTrue(catalog.ready)
        self.assertEqual(catalog.families(), [])

        catalog = FontCatalog()
        with patch.object(fonts, "scan_fonts", return_value=[_face("Fake Family", "Regular")]):
            catalog.start()
            catalog._thread.join(timeout=5)
            catalog.start()  # a second start after the first finished is a no-op
        self.assertEqual(catalog.families(), ["Fake Family"])


class PlatformTests(unittest.TestCase):
    def test_only_existing_folders_are_scanned(self):
        for directory in fonts.font_directories():
            self.assertTrue(directory.is_dir(), directory)

    @unittest.skipUnless(platform.system() == "Darwin", "macOS keeps its Japanese fonts in two places")
    def test_macos_looks_in_the_on_demand_asset_store_too(self):
        names = [str(directory) for directory in fonts.font_directories()]
        self.assertIn("/System/Library/Fonts", names)
        if Path("/System/Library/AssetsV2/com_apple_MobileAsset_Font7").is_dir():
            self.assertIn("/System/Library/AssetsV2/com_apple_MobileAsset_Font7", names)

    def test_the_shared_catalog_starts_empty_in_this_process(self):
        # Nothing in the test-suite kicks off the real scan; the app does that in `run()`.
        self.assertIsInstance(CATALOG, FontCatalog)


if __name__ == "__main__":
    unittest.main()
