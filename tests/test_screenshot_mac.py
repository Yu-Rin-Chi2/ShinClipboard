import platform
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from shinclipboard import screenshot_mac


def _bounds(x: float, y: float, width: float, height: float) -> SimpleNamespace:
    """Stand in for the CGRect that Quartz hands back for a display."""
    return SimpleNamespace(origin=SimpleNamespace(x=x, y=y), size=SimpleNamespace(width=width, height=height))


def _window(owner_pid: int, layer: int, rect: tuple[int, int, int, int], alpha: float = 1.0) -> dict:
    left, top, width, height = rect
    return {
        "kCGWindowOwnerPID": owner_pid,
        "kCGWindowLayer": layer,
        "kCGWindowAlpha": alpha,
        "kCGWindowBounds": {"X": left, "Y": top, "Width": width, "Height": height},
    }


class FakeQuartz:
    """The handful of Quartz calls the screenshot backend makes."""

    kCGWindowListOptionOnScreenOnly = 1
    kCGWindowListExcludeDesktopElements = 16
    kCGNullWindowID = 0

    kCGBitmapByteOrderMask = 0x7000
    kCGBitmapByteOrder32Little = 0x2000
    kCGBitmapByteOrder32Big = 0x4000
    kCGBitmapAlphaInfoMask = 0x1F
    kCGImageAlphaPremultipliedFirst = 2
    kCGImageAlphaFirst = 4
    kCGImageAlphaNoneSkipFirst = 6

    def __init__(self, displays=(), windows=()):
        self._displays = list(displays)
        self._windows = list(windows)

    def CGGetActiveDisplayList(self, maximum, _ids, _count):
        identifiers = tuple(range(len(self._displays)))[:maximum]
        return (0, identifiers, len(identifiers))

    def CGDisplayBounds(self, display):
        return self._displays[display]

    def CGWindowListCopyWindowInfo(self, _options, _window_id):
        return self._windows


class MonitorTests(unittest.TestCase):
    def test_each_active_display_becomes_a_rectangle(self):
        quartz = FakeQuartz(displays=[_bounds(0, 0, 1680, 1050), _bounds(1680, -200, 1920, 1080)])
        with patch.object(screenshot_mac, "_quartz", return_value=quartz):
            self.assertEqual(
                screenshot_mac.monitor_rects(),
                [(0, 0, 1680, 1050), (1680, -200, 3600, 880)],
            )

    def test_the_virtual_bounds_cover_every_display(self):
        quartz = FakeQuartz(displays=[_bounds(0, 0, 1680, 1050), _bounds(-1920, -200, 1920, 1080)])
        with patch.object(screenshot_mac, "_quartz", return_value=quartz):
            self.assertEqual(screenshot_mac.virtual_bounds(), (-1920, -200, 1680, 1050))

    def test_a_display_list_failure_reports_no_monitors(self):
        quartz = FakeQuartz(displays=[_bounds(0, 0, 800, 600)])
        quartz.CGGetActiveDisplayList = lambda *_args: (1, (), 0)  # a non-zero CGError
        with patch.object(screenshot_mac, "_quartz", return_value=quartz):
            self.assertEqual(screenshot_mac.monitor_rects(), [])
            self.assertEqual(screenshot_mac.virtual_bounds(), (0, 0, 0, 0))

    def test_everything_is_empty_without_pyobjc(self):
        with patch.object(screenshot_mac, "_quartz", return_value=None):
            self.assertEqual(screenshot_mac.monitor_rects(), [])
            self.assertEqual(screenshot_mac.window_rects(), [])
            self.assertEqual(screenshot_mac.virtual_bounds(), (0, 0, 0, 0))


class WindowTests(unittest.TestCase):
    def _rects(self, windows, own_pid=4242):
        quartz = FakeQuartz(windows=windows)
        with patch.object(screenshot_mac, "_quartz", return_value=quartz):
            with patch("os.getpid", return_value=own_pid):
                return screenshot_mac.window_rects()

    def test_ordinary_windows_keep_the_order_the_window_server_gave(self):
        windows = [
            _window(1, screenshot_mac.NORMAL_WINDOW_LAYER, (100, 50, 400, 300)),
            _window(2, screenshot_mac.NORMAL_WINDOW_LAYER, (0, 0, 1680, 1050)),
        ]
        self.assertEqual(self._rects(windows), [(100, 50, 500, 350), (0, 0, 1680, 1050)])

    def test_menu_bar_and_dock_windows_are_not_offered_as_targets(self):
        windows = [
            _window(1, 25, (1451, 0, 30, 24)),  # a menu bar extra
            _window(2, screenshot_mac.NORMAL_WINDOW_LAYER, (10, 10, 200, 200)),
        ]
        self.assertEqual(self._rects(windows), [(10, 10, 210, 210)])

    def test_our_own_windows_are_skipped(self):
        windows = [
            _window(4242, screenshot_mac.NORMAL_WINDOW_LAYER, (0, 0, 500, 500)),
            _window(9, screenshot_mac.NORMAL_WINDOW_LAYER, (20, 20, 100, 100)),
        ]
        self.assertEqual(self._rects(windows), [(20, 20, 120, 120)])

    def test_invisible_and_sliver_windows_are_skipped(self):
        windows = [
            _window(1, screenshot_mac.NORMAL_WINDOW_LAYER, (0, 0, 300, 300), alpha=0.0),
            _window(2, screenshot_mac.NORMAL_WINDOW_LAYER, (5, 5, 4, 400)),
            _window(3, screenshot_mac.NORMAL_WINDOW_LAYER, (5, 5, 400, 4)),
            _window(4, screenshot_mac.NORMAL_WINDOW_LAYER, (7, 8, 60, 40)),
        ]
        self.assertEqual(self._rects(windows), [(7, 8, 67, 48)])

    def test_a_window_without_bounds_is_skipped(self):
        broken = _window(1, screenshot_mac.NORMAL_WINDOW_LAYER, (0, 0, 100, 100))
        del broken["kCGWindowBounds"]
        self.assertEqual(self._rects([broken]), [])


class RawModeTests(unittest.TestCase):
    """Getting the byte order wrong swaps red and blue in every screenshot."""

    def _mode(self, bitmap_info: int) -> str | None:
        quartz = FakeQuartz()
        image = object()
        quartz.CGImageGetBitmapInfo = lambda _image: bitmap_info
        return screenshot_mac._raw_mode(quartz, image)

    def test_little_endian_alpha_first_is_stored_blue_first(self):
        info = FakeQuartz.kCGBitmapByteOrder32Little | FakeQuartz.kCGImageAlphaNoneSkipFirst
        self.assertEqual(self._mode(info), "BGRX")

    def test_little_endian_alpha_last_reverses_to_alpha_first(self):
        self.assertEqual(self._mode(FakeQuartz.kCGBitmapByteOrder32Little), "XBGR")

    def test_big_endian_keeps_the_declared_order(self):
        info = FakeQuartz.kCGBitmapByteOrder32Big | FakeQuartz.kCGImageAlphaNoneSkipFirst
        self.assertEqual(self._mode(info), "XRGB")
        self.assertEqual(self._mode(FakeQuartz.kCGBitmapByteOrder32Big), "RGBX")

    def test_an_unknown_byte_order_is_refused_rather_than_guessed(self):
        self.assertIsNone(self._mode(FakeQuartz.kCGBitmapByteOrderMask))


@unittest.skipUnless(platform.system() == "Darwin", "the Quartz backend only runs on macOS")
class LiveQuartzTests(unittest.TestCase):
    def test_the_real_desktop_is_described_and_can_be_grabbed(self):
        monitors = screenshot_mac.monitor_rects()
        if not monitors:
            self.skipTest("no active display, so there is nothing to describe")
        bounds = screenshot_mac.virtual_bounds()
        self.assertLessEqual(bounds[0], min(rect[0] for rect in monitors))
        self.assertGreaterEqual(bounds[2], max(rect[2] for rect in monitors))

        image = screenshot_mac.grab_screen()
        if image is None:
            self.skipTest("the screen cannot be read here, most likely without permission")
        # The grab covers the whole desktop at one scale, whole-numbered on every
        # display Apple ships (1x or 2x).
        self.assertEqual(image.width * (bounds[3] - bounds[1]), image.height * (bounds[2] - bounds[0]))
        self.assertEqual(image.width % (bounds[2] - bounds[0]), 0)


if __name__ == "__main__":
    unittest.main()
