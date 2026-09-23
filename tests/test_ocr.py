import queue
import tkinter as tk
import types
import unittest
import unittest.mock

from PIL import Image, ImageDraw, ImageFont

from shinclipboard import ocr
from shinclipboard.app import ShinClipboardApp


class NormalizeLineTests(unittest.TestCase):
    def test_spaces_between_japanese_characters_are_removed(self):
        self.assertEqual(ocr.normalize_line("日 本 語 の テ キ ス ト"), "日本語のテキスト")

    def test_spaces_between_latin_words_are_kept(self):
        self.assertEqual(ocr.normalize_line("Hello World 123"), "Hello World 123")

    def test_mixed_line_keeps_the_boundary_spaces(self):
        self.assertEqual(ocr.normalize_line("設 定 を 開 く Ctrl + S"), "設定を開く Ctrl + S")

    def test_dash_between_katakana_becomes_long_vowel_mark(self):
        self.assertEqual(ocr.normalize_line("ク リ ッ プ ボ - ド"), "クリップボード")
        self.assertEqual(ocr.normalize_line("A - B"), "A - B")

    def test_fullwidth_punctuation_joins_too(self):
        self.assertEqual(ocr.normalize_line("こ ん に ち は 、 世 界 。"), "こんにちは、世界。")


class PrepareTests(unittest.TestCase):
    def test_small_image_is_upscaled_and_large_one_shrunk(self):
        self.assertGreaterEqual(min(ocr._prepare(Image.new("RGBA", (200, 10)), 10000).size), 40)
        self.assertLessEqual(max(ocr._prepare(Image.new("RGB", (5000, 300)), 2000).size), 2000)

    def test_screenshot_is_doubled_when_it_fits(self):
        self.assertEqual(ocr._prepare(Image.new("RGB", (380, 262)), 10000).size, (760, 524))
        self.assertEqual(ocr._prepare(Image.new("RGB", (3000, 1000)), 4000).size, (4000, 1333))


class FakeStatus:
    def __init__(self):
        self.value = ""

    def set(self, value):
        self.value = value


def fake_app():
    """Just enough of the app for the OCR methods, without a Tk root."""
    app = types.SimpleNamespace(
        events=queue.Queue(),
        status_var=FakeStatus(),
        tray=unittest.mock.Mock(),
        ocr_running=False,
        _set_clipboard=unittest.mock.Mock(),
        _show_ocr_result=unittest.mock.Mock(),
    )
    for name in ("ocr_clipboard_image", "ocr_image", "_ocr_done", "_ocr_report", "_notify"):
        setattr(app, name, types.MethodType(getattr(ShinClipboardApp, name), app))
    return app


class AppOcrTests(unittest.TestCase):
    def test_no_image_only_notifies(self):
        app = fake_app()
        with unittest.mock.patch("shinclipboard.app.read_clipboard_image", return_value=None), \
                unittest.mock.patch("shinclipboard.app.threading.Thread") as thread:
            app.ocr_clipboard_image()
        thread.assert_not_called()
        self.assertFalse(app.ocr_running)
        app.tray.notify.assert_called_once()
        self.assertIn("画像がありません", app.status_var.value)

    def test_result_is_copied_through_the_event_queue(self):
        app = fake_app()
        with unittest.mock.patch("shinclipboard.app.read_clipboard_image", return_value=Image.new("RGB", (50, 50))), \
                unittest.mock.patch("shinclipboard.app.ocr.recognize", return_value="読めた文字"):
            app.ocr_clipboard_image()
            event, payload = app.events.get(timeout=5)
        self.assertEqual(event, "ocr_done")
        app._ocr_done(*payload)
        app._set_clipboard.assert_called_once_with("読めた文字")
        app._show_ocr_result.assert_called_once_with("読めた文字")
        app.tray.notify.assert_not_called()  # the result window says it already
        self.assertFalse(app.ocr_running)

    def test_failure_leaves_the_clipboard_alone(self):
        app = fake_app()
        with unittest.mock.patch("shinclipboard.app.read_clipboard_image", return_value=Image.new("RGB", (50, 50))), \
                unittest.mock.patch("shinclipboard.app.ocr.recognize", side_effect=ocr.OcrError("文字を認識できませんでした")):
            app.ocr_clipboard_image()
            event, payload = app.events.get(timeout=5)
        app._ocr_done(*payload)
        app._set_clipboard.assert_not_called()
        app._show_ocr_result.assert_not_called()
        self.assertEqual(app.status_var.value, "文字を認識できませんでした")

    def test_editor_hears_the_outcome_instead_of_the_tray(self):
        app = fake_app()
        said = []
        with unittest.mock.patch("shinclipboard.app.ocr.recognize", return_value="abc"):
            app.ocr_image(Image.new("RGB", (50, 50)), report=said.append)
            event, payload = app.events.get(timeout=5)
        app._ocr_done(*payload)
        app._set_clipboard.assert_called_once_with("abc")
        self.assertEqual(said[0], "OCR を実行中…")
        self.assertIn("3文字", said[-1])
        app.tray.notify.assert_not_called()

    def test_closed_editor_falls_back_to_the_tray(self):
        app = fake_app()

        def closed(_message):
            raise tk.TclError("invalid command name")

        app._ocr_done(False, "文字を認識できませんでした", closed)
        app.tray.notify.assert_called_once()

    def test_second_request_while_running_is_ignored(self):
        app = fake_app()
        app.ocr_running = True
        with unittest.mock.patch("shinclipboard.app.read_clipboard_image") as read:
            app.ocr_clipboard_image()
        read.assert_not_called()


@unittest.skipUnless(ocr.is_available(), "Windows OCR is unavailable")
class RealOcrTests(unittest.TestCase):
    def test_reads_rendered_text(self):
        image = Image.new("RGB", (640, 120), "white")
        try:
            font = ImageFont.truetype("C:/Windows/Fonts/meiryo.ttc", 40)
        except OSError:
            self.skipTest("Meiryo is not installed")
        ImageDraw.Draw(image).text((10, 30), "Hello OCR 12345", fill="black", font=font)
        self.assertIn("12345", ocr.recognize(image))


if __name__ == "__main__":
    unittest.main()
