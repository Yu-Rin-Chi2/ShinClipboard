import unittest
import unittest.mock

from shinclipboard import theme
from shinclipboard.theme import THEME_LABELS, THEME_NAMES, THEMES, is_dark, theme_colors, windows_dark_mode


class _Key:
    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def _fake_winreg(value=None, error=None):
    fake = unittest.mock.Mock()
    fake.HKEY_CURRENT_USER = object()
    fake.OpenKey.return_value = _Key()
    if error is not None:
        fake.QueryValueEx.side_effect = error
    else:
        fake.QueryValueEx.return_value = (value, 4)
    return fake


class ThemeTests(unittest.TestCase):
    def test_every_theme_has_the_same_colour_names(self):
        keys = set(THEMES["blue"])
        for name, colors in THEMES.items():
            self.assertEqual(set(colors), keys, name)

    def test_only_the_dark_theme_is_dark(self):
        self.assertTrue(is_dark(THEMES["dark"]))
        self.assertFalse(is_dark(THEMES["blue"]))
        self.assertFalse(is_dark(THEMES["green"]))
        self.assertTrue(is_dark(theme_colors("system", dark=True)))

    def test_every_theme_is_offered_under_its_own_label(self):
        self.assertEqual(set(THEME_LABELS), set(THEME_NAMES))
        self.assertEqual(len(set(THEME_LABELS.values())), len(THEME_LABELS))

    def test_windows_dark_mode_reads_the_apps_setting(self):
        with unittest.mock.patch.object(theme, "IS_WINDOWS", True):
            with unittest.mock.patch.dict("sys.modules", {"winreg": _fake_winreg(0)}):
                self.assertTrue(windows_dark_mode())
            with unittest.mock.patch.dict("sys.modules", {"winreg": _fake_winreg(1)}):
                self.assertFalse(windows_dark_mode())
            with unittest.mock.patch.dict("sys.modules", {"winreg": _fake_winreg(error=OSError("no key"))}):
                self.assertFalse(windows_dark_mode(), "an unreadable setting means light")

    def test_windows_dark_mode_is_false_elsewhere(self):
        with unittest.mock.patch.object(theme, "IS_WINDOWS", False):
            self.assertFalse(windows_dark_mode())


if __name__ == "__main__":
    unittest.main()
