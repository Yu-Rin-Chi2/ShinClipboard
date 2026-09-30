import queue
import unittest
from unittest.mock import Mock, patch

from shinclipboard.app import ShinClipboardApp
from shinclipboard.core import FifoQueue


class StockModeTests(unittest.TestCase):
    """The same key starts and stops a mode, so each switch must be visible."""

    def _app(self, values=()):
        app = ShinClipboardApp.__new__(ShinClipboardApp)
        app.stock_mode = "off"
        app.fifo_enabled = False
        app.last_fifo_value = None
        app._stock_empty_notified = False
        app.fifo = FifoQueue(values)
        app.events = queue.Queue()
        app._refresh_fifo = Mock()
        app._notify = Mock()
        return app

    def test_toggling_announces_start_and_stop(self):
        app = self._app(["a", "b"])
        app.toggle_fifo()
        self.assertEqual(app.stock_mode, "fifo")
        self.assertIn("FIFOモード開始（2件）", app._notify.call_args[0][0])
        app.toggle_fifo()
        self.assertEqual(app.stock_mode, "off")
        app._notify.assert_called_with("ストックモードを停止しました")

    def test_the_other_key_switches_modes_directly(self):
        app = self._app()
        app.toggle_fifo()
        app.toggle_lifo()
        self.assertEqual(app.stock_mode, "lifo")
        self.assertIn("LIFOモード開始", app._notify.call_args[0][0])

    def test_an_empty_stock_is_reported_once(self):
        app = self._app(["a"])
        app.toggle_fifo()
        with patch("shinclipboard.app.pyperclip.copy") as copy:
            app._fifo_paste_hotkey()
            app._fifo_paste_hotkey()
            app._fifo_paste_hotkey()
        copy.assert_called_once_with("a")
        events = []
        while not app.events.empty():
            events.append(app.events.get_nowait()[0])
        self.assertEqual(events.count("stock_empty"), 1)

    def test_ctrl_v_is_left_alone_when_the_mode_is_off(self):
        app = self._app(["a"])
        with patch("shinclipboard.app.pyperclip.copy") as copy:
            app._fifo_paste_hotkey()
        copy.assert_not_called()
        self.assertTrue(app.events.empty())


if __name__ == "__main__":
    unittest.main()
