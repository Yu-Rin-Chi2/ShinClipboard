from __future__ import annotations

import tkinter as tk
from tkinter import ttk


class ScrollableFrame(ttk.Frame):
    """A container whose contents scroll when they outgrow the space given.

    Tk has no such widget: a plain frame simply clips what does not fit and
    leaves no way to reach it. Children go into `body`, which is held as wide as
    the visible area so anything packed with `fill="x"` still stretches.
    """

    WHEEL_STEP = 3  # list rows per wheel notch, matching the other lists

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
        self._canvas.bind_all("<MouseWheel>", self._on_wheel)
        self._canvas.bind_all("<Button-4>", lambda _: self._scroll_by(-self.WHEEL_STEP))
        self._canvas.bind_all("<Button-5>", lambda _: self._scroll_by(self.WHEEL_STEP))

    def _unbind_wheel(self) -> None:
        for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self._canvas.unbind_all(sequence)

    def _on_wheel(self, event) -> None:
        # Windows reports the wheel in multiples of 120; macOS in small counts.
        delta = event.delta
        steps = -int(delta / 120) if abs(delta) >= 120 else -delta
        self._scroll_by(steps * self.WHEEL_STEP)

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
        self.bind("<MouseWheel>", lambda event: self._scroll(-int(event.delta / 120) * 3))
        self.bind("<Button-4>", lambda _: self._scroll(-3))
        self.bind("<Button-5>", lambda _: self._scroll(3))

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
