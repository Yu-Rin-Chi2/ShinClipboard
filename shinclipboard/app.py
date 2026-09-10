from __future__ import annotations

import argparse
import json
import platform
import queue
import re
import sys
import threading
import time
import tkinter as tk
from dataclasses import dataclass, field
from pathlib import Path
from tkinter import colorchooser, filedialog, messagebox, simpledialog, ttk
from typing import Callable
from uuid import uuid4

import pyperclip
from PIL import Image, ImageTk

from .capture_overlay import RegionSelector
from .clipboard_images import clipboard_change_token, read_clipboard_image, write_clipboard_image
from .colors import normalize_hex_color, parse_hex_color, swatch_image
from .core import FifoQueue, HistoryItem
from .editor import ImageEditorWindow, cleanup_exports, write_export
from .hotkeys import GlobalHotkeyService
from .macos import (
    ACCESSIBILITY_HINT,
    ACCESSIBILITY_PANE,
    SCREEN_RECORDING_HINT,
    SCREEN_RECORDING_PANE,
    accessibility_trusted,
    activate_app,
    activate_running_app,
    frontmost_app,
    hide_app,
    send_window_to_back,
    menu_bar_height,
    open_settings_pane,
    request_accessibility,
    request_screen_recording,
    screen_recording_allowed,
    send_paste_keystroke,
)
from .platform_support import IS_MAC, UI_FONT_FAMILY
from .single_instance import SingleInstance
from .startup import migrate_legacy_startup, set_startup, startup_enabled
from .storage import JsonStore, default_data_dir, library_image_paths, migrate_legacy_data_dir
from .transfer import create_backup, export_snippets_csv, import_snippets_csv, restore_backup
from .transforms import apply_enabled, apply_transform
from .widgets import GroupPanel, ImageListbox, ScrollableFrame


THEMES = {
    "blue": {"background": "#ffffff", "stripe": "#e4eaf2", "accent": "#075dcc", "foreground": "#172033"},
    "dark": {"background": "#1f2937", "stripe": "#354154", "accent": "#0284c7", "foreground": "#f8fafc"},
    "green": {"background": "#ffffff", "stripe": "#e1eee8", "accent": "#047857", "foreground": "#15332b"},
}


def resource_path(relative: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    return base / relative


QUICK_KEYS = "1234567890abcdefghijklmnopqrstuvwxyz"
CALL_WINDOW_HINT = (
    "Tab: 履歴/定型文/色/画像   ←→: グループ   1〜0/a〜z/Enter: 貼り付け   "
    "Ctrl+E: 画像を編集   ドラッグ: 他アプリへ   Esc: 閉じる"
)
THUMBNAIL_SIZE = (200, 72)  # max width / height of image previews in the popup
TREE_THUMBNAIL_SIZE = (96, 52)  # previews in the image tab's table
TREE_THUMBNAIL_ROW = 60


def quick_key(index: int) -> str:
    return QUICK_KEYS[index] if 0 <= index < len(QUICK_KEYS) else ""


@dataclass
class CallPage:
    """One grouped tab of the popup: definitions, colours or images.

    Each shows the items of one group at a time; `group_id` remembers which,
    `items` holds what the rows currently stand for, so a row index maps
    straight back to its dict.
    """

    groups_key: str  # config key holding the groups
    items_key: str  # key of the item list inside a group
    combo: ttk.Combobox
    var: tk.StringVar
    listbox: tk.Listbox | ImageListbox
    render: Callable[[str, dict], None]  # (quick-key prefix, item) -> appends a row
    group_id: str | None = None
    items: list[dict] = field(default_factory=list)


def shortcut_modifier_mask() -> int:
    """Tk `event.state` bits that mean a shortcut modifier is held.

    Lock-style keys are deliberately excluded: on Windows Tk reports NumLock as
    Mod1 (0x8), which must not disable quick selection.
    """
    system = platform.system()
    if system == "Windows":
        return 0x4 | 0x20000  # Control, Alt
    if system == "Darwin":
        return 0x4 | 0x8 | 0x10  # Control, Command, Option
    return 0x4 | 0x8  # Control, Alt (Mod1)


class ShinClipboardApp:
    POLL_MS = 350
    # How often to look at the macOS permission again. Rare enough that the
    # lookup never shows in the poll loop, often enough that granting it feels
    # like it took effect straight away.
    PERMISSION_CHECK_SECONDS = 2.0

    def __init__(self, root: tk.Tk, store: JsonStore):
        self.root = root
        self.store = store
        self.config = store.load_config()
        limit = int(self.config["settings"].get("history_limit", 1000))
        self.history = store.load_history(limit)
        self.fifo = FifoQueue()
        self.stock_mode = "off"
        self.fifo_enabled = False
        self.monitor_enabled = True
        self.last_fifo_value: str | None = None
        self.last_clipboard = self._read_clipboard()
        self.last_clipboard_token = clipboard_change_token()
        self.ignore_fifo_text: str | None = None
        self.events: queue.Queue[tuple[str, object | None]] = queue.Queue()
        self.hotkeys: GlobalHotkeyService | None = None
        self.tray = None
        self.closing = False
        self.editors: set[ImageEditorWindow] = set()
        self.capturing = False
        self._selector: RegionSelector | None = None
        self._countdown_window: tk.Toplevel | None = None
        self._countdown_job: str | None = None
        self._countdown_left = 0
        self._hidden_for_capture: list[tk.Misc] = []
        self._thumbnails: dict[str, ImageTk.PhotoImage] = {}
        self._tree_thumbnails: dict[str, ImageTk.PhotoImage] = {}
        self._swatches: dict[tuple[int, int, int, int], ImageTk.PhotoImage] = {}
        self.call_pages: dict[int, CallPage] = {}  # popup tab index -> page (the history tab has none)

        self.search_var = tk.StringVar()
        self.snippet_search_var = tk.StringVar()
        self.status_var = tk.StringVar(value="準備完了")
        self.fifo_status_var = tk.StringVar()
        self.group_var = tk.StringVar()
        self.popup_hotkey_var = tk.StringVar()
        self.fifo_hotkey_var = tk.StringVar()
        self.lifo_hotkey_var = tk.StringVar()
        self.monitor_hotkey_var = tk.StringVar()
        self.history_limit_var = tk.StringVar()
        self.font_size_var = tk.StringVar()
        self.theme_var = tk.StringVar()
        self.auto_paste_var = tk.BooleanVar()
        self.save_history_var = tk.BooleanVar()
        self.clear_history_var = tk.BooleanVar()
        self.always_on_top_var = tk.BooleanVar()
        self.start_minimized_var = tk.BooleanVar()
        self.startup_var = tk.BooleanVar(value=startup_enabled())
        self.double_ctrl_var = tk.BooleanVar()
        self.monitor_status_var = tk.StringVar()
        self.monitor_status_var.set("監視中")
        self.screenshot_hotkey_var = tk.StringVar()
        self.screenshot_delay_hotkey_var = tk.StringVar()
        self.screenshot_delay_var = tk.StringVar()
        self.screenshot_dir_var = tk.StringVar()
        self.screenshot_format_var = tk.StringVar()
        self.screenshot_copy_var = tk.BooleanVar()
        self.screenshot_history_var = tk.BooleanVar()
        self.accessibility_status_var = tk.StringVar(value="確認中")
        self.screen_recording_status_var = tk.StringVar(value="確認中")
        self._accessibility_warned = False
        self._accessibility_ok = True
        self._next_permission_check = 0.0
        self._status_wrap = 0
        self._previous_app = None

        self._configure_window()
        self._build_ui()
        self._build_call_window()
        self._load_settings_fields()
        self._refresh_all()
        self._restart_hotkeys()
        self._start_tray()
        self._check_macos_permissions()
        cleanup_exports(self.store.data_dir)
        self.root.after(self.POLL_MS, self._poll)

        if self.config["settings"].get("start_minimized"):
            self.root.withdraw()

    def _configure_window(self) -> None:
        self.root.title("ShinClipboard - 設定・編集")
        self.root.geometry("920x720")
        self.root.minsize(720, 500)
        self.root.protocol("WM_DELETE_WINDOW", self.hide_window)
        icon_png = resource_path("assets/shinclipboard-512.png")
        if icon_png.exists():
            self.window_icon = tk.PhotoImage(file=icon_png)
            self.root.iconphoto(True, self.window_icon)
        style = ttk.Style()
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Title.TLabel", font=(UI_FONT_FAMILY, 15, "bold"))
        style.configure("Muted.TLabel", foreground="#5f6b7a")

    def _build_ui(self) -> None:
        header = ttk.Frame(self.root, padding=(16, 14, 16, 6))
        header.pack(fill="x")
        ttk.Label(header, text="ShinClipboard", style="Title.TLabel").pack(side="left")
        ttk.Label(header, text="クリップボード履歴と定型文", style="Muted.TLabel").pack(side="left", padx=14)
        ttk.Button(header, text="隠す", command=self.hide_window).pack(side="right")

        self.tabs = ttk.Notebook(self.root)
        self.tabs.pack(fill="both", expand=True, padx=14, pady=8)
        self.history_tab = ttk.Frame(self.tabs, padding=10)
        self.snippet_tab = ttk.Frame(self.tabs, padding=10)
        self.color_tab = ttk.Frame(self.tabs, padding=10)
        self.image_tab = ttk.Frame(self.tabs, padding=10)
        self.fifo_tab = ttk.Frame(self.tabs, padding=10)
        self.transform_tab = ttk.Frame(self.tabs, padding=10)
        # The settings tab is far taller than the window and grows with the OS -
        # macOS lays the same rows out taller and adds the permissions box - so
        # it is the one tab that has to be reachable by scrolling.
        settings_scroller = ScrollableFrame(self.tabs, padding=10)
        self.settings_tab = settings_scroller.body
        self.tabs.add(self.history_tab, text="履歴")
        self.tabs.add(self.snippet_tab, text="定型文")
        self.tabs.add(self.color_tab, text="色")
        self.tabs.add(self.image_tab, text="画像")
        self.tabs.add(self.fifo_tab, text="FIFO")
        self.tabs.add(self.transform_tab, text="整形")
        self.tabs.add(settings_scroller, text="設定")
        self._build_history_tab()
        self._build_snippet_tab()
        self._build_color_tab()
        self._build_image_tab()
        self._build_fifo_tab()
        self._build_transform_tab()
        self._build_settings_tab()
        # Some of what goes here is a sentence of guidance rather than a word of
        # state, and a single line drops its end - the half that says what to do.
        status = ttk.Label(
            self.root, textvariable=self.status_var, relief="sunken", anchor="w", justify="left", padding=(8, 4)
        )
        status.pack(fill="x")
        status.bind("<Configure>", self._wrap_status)

    def _wrap_status(self, event) -> None:
        """Follow the window's width, ignoring the resize wrapping itself causes."""
        width = max(120, event.width - 16)
        if self._status_wrap == width:
            return
        self._status_wrap = width
        event.widget.configure(wraplength=width)

    def _build_call_window(self) -> None:
        self.call_window = tk.Toplevel(self.root)
        self.call_window.title("ShinClipboard - 貼り付け")
        self.call_window.geometry("520x340")
        self.call_window.minsize(340, 200)
        self.call_window.protocol("WM_DELETE_WINDOW", self.hide_call_window)
        self.call_window.bind("<Escape>", lambda _: self.hide_call_window())
        self.call_window.bind("<KeyPress>", self._quick_select)
        # Keyboard navigation works wherever the focus is inside the popup.
        for sequence in ("<Tab>", "<Control-Tab>"):
            self.call_window.bind(sequence, lambda _: self._move_call_tab(1))
        for sequence in ("<Shift-Tab>", "<Control-Shift-Tab>", "<ISO_Left_Tab>"):
            try:
                self.call_window.bind(sequence, lambda _: self._move_call_tab(-1))
            except tk.TclError:
                pass
        for sequence in ("<Left>", "<Control-Left>"):
            self.call_window.bind(sequence, lambda _: self._move_call_group(-1))
        for sequence in ("<Right>", "<Control-Right>"):
            self.call_window.bind(sequence, lambda _: self._move_call_group(1))

        hint = ttk.Label(self.call_window, text=CALL_WINDOW_HINT, style="Muted.TLabel", padding=(8, 2))
        hint.pack(side="bottom", fill="x")
        # The hint lists every key and the window is small and resizable, so it
        # does not fit on one line at every size or in every desktop's UI font.
        # Wrapping to the width it actually has beats clipping the last keys off.
        hint.bind("<Configure>", lambda event, label=hint: self._wrap_label(label, event.width))
        self.call_tabs = ttk.Notebook(self.call_window)
        self.call_tabs.pack(fill="both", expand=True, padx=6, pady=(6, 2))
        history_frame = ttk.Frame(self.call_tabs, padding=2)
        self.call_tabs.add(history_frame, text="履歴")
        self.call_tabs.bind("<<NotebookTabChanged>>", self._focus_call_list)

        self.call_history_list = ImageListbox(history_frame, font=(UI_FONT_FAMILY, 11))
        self.call_history_list.pack(fill="both", expand=True)
        self.call_history_list.bind("<ButtonRelease-1>", self._call_history_click)
        self.call_history_list.bind("<Return>", lambda _: self._paste_call_history())
        self.call_history_list.bind("<Button-3>", self._call_history_menu)
        # Ctrl is in `shortcut_modifier_mask`, so `_quick_select` ignores this and
        # a bare "e" still pastes as before.
        self.call_window.bind("<Control-e>", self._edit_call_history_image)

        snippet_page = self._build_call_page("定型文", "groups", "snippets", self._render_snippet_row, plain=True)
        self._build_call_page("色", "color_groups", "colors", self._render_color_row)
        self._build_call_page("画像", "image_groups", "images", self._render_image_row)
        # The definitions page predates the others and is still referred to by name.
        self.call_snippet_list = snippet_page.listbox
        self.call_group_combo = snippet_page.combo
        self._setup_drag_source()
        self.call_window.withdraw()

    def _build_call_page(self, title: str, groups_key: str, items_key: str, render, plain: bool = False) -> CallPage:
        """Add a grouped tab to the popup. `plain` rows are text only; the rest may carry pictures."""
        frame = ttk.Frame(self.call_tabs, padding=2)
        self.call_tabs.add(frame, text=title)
        group_bar = ttk.Frame(frame)
        group_bar.pack(fill="x", pady=(0, 4))
        ttk.Label(group_bar, text="グループ").pack(side="left", padx=(2, 6))
        var = tk.StringVar()
        combo = ttk.Combobox(group_bar, textvariable=var, state="readonly")
        combo.pack(side="left", fill="x", expand=True)
        if plain:
            listbox = tk.Listbox(
                frame, activestyle="none", exportselection=False, font=(UI_FONT_FAMILY, 11), selectborderwidth=0
            )
        else:
            listbox = ImageListbox(frame, font=(UI_FONT_FAMILY, 11))
        listbox.pack(fill="both", expand=True)
        page = CallPage(groups_key, items_key, combo, var, listbox, render)
        self.call_pages[len(self.call_tabs.tabs()) - 1] = page
        combo.bind("<<ComboboxSelected>>", lambda _: self._call_group_changed(page))
        listbox.bind("<ButtonRelease-1>", lambda event: self._call_page_click(page, event))
        listbox.bind("<Return>", lambda _: self._paste_call_page(page))
        listbox.bind("<Button-3>", lambda event: self._call_page_menu(page, event))
        return page

    @property
    def call_group_id(self) -> str | None:
        """The definitions group the popup is showing."""
        return self.call_pages[1].group_id

    @property
    def call_snippet_items(self) -> list[dict]:
        return self.call_pages[1].items

    def _build_history_tab(self) -> None:
        top = ttk.Frame(self.history_tab)
        top.pack(fill="x", pady=(0, 8))
        ttk.Label(top, text="検索").pack(side="left")
        search = ttk.Entry(top, textvariable=self.search_var)
        search.pack(side="left", fill="x", expand=True, padx=8)
        self.search_var.trace_add("write", lambda *_: self._refresh_history())
        ttk.Button(top, text="選択を削除", command=self._delete_history).pack(side="left", padx=(0, 6))
        ttk.Button(top, text="すべて削除", command=self._clear_history).pack(side="left")
        self.history_list = tk.Listbox(
            self.history_tab,
            activestyle="none",
            exportselection=False,
            font=(UI_FONT_FAMILY, 11),
            selectborderwidth=0,
            selectmode="extended",
        )
        self.history_list.pack(fill="both", expand=True)
        self.history_list.bind("<Button-3>", self._history_right_click)
        self.history_list.bind("<Return>", lambda _: self._paste_selected_history())
        bar = ttk.Frame(self.history_tab)
        bar.pack(fill="x", pady=(8, 0))
        ttk.Button(bar, text="クリップボードへ", command=self._copy_selected_history).pack(side="left")
        ttk.Button(bar, text="貼り付け", command=self._paste_selected_history).pack(side="left", padx=6)
        ttk.Button(bar, text="編集", command=self._edit_history).pack(side="left")
        ttk.Button(bar, text="画像を編集", command=self._edit_selected_history_image).pack(side="left", padx=6)
        ttk.Button(bar, text="改行ごとに展開", command=self._split_history_lines).pack(side="left", padx=6)
        ttk.Button(bar, text="選択を連結", command=self._join_history).pack(side="left")
        ttk.Label(bar, text="右クリックで色・画像を登録できます", style="Muted.TLabel").pack(side="right")

    def _build_color_tab(self) -> None:
        outer = ttk.Panedwindow(self.color_tab, orient="horizontal")
        outer.pack(fill="both", expand=True)
        self.color_groups = GroupPanel(
            outer,
            groups=lambda: self.config["color_groups"],
            items_key="colors",
            noun="色",
            on_change=self._library_changed,
            on_select=self._refresh_colors,
            font=(UI_FONT_FAMILY, 10),
        )
        right = ttk.Frame(outer)
        outer.add(self.color_groups, weight=1)
        outer.add(right, weight=3)
        ttk.Label(right, text="値は #RRGGBB / #RRGGBBAA で登録し、登録した表記のまま貼り付けます。", style="Muted.TLabel").pack(anchor="w")
        self.color_tree = ttk.Treeview(right, columns=("title", "value", "memo", "hotkey"), show="tree headings")
        self.color_tree.column("#0", width=48, stretch=False)
        # Column widths are what the table asks the window for, so they are kept
        # small enough that the buttons under it stay inside a 920px window.
        for key, label, width in (("title", "名前", 130), ("value", "値", 100), ("memo", "メモ", 140), ("hotkey", "ショートカット", 130)):
            self.color_tree.heading(key, text=label)
            self.color_tree.column(key, width=width)
        self.color_tree.pack(fill="both", expand=True, pady=6)
        self.color_tree.bind(
            "<Button-3>",
            lambda event: self._library_menu(
                self.color_tree, event, self._paste_selected_color, self._copy_selected_color, self._edit_color, self._delete_color
            ),
        )
        self.color_tree.bind("<Return>", lambda _: self._paste_selected_color())
        self.color_tree.bind("<Delete>", lambda _: self._delete_color())
        bar = ttk.Frame(right)
        bar.pack(fill="x")
        ttk.Button(bar, text="追加", command=self._add_color).pack(side="left")
        ttk.Button(bar, text="編集", command=self._edit_color).pack(side="left", padx=4)
        ttk.Button(bar, text="削除", command=self._delete_color).pack(side="left")
        ttk.Button(bar, text="↑", width=3, command=lambda: self._move_color(-1)).pack(side="left", padx=(8, 2))
        ttk.Button(bar, text="↓", width=3, command=lambda: self._move_color(1)).pack(side="left")
        ttk.Button(bar, text="貼り付け", command=self._paste_selected_color).pack(side="right")
        ttk.Button(bar, text="クリップボードへ", command=self._copy_selected_color).pack(side="right", padx=6)

    def _build_image_tab(self) -> None:
        outer = ttk.Panedwindow(self.image_tab, orient="horizontal")
        outer.pack(fill="both", expand=True)
        self.image_groups = GroupPanel(
            outer,
            groups=lambda: self.config["image_groups"],
            items_key="images",
            noun="画像",
            on_change=self._library_changed,
            on_select=self._refresh_images,
            font=(UI_FONT_FAMILY, 10),
        )
        right = ttk.Frame(outer)
        outer.add(self.image_groups, weight=1)
        outer.add(right, weight=3)
        ttk.Label(
            right,
            text="ファイル、クリップボード、履歴（右クリック）から登録できます。画像ファイルをこの表へドロップしても登録できます。",
            style="Muted.TLabel",
        ).pack(anchor="w")
        # Thumbnails need taller rows than the other tables; the style inherits
        # the font from "Treeview" and only overrides the height.
        ttk.Style().configure("Library.Treeview", rowheight=TREE_THUMBNAIL_ROW)
        # `height` is the number of rows the table asks for. With 60px rows the
        # default of 10 is taller than the tab, and pack answers by pushing the
        # button bar below it out of the window.
        self.image_tree = ttk.Treeview(
            right, columns=("title", "size", "memo", "hotkey"), show="tree headings", style="Library.Treeview", height=5
        )
        self.image_tree.column("#0", width=TREE_THUMBNAIL_SIZE[0] + 16, stretch=False)
        for key, label, width in (("title", "名前", 130), ("size", "サイズ", 70), ("memo", "メモ", 120), ("hotkey", "ショートカット", 120)):
            self.image_tree.heading(key, text=label)
            self.image_tree.column(key, width=width)
        self.image_tree.pack(fill="both", expand=True, pady=6)
        self.image_tree.bind(
            "<Button-3>",
            lambda event: self._library_menu(
                self.image_tree, event, self._paste_selected_image, self._copy_selected_image, self._edit_image, self._delete_image
            ),
        )
        self.image_tree.bind("<Return>", lambda _: self._paste_selected_image())
        self.image_tree.bind("<Delete>", lambda _: self._delete_image())
        bar = ttk.Frame(right)
        bar.pack(fill="x")
        ttk.Button(bar, text="追加", command=self._add_image).pack(side="left")
        ttk.Button(bar, text="編集", command=self._edit_image).pack(side="left", padx=4)
        ttk.Button(bar, text="削除", command=self._delete_image).pack(side="left")
        ttk.Button(bar, text="↑", width=3, command=lambda: self._move_image(-1)).pack(side="left", padx=(8, 2))
        ttk.Button(bar, text="↓", width=3, command=lambda: self._move_image(1)).pack(side="left")
        ttk.Button(bar, text="貼り付け", command=self._paste_selected_image).pack(side="right")
        ttk.Button(bar, text="クリップボードへ", command=self._copy_selected_image).pack(side="right", padx=6)

    def _build_snippet_tab(self) -> None:
        outer = ttk.Panedwindow(self.snippet_tab, orient="horizontal")
        outer.pack(fill="both", expand=True)
        left = ttk.Frame(outer, padding=(0, 0, 8, 0))
        right = ttk.Frame(outer)
        outer.add(left, weight=1)
        outer.add(right, weight=3)
        ttk.Label(left, text="グループ").pack(anchor="w")
        self.group_list = tk.Listbox(left, exportselection=False, font=(UI_FONT_FAMILY, 10))
        self.group_list.pack(fill="both", expand=True, pady=6)
        self.group_list.bind("<<ListboxSelect>>", lambda _: self._refresh_snippets())
        group_bar = ttk.Frame(left)
        group_bar.pack(fill="x")
        ttk.Button(group_bar, text="追加", command=self._add_group).pack(side="left")
        ttk.Button(group_bar, text="名前変更", command=self._rename_group).pack(side="left", padx=4)
        ttk.Button(group_bar, text="削除", command=self._delete_group).pack(side="left", padx=4)
        ttk.Button(group_bar, text="↑", width=3, command=lambda: self._move_group(-1)).pack(side="left")
        ttk.Button(group_bar, text="↓", width=3, command=lambda: self._move_group(1)).pack(side="left", padx=2)

        snippet_search = ttk.Frame(right)
        snippet_search.pack(fill="x")
        ttk.Label(snippet_search, text="定型文検索").pack(side="left")
        ttk.Entry(snippet_search, textvariable=self.snippet_search_var).pack(side="left", fill="x", expand=True, padx=8)
        self.snippet_search_var.trace_add("write", lambda *_: self._refresh_snippets())
        self.snippet_tree = ttk.Treeview(right, columns=("title", "memo", "preview", "hotkey"), show="headings")
        self.snippet_tree.heading("title", text="名前")
        self.snippet_tree.heading("memo", text="メモ")
        self.snippet_tree.heading("preview", text="内容")
        self.snippet_tree.heading("hotkey", text="ショートカット")
        self.snippet_tree.column("title", width=150)
        self.snippet_tree.column("memo", width=120)
        self.snippet_tree.column("preview", width=250)
        self.snippet_tree.column("hotkey", width=150)
        self.snippet_tree.pack(fill="both", expand=True, pady=6)
        self.snippet_tree.bind("<Button-3>", self._snippet_right_click)
        self.snippet_tree.bind("<Return>", lambda _: self._paste_selected_snippet())
        bar = ttk.Frame(right)
        bar.pack(fill="x")
        ttk.Button(bar, text="追加", command=self._add_snippet).pack(side="left")
        ttk.Button(bar, text="編集", command=self._edit_snippet).pack(side="left", padx=4)
        ttk.Button(bar, text="削除", command=self._delete_snippet).pack(side="left")
        ttk.Button(bar, text="↑", width=3, command=lambda: self._move_snippet(-1)).pack(side="left", padx=(8, 2))
        ttk.Button(bar, text="↓", width=3, command=lambda: self._move_snippet(1)).pack(side="left")
        ttk.Button(bar, text="貼り付け", command=self._paste_selected_snippet).pack(side="right")

    def _build_fifo_tab(self) -> None:
        top = ttk.Frame(self.fifo_tab)
        top.pack(fill="x", pady=(0, 8))
        ttk.Label(top, textvariable=self.fifo_status_var, font=(UI_FONT_FAMILY, 12, "bold")).pack(side="left")
        self.fifo_toggle_button = ttk.Button(top, text="FIFO開始", command=self.toggle_fifo)
        self.fifo_toggle_button.pack(side="right")
        ttk.Button(top, text="LIFO開始", command=self.toggle_lifo).pack(side="right", padx=6)
        ttk.Button(top, text="停止", command=self.stop_stock).pack(side="right")
        self.fifo_list = tk.Listbox(self.fifo_tab, font=(UI_FONT_FAMILY, 11))
        self.fifo_list.pack(fill="both", expand=True)
        bar = ttk.Frame(self.fifo_tab)
        bar.pack(fill="x", pady=(8, 0))
        ttk.Button(bar, text="貼り付けを1つ戻す", command=self.undo_fifo).pack(side="left")
        ttk.Button(bar, text="キューを消去", command=self.clear_fifo).pack(side="left", padx=6)
        ttk.Button(bar, text="追加", command=self._add_stock).pack(side="left")
        ttk.Button(bar, text="編集", command=self._edit_stock).pack(side="left", padx=6)
        ttk.Button(bar, text="削除", command=self._delete_stock).pack(side="left")
        ttk.Button(bar, text="全件を改行で連結", command=self._join_stock).pack(side="left", padx=6)
        ttk.Label(bar, text="有効中は通常の貼り付けキーで先頭から順に貼り付けます。", style="Muted.TLabel").pack(side="right")

    def _build_settings_tab(self) -> None:
        form = ttk.Frame(self.settings_tab)
        form.pack(fill="x")
        rows = [
            ("画面表示ショートカット（空欄で無効）", self.popup_hotkey_var),
            ("FIFO切替ショートカット", self.fifo_hotkey_var),
            ("LIFO切替ショートカット", self.lifo_hotkey_var),
            ("監視切替ショートカット", self.monitor_hotkey_var),
            ("履歴保持件数", self.history_limit_var),
        ]
        for row, (label, variable) in enumerate(rows):
            ttk.Label(form, text=label).grid(row=row, column=0, sticky="w", padx=(0, 14), pady=7)
            ttk.Entry(form, textvariable=variable, width=32).grid(row=row, column=1, sticky="w", pady=7)
        ttk.Label(
            form,
            text="primary は Windows では Ctrl、macOS では Command として扱われます。",
            style="Muted.TLabel",
        ).grid(row=len(rows), column=0, columnspan=2, sticky="w", pady=(3, 12))
        ttk.Button(form, text="設定を保存", command=self._save_settings).grid(row=len(rows) + 1, column=0, sticky="w")
        if IS_MAC:
            self._build_permissions_section()
        transfer = ttk.LabelFrame(self.settings_tab, text="設定ファイルの移管", padding=12)
        transfer.pack(fill="x", pady=20)
        ttk.Label(transfer, text="定型文・グループ・ショートカットを1つのUTF-8 JSONで移管できます。").pack(anchor="w")
        buttons = ttk.Frame(transfer)
        buttons.pack(fill="x", pady=(10, 0))
        ttk.Button(buttons, text="設定を書き出す", command=self._export_config).pack(side="left")
        ttk.Button(buttons, text="設定を読み込む", command=self._import_config).pack(side="left", padx=8)
        ttk.Label(transfer, text=f"現在: {self.store.config_path}", style="Muted.TLabel").pack(anchor="w", pady=(10, 0))

        advanced = ttk.LabelFrame(self.settings_tab, text="動作・表示", padding=12)
        advanced.pack(fill="x", pady=(0, 12))
        checks = [
            ("選択時に自動貼り付け", self.auto_paste_var),
            ("履歴を終了後も保存", self.save_history_var),
            ("終了時に履歴を削除", self.clear_history_var),
            ("常に手前へ表示", self.always_on_top_var),
            ("最小化状態で起動", self.start_minimized_var),
            ("OSログイン時に起動", self.startup_var),
            ("Ctrlキー2回で画面表示", self.double_ctrl_var),
        ]
        for index, (label, variable) in enumerate(checks):
            ttk.Checkbutton(advanced, text=label, variable=variable).grid(row=index // 2, column=index % 2, sticky="w", padx=(0, 25), pady=3)
        check_rows = (len(checks) + 1) // 2
        ttk.Label(advanced, text="フォントサイズ").grid(row=check_rows, column=0, sticky="w", pady=(8, 3))
        ttk.Spinbox(advanced, from_=8, to=24, textvariable=self.font_size_var, width=8).grid(row=check_rows + 1, column=0, sticky="w")
        ttk.Label(advanced, text="配色").grid(row=check_rows, column=1, sticky="w", pady=(8, 3))
        ttk.Combobox(advanced, textvariable=self.theme_var, values=list(THEMES), state="readonly", width=15).grid(row=check_rows + 1, column=1, sticky="w")

        shots = ttk.LabelFrame(self.settings_tab, text="スクリーンショット", padding=12)
        shots.pack(fill="x", pady=(0, 12))
        ttk.Label(shots, text="撮影ショートカット（空欄で無効）").grid(row=0, column=0, sticky="w", padx=(0, 14), pady=4)
        ttk.Entry(shots, textvariable=self.screenshot_hotkey_var, width=24).grid(row=0, column=1, sticky="w", pady=4)
        ttk.Label(shots, text="ディレイ撮影ショートカット（空欄で無効）").grid(row=1, column=0, sticky="w", padx=(0, 14), pady=4)
        delay_row = ttk.Frame(shots)
        delay_row.grid(row=1, column=1, columnspan=2, sticky="w", pady=4)
        ttk.Entry(delay_row, textvariable=self.screenshot_delay_hotkey_var, width=24).pack(side="left")
        ttk.Label(delay_row, text="秒数").pack(side="left", padx=(12, 4))
        ttk.Spinbox(delay_row, from_=1, to=60, textvariable=self.screenshot_delay_var, width=5).pack(side="left")
        ttk.Label(shots, text="既定の保存先（空欄でピクチャ）").grid(row=2, column=0, sticky="w", padx=(0, 14), pady=4)
        ttk.Entry(shots, textvariable=self.screenshot_dir_var, width=40).grid(row=2, column=1, sticky="w", pady=4)
        ttk.Button(shots, text="選ぶ", command=self._choose_screenshot_dir).grid(row=2, column=2, sticky="w", padx=6)
        ttk.Label(shots, text="既定の形式").grid(row=3, column=0, sticky="w", padx=(0, 14), pady=4)
        ttk.Combobox(
            shots, textvariable=self.screenshot_format_var, values=["png", "jpeg"], state="readonly", width=10
        ).grid(row=3, column=1, sticky="w", pady=4)
        ttk.Checkbutton(shots, text="保存後にクリップボードへもコピー", variable=self.screenshot_copy_var).grid(
            row=4, column=0, sticky="w", pady=3
        )
        ttk.Checkbutton(shots, text="コピーした編集結果を履歴へ残す", variable=self.screenshot_history_var).grid(
            row=4, column=1, sticky="w", pady=3
        )
        builtin = "macOS標準の Cmd+Shift+4" if IS_MAC else "Windows標準の Win+Shift+S"
        ttk.Label(
            shots,
            text=f"{builtin} とは別の機能です。ディレイ撮影はメニューやツールチップを開いてから写すときに使います。",
            style="Muted.TLabel",
        ).grid(row=5, column=0, columnspan=3, sticky="w", pady=(8, 0))

        portability = ttk.LabelFrame(self.settings_tab, text="CSV・バックアップ", padding=12)
        portability.pack(fill="x")
        ttk.Button(portability, text="定型文CSV出力", command=self._export_snippets_csv).pack(side="left")
        ttk.Button(portability, text="定型文CSV取込", command=self._import_snippets_csv).pack(side="left", padx=6)
        ttk.Button(portability, text="全バックアップ", command=self._backup_all).pack(side="left", padx=(14, 6))
        ttk.Button(portability, text="復元", command=self._restore_all).pack(side="left")

    @staticmethod
    def _wrap_label(label: ttk.Label, width: int) -> None:
        """Rewrap a label to the width it was just given.

        Changing `wraplength` re-lays the label out and so fires `<Configure>`
        again; only writing it when the value really changed stops the two from
        chasing each other.
        """
        wanted = max(120, width - 16)
        if label.cget("wraplength") != wanted:
            label.configure(wraplength=wanted)

    def _build_permissions_section(self) -> None:
        """Report the two macOS permissions the app cannot work without.

        Both fail silently when missing - the shortcut simply never fires, the
        screen grab comes back empty - and neither can be granted from in here,
        so the state is spelled out next to a button that opens the pane that
        changes it.
        """
        box = ttk.LabelFrame(self.settings_tab, text="macOS の許可", padding=12)
        box.pack(fill="x", pady=(20, 12))
        rows = (
            ("アクセシビリティ", "ショートカットと自動貼り付けに使います", self.accessibility_status_var, ACCESSIBILITY_PANE),
            ("画面収録", "スクリーンショットの撮影に使います", self.screen_recording_status_var, SCREEN_RECORDING_PANE),
        )
        for row, (title, hint, variable, pane) in enumerate(rows):
            ttk.Label(box, text=title).grid(row=row, column=0, sticky="w", padx=(0, 14), pady=4)
            ttk.Label(box, textvariable=variable).grid(row=row, column=1, sticky="w", padx=(0, 14))
            ttk.Button(box, text="許可画面を開く", command=lambda url=pane: open_settings_pane(url)).grid(
                row=row, column=2, sticky="w"
            )
            ttk.Label(box, text=hint, style="Muted.TLabel").grid(row=row, column=3, sticky="w", padx=(14, 0))
        ttk.Button(box, text="状態を再確認", command=self._refresh_permissions).grid(
            row=len(rows), column=0, sticky="w", pady=(10, 0)
        )
        ttk.Label(
            box,
            text="アクセシビリティは許可するとすぐに反映されます。画面収録は許可のあとアプリを再起動してください。",
            style="Muted.TLabel",
        ).grid(row=len(rows), column=1, columnspan=3, sticky="w", pady=(10, 0))
        # macOS records a grant against the signature the app had at the time.
        # A rebuild with a different signature leaves the switch showing on
        # while nothing works, and toggling it does not rewrite the record.
        ttk.Label(
            box,
            text="一覧でONなのに「未許可」のままなら、その登録は古い署名のものです。"
            "「−」で削除してから「+」で追加し直してください。",
            style="Muted.TLabel",
        ).grid(row=len(rows) + 1, column=0, columnspan=4, sticky="w", pady=(4, 0))

    def _refresh_permissions(self) -> None:
        if not IS_MAC:
            return
        self.accessibility_status_var.set("許可済み" if accessibility_trusted() else "未許可")
        self.screen_recording_status_var.set("許可済み" if screen_recording_allowed() else "未許可")

    def _check_macos_permissions(self) -> None:
        """Show the system prompts once at startup and say what is still missing.

        macOS only ever shows each prompt once per app, so a user who dismissed
        it is left with the settings tab and the status line to find their way
        back; both are wired up before this runs.
        """
        if not IS_MAC:
            return
        self._refresh_permissions()
        self._accessibility_ok = accessibility_trusted()
        if not self._accessibility_ok:
            request_accessibility()
            self.status_var.set(f"ショートカットが使えません。{ACCESSIBILITY_HINT}")
        elif not screen_recording_allowed():
            request_screen_recording()
            self.status_var.set(f"スクリーンショットが撮れません。{SCREEN_RECORDING_HINT}")

    def _watch_accessibility(self) -> None:
        """Take the shortcuts up again the moment macOS grants the permission.

        Listeners started without it get an event tap that macOS creates and
        immediately switches off, so no key ever reaches them and every shortcut
        - the double Ctrl included - stays dead for the life of the process.
        Granting it cannot be done from in here, but it can be noticed, and
        starting the listeners again is what gets a tap that works. Without this
        the only way back is quitting and opening the app once more.
        """
        if not IS_MAC:
            return
        now = time.monotonic()
        if now < self._next_permission_check:
            return
        self._next_permission_check = now + self.PERMISSION_CHECK_SECONDS
        trusted = accessibility_trusted()
        if trusted == self._accessibility_ok:
            return
        self._accessibility_ok = trusted
        self._refresh_permissions()
        if trusted:
            self._restart_hotkeys()
            self.status_var.set("アクセシビリティが許可されました。ショートカットを有効にしました。")
        else:
            self.status_var.set(f"ショートカットが使えません。{ACCESSIBILITY_HINT}")

    def _build_transform_tab(self) -> None:
        ttk.Label(
            self.transform_tab,
            text="履歴へ保存する前に、手動または自動でテキストを整形できます。",
            style="Muted.TLabel",
        ).pack(anchor="w", pady=(0, 8))
        self.transform_tree = ttk.Treeview(
            self.transform_tab,
            columns=("name", "type", "hotkey", "auto"),
            show="headings",
        )
        for key, label, width in (
            ("name", "整形名", 260),
            ("type", "方法", 180),
            ("hotkey", "ショートカット", 160),
            ("auto", "自動", 70),
        ):
            self.transform_tree.heading(key, text=label)
            self.transform_tree.column(key, width=width)
        self.transform_tree.pack(fill="both", expand=True)
        self.transform_tree.bind("<Double-Button-1>", lambda _: self._apply_selected_transform())
        bar = ttk.Frame(self.transform_tab)
        bar.pack(fill="x", pady=(8, 0))
        ttk.Button(bar, text="選択履歴へ適用", command=self._apply_selected_transform).pack(side="left")
        ttk.Button(bar, text="追加", command=self._add_transform).pack(side="left", padx=(14, 4))
        ttk.Button(bar, text="編集", command=self._edit_transform).pack(side="left")
        ttk.Button(bar, text="削除", command=self._delete_transform).pack(side="left", padx=4)

    def _poll(self) -> None:
        if self.closing:
            return
        self._watch_accessibility()
        text = self._read_clipboard()
        token = clipboard_change_token()
        changed = token != self.last_clipboard_token if token is not None else text != self.last_clipboard
        # While the selection overlay owns the screen, redrawing the popup would
        # both waste work and risk pulling a withdrawn window back into view.
        if changed and self.capturing:
            changed = False
        if changed:
            self.last_clipboard_token = token
            self.last_clipboard = text
            if self.monitor_enabled:
                image = read_clipboard_image()
                if image is not None:
                    relative, width, height = self.store.save_image(image)
                    self.history.add_image(relative, width, height)
                    self.status_var.set(f"画像を履歴へ保存しました（{width}×{height}）")
                elif text:
                    auto_rules = [rule for rule in self.config.get("transforms", []) if rule.get("auto")]
                    try:
                        transformed = apply_enabled(text, auto_rules)
                    except (ValueError, re.error) as error:
                        transformed = text
                        self.status_var.set(f"自動整形エラー: {error}")
                    if transformed != text:
                        text = transformed
                        pyperclip.copy(text)
                        self.last_clipboard = text
                    self.history.add(text)
                    if self.fifo_enabled and text != self.ignore_fifo_text:
                        self.fifo.append(text)
                    if text == self.ignore_fifo_text:
                        self.ignore_fifo_text = None
                if self.config["settings"].get("save_history", True):
                    self.store.save_history(self.history)
                self._refresh_history()
                self._refresh_fifo()
        try:
            while True:
                event, payload = self.events.get_nowait()
                if self.capturing and event in ("show", "show_settings"):
                    continue  # a capture is in flight; nothing may steal the screen
                if event == "show":
                    self.show_window()
                elif event == "show_settings":
                    self.show_settings_window()
                elif event == "refresh_fifo":
                    self._refresh_fifo()
                elif event == "toggle_fifo":
                    self.toggle_fifo()
                elif event == "toggle_lifo":
                    self.toggle_lifo()
                elif event == "toggle_monitor":
                    self.toggle_monitor()
                elif event == "undo_fifo":
                    self.undo_fifo()
                elif event == "paste_text":
                    self._paste_text(str(payload))
                elif event == "paste_image":
                    self._paste_stored_image(str(payload))
                elif event == "paste_failed":
                    self.status_var.set("自動貼り付けに失敗しました。貼り付け先で手動で貼り付けてください。")
                elif event == "apply_transform":
                    self._apply_transform_by_id(str(payload))
                elif event == "screenshot":
                    self.start_capture()
                elif event == "screenshot_delayed":
                    self.start_delayed_capture()
                elif event == "edit_clipboard_image":
                    self.edit_clipboard_image()
                elif event == "quit":
                    self.quit()
                    return
        except queue.Empty:
            pass
        self.root.after(self.POLL_MS, self._poll)

    @staticmethod
    def _read_clipboard() -> str:
        try:
            value = pyperclip.paste()
            return value if isinstance(value, str) else ""
        except pyperclip.PyperclipException:
            return ""

    def _set_clipboard(self, text: str, ignore_fifo: bool = True) -> None:
        if ignore_fifo:
            self.ignore_fifo_text = text
        pyperclip.copy(text)

    def _set_clipboard_image(self, path: Path) -> None:
        """Put an image on the clipboard without `_poll` filing it as a new copy.

        `write_clipboard_image` reads the change token while it still holds the
        clipboard, so the token below is unambiguously our own write. Reading the
        token afterwards instead would also swallow anything another application
        copied in between.
        """
        token = write_clipboard_image(path)
        if token is not None:
            self.last_clipboard_token = token

    def _refresh_all(self) -> None:
        self._refresh_history()
        self._refresh_groups()
        self.color_groups.refresh()
        self.image_groups.refresh()
        self._refresh_fifo()
        self._refresh_transforms()
        self._refresh_call_window()

    def _refresh_history(self) -> None:
        if not hasattr(self, "history_list"):
            return
        self.visible_history = self.history.search(self.search_var.get())
        self.history_list.delete(0, "end")
        colors = THEMES.get(self.config["settings"].get("theme", "blue"), THEMES["blue"])
        for index, item in enumerate(self.visible_history):
            if item.kind == "image":
                preview = f"[画像] {item.width}×{item.height}  {Path(item.image_path).name[:12]}"
            elif parse_hex_color(item.text) is not None:
                preview = f"[色] {item.text.strip()}"
            else:
                preview = item.text.replace("\r", " ").replace("\n", " ↵ ")
            self.history_list.insert("end", preview[:180])
            self.history_list.itemconfigure(
                index,
                background=colors["stripe"] if index % 2 else colors["background"],
                foreground=colors["foreground"],
                selectbackground=colors["accent"],
                selectforeground="#ffffff",
            )
        self._refresh_call_history()

    def _refresh_call_window(self) -> None:
        self._refresh_call_history()
        for page in self.call_pages.values():
            self._refresh_call_page(page)

    def _refresh_call_snippets(self) -> None:
        self._refresh_call_page(self.call_pages[1])

    def _refresh_call_history(self) -> None:
        if not hasattr(self, "call_history_list"):
            return
        self.call_history_items = self.history.search()
        self.call_history_list.delete(0, "end")
        colors = THEMES.get(self.config["settings"].get("theme", "blue"), THEMES["blue"])
        for index, item in enumerate(self.call_history_items):
            prefix = f"{quick_key(index)}: " if quick_key(index) else "   "
            thumbnail = self._thumbnail(item.image_path) if item.kind == "image" else None
            rgba = parse_hex_color(item.text) if item.kind == "text" else None
            if thumbnail is not None:
                self.call_history_list.insert("end", prefix, image=thumbnail, suffix=f" {item.width}×{item.height}")
            elif rgba is not None:
                self.call_history_list.insert("end", prefix, image=self._swatch(rgba), suffix=f" {item.text.strip()}")
            else:
                if item.kind == "image":
                    preview = f"[画像] {item.width}×{item.height}"
                else:
                    preview = item.text.replace("\r", " ").replace("\n", " ↵ ")
                self.call_history_list.insert("end", prefix + preview[:160])
            self.call_history_list.itemconfigure(
                index,
                background=colors["stripe"] if index % 2 else colors["background"],
                foreground=colors["foreground"],
                selectbackground=colors["accent"],
                selectforeground="#ffffff",
            )
        referenced = {item.image_path for item in self.call_history_items if item.kind == "image"}
        referenced |= library_image_paths(self.config)
        self._thumbnails = {path: photo for path, photo in self._thumbnails.items() if path in referenced}

    def _thumbnail(self, image_path: str) -> ImageTk.PhotoImage | None:
        """Return a cached, scaled-down PhotoImage for a stored image, or None if unreadable."""
        return self._scaled_photo(self._thumbnails, image_path, THUMBNAIL_SIZE, self.call_window)

    def _tree_thumbnail(self, image_path: str) -> ImageTk.PhotoImage | None:
        """The same picture at the size of the image tab's rows."""
        return self._scaled_photo(self._tree_thumbnails, image_path, TREE_THUMBNAIL_SIZE, self.root)

    def _scaled_photo(self, cache: dict, image_path: str, size: tuple[int, int], master) -> ImageTk.PhotoImage | None:
        cached = cache.get(image_path)
        if cached is not None:
            return cached
        try:
            with Image.open(self.store.image_path(image_path)) as source:
                image = source.convert("RGBA")
            image.thumbnail(size)
            photo = ImageTk.PhotoImage(image, master=master)
        except (OSError, ValueError):
            return None
        cache[image_path] = photo
        return photo

    def _swatch(self, rgba: tuple[int, int, int, int]) -> ImageTk.PhotoImage:
        """A small block of the colour; there are few distinct ones, so none is ever evicted."""
        photo = self._swatches.get(rgba)
        if photo is None:
            photo = ImageTk.PhotoImage(swatch_image(rgba), master=self.root)
            self._swatches[rgba] = photo
        return photo

    # ----- popup pages (definitions / colours / images) ---------------------------

    def _refresh_call_page(self, page: CallPage) -> None:
        page.items = []
        page.listbox.delete(0, "end")
        groups = self.config[page.groups_key]
        page.combo.configure(values=[group["name"] for group in groups])
        if not groups:
            page.group_id = None
            page.var.set("")
            return
        group_index = next((index for index, group in enumerate(groups) if group["id"] == page.group_id), 0)
        group = groups[group_index]
        page.group_id = group["id"]
        page.combo.current(group_index)
        colors = THEMES.get(self.config["settings"].get("theme", "blue"), THEMES["blue"])
        for item in group.get(page.items_key, []):
            index = len(page.items)
            page.items.append(item)
            prefix = f"{quick_key(index)}: " if quick_key(index) else "   "
            page.render(prefix, item)
            page.listbox.itemconfigure(
                index,
                background=colors["stripe"] if index % 2 else colors["background"],
                foreground=colors["foreground"],
                selectbackground=colors["accent"],
                selectforeground="#ffffff",
            )

    def _render_snippet_row(self, prefix: str, snippet: dict) -> None:
        preview = snippet["text"].replace("\r", " ").replace("\n", " ↵ ")
        self.call_pages[1].listbox.insert("end", f"{prefix}{snippet['title']}  —  {preview[:130]}")

    def _render_color_row(self, prefix: str, color: dict) -> None:
        listbox = self.call_pages[2].listbox
        rgba = parse_hex_color(color.get("value", ""))
        suffix = f" {color['title']}  {color.get('value', '')}"
        if rgba is None:
            listbox.insert("end", prefix + suffix.strip())
        else:
            listbox.insert("end", prefix, image=self._swatch(rgba), suffix=suffix)

    def _render_image_row(self, prefix: str, entry: dict) -> None:
        listbox = self.call_pages[3].listbox
        size = f"{entry.get('width', 0)}×{entry.get('height', 0)}"
        thumbnail = self._thumbnail(entry.get("image_path", ""))
        if thumbnail is None:
            listbox.insert("end", f"{prefix}{entry['title']}  [画像] {size}")
        else:
            listbox.insert("end", f"{prefix}{entry['title']}  ", image=thumbnail, suffix=f" {size}")

    def _current_call_page(self) -> CallPage | None:
        return self.call_pages.get(self.call_tabs.index("current"))

    def _call_group_changed(self, page: CallPage) -> None:
        index = page.combo.current()
        groups = self.config[page.groups_key]
        if 0 <= index < len(groups):
            page.group_id = groups[index]["id"]
            self._refresh_call_page(page)
            self._focus_call_list()

    def _move_call_tab(self, offset: int):
        tabs = self.call_tabs.tabs()
        if tabs:
            current = self.call_tabs.index("current")
            self.call_tabs.select(tabs[(current + offset) % len(tabs)])
        return "break"

    def _move_call_group(self, offset: int):
        page = self._current_call_page()
        groups = self.config[page.groups_key] if page else []
        if not page or not groups:
            return "break"
        current = max(0, page.combo.current())
        target = (current + offset) % len(groups)
        page.group_id = groups[target]["id"]
        self._refresh_call_page(page)
        self._focus_call_list()
        return "break"

    def _selected_history(self):
        selected = self.history_list.curselection()
        return self.visible_history[selected[0]] if selected else None

    def _selected_histories(self):
        return [self.visible_history[index] for index in self.history_list.curselection()]

    def _history_right_click(self, event):
        index = self.history_list.nearest(event.y)
        if not 0 <= index < self.history_list.size():
            return "break"
        self.history_list.selection_clear(0, "end")
        self.history_list.selection_set(index)
        item = self.visible_history[index]
        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(label="貼り付け", command=self._paste_selected_history)
        menu.add_command(label="クリップボードへ", command=self._copy_selected_history)
        if item.kind == "image":
            menu.add_command(label="画像を編集", command=self._edit_selected_history_image)
            menu.add_command(label="画像に登録...", command=lambda: self._register_history_image(item))
        else:
            menu.add_command(label="編集", command=self._edit_history)
            if parse_hex_color(item.text) is not None:
                menu.add_command(label="色に登録...", command=lambda: self._register_history_color(item))
        menu.add_separator()
        menu.add_command(label="削除", command=self._delete_history)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
        return "break"

    def _snippet_right_click(self, event):
        return self._tree_right_click(self.snippet_tree, event)

    @staticmethod
    def _tree_right_click(tree: ttk.Treeview, event):
        item_id = tree.identify_row(event.y)
        if item_id:
            tree.selection_set(item_id)
        return "break"

    def _library_menu(self, tree: ttk.Treeview, event, paste, copy, edit, delete) -> str:
        """Right-click on a colour or image row: the same actions as the buttons under the table."""
        if not tree.identify_row(event.y):
            return "break"
        self._tree_right_click(tree, event)
        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(label="貼り付け", command=paste)
        menu.add_command(label="クリップボードへ", command=copy)
        menu.add_command(label="編集", command=edit)
        menu.add_separator()
        menu.add_command(label="削除 (Delete)", command=delete)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
        return "break"

    def _quick_select(self, event):
        focus = self.call_window.focus_get()
        if (
            focus
            and not isinstance(focus, ImageListbox)
            and focus.winfo_class() in {"Entry", "TEntry", "Text", "TCombobox", "TSpinbox"}
        ):
            return None
        if event.state & shortcut_modifier_mask():
            return None
        key = (event.char or "").lower()
        # Keys without a character (arrows, Home, modifiers...) give an empty
        # string, and `"" in QUICK_KEYS` is True, so require exactly one char.
        if len(key) != 1 or key not in QUICK_KEYS:
            return None
        index = QUICK_KEYS.index(key)
        page = self._current_call_page()
        if page is None:
            if index >= len(self.call_history_items):
                return None
            self.call_history_list.selection_clear(0, "end")
            self.call_history_list.selection_set(index)
            self.call_history_list.see(index)
            self._paste_call_history()
            return "break"
        if index < len(page.items):
            page.listbox.selection_clear(0, "end")
            page.listbox.selection_set(index)
            page.listbox.see(index)
            self._paste_call_page(page)
            return "break"
        return None

    def _focus_call_list(self, _event=None) -> None:
        if not hasattr(self, "call_tabs"):
            return
        page = self._current_call_page()
        widget = self.call_history_list if page is None else page.listbox
        if widget.size() and not widget.curselection():
            widget.selection_set(0)
        self.call_window.after_idle(widget.focus_set)

    @staticmethod
    def _clicked_list_row(widget: tk.Listbox, y: int) -> int | None:
        if not widget.size():
            return None
        index = widget.nearest(y)
        bounds = widget.bbox(index)
        return index if bounds and bounds[1] <= y <= bounds[1] + bounds[3] else None

    def _call_history_click(self, event) -> None:
        if self._drag_active:
            return  # the button release belongs to a drag & drop, not a click
        index = self._clicked_list_row(self.call_history_list, event.y)
        if index is not None:
            self.call_history_list.selection_clear(0, "end")
            self.call_history_list.selection_set(index)
            self.call_window.after_idle(self._paste_call_history)

    def _setup_drag_source(self) -> None:
        """Let popup rows be dragged into other applications (images as files, text as text)."""
        self.drag_enabled = False
        self._drag_active = False
        try:
            from tkinterdnd2 import TkinterDnD

            TkinterDnD._require(self.root)
            sources = [(self.call_history_list, None)] + [(page.listbox, page) for page in self.call_pages.values()]
            for widget, page in sources:
                widget.drag_source_register(1, "DND_Files", "DND_Text")
                widget.dnd_bind("<<DragInitCmd>>", lambda event, page=page: self._drag_init(event, page))
                widget.dnd_bind("<<DragEndCmd>>", self._drag_end)
            # Picture files dropped from Explorer / Finder go straight into the image library.
            for widget, page in ((self.image_tree, None), (self.call_pages[3].listbox, self.call_pages[3])):
                widget.drop_target_register("DND_Files", "DND_Text")
                widget.dnd_bind("<<Drop>>", lambda event, page=page: self._drop_images(event, page))
        except (ImportError, RuntimeError, tk.TclError) as error:
            self.status_var.set(f"ドラッグ＆ドロップは利用できません: {error}")
            return
        self.drag_enabled = True

    def _drop_images(self, event, page: CallPage | None = None):
        """tkdnd <<Drop>>: register every picture file dropped, named after the file.

        No page means the settings tab, where a group is asked for when there is
        none; the popup only ever adds to the group it is showing.
        """
        if self._drag_active:
            return "refuse_drop"  # a row of our own popup let go over the list
        if page is None:
            group = self._ensure_group(self.image_groups)
        else:
            groups = self.config["image_groups"]
            group = next((candidate for candidate in groups if candidate["id"] == page.group_id), None)
        if not group:
            self.status_var.set("先に画像のグループを追加してください。")
            return "refuse_drop"
        added = skipped = 0
        for path in self._dropped_paths(getattr(event, "data", "")):
            try:
                with Image.open(path) as opened:
                    picture = opened.convert("RGBA")
            except (OSError, ValueError):
                skipped += 1
                continue
            entry = {"id": str(uuid4()), "title": path.stem, "memo": "", "hotkey": ""}
            entry["image_path"], entry["width"], entry["height"] = self.store.save_library_image(picture)
            group["images"].append(entry)
            added += 1
        if added:
            self._save_config()
            self.image_groups.refresh()  # redraws the settings table and, through it, the popup page
        summary = f"画像を{added}件登録しました"
        if skipped:
            summary += f"（画像として読めない{skipped}件は飛ばしました）"
        self.status_var.set(summary if added or skipped else "画像ファイルをドロップしてください。")
        return "copy" if added else "refuse_drop"

    def _dropped_paths(self, data: str) -> list[Path]:
        """The files in a drop payload: a Tcl list of paths, or file URLs from some browsers."""
        try:
            raw = self.root.tk.splitlist(data)
        except tk.TclError:
            raw = data.split()
        paths: list[Path] = []
        for item in raw:
            text = str(item).strip()
            if text.startswith("file://"):
                from urllib.parse import unquote, urlparse

                text = unquote(urlparse(text).path)
                if text.startswith("/") and len(text) > 2 and text[2] == ":":
                    text = text[1:]  # file:///C:/... keeps a leading slash on Windows
            path = Path(text)
            if path.is_file():
                paths.append(path)
        return paths

    def _drag_init(self, _event=None, page: CallPage | None = None):
        """tkdnd <<DragInitCmd>>: describe what the selected row drops as. No page means the history."""
        if page is None:
            selected = self.call_history_list.curselection()
            if not selected or selected[0] >= len(self.call_history_items):
                return ("refuse_drop",)
            item = self.call_history_items[selected[0]]
            payload = self._drag_payload(item.image_path if item.kind == "image" else "", item.text)
        else:
            selected = page.listbox.curselection()
            if not selected or selected[0] >= len(page.items):
                return ("refuse_drop",)
            item = page.items[selected[0]]
            if page.items_key == "images":
                payload = self._drag_payload(item.get("image_path", ""), "")
            else:
                payload = self._drag_payload("", item.get("value") or item.get("text") or "")
        if payload == ("refuse_drop",):
            return payload
        self._drag_active = True
        return payload

    def _drag_payload(self, image_path: str, text: str):
        if image_path:
            try:
                path = self.store.image_path(image_path)
            except ValueError:
                return ("refuse_drop",)
            return ("copy", "DND_Files", (str(path),)) if path.exists() else ("refuse_drop",)
        return ("copy", "DND_Text", text) if text else ("refuse_drop",)

    def _drag_end(self, event=None) -> None:
        """tkdnd <<DragEndCmd>>: hide the popup after a successful drop."""
        action = str(getattr(event, "action", "") or "")
        # Keep the guard a little longer: the button release may arrive after this.
        self.call_window.after(300, self._finish_drag)
        if action in {"copy", "move", "link"}:
            self.hide_call_window()

    def _finish_drag(self) -> None:
        self._drag_active = False

    def _call_page_click(self, page: CallPage, event) -> None:
        if self._drag_active:
            return  # the button release belongs to a drag & drop, not a click
        index = self._clicked_list_row(page.listbox, event.y)
        if index is not None:
            page.listbox.selection_clear(0, "end")
            page.listbox.selection_set(index)
            self.call_window.after_idle(lambda: self._paste_call_page(page))

    def _call_snippet_click(self, event) -> None:
        self._call_page_click(self.call_pages[1], event)

    def _paste_call_history(self) -> None:
        selected = self.call_history_list.curselection()
        if not selected:
            return
        item = self.call_history_items[selected[0]]
        if item.kind == "image":
            self._paste_stored_image(item.image_path, self.call_window)
        else:
            self._paste_text(item.text)

    def _paste_call_page(self, page: CallPage) -> None:
        selected = page.listbox.curselection()
        if not selected or selected[0] >= len(page.items):
            return
        self._paste_library_item(page.items_key, page.items[selected[0]], self.call_window)

    def _paste_call_snippet(self) -> None:
        self._paste_call_page(self.call_pages[1])

    def _paste_library_item(self, items_key: str, item: dict, parent: tk.Misc | None = None) -> None:
        if items_key == "images":
            self._paste_stored_image(item.get("image_path", ""), parent)
        elif items_key == "colors":
            self._paste_text(item["value"])
        else:
            self._paste_text(item["text"])

    def _paste_stored_image(self, relative: str, parent: tk.Misc | None = None) -> bool:
        """Put a stored picture on the clipboard and, when enabled, paste it where the user was."""
        if not self._copy_stored_image(relative, parent):
            return False
        if self.config["settings"].get("auto_paste", True):
            self._send_paste()
        return True

    def _copy_stored_image(self, relative: str, parent: tk.Misc | None = None) -> bool:
        try:
            self._set_clipboard_image(self.store.image_path(relative))
        except (ImportError, OSError, RuntimeError, ValueError) as error:
            messagebox.showerror("画像", str(error), parent=parent or self.root)
            return False
        return True

    def _copy_selected_history(self) -> None:
        item = self._selected_history()
        if item:
            if item.kind == "image":
                if self._copy_stored_image(item.image_path):
                    self.status_var.set("画像をクリップボードへコピーしました")
            else:
                self._set_clipboard(item.text)
                self.status_var.set("履歴をクリップボードへコピーしました")

    def _paste_selected_history(self) -> None:
        item = self._selected_history()
        if item:
            if item.kind == "image":
                self._paste_stored_image(item.image_path)
            else:
                self._paste_text(item.text)

    def _delete_history(self) -> None:
        item = self._selected_history()
        if not item:
            return
        index = next(i for i, candidate in enumerate(self.history.search()) if candidate == item)
        self.history.remove(index)
        self.store.save_history(self.history)
        self._refresh_history()

    def _clear_history(self) -> None:
        if messagebox.askyesno("履歴の削除", "クリップボード履歴をすべて削除しますか？"):
            self.history.clear()
            self.store.save_history(self.history)
            self._refresh_history()

    def _edit_history(self) -> None:
        item = self._selected_history()
        if not item or item.kind != "text":
            return
        value = self._text_dialog("履歴の編集", item.text)
        if value is None:
            return
        index = next(i for i, candidate in enumerate(self.history.search()) if candidate == item)
        self.history.remove(index)
        self.history.add(value)
        self.store.save_history(self.history)
        self._set_clipboard(value)
        self._refresh_history()

    def _split_history_lines(self) -> None:
        item = self._selected_history()
        if not item or item.kind != "text":
            return
        lines = [line for line in item.text.splitlines() if line]
        for line in reversed(lines):
            self.history.add(line)
        self.store.save_history(self.history)
        self._refresh_history()

    def _join_history(self) -> None:
        items = self._selected_histories()
        if not items:
            return
        text_items = [item for item in items if item.kind == "text"]
        if not text_items:
            return
        value = "\n".join(item.text for item in text_items)
        self.history.add(value)
        self.store.save_history(self.history)
        self._set_clipboard(value)
        self._refresh_history()

    def _text_dialog(self, title: str, initial: str = "", parent: tk.Misc | None = None) -> str | None:
        # The editor passes its own window: the root is often withdrawn while an
        # editor is open, and a dialog transient to a hidden window is unreachable.
        owner = parent or self.root
        dialog = tk.Toplevel(owner)
        dialog.title(title)
        dialog.geometry("560x360")
        dialog.transient(owner)
        dialog.grab_set()
        editor = tk.Text(dialog, wrap="word", font=(UI_FONT_FAMILY, int(self.config["settings"].get("font_size", 11))))
        editor.pack(fill="both", expand=True, padx=12, pady=12)
        editor.insert("1.0", initial)
        result: list[str] = []

        def accept() -> None:
            value = editor.get("1.0", "end-1c")
            if value:
                result.append(value)
                dialog.destroy()

        buttons = ttk.Frame(dialog)
        buttons.pack(fill="x", padx=12, pady=(0, 12))
        ttk.Button(buttons, text="保存", command=accept).pack(side="right")
        ttk.Button(buttons, text="キャンセル", command=dialog.destroy).pack(side="right", padx=6)
        dialog.wait_window()
        return result[0] if result else None

    def _selected_group_index(self) -> int | None:
        selected = self.group_list.curselection()
        return selected[0] if selected else None

    def _selected_group(self) -> dict | None:
        index = self._selected_group_index()
        groups = self.config["groups"]
        return groups[index] if index is not None and index < len(groups) else None

    def _refresh_groups(self, select: int | None = None) -> None:
        current = self._selected_group_index() if select is None else select
        self.group_list.delete(0, "end")
        for group in self.config["groups"]:
            self.group_list.insert("end", group["name"])
        if self.config["groups"]:
            index = min(current or 0, len(self.config["groups"]) - 1)
            self.group_list.selection_set(index)
        self._refresh_snippets()

    def _refresh_snippets(self) -> None:
        for item in self.snippet_tree.get_children():
            self.snippet_tree.delete(item)
        group = self._selected_group()
        if not group:
            self.visible_snippets = []
            self._refresh_call_snippets()
            return
        needle = self.snippet_search_var.get().strip().casefold()
        self.visible_snippets = []
        for snippet in group.get("snippets", []):
            haystack = "\n".join((snippet.get("title", ""), snippet.get("memo", ""), snippet.get("text", ""))).casefold()
            if needle and needle not in haystack:
                continue
            self.visible_snippets.append(snippet)
            preview = snippet["text"].replace("\r", " ").replace("\n", " ↵ ")[:120]
            self.snippet_tree.insert("", "end", iid=snippet["id"], values=(snippet["title"], snippet.get("memo", ""), preview, snippet.get("hotkey", "")))
        self._refresh_call_snippets()

    def _selected_snippet(self) -> dict | None:
        group = self._selected_group()
        selected = self.snippet_tree.selection()
        if not group or not selected:
            return None
        return next((item for item in group["snippets"] if item["id"] == selected[0]), None)

    def _add_group(self) -> None:
        name = simpledialog.askstring("グループ追加", "グループ名:", parent=self.root)
        if name and name.strip():
            self.config["groups"].append({"id": str(uuid4()), "name": name.strip(), "snippets": []})
            self._save_config()
            self._refresh_groups(len(self.config["groups"]) - 1)

    def _delete_group(self) -> None:
        index = self._selected_group_index()
        if index is not None and messagebox.askyesno("グループ削除", "グループと中の定型文を削除しますか？"):
            del self.config["groups"][index]
            self._save_config()
            self._refresh_groups()

    def _rename_group(self) -> None:
        group = self._selected_group()
        if not group:
            return
        name = simpledialog.askstring("グループ名変更", "グループ名:", initialvalue=group["name"], parent=self.root)
        if name and name.strip():
            group["name"] = name.strip()
            self._save_config()
            self._refresh_groups(self._selected_group_index())

    def _move_group(self, offset: int) -> None:
        index = self._selected_group_index()
        if index is None:
            return
        target = index + offset
        groups = self.config["groups"]
        if not 0 <= target < len(groups):
            return
        groups[index], groups[target] = groups[target], groups[index]
        self._save_config()
        self._refresh_groups(target)

    def _snippet_dialog(self, snippet: dict | None = None) -> dict | None:
        dialog = tk.Toplevel(self.root)
        dialog.title("定型文の編集" if snippet else "定型文の追加")
        dialog.geometry("560x440")
        dialog.transient(self.root)
        dialog.grab_set()
        title_var = tk.StringVar(value=snippet.get("title", "") if snippet else "")
        memo_var = tk.StringVar(value=snippet.get("memo", "") if snippet else "")
        hotkey_var = tk.StringVar(value=snippet.get("hotkey", "") if snippet else "")
        ttk.Label(dialog, text="名前").pack(anchor="w", padx=14, pady=(14, 3))
        ttk.Entry(dialog, textvariable=title_var).pack(fill="x", padx=14)
        ttk.Label(dialog, text="メモ").pack(anchor="w", padx=14, pady=(8, 3))
        ttk.Entry(dialog, textvariable=memo_var).pack(fill="x", padx=14)
        ttk.Label(dialog, text="内容").pack(anchor="w", padx=14, pady=(10, 3))
        content = tk.Text(dialog, wrap="word", height=10, font=(UI_FONT_FAMILY, 10))
        content.pack(fill="both", expand=True, padx=14)
        if snippet:
            content.insert("1.0", snippet.get("text", ""))
        ttk.Label(dialog, text="ショートカット（任意、例: primary+alt+1）").pack(anchor="w", padx=14, pady=(10, 3))
        ttk.Entry(dialog, textvariable=hotkey_var).pack(fill="x", padx=14)
        result: list[dict] = []

        def accept() -> None:
            title = title_var.get().strip()
            text = content.get("1.0", "end-1c")
            if not title or not text:
                messagebox.showwarning("入力不足", "名前と内容を入力してください。", parent=dialog)
                return
            result.append({
                "id": snippet.get("id", str(uuid4())) if snippet else str(uuid4()),
                "title": title,
                "text": text,
                "memo": memo_var.get().strip(),
                "hotkey": hotkey_var.get().strip().lower(),
            })
            dialog.destroy()

        buttons = ttk.Frame(dialog)
        buttons.pack(fill="x", padx=14, pady=12)
        ttk.Button(buttons, text="保存", command=accept).pack(side="right")
        ttk.Button(buttons, text="キャンセル", command=dialog.destroy).pack(side="right", padx=6)
        dialog.wait_window()
        return result[0] if result else None

    def _add_snippet(self) -> None:
        group = self._selected_group()
        if not group:
            messagebox.showinfo("定型文", "先にグループを追加してください。")
            return
        value = self._snippet_dialog()
        if value:
            group["snippets"].append(value)
            self._save_config(restart_hotkeys=True)
            self._refresh_snippets()

    def _edit_snippet(self) -> None:
        group = self._selected_group()
        snippet = self._selected_snippet()
        if not group or not snippet:
            return
        value = self._snippet_dialog(snippet)
        if value:
            group["snippets"][group["snippets"].index(snippet)] = value
            self._save_config(restart_hotkeys=True)
            self._refresh_snippets()

    def _delete_snippet(self) -> None:
        group = self._selected_group()
        snippet = self._selected_snippet()
        if group and snippet and messagebox.askyesno("定型文削除", f"「{snippet['title']}」を削除しますか？"):
            group["snippets"].remove(snippet)
            self._save_config(restart_hotkeys=True)
            self._refresh_snippets()

    def _move_snippet(self, offset: int) -> None:
        group = self._selected_group()
        snippet = self._selected_snippet()
        if not group or not snippet:
            return
        index = group["snippets"].index(snippet)
        target = index + offset
        if not 0 <= target < len(group["snippets"]):
            return
        group["snippets"][index], group["snippets"][target] = group["snippets"][target], group["snippets"][index]
        self._save_config()
        self._refresh_snippets()
        self.snippet_tree.selection_set(snippet["id"])

    def _paste_selected_snippet(self) -> None:
        snippet = self._selected_snippet()
        if snippet:
            self._paste_text(snippet["text"])

    # ----- colour and image libraries ---------------------------------------------

    def _library_changed(self) -> None:
        """A group was added, renamed, removed or moved in the colour or image tab."""
        self._save_config(restart_hotkeys=True)
        self.store.cleanup_library(self.config)
        self._refresh_call_window()

    @staticmethod
    def _ensure_group(panel: GroupPanel) -> dict | None:
        """The selected group, or a freshly named one when the tab has none yet."""
        return panel.selected_group() or panel.add()

    @staticmethod
    def _selected_tree_item(tree: ttk.Treeview, group: dict | None, items_key: str) -> dict | None:
        selected = tree.selection()
        if not group or not selected:
            return None
        return next((item for item in group.get(items_key, []) if item["id"] == selected[0]), None)

    def _refresh_colors(self) -> None:
        for row in self.color_tree.get_children():
            self.color_tree.delete(row)
        group = self.color_groups.selected_group()
        for color in (group or {}).get("colors", []):
            rgba = parse_hex_color(color.get("value", ""))
            options = {"image": self._swatch(rgba)} if rgba is not None else {}
            self.color_tree.insert(
                "", "end", iid=color["id"],
                values=(color["title"], color.get("value", ""), color.get("memo", ""), color.get("hotkey", "")),
                **options,
            )
        if 2 in self.call_pages:
            self._refresh_call_page(self.call_pages[2])

    def _selected_color(self) -> dict | None:
        return self._selected_tree_item(self.color_tree, self.color_groups.selected_group(), "colors")

    def _color_dialog(self, color: dict | None = None, initial_value: str = "", parent: tk.Misc | None = None) -> dict | None:
        owner = parent or self.root
        dialog = tk.Toplevel(owner)
        dialog.title("色の編集" if color else "色の追加")
        dialog.geometry("480x300")
        dialog.transient(owner)
        dialog.grab_set()
        title_var = tk.StringVar(value=color.get("title", "") if color else "")
        value_var = tk.StringVar(value=color.get("value", "") if color else initial_value)
        memo_var = tk.StringVar(value=color.get("memo", "") if color else "")
        hotkey_var = tk.StringVar(value=color.get("hotkey", "") if color else "")
        form = ttk.Frame(dialog, padding=14)
        form.pack(fill="both", expand=True)
        form.columnconfigure(1, weight=1)
        ttk.Label(form, text="名前").grid(row=0, column=0, sticky="w", pady=6, padx=(0, 12))
        ttk.Entry(form, textvariable=title_var).grid(row=0, column=1, columnspan=2, sticky="ew", pady=6)
        ttk.Label(form, text="値").grid(row=1, column=0, sticky="w", pady=6, padx=(0, 12))
        value_row = ttk.Frame(form)
        value_row.grid(row=1, column=1, columnspan=2, sticky="ew", pady=6)
        ttk.Entry(value_row, textvariable=value_var, width=14).pack(side="left")
        # A plain tk label: ttk labels take their background from the theme.
        preview = tk.Label(value_row, width=6, relief="solid", borderwidth=1)
        preview.pack(side="left", padx=8, fill="y")
        default_background = preview.cget("background")

        def show_preview(*_) -> None:
            rgba = parse_hex_color(value_var.get())
            preview.configure(background=f"#{rgba[0]:02x}{rgba[1]:02x}{rgba[2]:02x}" if rgba else default_background)

        def choose() -> None:
            rgba = parse_hex_color(value_var.get())
            current = f"#{rgba[0]:02x}{rgba[1]:02x}{rgba[2]:02x}" if rgba else None
            _, chosen = colorchooser.askcolor(color=current, parent=dialog, title="色を選ぶ")
            if chosen:
                value_var.set(chosen)

        ttk.Button(value_row, text="選ぶ…", command=choose).pack(side="left")
        value_var.trace_add("write", show_preview)
        show_preview()
        ttk.Label(form, text="#RGB / #RRGGBB / #RRGGBBAA。登録した表記のまま貼り付けます。", style="Muted.TLabel").grid(
            row=2, column=1, columnspan=2, sticky="w"
        )
        ttk.Label(form, text="メモ").grid(row=3, column=0, sticky="w", pady=6, padx=(0, 12))
        ttk.Entry(form, textvariable=memo_var).grid(row=3, column=1, columnspan=2, sticky="ew", pady=6)
        ttk.Label(form, text="ショートカット").grid(row=4, column=0, sticky="w", pady=6, padx=(0, 12))
        ttk.Entry(form, textvariable=hotkey_var).grid(row=4, column=1, columnspan=2, sticky="ew", pady=6)
        ttk.Label(form, text="任意、例: primary+alt+1", style="Muted.TLabel").grid(row=5, column=1, sticky="w")
        result: list[dict] = []

        def accept() -> None:
            title = title_var.get().strip()
            value = value_var.get().strip()
            if not title:
                messagebox.showwarning("入力不足", "名前を入力してください。", parent=dialog)
                return
            if parse_hex_color(value) is None:
                messagebox.showwarning("色", "値は #RRGGBB のような16進表記で入力してください。", parent=dialog)
                return
            result.append({
                "id": color.get("id", str(uuid4())) if color else str(uuid4()),
                "title": title,
                "value": value,
                "memo": memo_var.get().strip(),
                "hotkey": hotkey_var.get().strip().lower(),
            })
            dialog.destroy()

        buttons = ttk.Frame(form)
        buttons.grid(row=6, column=0, columnspan=3, sticky="e", pady=(16, 0))
        ttk.Button(buttons, text="保存", command=accept).pack(side="right")
        ttk.Button(buttons, text="キャンセル", command=dialog.destroy).pack(side="right", padx=6)
        dialog.wait_window()
        return result[0] if result else None

    def _add_color(self, initial_value: str = "") -> None:
        group = self._ensure_group(self.color_groups)
        if not group:
            return
        value = self._color_dialog(initial_value=initial_value)
        if value:
            group["colors"].append(value)
            self._save_config(restart_hotkeys=True)
            self._refresh_colors()
            self.color_tree.selection_set(value["id"])

    def _edit_color(self) -> None:
        group = self.color_groups.selected_group()
        color = self._selected_color()
        if not group or not color:
            return
        value = self._color_dialog(color)
        if value:
            group["colors"][group["colors"].index(color)] = value
            self._save_config(restart_hotkeys=True)
            self._refresh_colors()
            self.color_tree.selection_set(value["id"])

    def _delete_color(self) -> None:
        group = self.color_groups.selected_group()
        color = self._selected_color()
        if group and color and messagebox.askyesno("色の削除", f"「{color['title']}」を削除しますか？"):
            group["colors"].remove(color)
            self._save_config(restart_hotkeys=True)
            self._refresh_colors()

    def _move_color(self, offset: int) -> None:
        self._move_library_item(self.color_groups.selected_group(), "colors", self._selected_color(), offset, self.color_tree)
        self._refresh_colors()

    def _move_library_item(self, group: dict | None, items_key: str, item: dict | None, offset: int, tree: ttk.Treeview) -> None:
        if not group or not item:
            return
        items = group[items_key]
        index = items.index(item)
        target = index + offset
        if not 0 <= target < len(items):
            return
        items[index], items[target] = items[target], items[index]
        self._save_config()
        tree.after_idle(lambda: tree.selection_set(item["id"]))

    def _paste_selected_color(self) -> None:
        color = self._selected_color()
        if color:
            self._paste_text(color["value"])

    def _copy_selected_color(self) -> None:
        color = self._selected_color()
        if color:
            self._set_clipboard(color["value"])
            self.status_var.set(f"色をクリップボードへコピーしました: {color['value']}")

    def _register_history_color(self, item: HistoryItem) -> None:
        """Turn a copied colour code into a library entry."""
        self.tabs.select(self.color_tab)
        self._add_color(initial_value=item.text.strip())

    def _refresh_images(self) -> None:
        for row in self.image_tree.get_children():
            self.image_tree.delete(row)
        group = self.image_groups.selected_group()
        for entry in (group or {}).get("images", []):
            thumbnail = self._tree_thumbnail(entry.get("image_path", ""))
            options = {"image": thumbnail} if thumbnail is not None else {}
            size = f"{entry.get('width', 0)}×{entry.get('height', 0)}"
            self.image_tree.insert(
                "", "end", iid=entry["id"],
                values=(entry["title"], size, entry.get("memo", ""), entry.get("hotkey", "")),
                **options,
            )
        referenced = library_image_paths(self.config)
        self._tree_thumbnails = {path: photo for path, photo in self._tree_thumbnails.items() if path in referenced}
        if 3 in self.call_pages:
            self._refresh_call_page(self.call_pages[3])

    def _selected_image(self) -> dict | None:
        return self._selected_tree_item(self.image_tree, self.image_groups.selected_group(), "images")

    def _image_dialog(self, entry: dict | None = None, image: Image.Image | None = None, parent: tk.Misc | None = None) -> dict | None:
        """Ask for an image entry. The returned dict carries a new picture under "image" until it is stored."""
        owner = parent or self.root
        dialog = tk.Toplevel(owner)
        dialog.title("画像の編集" if entry else "画像の追加")
        dialog.geometry("520x460")
        dialog.transient(owner)
        dialog.grab_set()
        title_var = tk.StringVar(value=entry.get("title", "") if entry else "")
        memo_var = tk.StringVar(value=entry.get("memo", "") if entry else "")
        hotkey_var = tk.StringVar(value=entry.get("hotkey", "") if entry else "")
        chosen: dict[str, object] = {"image": image}
        form = ttk.Frame(dialog, padding=14)
        form.pack(fill="both", expand=True)
        form.columnconfigure(1, weight=1)
        for row, (label, variable) in enumerate((("名前", title_var), ("メモ", memo_var), ("ショートカット", hotkey_var))):
            ttk.Label(form, text=label).grid(row=row, column=0, sticky="w", pady=6, padx=(0, 12))
            ttk.Entry(form, textvariable=variable).grid(row=row, column=1, sticky="ew", pady=6)
        ttk.Label(form, text="ショートカットは任意、例: primary+alt+1", style="Muted.TLabel").grid(row=3, column=1, sticky="w")
        picture = ttk.LabelFrame(form, text="画像", padding=10)
        picture.grid(row=4, column=0, columnspan=2, sticky="nsew", pady=(12, 0))
        form.rowconfigure(4, weight=1)
        sources = ttk.Frame(picture)
        sources.pack(fill="x")
        size_var = tk.StringVar(value="未選択")
        preview = tk.Label(picture, textvariable=size_var, compound="top", anchor="center")
        preview.pack(fill="both", expand=True, pady=(8, 0))

        def show(source: Image.Image | None) -> None:
            if source is None:
                preview.configure(image="")
                size_var.set("未選択")
                return
            thumbnail = source.copy()
            thumbnail.thumbnail((240, 160))
            photo = ImageTk.PhotoImage(thumbnail, master=dialog)
            preview.configure(image=photo)
            preview.image = photo  # keep a reference or Tk drops the picture
            size_var.set(f"{source.width}×{source.height}")

        def pick_file() -> None:
            path = filedialog.askopenfilename(
                title="画像を選ぶ",
                filetypes=[("画像", "*.png *.jpg *.jpeg *.gif *.bmp *.webp"), ("すべて", "*.*")],
                parent=dialog,
            )
            if not path:
                return
            try:
                with Image.open(path) as opened:
                    chosen["image"] = opened.convert("RGBA")
            except (OSError, ValueError) as error:
                messagebox.showerror("画像", f"画像を開けません: {error}", parent=dialog)
                return
            if not title_var.get().strip():
                title_var.set(Path(path).stem)
            show(chosen["image"])

        def from_clipboard() -> None:
            clipboard = read_clipboard_image()
            if clipboard is None:
                messagebox.showinfo("画像", "クリップボードに画像がありません。", parent=dialog)
                return
            chosen["image"] = clipboard.convert("RGBA")
            show(chosen["image"])

        ttk.Button(sources, text="ファイルを選ぶ…", command=pick_file).pack(side="left")
        ttk.Button(sources, text="クリップボードの画像", command=from_clipboard).pack(side="left", padx=6)
        if image is not None:
            show(image)
        elif entry and entry.get("image_path"):
            try:
                with Image.open(self.store.image_path(entry["image_path"])) as opened:
                    show(opened.convert("RGBA"))
            except (OSError, ValueError):
                size_var.set("画像ファイルが見つかりません")
        result: list[dict] = []

        def accept() -> None:
            title = title_var.get().strip()
            if not title:
                messagebox.showwarning("入力不足", "名前を入力してください。", parent=dialog)
                return
            if chosen["image"] is None and not (entry and entry.get("image_path")):
                messagebox.showwarning("入力不足", "画像を選んでください。", parent=dialog)
                return
            result.append({
                "id": entry.get("id", str(uuid4())) if entry else str(uuid4()),
                "title": title,
                "memo": memo_var.get().strip(),
                "hotkey": hotkey_var.get().strip().lower(),
                "image_path": entry.get("image_path", "") if entry else "",
                "width": int(entry.get("width", 0)) if entry else 0,
                "height": int(entry.get("height", 0)) if entry else 0,
                "image": chosen["image"],
            })
            dialog.destroy()

        buttons = ttk.Frame(form)
        buttons.grid(row=5, column=0, columnspan=2, sticky="e", pady=(12, 0))
        ttk.Button(buttons, text="保存", command=accept).pack(side="right")
        ttk.Button(buttons, text="キャンセル", command=dialog.destroy).pack(side="right", padx=6)
        dialog.wait_window()
        return result[0] if result else None

    def _store_image_entry(self, value: dict) -> dict:
        """Write the picture a dialog returned into the library and finish the entry."""
        picture = value.pop("image", None)
        if picture is not None:
            value["image_path"], value["width"], value["height"] = self.store.save_library_image(picture)
        return value

    def _add_image(self, image: Image.Image | None = None) -> None:
        group = self._ensure_group(self.image_groups)
        if not group:
            return
        value = self._image_dialog(image=image)
        if value:
            group["images"].append(self._store_image_entry(value))
            self._save_config(restart_hotkeys=True)
            self._refresh_images()
            self.image_tree.selection_set(value["id"])

    def _edit_image(self) -> None:
        group = self.image_groups.selected_group()
        entry = self._selected_image()
        if not group or not entry:
            return
        value = self._image_dialog(entry)
        if value:
            group["images"][group["images"].index(entry)] = self._store_image_entry(value)
            self._save_config(restart_hotkeys=True)
            self.store.cleanup_library(self.config)  # the replaced picture may be orphaned now
            self._refresh_images()
            self.image_tree.selection_set(value["id"])

    def _delete_image(self) -> None:
        group = self.image_groups.selected_group()
        entry = self._selected_image()
        if group and entry and messagebox.askyesno("画像の削除", f"「{entry['title']}」を削除しますか？"):
            group["images"].remove(entry)
            self._save_config(restart_hotkeys=True)
            self.store.cleanup_library(self.config)
            self._refresh_images()

    def _move_image(self, offset: int) -> None:
        self._move_library_item(self.image_groups.selected_group(), "images", self._selected_image(), offset, self.image_tree)
        self._refresh_images()

    def _paste_selected_image(self) -> None:
        entry = self._selected_image()
        if entry:
            self._paste_stored_image(entry.get("image_path", ""))

    def _copy_selected_image(self) -> None:
        entry = self._selected_image()
        if entry and self._copy_stored_image(entry.get("image_path", "")):
            self.status_var.set("画像をクリップボードへコピーしました")

    def _register_history_image(self, item: HistoryItem) -> None:
        """Copy a history picture into the library; the history entry keeps its own file."""
        try:
            with Image.open(self.store.image_path(item.image_path)) as source:
                picture = source.convert("RGBA")
        except (OSError, ValueError) as error:
            messagebox.showerror("画像", str(error), parent=self.root)
            return
        self.tabs.select(self.image_tab)
        self._add_image(picture)

    def _register_from_popup(self, item: HistoryItem) -> None:
        """The popup's "register" entries: the dialog needs a visible owner, so the settings window opens."""
        self.hide_call_window()
        self.show_settings_window()
        if item.kind == "image":
            self._register_history_image(item)
        else:
            self._register_history_color(item)

    def _paste_text(self, text: str) -> None:
        self._set_clipboard(text)
        if not self.config["settings"].get("auto_paste", True):
            self.status_var.set("クリップボードへセットしました")
            return
        self._send_paste()

    def _send_paste(self) -> None:
        if IS_MAC and not accessibility_trusted():
            self._warn_missing_accessibility()
            return
        # Closing the popup hands the keyboard back to the window it covered, and
        # leaves the rest of this app exactly where the user had it. A paste
        # started anywhere else has nothing to hand back to, so it falls back to
        # stepping aside entirely: withdrawing the windows does not move macOS'
        # idea of the active application, and the keystroke would land on nothing.
        if not self.hide_call_window():
            self.hide_window()
            hide_app()

        def send_paste() -> None:
            # Long enough for the window we just hid to hand focus back.
            time.sleep(0.12)
            try:
                if IS_MAC:
                    if not send_paste_keystroke():
                        self.events.put(("paste_failed", None))
                    return
                from pynput.keyboard import Controller, Key

                keyboard = Controller()
                with keyboard.pressed(Key.ctrl):
                    keyboard.press("v")
                    keyboard.release("v")
            except Exception:
                self.events.put(("paste_failed", None))

        threading.Thread(target=send_paste, daemon=True).start()

    def _warn_missing_accessibility(self) -> None:
        """Explain the paste that did not happen, once per run.

        The text is already on the clipboard by the time this runs, so the popup
        still closes and Cmd+V works; only the last step is missing.
        """
        self.hide_call_window()
        self._refresh_permissions()
        self.status_var.set(f"自動貼り付けにはアクセシビリティの許可が必要です。{ACCESSIBILITY_HINT}")
        if self._accessibility_warned:
            return
        self._accessibility_warned = True
        if messagebox.askyesno(
            "アクセシビリティの許可",
            "選んだ内容はクリップボードへ入れました。\n"
            "貼り付け先へ自動で貼り付けるには、macOSの許可が必要です。\n\n"
            f"{ACCESSIBILITY_HINT}\n\n許可の画面を開きますか？",
        ):
            open_settings_pane(ACCESSIBILITY_PANE)

    def toggle_fifo(self) -> None:
        self.stock_mode = "off" if self.stock_mode == "fifo" else "fifo"
        self.fifo_enabled = self.stock_mode != "off"
        self.last_fifo_value = None
        self._refresh_fifo()
        self.status_var.set("FIFOモードを有効にしました" if self.stock_mode == "fifo" else "ストックモードを停止しました")

    def toggle_lifo(self) -> None:
        self.stock_mode = "off" if self.stock_mode == "lifo" else "lifo"
        self.fifo_enabled = self.stock_mode != "off"
        self.last_fifo_value = None
        self._refresh_fifo()
        self.status_var.set("LIFOモードを有効にしました" if self.stock_mode == "lifo" else "ストックモードを停止しました")

    def stop_stock(self) -> None:
        self.stock_mode = "off"
        self.fifo_enabled = False
        self.last_fifo_value = None
        self._refresh_fifo()

    def _fifo_paste_hotkey(self) -> None:
        if not self.fifo_enabled:
            return
        text = self.fifo.pop(self.stock_mode)
        if text is None:
            return
        self.last_fifo_value = text
        self.ignore_fifo_text = text
        pyperclip.copy(text)
        self.events.put(("refresh_fifo", None))

    def undo_fifo(self) -> None:
        if self.last_fifo_value:
            self.fifo.undo(self.last_fifo_value, self.stock_mode)
            self.last_fifo_value = None
            self._refresh_fifo()

    def clear_fifo(self) -> None:
        self.fifo.clear()
        self.last_fifo_value = None
        self._refresh_fifo()

    def _selected_stock_index(self) -> int | None:
        selected = self.fifo_list.curselection()
        return selected[0] if selected else None

    def _add_stock(self) -> None:
        value = self._text_dialog("ストックへ追加")
        if value:
            self.fifo.append(value)
            self._refresh_fifo()

    def _edit_stock(self) -> None:
        index = self._selected_stock_index()
        if index is None:
            return
        value = self._text_dialog("ストックの編集", self.fifo.values()[index])
        if value:
            self.fifo.edit(index, value)
            self._refresh_fifo()

    def _delete_stock(self) -> None:
        index = self._selected_stock_index()
        if index is not None:
            self.fifo.remove(index)
            self._refresh_fifo()

    def _join_stock(self) -> None:
        value = self.fifo.joined()
        if value:
            self._set_clipboard(value)
            self.status_var.set("ストック全件を改行で連結しました")

    def _refresh_fifo(self) -> None:
        mode_label = {"off": "停止中", "fifo": "FIFO", "lifo": "LIFO"}[self.stock_mode]
        self.fifo_status_var.set(f"ストックモード: {mode_label}（{len(self.fifo)}件）")
        self.fifo_toggle_button.configure(text="FIFO停止" if self.stock_mode == "fifo" else "FIFO開始")
        self.fifo_list.delete(0, "end")
        for index, value in enumerate(self.fifo.values(), 1):
            self.fifo_list.insert("end", f"{index}. {value.replace(chr(10), ' ↵ ')[:180]}")

    def _refresh_transforms(self) -> None:
        if not hasattr(self, "transform_tree"):
            return
        for item in self.transform_tree.get_children():
            self.transform_tree.delete(item)
        labels = {
            "prefix_each_line": "各行に挿入",
            "surround_each_line": "各行の前後に挿入",
            "number_lines": "連番",
            "regex": "正規表現置換",
            "trim": "前後空白削除",
            "upper": "大文字化",
            "lower": "小文字化",
            "prefix_suffix": "全文の前後に挿入",
        }
        for rule in self.config.get("transforms", []):
            self.transform_tree.insert(
                "",
                "end",
                iid=rule["id"],
                values=(rule["name"], labels.get(rule.get("type"), rule.get("type")), rule.get("hotkey", ""), "有効" if rule.get("auto") else ""),
            )

    def _selected_transform(self) -> dict | None:
        selected = self.transform_tree.selection()
        if not selected:
            return None
        return next((rule for rule in self.config.get("transforms", []) if rule["id"] == selected[0]), None)

    def _transform_dialog(self, rule: dict | None = None) -> dict | None:
        dialog = tk.Toplevel(self.root)
        dialog.title("整形の編集" if rule else "整形の追加")
        dialog.geometry("520x360")
        dialog.transient(self.root)
        dialog.grab_set()
        name_var = tk.StringVar(value=rule.get("name", "") if rule else "")
        type_var = tk.StringVar(value=rule.get("type", "prefix_each_line") if rule else "prefix_each_line")
        hotkey_var = tk.StringVar(value=rule.get("hotkey", "") if rule else "")
        auto_var = tk.BooleanVar(value=bool(rule and rule.get("auto")))
        params = rule.get("params", {}) if rule else {}
        first_var = tk.StringVar()
        second_var = tk.StringVar()
        if type_var.get() == "prefix_each_line": first_var.set(str(params.get("prefix", "")))
        elif type_var.get() == "surround_each_line": first_var.set(str(params.get("before", ""))); second_var.set(str(params.get("after", "")))
        elif type_var.get() == "number_lines": first_var.set(str(params.get("width", 3))); second_var.set(str(params.get("separator", ": ")))
        elif type_var.get() == "regex": first_var.set(str(params.get("pattern", ""))); second_var.set(str(params.get("replacement", "")))
        elif type_var.get() == "prefix_suffix": first_var.set(str(params.get("prefix", ""))); second_var.set(str(params.get("suffix", "")))
        form = ttk.Frame(dialog, padding=14)
        form.pack(fill="both", expand=True)
        for row, (label, variable) in enumerate((("整形名", name_var), ("方法", type_var), ("値1", first_var), ("値2", second_var), ("ショートカット", hotkey_var))):
            ttk.Label(form, text=label).grid(row=row, column=0, sticky="w", pady=6, padx=(0, 12))
            if row == 1:
                widget = ttk.Combobox(form, textvariable=variable, state="readonly", values=("prefix_each_line", "surround_each_line", "number_lines", "regex", "trim", "upper", "lower", "prefix_suffix"))
            else:
                widget = ttk.Entry(form, textvariable=variable)
            widget.grid(row=row, column=1, sticky="ew", pady=6)
        form.columnconfigure(1, weight=1)
        ttk.Checkbutton(form, text="コピー時に自動適用", variable=auto_var).grid(row=5, column=1, sticky="w", pady=6)
        ttk.Label(form, text="値1/値2: 前/後、正規表現/置換、連番の桁数/区切りとして使います。", style="Muted.TLabel").grid(row=6, column=0, columnspan=2, sticky="w", pady=5)
        result: list[dict] = []

        def accept() -> None:
            kind = type_var.get()
            if not name_var.get().strip():
                return
            if kind == "prefix_each_line": values = {"prefix": first_var.get()}
            elif kind == "surround_each_line": values = {"before": first_var.get(), "after": second_var.get()}
            elif kind == "number_lines":
                try: values = {"start": 1, "width": int(first_var.get() or 3), "separator": second_var.get()}
                except ValueError:
                    messagebox.showerror("整形", "連番の桁数は数値で入力してください。", parent=dialog); return
            elif kind == "regex": values = {"pattern": first_var.get(), "replacement": second_var.get()}
            elif kind == "prefix_suffix": values = {"prefix": first_var.get(), "suffix": second_var.get()}
            else: values = {}
            candidate = {"id": rule.get("id", str(uuid4())) if rule else str(uuid4()), "name": name_var.get().strip(), "type": kind, "params": values, "hotkey": hotkey_var.get().strip().lower(), "auto": auto_var.get(), "enabled": True}
            try:
                apply_transform("テスト", candidate)
            except (ValueError, json.JSONDecodeError) as error:
                messagebox.showerror("整形", str(error), parent=dialog); return
            result.append(candidate)
            dialog.destroy()

        buttons = ttk.Frame(form)
        buttons.grid(row=7, column=0, columnspan=2, sticky="e", pady=(12, 0))
        ttk.Button(buttons, text="保存", command=accept).pack(side="right")
        ttk.Button(buttons, text="キャンセル", command=dialog.destroy).pack(side="right", padx=6)
        dialog.wait_window()
        return result[0] if result else None

    def _add_transform(self) -> None:
        value = self._transform_dialog()
        if value:
            self.config.setdefault("transforms", []).append(value)
            self._save_config(restart_hotkeys=True)
            self._refresh_transforms()

    def _edit_transform(self) -> None:
        rule = self._selected_transform()
        if not rule:
            return
        value = self._transform_dialog(rule)
        if value:
            index = self.config["transforms"].index(rule)
            self.config["transforms"][index] = value
            self._save_config(restart_hotkeys=True)
            self._refresh_transforms()

    def _delete_transform(self) -> None:
        rule = self._selected_transform()
        if rule and messagebox.askyesno("整形削除", f"「{rule['name']}」を削除しますか？"):
            self.config["transforms"].remove(rule)
            self._save_config(restart_hotkeys=True)
            self._refresh_transforms()

    def _apply_selected_transform(self) -> None:
        rule = self._selected_transform()
        item = self._selected_history()
        text = item.text if item else self._read_clipboard()
        if not rule or not text:
            self.status_var.set("整形と対象履歴を選択してください")
            return
        try:
            value = apply_transform(text, rule)
        except (ValueError, re.error) as error:
            messagebox.showerror("整形エラー", str(error))
            return
        self.history.add(value)
        self._set_clipboard(value)
        self.store.save_history(self.history)
        self._refresh_history()
        self.status_var.set(f"整形を適用しました: {rule['name']}")

    def _apply_transform_by_id(self, rule_id: str) -> None:
        rule = next((item for item in self.config.get("transforms", []) if item["id"] == rule_id), None)
        text = self._read_clipboard()
        if rule and text:
            try:
                self._set_clipboard(apply_transform(text, rule))
            except (ValueError, re.error) as error:
                self.status_var.set(f"整形エラー: {error}")

    def _load_settings_fields(self) -> None:
        settings = self.config["settings"]
        self.popup_hotkey_var.set(settings["popup_hotkey"])
        self.fifo_hotkey_var.set(settings["fifo_toggle_hotkey"])
        self.lifo_hotkey_var.set(settings["lifo_toggle_hotkey"])
        self.monitor_hotkey_var.set(settings["monitor_toggle_hotkey"])
        self.history_limit_var.set(str(settings["history_limit"]))
        self.font_size_var.set(str(settings.get("font_size", 11)))
        self.theme_var.set(settings.get("theme", "blue"))
        self.auto_paste_var.set(settings.get("auto_paste", True))
        self.save_history_var.set(settings.get("save_history", True))
        self.clear_history_var.set(settings.get("clear_history_on_exit", False))
        self.always_on_top_var.set(settings.get("always_on_top", False))
        self.start_minimized_var.set(settings.get("start_minimized", False))
        self.startup_var.set(startup_enabled())
        self.double_ctrl_var.set(settings.get("double_ctrl_popup", True))
        self.screenshot_hotkey_var.set(settings.get("screenshot_hotkey", ""))
        self.screenshot_delay_hotkey_var.set(settings.get("screenshot_delay_hotkey", ""))
        self.screenshot_delay_var.set(str(settings.get("screenshot_delay_seconds", 3)))
        self.screenshot_dir_var.set(settings.get("screenshot_save_dir", ""))
        self.screenshot_format_var.set(settings.get("screenshot_format", "png"))
        self.screenshot_copy_var.set(settings.get("screenshot_copy_after_save", True))
        self.screenshot_history_var.set(settings.get("screenshot_save_to_history", True))
        self._apply_appearance()

    def _save_settings(self) -> None:
        try:
            limit = int(self.history_limit_var.get())
            font_size = int(self.font_size_var.get())
            if not 1 <= limit <= 10000:
                raise ValueError
            if not 8 <= font_size <= 24:
                raise ValueError
        except ValueError:
            messagebox.showerror("設定", "履歴保持件数は1〜10000で入力してください。")
            return
        settings = self.config["settings"]
        settings["popup_hotkey"] = self.popup_hotkey_var.get().strip().lower()
        settings["fifo_toggle_hotkey"] = self.fifo_hotkey_var.get().strip().lower()
        settings["lifo_toggle_hotkey"] = self.lifo_hotkey_var.get().strip().lower()
        settings["monitor_toggle_hotkey"] = self.monitor_hotkey_var.get().strip().lower()
        settings["history_limit"] = limit
        settings["font_size"] = font_size
        settings["theme"] = self.theme_var.get()
        settings["auto_paste"] = self.auto_paste_var.get()
        settings["save_history"] = self.save_history_var.get()
        settings["clear_history_on_exit"] = self.clear_history_var.get()
        settings["always_on_top"] = self.always_on_top_var.get()
        settings["start_minimized"] = self.start_minimized_var.get()
        settings["double_ctrl_popup"] = self.double_ctrl_var.get()
        settings["screenshot_hotkey"] = self.screenshot_hotkey_var.get().strip().lower()
        settings["screenshot_delay_hotkey"] = self.screenshot_delay_hotkey_var.get().strip().lower()
        try:
            settings["screenshot_delay_seconds"] = max(1, min(60, int(self.screenshot_delay_var.get())))
        except ValueError:
            messagebox.showerror("設定", "ディレイ撮影の秒数は1〜60で入力してください。")
            return
        settings["screenshot_save_dir"] = self.screenshot_dir_var.get().strip()
        settings["screenshot_format"] = self.screenshot_format_var.get()
        settings["screenshot_copy_after_save"] = self.screenshot_copy_var.get()
        settings["screenshot_save_to_history"] = self.screenshot_history_var.get()
        self.history.limit = limit
        try:
            self._save_config(restart_hotkeys=True)
        except ValueError as error:
            messagebox.showerror("ショートカット", str(error))
            return
        set_startup(self.startup_var.get())
        if settings["save_history"]:
            self.store.save_history(self.history)
        self._apply_appearance()
        self.status_var.set("設定を保存しました")

    def _save_config(self, restart_hotkeys: bool = False) -> None:
        self.store.save_config(self.config)
        if restart_hotkeys:
            self._restart_hotkeys()

    def _hotkey_mappings(self) -> dict[str, object]:
        settings = self.config["settings"]
        mappings = {
            settings["fifo_toggle_hotkey"]: lambda: self.events.put(("toggle_fifo", None)),
            settings["lifo_toggle_hotkey"]: lambda: self.events.put(("toggle_lifo", None)),
            settings["monitor_toggle_hotkey"]: lambda: self.events.put(("toggle_monitor", None)),
            "primary+v": self._fifo_paste_hotkey,
            "primary+shift+z": lambda: self.events.put(("undo_fifo", None)),
        }
        if settings["popup_hotkey"].strip():
            mappings[settings["popup_hotkey"]] = lambda: self.events.put(("show", None))
        if str(settings.get("screenshot_hotkey", "")).strip():
            mappings[settings["screenshot_hotkey"]] = lambda: self.events.put(("screenshot", None))
        if str(settings.get("screenshot_delay_hotkey", "")).strip():
            mappings[settings["screenshot_delay_hotkey"]] = lambda: self.events.put(("screenshot_delayed", None))
        for group in self.config["groups"]:
            for snippet in group.get("snippets", []):
                if snippet.get("hotkey"):
                    text = snippet["text"]
                    mappings[snippet["hotkey"]] = lambda value=text: self.events.put(("paste_text", value))
        for group in self.config.get("color_groups", []):
            for color in group.get("colors", []):
                if color.get("hotkey"):
                    text = color["value"]
                    mappings[color["hotkey"]] = lambda value=text: self.events.put(("paste_text", value))
        for group in self.config.get("image_groups", []):
            for entry in group.get("images", []):
                if entry.get("hotkey") and entry.get("image_path"):
                    path = entry["image_path"]
                    mappings[entry["hotkey"]] = lambda value=path: self.events.put(("paste_image", value))
        for rule in self.config.get("transforms", []):
            if rule.get("hotkey"):
                rule_id = rule["id"]
                mappings[rule["hotkey"]] = lambda value=rule_id: self.events.put(("apply_transform", value))
        return mappings

    def _apply_appearance(self) -> None:
        settings = self.config["settings"]
        size = int(settings.get("font_size", 11))
        colors = THEMES.get(settings.get("theme", "blue"), THEMES["blue"])
        self.root.configure(background=colors["background"])
        if hasattr(self, "call_window"):
            self.call_window.configure(background=colors["background"])
        self.root.attributes("-topmost", bool(settings.get("always_on_top", False)))
        widgets = [getattr(self, name, None) for name in ("history_list", "group_list", "fifo_list", "call_history_list")]
        widgets += [panel.listbox for panel in (getattr(self, "color_groups", None), getattr(self, "image_groups", None)) if panel]
        widgets += [page.listbox for page in self.call_pages.values()]
        for widget in widgets:
            if widget:
                widget.configure(font=(UI_FONT_FAMILY, size), background=colors["background"], foreground=colors["foreground"], selectbackground=colors["accent"])
        style = ttk.Style()
        style.configure("Treeview", font=(UI_FONT_FAMILY, size), rowheight=max(24, size * 2 + 4))
        self._refresh_history()

    def _choose_screenshot_dir(self) -> None:
        folder = filedialog.askdirectory(title="スクリーンショットの保存先", parent=self.root)
        if folder:
            self.screenshot_dir_var.set(folder)

    def _export_snippets_csv(self) -> None:
        path = filedialog.asksaveasfilename(title="定型文CSV出力", defaultextension=".csv", filetypes=[("CSV", "*.csv")])
        if path:
            export_snippets_csv(self.config, Path(path))
            self.status_var.set(f"定型文CSVを出力しました: {path}")

    def _import_snippets_csv(self) -> None:
        path = filedialog.askopenfilename(title="定型文CSV取込", filetypes=[("CSV", "*.csv")])
        if not path:
            return
        clear = messagebox.askyesno("CSV取込", "既存の定型文をすべて消してから取り込みますか？\n「いいえ」で追加・更新します。")
        try:
            added, updated = import_snippets_csv(self.config, Path(path), clear)
            self._save_config(restart_hotkeys=True)
            self._refresh_groups()
            self.status_var.set(f"CSV取込完了: 追加{added}件、更新{updated}件")
        except (OSError, UnicodeError) as error:
            messagebox.showerror("CSV取込", str(error))

    def _backup_all(self) -> None:
        self.store.save_history(self.history)
        path = filedialog.asksaveasfilename(title="全バックアップ", defaultextension=".zip", filetypes=[("ZIP", "*.zip")])
        if path:
            create_backup(self.store.config_path, self.store.history_path, Path(path))
            self.status_var.set(f"バックアップを作成しました: {path}")

    def _restore_all(self) -> None:
        path = filedialog.askopenfilename(title="バックアップから復元", filetypes=[("ZIP", "*.zip")])
        if not path or not messagebox.askyesno("復元", "現在の設定と履歴を置き換えますか？"):
            return
        safety = self.store.data_dir / "before-restore.zip"
        create_backup(self.store.config_path, self.store.history_path, safety)
        try:
            restore_backup(Path(path), self.store.config_path, self.store.history_path)
            self.config = self.store.load_config()
            self.history = self.store.load_history(int(self.config["settings"]["history_limit"]))
            self._load_settings_fields()
            self._refresh_all()
            self._restart_hotkeys()
            self.status_var.set(f"復元しました（復元前: {safety}）")
        except (OSError, ValueError, json.JSONDecodeError) as error:
            messagebox.showerror("復元", str(error))

    def _restart_hotkeys(self) -> None:
        if self.hotkeys:
            self.hotkeys.stop()
        double_ctrl_callback = None
        if self.config["settings"].get("double_ctrl_popup", True):
            double_ctrl_callback = lambda: self.events.put(("show", None))
        self.hotkeys = GlobalHotkeyService(self._hotkey_mappings(), double_ctrl_callback)
        try:
            self.hotkeys.start()
        except Exception as error:
            self.status_var.set(f"ショートカットを開始できません: {error}")

    def _export_config(self) -> None:
        path = filedialog.asksaveasfilename(title="設定を書き出す", defaultextension=".json", filetypes=[("JSON", "*.json")])
        if path:
            self.store.export_config(Path(path))
            self.status_var.set(f"設定を書き出しました: {path}")

    def _import_config(self) -> None:
        path = filedialog.askopenfilename(title="設定を読み込む", filetypes=[("JSON", "*.json"), ("すべて", "*.*")])
        if not path:
            return
        try:
            self.config = self.store.import_config(Path(path))
            self._load_settings_fields()
            self._refresh_all()
            self._restart_hotkeys()
            self.status_var.set("設定を読み込みました（以前の設定は .bak に保存済み）")
        except (OSError, ValueError) as error:
            messagebox.showerror("読み込みエラー", str(error))

    def show_window(self) -> None:
        """Put the popup over the window the user is working in, and nothing else.

        The application underneath has to stay where it is, so the one thing that
        comes forward is this window: it is raised first and stays above the rest
        while it is open, and the application it interrupted is remembered so the
        keyboard can go straight back there when it closes.
        """
        self._refresh_call_window()
        self._previous_app = frontmost_app()
        self.call_window.deiconify()
        self.call_window.lift()
        self.call_window.attributes("-topmost", True)
        activate_app()
        if self._previous_app is not None:
            send_window_to_back(self.root.title())
        self.call_window.focus_force()
        self._focus_call_list()

    def show_settings_window(self) -> None:
        activate_app()
        self.root.deiconify()
        self.root.lift()
        self.root.attributes("-topmost", True)
        keep_top = bool(self.config["settings"].get("always_on_top", False))
        self.root.after(80, lambda: self.root.attributes("-topmost", keep_top))
        self.root.focus_force()

    def hide_call_window(self) -> bool:
        """Close the popup and give the keyboard back. True if it went somewhere."""
        self.call_window.attributes("-topmost", False)
        self.call_window.withdraw()
        app, self._previous_app = self._previous_app, None
        return activate_running_app(app)

    def hide_window(self) -> None:
        if hasattr(self, "call_window"):
            self.call_window.withdraw()
        self.root.withdraw()

    # ----- screenshots ------------------------------------------------------------

    def start_capture(self, delay_seconds: float = 0) -> None:
        """Freeze the screen and let the user pick a region, now or after a countdown.

        Pressing either screenshot hotkey while a countdown runs cancels it; that
        is the only way to abort one, since the countdown window never takes focus.
        """
        if self._countdown_window is not None:
            self._cancel_countdown()
            return
        if self.capturing:
            return  # a second overlay would fight the first one for the grab
        if IS_MAC and not screen_recording_allowed():
            # Without the permission the grab returns the desktop picture only,
            # which would look like a bug rather than a missing setting.
            request_screen_recording()
            self._refresh_permissions()
            self.status_var.set(f"スクリーンショットが撮れません。{SCREEN_RECORDING_HINT}")
            self.show_settings_window()
            return
        self.capturing = True
        if delay_seconds > 0:
            self._start_countdown(int(round(delay_seconds)))
            return
        self._begin_capture()

    def start_delayed_capture(self) -> None:
        try:
            delay = float(self.config["settings"].get("screenshot_delay_seconds", 3))
        except (TypeError, ValueError):
            delay = 3
        self.start_capture(max(1, delay))

    def _begin_capture(self, wait: bool = False) -> None:
        self._hidden_for_capture = [
            window
            for window in [self.root, getattr(self, "call_window", None)]
            if window is not None and window.winfo_viewable()
        ]
        for window in self._hidden_for_capture:
            window.withdraw()
        self.root.update_idletasks()
        # Give the compositor a moment to actually take our windows off screen,
        # otherwise they end up inside the screenshot. Nothing was hidden when
        # the request came from a hotkey, so no wait is needed then.
        self.root.after(120 if (self._hidden_for_capture or wait) else 0, self._run_selector)

    def _start_countdown(self, seconds: int) -> None:
        window = tk.Toplevel(self.root)
        window.overrideredirect(True)
        window.attributes("-topmost", True)
        window.configure(background="#111827")
        self._countdown_var = tk.StringVar()
        tk.Label(
            window, textvariable=self._countdown_var, background="#111827", foreground="#f8fafc",
            font=(UI_FONT_FAMILY, 28, "bold"), padx=24, pady=6,
        ).pack()
        tk.Label(
            window, text="もう一度ショートカットを押すと中止", background="#111827", foreground="#cbd5e1",
            font=(UI_FONT_FAMILY, 10), padx=12, pady=6,
        ).pack()
        window.update_idletasks()
        x = (self.root.winfo_screenwidth() - window.winfo_reqwidth()) // 2
        # macOS paints its menu bar over even a topmost window, so start below it.
        window.geometry(f"+{x}+{menu_bar_height() + 24}")
        self._countdown_window = window
        self._countdown_left = seconds
        self._tick_countdown()

    def _tick_countdown(self) -> None:
        if self._countdown_window is None:
            return
        if self._countdown_left <= 0:
            window, self._countdown_window = self._countdown_window, None
            window.destroy()
            self._begin_capture(wait=True)  # the countdown itself must not be in the shot
            return
        self._countdown_var.set(f"{self._countdown_left} 秒後に撮影")
        self.status_var.set(f"{self._countdown_left} 秒後にスクリーンショットを撮ります")
        self._countdown_left -= 1
        self._countdown_job = self.root.after(1000, self._tick_countdown)

    def _cancel_countdown(self) -> None:
        window, self._countdown_window = self._countdown_window, None
        if self._countdown_job is not None:
            try:
                self.root.after_cancel(self._countdown_job)
            except tk.TclError:
                pass
            self._countdown_job = None
        if window is not None:
            try:
                window.destroy()
            except tk.TclError:
                pass
        self.capturing = False
        self.status_var.set("ディレイ撮影を中止しました")

    def _run_selector(self) -> None:
        self._selector = RegionSelector(self.root, self._capture_done)
        try:
            self._selector.start()
        except Exception as error:  # noqa: BLE001 - never leave capture mode stuck
            self._capture_done(None)
            self.status_var.set(f"スクリーンショットを撮影できません: {error}")

    def _capture_done(self, image: Image.Image | None) -> None:
        self._selector = None
        self.capturing = False
        hidden, self._hidden_for_capture = self._hidden_for_capture, []
        if image is None:
            # Aborted: put things back the way they were.
            for window in hidden:
                try:
                    window.deiconify()
                except tk.TclError:
                    pass
            self.status_var.set("スクリーンショットを中止しました")
            return
        # Captured: only the editor should appear. Restoring the settings or the
        # popup here would raise them on top of it on every single shot.
        # The plain capture is usable straight away; the editor is optional.
        self._copy_capture(image)
        self.open_editor(image, f"スクリーンショット {image.width}×{image.height}")

    def _copy_capture(self, image: Image.Image) -> None:
        try:
            self._set_clipboard_image(write_export(self.store.data_dir, image))
        except (ImportError, OSError, RuntimeError, ValueError) as error:
            self.status_var.set(f"クリップボードへコピーできません: {error}")
            return
        if self.config["settings"].get("screenshot_save_to_history", True):
            relative, width, height = self.store.save_image(image)
            self.history.add_image(relative, width, height)
            if self.config["settings"].get("save_history", True):
                self.store.save_history(self.history)
            self._refresh_history()
        self.status_var.set(f"スクリーンショットをクリップボードへコピーしました（{image.width}×{image.height}）")

    def open_editor(self, image: Image.Image, title: str = "画像編集") -> ImageEditorWindow | None:
        try:
            editor = ImageEditorWindow(self, image, title)
        except (OSError, ValueError, tk.TclError) as error:
            messagebox.showerror("画像編集", str(error))
            return None
        self.editors.add(editor)
        editor.bring_to_front()
        return editor

    def edit_clipboard_image(self) -> None:
        image = read_clipboard_image()
        if image is None:
            self.status_var.set("クリップボードに画像がありません")
            return
        self.open_editor(image, "クリップボードの画像")

    def _edit_image_item(self, item, parent: tk.Misc | None = None) -> None:
        if item is None or item.kind != "image":
            messagebox.showinfo("画像編集", "画像の履歴を選んでください。", parent=parent or self.root)
            return
        try:
            with Image.open(self.store.image_path(item.image_path)) as source:
                image = source.convert("RGBA")
        except (OSError, ValueError) as error:
            messagebox.showerror("画像編集", str(error), parent=parent or self.root)
            return
        self.open_editor(image, f"履歴の画像 {item.width}×{item.height}")

    def _edit_selected_history_image(self) -> None:
        self._edit_image_item(self._selected_history())

    def _edit_call_history_image(self, _event=None) -> str:
        selected = self.call_history_list.curselection()
        if selected and selected[0] < len(self.call_history_items):
            self.hide_call_window()
            self._edit_image_item(self.call_history_items[selected[0]], self.call_window)
        return "break"

    def _call_history_menu(self, event) -> str:
        index = self._clicked_list_row(self.call_history_list, event.y)
        if index is None:
            return "break"
        self.call_history_list.selection_clear()
        self.call_history_list.selection_set(index)
        item = self.call_history_items[index]
        menu = tk.Menu(self.call_window, tearoff=0)
        menu.add_command(label="貼り付け", command=self._paste_call_history)
        if item.kind == "image":
            menu.add_command(label="この画像を編集 (Ctrl+E)", command=self._edit_call_history_image)
            menu.add_command(label="画像に登録...", command=lambda: self._register_from_popup(item))
        elif parse_hex_color(item.text) is not None:
            menu.add_command(label="色に登録...", command=lambda: self._register_from_popup(item))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
        return "break"

    def _call_page_menu(self, page: CallPage, event) -> str:
        index = self._clicked_list_row(page.listbox, event.y)
        if index is None:
            return "break"
        page.listbox.selection_clear(0, "end")
        page.listbox.selection_set(index)
        menu = tk.Menu(self.call_window, tearoff=0)
        menu.add_command(label="貼り付け", command=lambda: self._paste_call_page(page))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
        return "break"

    def toggle_monitor(self) -> None:
        self.monitor_enabled = not self.monitor_enabled
        self.monitor_status_var.set("監視中" if self.monitor_enabled else "監視停止中")
        self.status_var.set(f"クリップボード{self.monitor_status_var.get()}")

    def _start_tray(self) -> None:
        try:
            import pystray
            from PIL import Image

            image = Image.open(resource_path("assets/shinclipboard.png"))
            menu = pystray.Menu(
                pystray.MenuItem("貼り付け候補を開く", lambda *_: self.events.put(("show", None)), default=True),
                pystray.MenuItem("設定・編集を開く", lambda *_: self.events.put(("show_settings", None))),
                pystray.MenuItem("スクリーンショットを撮る", lambda *_: self.events.put(("screenshot", None))),
                pystray.MenuItem("数秒後にスクリーンショットを撮る", lambda *_: self.events.put(("screenshot_delayed", None))),
                pystray.MenuItem("クリップボードの画像を編集", lambda *_: self.events.put(("edit_clipboard_image", None))),
                pystray.MenuItem("FIFO 切替", lambda *_: self.events.put(("toggle_fifo", None))),
                pystray.MenuItem("LIFO 切替", lambda *_: self.events.put(("toggle_lifo", None))),
                pystray.MenuItem("クリップボード監視 切替", lambda *_: self.events.put(("toggle_monitor", None))),
                pystray.MenuItem("終了", lambda *_: self.events.put(("quit", None))),
            )
            self.tray = pystray.Icon("ShinClipboard", image, "ShinClipboard", menu)
            self.tray.run_detached()
        except Exception as error:
            self.status_var.set(f"タスクトレイを開始できません: {error}")

    def quit(self) -> None:
        self.closing = True
        if self._countdown_window is not None:
            self._cancel_countdown()
        if self._selector is not None:
            self._selector.cancel()
        for editor in list(self.editors):
            editor.close()
        cleanup_exports(self.store.data_dir, max_age=0)
        if self.config["settings"].get("clear_history_on_exit"):
            self.history.clear()
            self.store.save_history(self.history)
        elif self.config["settings"].get("save_history", True):
            self.store.save_history(self.history)
        if self.hotkeys:
            self.hotkeys.stop()
        if self.tray:
            self.tray.stop()
        self.root.destroy()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="ShinClipboard")
    parser.add_argument("--data-dir", type=Path, default=default_data_dir(), help="履歴などの保存先")
    parser.add_argument("--config", type=Path, help="共有する設定JSONのパス")
    return parser


def run(argv: list[str] | None = None) -> None:
    migrate_legacy_data_dir()
    migrate_legacy_startup()
    args = build_parser().parse_args(argv)
    instance = SingleInstance(args.data_dir)
    if not instance.acquire():
        instance.notify_existing()
        return
    try:
        root = tk.Tk()
        app = ShinClipboardApp(root, JsonStore(args.data_dir, args.config))
        instance.start_listener(lambda: app.events.put(("show", None)))
        root.mainloop()
    finally:
        instance.close()
