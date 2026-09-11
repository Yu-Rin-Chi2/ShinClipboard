from __future__ import annotations

import tkinter as tk
from collections.abc import Callable
from tkinter import messagebox, simpledialog, ttk
from uuid import uuid4

from .platform_support import IS_MAC

WHEEL_STEP = 3  # list rows per wheel notch, matching the other lists
# Characters of the group list, which is what its pane opens at. On Windows it
# is sized to keep the add / rename / delete / up / down row under it on one
# line; on macOS the buttons are too broad for that at any sensible width, so
# the row wraps and the table beside the list gets the space instead.
GROUP_LIST_WIDTH = 24 if IS_MAC else 34


def wheel_units(delta: int) -> int:
    """Rows to scroll for a <MouseWheel> event with the given delta.

    Windows reports the wheel in multiples of 120 per notch; macOS reports small
    counts (often 1 per tick), so dividing by 120 there rounds every event down
    to nothing and the list never moves.
    """
    notches = int(delta / 120) if abs(delta) >= 120 else delta
    return -notches * WHEEL_STEP


class FlowBar(ttk.Frame):
    """A row of controls that wraps onto further rows when the width runs out.

    pack and grid never wrap: a row of buttons wider than its frame simply
    loses its end, and macOS draws every button a third wider than Windows, so
    rows laid out to fit there come up short here. Children are added in order
    with `add()`; those added with `right=True` sit against the right edge, as
    `pack(side="right")` would put them, and drop to a row of their own when
    the left-hand ones leave no room. `together=True` keeps a child on the same
    row as the one before it - a pair of up / down buttons should not split. A
    label added with `wrap=True` is re-wrapped to the width it gets instead of
    being cut off.

    The frame asks for the height its rows need and next to no width, so a
    narrow window wraps the row rather than clipping it.
    """

    GAP = 4  # between neighbours
    ROW_GAP = 4

    def __init__(self, master=None, **kwargs):
        super().__init__(master, **kwargs)
        self._groups: list[list[tuple[tk.Widget, int]]] = []  # runs of (widget, gap before it)
        self._right: list[bool] = []  # per group
        self._wrapped: list[tk.Widget] = []
        self._laid_out: tuple[int, ...] | None = None
        self.bind("<Configure>", lambda event: self._reflow(event.width))

    def add(
        self, widget: tk.Widget, gap: int = 0, right: bool = False, together: bool = False, wrap: bool = False
    ) -> tk.Widget:
        """Append `widget` (a child of this bar). `gap` is extra space before it."""
        if together and self._groups and self._right[-1] == right:
            self._groups[-1].append((widget, gap))
        else:
            self._groups.append([(widget, gap)])
            self._right.append(right)
        if wrap:
            self._wrapped.append(widget)
        self._laid_out = None
        if self.winfo_width() > 1:
            self._reflow(self.winfo_width())
        else:
            # Not laid out yet, so the width is unknown. Ask for one row rather
            # than the tower a 1px-wide layout would be: a tab that is not
            # showing never gets a real width, and its request still sizes the
            # window.
            self.configure(height=max(item.winfo_reqheight() for group in self._groups for item, _ in group), width=1)
        return widget

    def _reflow(self, width: int) -> None:
        if width <= 1:
            return  # not mapped yet
        signature = (width, sum(len(group) for group in self._groups))
        if signature == self._laid_out:
            return
        self._laid_out = signature
        for widget in self._wrapped:
            widget.configure(wraplength=width)
        rows: list[list[tuple[tk.Widget, int]]] = [[]]  # per row: (widget, x)
        x = 0
        for group, right in zip(self._groups, self._right):
            if right:
                continue
            span = sum(gap + item.winfo_reqwidth() for item, gap in group) + self.GAP * (len(group) - 1)
            if rows[-1] and x + span > width:
                rows.append([])
                x = 0
            for index, (item, gap) in enumerate(group):
                x += gap if (index or rows[-1]) else 0
                rows[-1].append((item, x))
                x += item.winfo_reqwidth() + self.GAP
        left_end = x  # where the last left-hand row stops
        right_rows: list[list[tuple[tk.Widget, int]]] = []
        edge = width
        for group, right in zip(self._groups, self._right):
            if not right:
                continue
            span = sum(gap + item.winfo_reqwidth() for item, gap in group) + self.GAP * (len(group) - 1)
            if not right_rows and edge - span >= left_end:
                row = rows[-1]  # shares the last left-hand row
            else:
                if not right_rows or edge - span < 0:
                    right_rows.append([])
                    edge = width
                row = right_rows[-1]
            for item, gap in group:
                edge -= item.winfo_reqwidth() + gap
                row.append((item, edge))
                edge -= self.GAP
        y = 0
        for row in rows + right_rows:
            if not row:
                continue
            height = max(item.winfo_reqheight() for item, _x in row)
            for item, item_x in row:
                item.place(x=item_x, y=y + (height - item.winfo_reqheight()) // 2)
            y += height + self.ROW_GAP
        self.configure(height=max(1, y - self.ROW_GAP), width=1)


def fit_tree_columns(tree: ttk.Treeview) -> None:
    """Keep a table's stretchable columns inside the width the table is given.

    Treeview grows and shrinks its columns with the window - except on the first
    layout, where a table narrower than its columns keeps them as asked and
    clips the last one instead. Once the columns have been fitted by hand the
    widget tracks later resizes on its own.
    """

    def fit(event) -> None:
        shown = [column for column in ("#0", *tree.cget("columns")) if column != "#0" or "tree" in str(tree.cget("show"))]
        widths = {column: int(tree.column(column, "width")) for column in shown}
        available = event.width - 4  # the border
        if sum(widths.values()) <= available:
            return
        stretchable = [column for column in shown if int(tree.column(column, "stretch"))]
        if not stretchable:
            return
        fixed = sum(width for column, width in widths.items() if column not in stretchable)
        share = sum(widths[column] for column in stretchable)
        for column in stretchable:
            wanted = int(widths[column] * max(0, available - fixed) / share)
            tree.column(column, width=max(int(tree.column(column, "minwidth")), wanted))

    tree.bind("<Configure>", fit, add="+")


class GroupPanel(ttk.Frame):
    """The group column of a library tab: a list plus add / rename / delete / reorder.

    `groups` returns the live list of group dicts (each with "id" and "name");
    the panel edits that list in place and then calls `on_change`, which is
    where the owner persists it. `on_select` fires whenever the selection may
    have moved, so the owner can show the chosen group's items.
    """

    def __init__(
        self,
        master,
        groups: Callable[[], list[dict]],
        items_key: str,
        noun: str,
        on_change: Callable[[], None],
        on_select: Callable[[], None],
        font=None,
    ):
        super().__init__(master, padding=(0, 0, 8, 0))
        self._groups = groups
        self._items_key = items_key
        self._noun = noun
        self._on_change = on_change
        self._on_select = on_select
        ttk.Label(self, text="グループ").pack(anchor="w")
        self.listbox = tk.Listbox(self, exportselection=False, font=font, width=GROUP_LIST_WIDTH)
        self.listbox.pack(fill="both", expand=True, pady=6)
        self.listbox.bind("<<ListboxSelect>>", lambda _: self._on_select())
        bar = FlowBar(self)
        bar.pack(fill="x")
        bar.add(ttk.Button(bar, text="追加", command=self.add))
        bar.add(ttk.Button(bar, text="名前変更", command=self.rename))
        bar.add(ttk.Button(bar, text="削除", command=self.delete))
        bar.add(ttk.Button(bar, text="↑", width=3, command=lambda: self.move(-1)), gap=4)
        bar.add(ttk.Button(bar, text="↓", width=3, command=lambda: self.move(1)), together=True)

    def selected_index(self) -> int | None:
        selected = self.listbox.curselection()
        return selected[0] if selected else None

    def selected_group(self) -> dict | None:
        index = self.selected_index()
        groups = self._groups()
        return groups[index] if index is not None and index < len(groups) else None

    def refresh(self, select: int | None = None) -> None:
        current = self.selected_index() if select is None else select
        self.listbox.delete(0, "end")
        groups = self._groups()
        for group in groups:
            self.listbox.insert("end", group["name"])
        if groups:
            self.listbox.selection_set(min(current or 0, len(groups) - 1))
        self._on_select()

    def add(self) -> dict | None:
        name = simpledialog.askstring("グループ追加", "グループ名:", parent=self.winfo_toplevel())
        if not name or not name.strip():
            return None
        group = {"id": str(uuid4()), "name": name.strip(), self._items_key: []}
        self._groups().append(group)
        self._on_change()
        self.refresh(len(self._groups()) - 1)
        return group

    def rename(self) -> None:
        group = self.selected_group()
        if not group:
            return
        name = simpledialog.askstring("グループ名変更", "グループ名:", initialvalue=group["name"], parent=self.winfo_toplevel())
        if name and name.strip():
            group["name"] = name.strip()
            self._on_change()
            self.refresh(self.selected_index())

    def delete(self) -> None:
        index = self.selected_index()
        if index is None:
            return
        if messagebox.askyesno("グループ削除", f"グループと中の{self._noun}を削除しますか？", parent=self.winfo_toplevel()):
            del self._groups()[index]
            self._on_change()
            self.refresh()

    def move(self, offset: int) -> None:
        index = self.selected_index()
        if index is None:
            return
        groups = self._groups()
        target = index + offset
        if not 0 <= target < len(groups):
            return
        groups[index], groups[target] = groups[target], groups[index]
        self._on_change()
        self.refresh(target)


class ScrollableFrame(ttk.Frame):
    """A container whose contents scroll when they outgrow the space given.

    Tk has no such widget: a plain frame simply clips what does not fit and
    leaves no way to reach it. Children go into `body`, which is held as wide as
    the visible area so anything packed with `fill="x"` still stretches.
    """

    def __init__(self, master=None, **kwargs):
        super().__init__(master)
        self._canvas = tk.Canvas(self, highlightthickness=0, borderwidth=0, takefocus=0)
        background = ttk.Style().lookup("TFrame", "background")
        if background:
            self._canvas.configure(background=background)
        self._scrollbar = ttk.Scrollbar(self, orient="vertical", command=self._canvas.yview)
        self._canvas.configure(yscrollcommand=self._scrollbar.set)
        self._canvas.pack(side="left", fill="both", expand=True)
        self._scrollbar.pack(side="right", fill="y")
        self.body = ttk.Frame(self._canvas, **kwargs)
        self._body_id = self._canvas.create_window((0, 0), window=self.body, anchor="nw")
        self.body.bind("<Configure>", self._on_body_configure)
        self._canvas.bind("<Configure>", self._on_canvas_configure)
        # A wheel event goes to the widget under the pointer, which is one of the
        # rows rather than the canvas, so the binding has to be global. It is
        # attached only while the pointer is inside, which keeps it from
        # swallowing the scrolling of every other window in the app.
        self._canvas.bind("<Enter>", lambda _: self._bind_wheel())
        self._canvas.bind("<Leave>", lambda _: self._unbind_wheel())

    def _on_body_configure(self, _event=None) -> None:
        self._canvas.configure(scrollregion=self._canvas.bbox("all"))

    def _on_canvas_configure(self, event) -> None:
        self._canvas.itemconfigure(self._body_id, width=event.width)

    def _bind_wheel(self) -> None:
        self._canvas.bind_all("<MouseWheel>", lambda event: self._scroll_by(wheel_units(event.delta)))
        self._canvas.bind_all("<Button-4>", lambda _: self._scroll_by(-WHEEL_STEP))
        self._canvas.bind_all("<Button-5>", lambda _: self._scroll_by(WHEEL_STEP))

    def _unbind_wheel(self) -> None:
        for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self._canvas.unbind_all(sequence)

    def _scroll_by(self, units: int) -> None:
        if units:
            self._canvas.yview_scroll(units, "units")


class ImageListbox(tk.Text):
    """Read-only, single-selection list whose rows may carry an image.

    It mirrors the subset of the `tk.Listbox` API the app relies on (insert,
    delete, itemconfigure, selection_set / selection_clear / curselection,
    see, nearest, bbox, size) so it can replace a Listbox in place, while
    allowing rows of different heights for thumbnails.
    """

    _ROW_TAG = "row{}"
    _SELECTED_TAG = "selected"

    def __init__(self, master=None, **kwargs):
        kwargs.setdefault("wrap", "none")
        kwargs.setdefault("cursor", "arrow")
        kwargs.setdefault("exportselection", False)
        kwargs.setdefault("insertwidth", 0)
        kwargs.setdefault("spacing1", 1)
        kwargs.setdefault("spacing3", 1)
        # A Text on macOS draws a 3px focus ring in the text colour, and this
        # list holds the focus whenever the popup is open: in dark mode that is
        # a white frame around it. A list gets the Listbox's thin border instead.
        kwargs.setdefault("highlightthickness", 0)
        if IS_MAC:
            kwargs.setdefault("relief", "solid")
            kwargs.setdefault("borderwidth", 1)
        super().__init__(master, **kwargs)
        self._rows: list[dict[str, object]] = []
        self._selected: int | None = None
        # Drop the Text class bindings so there is no caret, no text selection
        # and no editing; only the toplevel bindings (quick keys, Tab...) and
        # the handlers below remain.
        self.bindtags((str(self), str(self.winfo_toplevel()), "all"))
        self.configure(state="disabled")
        self.bind("<Button-1>", self._on_click)
        self.bind("<Up>", lambda _: self._move(-1))
        self.bind("<Down>", lambda _: self._move(1))
        self.bind("<Prior>", lambda _: self._move(-self._visible_rows()))
        self.bind("<Next>", lambda _: self._move(self._visible_rows()))
        self.bind("<Home>", lambda _: self._select_and_see(0))
        self.bind("<End>", lambda _: self._select_and_see(len(self._rows) - 1))
        self.bind("<MouseWheel>", lambda event: self._scroll(wheel_units(event.delta)))
        self.bind("<Button-4>", lambda _: self._scroll(-WHEEL_STEP))
        self.bind("<Button-5>", lambda _: self._scroll(WHEEL_STEP))

    # ----- Listbox-compatible API -------------------------------------------------

    def insert(self, index, text: str, image=None, suffix: str = "") -> None:  # type: ignore[override]
        """Append a row: `text`, then the optional `image`, then `suffix`."""
        if index != "end":
            raise ValueError("ImageListbox only supports appending rows at 'end'.")
        row = len(self._rows)
        self._rows.append({
            "text": text,
            "image": image,
            "background": "",
            "foreground": "",
            "selectbackground": "",
            "selectforeground": "",
        })
        tag = self._ROW_TAG.format(row)

        def append() -> None:
            tk.Text.insert(self, "end", text, (tag,))
            if image is not None:
                self.image_create("end", image=image, padx=4, pady=2, align="center")
                self.tag_add(tag, "end-2c", "end-1c")
            tk.Text.insert(self, "end", suffix + "\n", (tag,))

        self._edit(append)

    def delete(self, first, last=None) -> None:  # type: ignore[override]
        if first != 0 or last != "end":
            raise ValueError("ImageListbox only supports delete(0, 'end').")
        self._rows.clear()
        self._selected = None
        self._edit(lambda: tk.Text.delete(self, "1.0", "end"))

    def itemconfigure(self, index: int, **options) -> None:
        row = self._rows[index]
        for key in ("background", "foreground", "selectbackground", "selectforeground"):
            if key in options:
                row[key] = options[key]
        self.tag_configure(self._ROW_TAG.format(index), background=row["background"], foreground=row["foreground"])
        if self._selected == index:
            self._paint_selection()

    itemconfig = itemconfigure

    def size(self) -> int:  # type: ignore[override]
        return len(self._rows)

    def curselection(self) -> tuple[int, ...]:
        return (self._selected,) if self._selected is not None else ()

    def selection_set(self, index: int) -> None:
        if not 0 <= index < len(self._rows):
            return
        self._selected = index
        self._paint_selection()

    def selection_clear(self, first=None, last=None) -> None:  # type: ignore[override]
        self._selected = None
        self.tag_remove(self._SELECTED_TAG, "1.0", "end")

    def see(self, index) -> None:  # type: ignore[override]
        if isinstance(index, int):
            if 0 <= index < len(self._rows):
                tk.Text.see(self, f"{index + 1}.0")
        else:
            tk.Text.see(self, index)

    def nearest(self, y: int) -> int:
        if not self._rows:
            return -1
        line = int(tk.Text.index(self, f"@0,{y}").split(".")[0]) - 1
        return max(0, min(line, len(self._rows) - 1))

    def bbox(self, index) -> tuple[int, int, int, int] | None:  # type: ignore[override]
        if not isinstance(index, int) or not 0 <= index < len(self._rows):
            return None
        info = self.dlineinfo(f"{index + 1}.0")
        return (info[0], info[1], info[2], info[3]) if info else None

    def row_has_image(self, index: int) -> bool:
        return self._rows[index]["image"] is not None

    # ----- internals --------------------------------------------------------------

    def _edit(self, action) -> None:
        self.configure(state="normal")
        try:
            action()
        finally:
            self.configure(state="disabled")

    def _paint_selection(self) -> None:
        self.tag_remove(self._SELECTED_TAG, "1.0", "end")
        if self._selected is None:
            return
        row = self._rows[self._selected]
        self.tag_configure(
            self._SELECTED_TAG,
            background=row["selectbackground"] or self.cget("selectbackground"),
            foreground=row["selectforeground"] or self.cget("selectforeground"),
        )
        self.tag_add(self._SELECTED_TAG, f"{self._selected + 1}.0", f"{self._selected + 2}.0")
        self.tag_raise(self._SELECTED_TAG)

    def _select_and_see(self, index: int) -> str:
        if self._rows:
            index = max(0, min(index, len(self._rows) - 1))
            self.selection_clear()
            self.selection_set(index)
            self.see(index)
        return "break"

    def _move(self, offset: int) -> str:
        current = self._selected if self._selected is not None else 0
        return self._select_and_see(current + offset)

    def _visible_rows(self) -> int:
        visible = sum(1 for index in range(len(self._rows)) if self.dlineinfo(f"{index + 1}.0"))
        return max(1, visible - 1)

    def _scroll(self, units: int) -> str:
        self.yview_scroll(units, "units")
        return "break"

    def _on_click(self, event) -> None:
        self.focus_set()
        if not self._rows:
            return
        index = self.nearest(event.y)
        bounds = self.bbox(index)
        if bounds and bounds[1] <= event.y <= bounds[1] + bounds[3]:
            self.selection_clear()
            self.selection_set(index)
