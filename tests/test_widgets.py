import tkinter as tk
import unittest
from tkinter import ttk

from shinclipboard.widgets import FlowBar, fit_tree_columns


def _root():
    try:
        root = tk.Tk()
    except tk.TclError as error:
        raise unittest.SkipTest(f"Tk is unavailable: {error}")
    root.geometry("+3000+3000")  # off screen, but mapped so widgets get real sizes
    return root


def _destroy(root) -> None:
    try:
        for timer in root.tk.splitlist(root.tk.call("after", "info")):
            root.tk.call("after", "cancel", timer)
        root.destroy()
    except tk.TclError:
        pass


def _position(widget) -> tuple[int, int]:
    info = widget.place_info()
    return int(info["x"]), int(info["y"])


class FlowBarTests(unittest.TestCase):
    """The bar is sized by the window; buttons are given fixed widths so the maths is stable."""

    def _bar(self, root, width: int) -> tuple[FlowBar, dict[str, tk.Widget]]:
        root.geometry(f"{width}x200")
        bar = FlowBar(root)
        bar.pack(fill="x")
        items = {}
        for name in ("a", "b", "c"):
            items[name] = bar.add(ttk.Label(bar, text=name, width=10))  # about 70px each on macOS
        items["up"] = bar.add(ttk.Label(bar, text="↑", width=3), gap=4)
        items["down"] = bar.add(ttk.Label(bar, text="↓", width=3), together=True)
        items["paste"] = bar.add(ttk.Label(bar, text="paste", width=8), right=True)
        items["hint"] = bar.add(ttk.Label(bar, text="hint " * 30), right=True, wrap=True)
        root.update()
        return bar, items

    def test_everything_fits_on_one_row_when_there_is_room(self):
        root = _root()
        try:
            bar, items = self._bar(root, 1400)
            rows = {name: _position(widget)[1] for name, widget in items.items()}
            self.assertEqual(len(set(rows.values())), 1, rows)
            xs = [_position(items[name])[0] for name in ("a", "b", "c", "up", "down")]
            self.assertEqual(xs, sorted(xs), "left-hand items keep their order")
            self.assertGreater(_position(items["up"])[0] - (_position(items["c"])[0] + items["c"].winfo_reqwidth()), FlowBar.GAP, "the gap before ↑ is honoured")
            paste_x = _position(items["paste"])[0]
            self.assertEqual(paste_x + items["paste"].winfo_reqwidth(), bar.winfo_width(), "the first right-hand item hugs the edge")
            self.assertLess(_position(items["hint"])[0], paste_x, "later right-hand items line up to its left")
            self.assertEqual(bar.winfo_reqheight(), max(w.winfo_reqheight() for w in items.values()))
        finally:
            _destroy(root)

    def test_rows_wrap_and_the_up_down_pair_stays_together(self):
        root = _root()
        try:
            bar, items = self._bar(root, 300)
            rows = {name: _position(widget)[1] for name, widget in items.items()}
            self.assertGreater(len(set(rows.values())), 1, "a 300px bar cannot hold everything on one row")
            self.assertEqual(rows["up"], rows["down"], "↑ and ↓ never split across rows")
            self.assertLessEqual(
                max(_position(w)[0] + w.winfo_reqwidth() for w in items.values()), bar.winfo_width(),
                "nothing sticks out to the right",
            )
            self.assertEqual(int(items["hint"].cget("wraplength")), bar.winfo_width(), "the hint wraps to the bar")
            self.assertGreaterEqual(bar.winfo_reqheight(), 2 * items["a"].winfo_reqheight(), "the bar asks for its rows")
            bottom = max(_position(w)[1] + w.winfo_reqheight() for w in items.values())
            self.assertEqual(bar.winfo_reqheight(), bottom, "and no more")
        finally:
            _destroy(root)

    def test_widening_the_window_pulls_the_rows_back_together(self):
        root = _root()
        try:
            bar, items = self._bar(root, 300)
            root.geometry("1400x200")
            root.update()
            rows = {_position(widget)[1] for widget in items.values()}
            self.assertEqual(len(rows), 1)
        finally:
            _destroy(root)

    def test_an_unmapped_bar_asks_for_a_single_row(self):
        # A bar on a tab that is not showing never gets a width; if it laid
        # itself out at 1px it would ask for a tower and stretch the window.
        root = _root()
        try:
            hidden = ttk.Frame(root)  # never packed
            bar = FlowBar(hidden)
            for index in range(8):
                bar.add(ttk.Label(bar, text=f"button {index}", width=12))
            bar.add(ttk.Label(bar, text="hint " * 40), right=True, wrap=True)
            root.update()
            self.assertEqual(bar.winfo_reqheight(), max(w.winfo_reqheight() for w in bar.winfo_children()))
        finally:
            _destroy(root)


class FitTreeColumnsTests(unittest.TestCase):
    def test_columns_are_shrunk_to_the_table_on_the_first_layout(self):
        root = _root()
        try:
            root.geometry("300x150")
            tree = ttk.Treeview(root, columns=("a", "b", "c"), show="headings")
            for column in "abc":
                tree.column(column, width=200)
            tree.pack(fill="both", expand=True)
            fit_tree_columns(tree)
            root.update()
            widths = [int(tree.column(column, "width")) for column in "abc"]
            self.assertLessEqual(sum(widths), tree.winfo_width(), widths)
            self.assertTrue(all(width >= 20 for width in widths), "never below the minimum width")
        finally:
            _destroy(root)

    def test_a_table_with_room_to_spare_is_left_alone(self):
        root = _root()
        try:
            root.geometry("900x150")
            tree = ttk.Treeview(root, columns=("a", "b"), show="headings")
            for column in "ab":
                tree.column(column, width=100)
            tree.pack(fill="both", expand=True)
            fit_tree_columns(tree)
            root.update()
            self.assertTrue(all(int(tree.column(column, "width")) >= 100 for column in "ab"))
        finally:
            _destroy(root)


if __name__ == "__main__":
    unittest.main()
