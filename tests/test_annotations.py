import platform
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageFont

from shinclipboard import annotations
from shinclipboard.fonts import FontCatalog, FontFace
from shinclipboard.annotations import (
    AnnotationDocument,
    AnnotationHistory,
    Shape,
    effective_blur_radius,
    effective_pixel_block,
    has_glyphs,
    hit_test,
    render,
    render_raster,
    resolve_font,
    text_line_height,
)


def _base(color: str = "white", size: tuple[int, int] = (200, 120)) -> Image.Image:
    return Image.new("RGB", size, color)


class RenderTests(unittest.TestCase):
    def test_rendering_never_touches_the_source_image(self):
        base = _base()
        before = base.tobytes()
        document = AnnotationDocument([Shape("rect", [(10, 10), (90, 90)], color="#ff0000", filled=True)])
        render(base, document)
        render_raster(base, document)
        self.assertEqual(base.tobytes(), before)
        self.assertEqual(base.mode, "RGB", "the source must keep its own mode")

    def test_filled_rectangle_paints_the_requested_colour(self):
        document = AnnotationDocument([Shape("rect", [(20, 20), (80, 80)], color="#ff0000", filled=True)])
        result = render(_base(), document)
        self.assertEqual(result.getpixel((50, 50))[:3], (255, 0, 0))
        self.assertEqual(result.getpixel((150, 100))[:3], (255, 255, 255), "outside the shape is untouched")

    def test_arrow_and_freehand_draw_something(self):
        for shape in (
            Shape("arrow", [(10, 10), (150, 100)], color="#0000ff", width=6),
            Shape("freehand", [(10, 10), (60, 40), (120, 90)], color="#0000ff", width=6),
        ):
            result = render(_base(), AnnotationDocument([shape]))
            painted = sum(1 for pixel in result.convert("RGB").getdata() if pixel != (255, 255, 255))
            self.assertGreater(painted, 0, shape.kind)

    def test_number_badge_draws_a_disc_with_a_white_digit(self):
        document = AnnotationDocument([Shape("number", [(60, 60)], color="#ff0000", font_size=30, number=7)])
        result = render(_base(), document).convert("RGB")
        self.assertEqual(result.getpixel((60, 35)), (255, 0, 0), "the badge fill surrounds the digit")
        self.assertIn((255, 255, 255), list(result.crop((40, 40, 80, 80)).getdata()), "the digit is drawn in white")

    def test_japanese_text_renders_without_raising(self):
        document = AnnotationDocument([Shape("text", [(10, 10)], text="注釈テキスト\n2行目", color="#000000", font_size=20)])
        result = render(_base(), document).convert("RGB")
        self.assertLess(min(sum(pixel) for pixel in result.getdata()), 3 * 255, "some dark pixels were drawn")

    def test_a_text_is_drawn_in_its_chosen_family_when_the_catalog_has_it(self):
        mincho = Path("/System/Library/Fonts/ヒラギノ明朝 ProN.ttc")
        if not mincho.exists():
            self.skipTest("uses the fonts that ship with macOS")
        catalog = FontCatalog([FontFace("Hiragino Mincho ProN", "W3", str(mincho))])
        with patch.object(annotations, "CATALOG", catalog):
            self.assertEqual(resolve_font(24, "Hiragino Mincho ProN").getname(), ("Hiragino Mincho ProN", "W3"))
            self.assertEqual(resolve_font(24, "Unknown Family").getname(), resolve_font(24).getname(),
                             "a family the catalog lacks falls back to the default face")
            plain = render(_base(), AnnotationDocument([Shape("text", [(10, 10)], text="注釈", font_size=24)]))
            styled = render(_base(), AnnotationDocument([
                Shape("text", [(10, 10)], text="注釈", font_size=24, font="Hiragino Mincho ProN"),
            ]))
            self.assertNotEqual(plain.tobytes(), styled.tobytes())
            self.assertNotEqual(text_line_height(24, "Hiragino Mincho ProN"), None)

    def test_a_family_whose_file_is_gone_falls_back_to_the_default(self):
        catalog = FontCatalog([FontFace("Ghost", "Regular", "/nowhere/ghost.ttf")])
        with patch.object(annotations, "CATALOG", catalog):
            self.assertEqual(resolve_font(24, "Ghost").getname(), resolve_font(24).getname())

    def test_the_line_pitch_is_the_one_a_multi_line_export_uses(self):
        # The preview draws one canvas item per line at this pitch, so it must
        # be exactly where ImageDraw puts the second line of a "\n" text.
        pitch = text_line_height(24)
        joined = AnnotationDocument([Shape("text", [(10, 10)], text="行1\n行2", color="#000000", font_size=24)])
        split = AnnotationDocument([
            Shape("text", [(10, 10)], text="行1", color="#000000", font_size=24),
            Shape("text", [(10, 10 + pitch)], text="行2", color="#000000", font_size=24),
        ])
        self.assertEqual(render(_base(), joined).tobytes(), render(_base(), split).tobytes())

    def test_resolve_font_always_returns_a_font(self):
        self.assertIsNotNone(resolve_font(24))
        self.assertIsNotNone(resolve_font(1), "tiny sizes are clamped, not rejected")

    def test_a_scalable_font_is_only_chosen_when_it_covers_japanese(self):
        font = resolve_font(24)
        if not isinstance(font, ImageFont.FreeTypeFont):
            self.skipTest("no scalable font is installed")
        # "ー" is the character that turned into a box: Pillow could not find
        # Hiragino by its decomposed file name and fell back to a Korean face.
        self.assertTrue(has_glyphs(font, "キーボード"), font.getname())

    @unittest.skipUnless(platform.system() == "Darwin", "uses the fonts that ship with macOS")
    def test_a_face_missing_the_prolonged_sound_mark_is_detected(self):
        path = Path("/System/Library/Fonts/Supplemental/AppleGothic.ttf")
        if not path.exists():
            self.skipTest("AppleGothic is not installed")
        korean = ImageFont.truetype(str(path), 24)
        self.assertTrue(has_glyphs(korean, "あ"), "the probe must not reject glyphs the face does have")
        self.assertFalse(has_glyphs(korean, "ー"))
        self.assertNotEqual(resolve_font(24).getname()[0], "AppleGothic")


class RasterEffectTests(unittest.TestCase):
    def _noisy(self) -> Image.Image:
        image = Image.new("RGB", (120, 120), "white")
        for x in range(0, 120, 2):
            for y in range(0, 120, 2):
                image.putpixel((x, y), (0, 0, 0))
        return image

    def test_blur_changes_the_region_and_leaves_the_rest_alone(self):
        base = self._noisy()
        document = AnnotationDocument([Shape("blur", [(20, 20), (80, 80)], strength=12)])
        result = render(base, document).convert("RGB")
        self.assertNotEqual(result.crop((20, 20, 80, 80)).tobytes(), base.crop((20, 20, 80, 80)).tobytes())
        self.assertEqual(result.crop((90, 90, 120, 120)).tobytes(), base.crop((90, 90, 120, 120)).tobytes())

    def test_pixelate_makes_each_block_uniform(self):
        document = AnnotationDocument([Shape("pixelate", [(0, 0), (120, 120)], strength=12)])
        result = render(self._noisy(), document).convert("RGB")
        block = result.crop((0, 0, 12, 12))
        self.assertEqual(len(set(block.getdata())), 1, "a mosaic block must hold a single colour")

    def test_a_mosaic_still_leaks_the_average_so_it_is_not_a_secrecy_guarantee(self):
        """Guards the wording in the tool hints: only an opaque fill truly hides."""
        shape = Shape("pixelate", [(0, 0), (60, 60)], strength=12)
        dark = render(Image.new("RGB", (60, 60), "black"), AnnotationDocument([shape])).convert("RGB")
        light = render(Image.new("RGB", (60, 60), "white"), AnnotationDocument([shape])).convert("RGB")
        self.assertNotEqual(dark.tobytes(), light.tobytes(), "different content still produces different mosaics")

    def test_an_opaque_filled_rectangle_erases_whatever_was_underneath(self):
        shape = Shape("rect", [(0, 0), (60, 60)], color="#000000", filled=True, width=1)
        dark = render(Image.new("RGB", (60, 60), "black"), AnnotationDocument([shape])).convert("RGB")
        light = render(Image.new("RGB", (60, 60), "white"), AnnotationDocument([shape])).convert("RGB")
        self.assertEqual(dark.tobytes(), light.tobytes(), "the fill leaves no trace of the source")

    def test_a_redaction_does_not_hide_annotations_drawn_on_top_of_it(self):
        """Raster effects run before the vector pass, in the preview and the export alike."""
        text = Shape("text", [(10, 10)], text="秘密", color="#000000", font_size=20)
        blurred = AnnotationDocument([text, Shape("blur", [(0, 0), (120, 120)], strength=20)])
        plain = AnnotationDocument([text])
        self.assertEqual(render(_base(), blurred).tobytes(), render(_base(), plain).tobytes())

    def test_weak_settings_are_raised_to_the_safe_minimum(self):
        self.assertEqual(effective_pixel_block(1), 6)
        self.assertEqual(effective_pixel_block(20), 20)
        self.assertEqual(effective_blur_radius(1, (40, 40)), 5)
        self.assertEqual(effective_blur_radius(1, (400, 800)), 20, "big regions need a proportionally bigger radius")

    def test_highlight_tints_without_hiding_what_is_underneath(self):
        base = Image.new("RGB", (60, 60), "black")
        document = AnnotationDocument([Shape("highlight", [(0, 0), (30, 30)], color="#ffff00")])
        result = render(base, document).convert("RGB")
        self.assertGreater(result.getpixel((10, 10))[0], 0, "the tint lightens the pixels")
        self.assertEqual(result.getpixel((50, 50)), (0, 0, 0))

    def test_effects_outside_the_image_are_skipped(self):
        base = _base()
        document = AnnotationDocument([Shape("blur", [(500, 500), (600, 600)], strength=12)])
        self.assertEqual(render(base, document).convert("RGB").tobytes(), base.tobytes())


class CropTests(unittest.TestCase):
    def test_crop_is_non_destructive_and_reversible(self):
        base = _base(size=(200, 120))
        document = AnnotationDocument(crop=(50, 20, 150, 100))
        self.assertEqual(render(base, document).size, (100, 80))
        document.crop = None
        self.assertEqual(render(base, document).size, (200, 120))

    def test_annotations_are_placed_in_source_coordinates_then_cropped(self):
        document = AnnotationDocument(
            [Shape("rect", [(100, 40), (140, 80)], color="#ff0000", filled=True)],
            crop=(80, 20, 180, 100),
        )
        result = render(_base(), document).convert("RGB")
        self.assertEqual(result.getpixel((40, 40)), (255, 0, 0), "the shape moved with the crop origin")

    def test_an_empty_crop_is_ignored(self):
        base = _base()
        self.assertEqual(render(base, AnnotationDocument(crop=(50, 50, 50, 50))).size, base.size)


class HitTestTests(unittest.TestCase):
    def test_the_topmost_shape_wins(self):
        under = Shape("rect", [(0, 0), (100, 100)])
        over = Shape("rect", [(40, 40), (60, 60)])
        document = AnnotationDocument([under, over])
        self.assertIs(hit_test(document, 50, 50), over)
        self.assertIs(hit_test(document, 10, 10), under)
        self.assertIsNone(hit_test(document, 300, 300))

    def test_the_tolerance_lets_thin_shapes_be_grabbed(self):
        document = AnnotationDocument([Shape("arrow", [(50, 50), (50, 90)])])
        self.assertIsNone(hit_test(document, 62, 70, tolerance=6))
        self.assertIsNotNone(hit_test(document, 54, 70, tolerance=6))


class DocumentTests(unittest.TestCase):
    def test_numbers_keep_counting_up(self):
        document = AnnotationDocument()
        self.assertEqual(document.next_number(), 1)
        document.shapes.append(Shape("number", [(0, 0)], number=1))
        document.shapes.append(Shape("number", [(0, 0)], number=2))
        self.assertEqual(document.next_number(), 3)

    def test_replace_and_remove_work_by_id(self):
        shape = Shape("rect", [(0, 0), (10, 10)])
        document = AnnotationDocument([shape])
        document.replace(shape.moved(5, 5))
        self.assertEqual(len(document.shapes), 1)
        self.assertEqual(document.shapes[0].points, [(5, 5), (15, 15)])
        self.assertEqual(document.shapes[0].id, shape.id, "moving keeps the identity")
        document.remove(shape.id)
        self.assertEqual(document.shapes, [])


class HistoryTests(unittest.TestCase):
    def test_undo_then_redo_round_trips(self):
        history = AnnotationHistory()
        empty = AnnotationDocument()
        history.push(empty)
        drawn = AnnotationDocument([Shape("rect", [(0, 0), (10, 10)])])

        restored = history.undo(drawn)
        self.assertEqual(restored.shapes, [])
        self.assertTrue(history.can_redo())
        self.assertEqual(len(history.redo(restored).shapes), 1)

    def test_a_new_edit_discards_the_redo_stack(self):
        history = AnnotationHistory()
        history.push(AnnotationDocument())
        current = AnnotationDocument([Shape("rect", [(0, 0), (10, 10)])])
        history.undo(current)
        self.assertTrue(history.can_redo())
        history.push(AnnotationDocument())
        self.assertFalse(history.can_redo())

    def test_undo_on_a_fresh_history_returns_none(self):
        self.assertIsNone(AnnotationHistory().undo(AnnotationDocument()))
        self.assertIsNone(AnnotationHistory().redo(AnnotationDocument()))

    def test_snapshots_are_deep_copies(self):
        history = AnnotationHistory()
        shape = Shape("rect", [(0, 0), (10, 10)])
        document = AnnotationDocument([shape])
        history.push(document)
        shape.points = [(99, 99), (100, 100)]
        self.assertEqual(history.undo(document).shapes[0].points, [(0, 0), (10, 10)])

    def test_the_stack_is_capped(self):
        history = AnnotationHistory(limit=3)
        for _ in range(10):
            history.push(AnnotationDocument())
        self.assertEqual(len(history._undo), 3)


if __name__ == "__main__":
    unittest.main()
