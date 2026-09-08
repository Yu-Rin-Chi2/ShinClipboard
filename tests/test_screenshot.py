import os
import unittest

from PIL import Image

from shinclipboard.screenshot import (
    ScreenCapture,
    capture_virtual_screen,
    dpi_aware_windows,
    normalize_rect,
    rect_under_point,
    virtual_bounds,
)


class RectTests(unittest.TestCase):
    def test_a_rectangle_dragged_backwards_is_ordered(self):
        self.assertEqual(normalize_rect(100, 80, 20, 10), (20, 10, 100, 80))
        self.assertEqual(normalize_rect(20, 10, 100, 80), (20, 10, 100, 80))

    def test_fractional_coordinates_are_rounded(self):
        self.assertEqual(normalize_rect(10.4, 10.6, 20.7, 20.4), (10, 11, 21, 20))

    def test_a_zero_size_drag_stays_empty(self):
        self.assertEqual(normalize_rect(30, 30, 30, 30), (30, 30, 30, 30))

    def test_the_front_most_rectangle_under_the_point_wins(self):
        rects = [(40, 40, 100, 100), (0, 0, 200, 200)]
        self.assertEqual(rect_under_point(rects, 50, 50), (40, 40, 100, 100))
        self.assertEqual(rect_under_point(rects, 10, 10), (0, 0, 200, 200))
        self.assertIsNone(rect_under_point(rects, 500, 500))

    def test_the_right_and_bottom_edges_are_exclusive(self):
        self.assertIsNone(rect_under_point([(0, 0, 10, 10)], 10, 5))
        self.assertIsNotNone(rect_under_point([(0, 0, 10, 10)], 0, 0))


class ScreenCaptureTests(unittest.TestCase):
    def _capture(self, scale: float) -> ScreenCapture:
        # A 400x300 desktop starting at (-100, -50), captured at `scale`.
        image = Image.new("RGB", (int(400 * scale), int(300 * scale)), "white")
        return ScreenCapture(image=image, origin=(-100, -50), scale=scale)

    def test_screen_coordinates_are_shifted_by_the_origin(self):
        capture = self._capture(1.0)
        self.assertEqual(capture.crop_screen((-100, -50, -60, -20)).size, (40, 30))
        self.assertEqual(capture.crop_screen((0, 0, 100, 100)).size, (100, 100))

    def test_a_scaled_capture_maps_onto_the_bigger_picture(self):
        capture = self._capture(1.5)
        self.assertEqual(capture.image.size, (600, 450))
        self.assertEqual(capture.crop_screen((-100, -50, -60, -20)).size, (60, 45))

    def test_the_crop_is_clamped_to_the_picture(self):
        capture = self._capture(1.0)
        self.assertEqual(capture.crop_screen((-500, -500, 5000, 5000)).size, (400, 300))

    def test_a_backwards_selection_still_crops(self):
        capture = self._capture(1.0)
        self.assertEqual(capture.crop_screen((0, 0, -100, -50)).size, (100, 50))

    def test_an_empty_selection_falls_back_to_the_whole_picture(self):
        capture = self._capture(1.0)
        self.assertEqual(capture.crop_screen((10, 10, 10, 10)).size, (400, 300))

    def test_bounds_describe_the_screen_not_the_pixels(self):
        self.assertEqual(self._capture(1.5).bounds, (-100, -50, 300, 250))


class PlatformTests(unittest.TestCase):
    def test_the_dpi_context_manager_never_raises(self):
        with dpi_aware_windows() as aware:
            self.assertIsInstance(aware, bool)
            if os.name != "nt":
                self.assertFalse(aware)

    @unittest.skipUnless(os.name == "nt", "virtual screen metrics are Windows only")
    def test_dpi_awareness_only_grows_the_reported_desktop(self):
        virtualised = virtual_bounds()
        with dpi_aware_windows() as aware:
            physical = virtual_bounds()
        if not aware:
            self.skipTest("this Windows build has no SetThreadDpiAwarenessContext")
        self.assertGreaterEqual(physical[2] - physical[0], virtualised[2] - virtualised[0])
        self.assertGreaterEqual(physical[3] - physical[1], virtualised[3] - virtualised[1])

    @unittest.skipUnless(os.name == "nt", "grabbing every screen is Windows only")
    def test_a_real_capture_covers_the_whole_desktop(self):
        try:
            with dpi_aware_windows():
                capture = capture_virtual_screen()
        except OSError as error:
            # No desktop to read: a locked session, the secure desktop, or a
            # headless runner. That is the environment, not a defect.
            self.skipTest(f"the screen cannot be grabbed here: {error}")
        self.assertGreater(capture.image.width, 0)
        left, top, right, bottom = capture.bounds
        self.assertEqual(capture.image.size, (right - left, bottom - top))
        # Inside the aware block the metrics are physical, so no scaling is needed.
        self.assertAlmostEqual(capture.scale, 1.0, places=2)


if __name__ == "__main__":
    unittest.main()
