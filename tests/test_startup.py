import sys
import unittest
from unittest import mock

from shinclipboard import startup


class StartupCommandTests(unittest.TestCase):
    def test_the_autostart_entry_starts_in_the_background(self):
        self.assertEqual(startup._command()[-1], startup.BACKGROUND_FLAG)

    def test_the_flag_is_understood_by_the_app(self):
        from shinclipboard.app import build_parser

        self.assertTrue(build_parser().parse_args([startup.BACKGROUND_FLAG]).background)
        self.assertFalse(build_parser().parse_args([]).background)


@unittest.skipUnless(sys.platform == "win32", "the registry entry is Windows only")
class RefreshStartupTests(unittest.TestCase):
    """The real registry is never touched: reads and writes are stubbed out."""

    def test_a_run_from_source_leaves_the_entry_alone(self):
        with mock.patch.object(startup, "_registry_value", return_value='"old.exe"'), \
                mock.patch.object(startup, "set_startup") as write:
            self.assertFalse(startup.refresh_startup())
        write.assert_not_called()

    def test_a_stale_entry_is_pointed_at_this_exe(self):
        with mock.patch.object(sys, "frozen", True, create=True), \
                mock.patch.object(startup, "_registry_value", return_value='"C:\\old\\ShinClipboard.exe"'), \
                mock.patch.object(startup, "set_startup") as write:
            self.assertTrue(startup.refresh_startup())
        write.assert_called_once_with(True)

    def test_a_current_entry_is_not_rewritten(self):
        with mock.patch.object(sys, "frozen", True, create=True):
            current = startup._registry_command(startup._command())
            with mock.patch.object(startup, "_registry_value", return_value=current), \
                    mock.patch.object(startup, "set_startup") as write:
                self.assertFalse(startup.refresh_startup())
        write.assert_not_called()

    def test_no_entry_means_autostart_stays_off(self):
        with mock.patch.object(sys, "frozen", True, create=True), \
                mock.patch.object(startup, "_registry_value", return_value=None), \
                mock.patch.object(startup, "set_startup") as write:
            self.assertFalse(startup.refresh_startup())
        write.assert_not_called()


if __name__ == "__main__":
    unittest.main()
