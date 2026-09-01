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
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk
from uuid import uuid4

import pyperclip

from .clipboard_images import clipboard_change_token, read_clipboard_image, write_clipboard_image
from .core import FifoQueue
from .hotkeys import GlobalHotkeyService
from .startup import set_startup, startup_enabled
from .storage import JsonStore, default_data_dir
from .transfer import create_backup, export_snippets_csv, import_snippets_csv, restore_backup
from .transforms import apply_enabled, apply_transform


THEMES = {
    "blue": {"background": "#ffffff", "stripe": "#e4eaf2", "accent": "#075dcc", "foreground": "#172033"},
    "dark": {"background": "#1f2937", "stripe": "#354154", "accent": "#0284c7", "foreground": "#f8fafc"},
    "green": {"background": "#ffffff", "stripe": "#e1eee8", "accent": "#047857", "foreground": "#15332b"},
}


def resource_path(relative: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    return base / relative


QUICK_KEYS = "1234567890abcdefghijklmnopqrstuvwxyz"


def quick_key(index: int) -> str:
    return QUICK_KEYS[index] if 0 <= index < len(QUICK_KEYS) else ""


class NewClipboardApp:
    POLL_MS = 350

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

        self._configure_window()
        self._build_ui()
        self._build_call_window()
        self._load_settings_fields()
        self._refresh_all()
        self._restart_hotkeys()
        self._start_tray()
        self.root.after(self.POLL_MS, self._poll)

        if self.config["settings"].get("start_minimized"):
            self.root.withdraw()

    def _configure_window(self) -> None:
        self.root.title("NewClipboard - 設定・編集")
        self.root.geometry("920x720")
        self.root.minsize(720, 500)
        self.root.protocol("WM_DELETE_WINDOW", self.hide_window)
        icon_png = resource_path("assets/newclipboard-512.png")
        if icon_png.exists():
            self.window_icon = tk.PhotoImage(file=icon_png)
            self.root.iconphoto(True, self.window_icon)
        style = ttk.Style()
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Title.TLabel", font=("Yu Gothic UI", 15, "bold"))
        style.configure("Muted.TLabel", foreground="#5f6b7a")

    def _build_ui(self) -> None:
        header = ttk.Frame(self.root, padding=(16, 14, 16, 6))
        header.pack(fill="x")
        ttk.Label(header, text="NewClipboard", style="Title.TLabel").pack(side="left")
        ttk.Label(header, text="クリップボード履歴と定型文", style="Muted.TLabel").pack(side="left", padx=14)
        ttk.Button(header, text="隠す", command=self.hide_window).pack(side="right")

        self.tabs = ttk.Notebook(self.root)
        self.tabs.pack(fill="both", expand=True, padx=14, pady=8)
        self.history_tab = ttk.Frame(self.tabs, padding=10)
        self.snippet_tab = ttk.Frame(self.tabs, padding=10)
        self.fifo_tab = ttk.Frame(self.tabs, padding=10)
        self.transform_tab = ttk.Frame(self.tabs, padding=10)
        self.settings_tab = ttk.Frame(self.tabs, padding=10)
        self.tabs.add(self.history_tab, text="履歴")
        self.tabs.add(self.snippet_tab, text="定型文")
        self.tabs.add(self.fifo_tab, text="FIFO")
        self.tabs.add(self.transform_tab, text="整形")
        self.tabs.add(self.settings_tab, text="設定")
        self._build_history_tab()
        self._build_snippet_tab()
        self._build_fifo_tab()
        self._build_transform_tab()
        self._build_settings_tab()
        ttk.Label(self.root, textvariable=self.status_var, relief="sunken", anchor="w", padding=(8, 4)).pack(fill="x")

    def _build_call_window(self) -> None:
        self.call_window = tk.Toplevel(self.root)
        self.call_window.title("NewClipboard - 貼り付け")
        self.call_window.geometry("520x340")
        self.call_window.minsize(340, 200)
        self.call_window.protocol("WM_DELETE_WINDOW", self.hide_call_window)
        self.call_window.bind("<Escape>", lambda _: self.hide_call_window())
        self.call_window.bind("<KeyPress>", self._quick_select)

        self.call_tabs = ttk.Notebook(self.call_window)
        self.call_tabs.pack(fill="both", expand=True, padx=6, pady=6)
        history_frame = ttk.Frame(self.call_tabs, padding=2)
        snippet_frame = ttk.Frame(self.call_tabs, padding=2)
        self.call_tabs.add(history_frame, text="履歴")
        self.call_tabs.add(snippet_frame, text="定型文")
        self.call_tabs.bind("<<NotebookTabChanged>>", self._focus_call_list)

        self.call_history_list = tk.Listbox(
            history_frame,
            activestyle="none",
            exportselection=False,
            font=("Yu Gothic UI", 11),
            selectborderwidth=0,
        )
        self.call_history_list.pack(fill="both", expand=True)
        self.call_history_list.bind("<ButtonRelease-1>", self._call_history_click)
        self.call_history_list.bind("<Return>", lambda _: self._paste_call_history())

        self.call_snippet_list = tk.Listbox(
            snippet_frame,
            activestyle="none",
            exportselection=False,
            font=("Yu Gothic UI", 11),
            selectborderwidth=0,
        )
        self.call_snippet_list.pack(fill="both", expand=True)
        self.call_snippet_list.bind("<ButtonRelease-1>", self._call_snippet_click)
        self.call_snippet_list.bind("<Return>", lambda _: self._paste_call_snippet())
        self.call_window.withdraw()

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
            font=("Yu Gothic UI", 11),
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
        ttk.Button(bar, text="改行ごとに展開", command=self._split_history_lines).pack(side="left", padx=6)
        ttk.Button(bar, text="選択を連結", command=self._join_history).pack(side="left")

    def _build_snippet_tab(self) -> None:
        outer = ttk.Panedwindow(self.snippet_tab, orient="horizontal")
        outer.pack(fill="both", expand=True)
        left = ttk.Frame(outer, padding=(0, 0, 8, 0))
        right = ttk.Frame(outer)
        outer.add(left, weight=1)
        outer.add(right, weight=3)
        ttk.Label(left, text="グループ").pack(anchor="w")
        self.group_list = tk.Listbox(left, exportselection=False, font=("Yu Gothic UI", 10))
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
        ttk.Label(top, textvariable=self.fifo_status_var, font=("Yu Gothic UI", 12, "bold")).pack(side="left")
        self.fifo_toggle_button = ttk.Button(top, text="FIFO開始", command=self.toggle_fifo)
        self.fifo_toggle_button.pack(side="right")
        ttk.Button(top, text="LIFO開始", command=self.toggle_lifo).pack(side="right", padx=6)
        ttk.Button(top, text="停止", command=self.stop_stock).pack(side="right")
        self.fifo_list = tk.Listbox(self.fifo_tab, font=("Yu Gothic UI", 11))
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
            ("画面表示ショートカット", self.popup_hotkey_var),
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
            ("Ctrlキー2回でも画面表示", self.double_ctrl_var),
        ]
        for index, (label, variable) in enumerate(checks):
            ttk.Checkbutton(advanced, text=label, variable=variable).grid(row=index // 2, column=index % 2, sticky="w", padx=(0, 25), pady=3)
        check_rows = (len(checks) + 1) // 2
        ttk.Label(advanced, text="フォントサイズ").grid(row=check_rows, column=0, sticky="w", pady=(8, 3))
        ttk.Spinbox(advanced, from_=8, to=24, textvariable=self.font_size_var, width=8).grid(row=check_rows + 1, column=0, sticky="w")
        ttk.Label(advanced, text="配色").grid(row=check_rows, column=1, sticky="w", pady=(8, 3))
        ttk.Combobox(advanced, textvariable=self.theme_var, values=list(THEMES), state="readonly", width=15).grid(row=check_rows + 1, column=1, sticky="w")

        portability = ttk.LabelFrame(self.settings_tab, text="CSV・バックアップ", padding=12)
        portability.pack(fill="x")
        ttk.Button(portability, text="定型文CSV出力", command=self._export_snippets_csv).pack(side="left")
        ttk.Button(portability, text="定型文CSV取込", command=self._import_snippets_csv).pack(side="left", padx=6)
        ttk.Button(portability, text="全バックアップ", command=self._backup_all).pack(side="left", padx=(14, 6))
        ttk.Button(portability, text="復元", command=self._restore_all).pack(side="left")

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
        text = self._read_clipboard()
        token = clipboard_change_token()
        changed = token != self.last_clipboard_token if token is not None else text != self.last_clipboard
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
                elif event == "apply_transform":
                    self._apply_transform_by_id(str(payload))
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

    def _refresh_all(self) -> None:
        self._refresh_history()
        self._refresh_groups()
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
        self._refresh_call_snippets()

    def _refresh_call_history(self) -> None:
        if not hasattr(self, "call_history_list"):
            return
        self.call_history_items = self.history.search()
        self.call_history_list.delete(0, "end")
        colors = THEMES.get(self.config["settings"].get("theme", "blue"), THEMES["blue"])
        for index, item in enumerate(self.call_history_items):
            if item.kind == "image":
                preview = f"[画像] {item.width}×{item.height}"
            else:
                preview = item.text.replace("\r", " ").replace("\n", " ↵ ")
            prefix = f"{quick_key(index)}: " if quick_key(index) else "   "
            self.call_history_list.insert("end", prefix + preview[:160])
            self.call_history_list.itemconfigure(
                index,
                background=colors["stripe"] if index % 2 else colors["background"],
                foreground=colors["foreground"],
                selectbackground=colors["accent"],
                selectforeground="#ffffff",
            )

    def _refresh_call_snippets(self) -> None:
        if not hasattr(self, "call_snippet_list"):
            return
        self.call_snippet_items = []
        self.call_snippet_list.delete(0, "end")
        colors = THEMES.get(self.config["settings"].get("theme", "blue"), THEMES["blue"])
        for group in self.config["groups"]:
            for snippet in group.get("snippets", []):
                index = len(self.call_snippet_items)
                self.call_snippet_items.append(snippet)
                preview = snippet["text"].replace("\r", " ").replace("\n", " ↵ ")
                prefix = f"{quick_key(index)}: " if quick_key(index) else "   "
                label = f"{prefix}[{group['name']}] {snippet['title']}  —  {preview[:110]}"
                self.call_snippet_list.insert("end", label)
                self.call_snippet_list.itemconfigure(
                    index,
                    background=colors["stripe"] if index % 2 else colors["background"],
                    foreground=colors["foreground"],
                    selectbackground=colors["accent"],
                    selectforeground="#ffffff",
                )

    def _selected_history(self):
        selected = self.history_list.curselection()
        return self.visible_history[selected[0]] if selected else None

    def _selected_histories(self):
        return [self.visible_history[index] for index in self.history_list.curselection()]

    def _history_right_click(self, event):
        index = self.history_list.nearest(event.y)
        if 0 <= index < self.history_list.size():
            self.history_list.selection_clear(0, "end")
            self.history_list.selection_set(index)
        return "break"

    def _snippet_right_click(self, event):
        item_id = self.snippet_tree.identify_row(event.y)
        if item_id:
            self.snippet_tree.selection_set(item_id)
        return "break"

    def _quick_select(self, event):
        focus = self.call_window.focus_get()
        if focus and focus.winfo_class() in {"Entry", "TEntry", "Text", "TCombobox", "TSpinbox"}:
            return None
        if event.state & 0x2000C:
            return None
        key = (event.char or "").lower()
        if key not in QUICK_KEYS:
            return None
        index = QUICK_KEYS.index(key)
        selected_tab = self.call_tabs.select()
        if selected_tab == self.call_tabs.tabs()[0] and index < len(self.call_history_items):
            self.call_history_list.selection_clear(0, "end")
            self.call_history_list.selection_set(index)
            self.call_history_list.see(index)
            self._paste_call_history()
            return "break"
        if selected_tab == self.call_tabs.tabs()[1] and index < len(self.call_snippet_items):
            self.call_snippet_list.selection_clear(0, "end")
            self.call_snippet_list.selection_set(index)
            self.call_snippet_list.see(index)
            self._paste_call_snippet()
            return "break"
        return None

    def _focus_call_list(self, _event=None) -> None:
        if not hasattr(self, "call_tabs"):
            return
        widget = self.call_history_list if self.call_tabs.index("current") == 0 else self.call_snippet_list
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
        index = self._clicked_list_row(self.call_history_list, event.y)
        if index is not None:
            self.call_history_list.selection_clear(0, "end")
            self.call_history_list.selection_set(index)
            self.call_window.after_idle(self._paste_call_history)

    def _call_snippet_click(self, event) -> None:
        index = self._clicked_list_row(self.call_snippet_list, event.y)
        if index is not None:
            self.call_snippet_list.selection_clear(0, "end")
            self.call_snippet_list.selection_set(index)
            self.call_window.after_idle(self._paste_call_snippet)

    def _paste_call_history(self) -> None:
        selected = self.call_history_list.curselection()
        if not selected:
            return
        item = self.call_history_items[selected[0]]
        if item.kind == "image":
            try:
                write_clipboard_image(self.store.image_path(item.image_path))
            except (ImportError, OSError, RuntimeError, ValueError) as error:
                messagebox.showerror("画像履歴", str(error), parent=self.call_window)
                return
            if self.config["settings"].get("auto_paste", True):
                self._send_paste()
        else:
            self._paste_text(item.text)

    def _paste_call_snippet(self) -> None:
        selected = self.call_snippet_list.curselection()
        if selected:
            self._paste_text(self.call_snippet_items[selected[0]]["text"])

    def _copy_selected_history(self) -> None:
        item = self._selected_history()
        if item:
            if item.kind == "image":
                try:
                    write_clipboard_image(self.store.image_path(item.image_path))
                    self.status_var.set("画像をクリップボードへコピーしました")
                except (ImportError, OSError, RuntimeError, ValueError) as error:
                    messagebox.showerror("画像履歴", str(error))
            else:
                self._set_clipboard(item.text)
                self.status_var.set("履歴をクリップボードへコピーしました")

    def _paste_selected_history(self) -> None:
        item = self._selected_history()
        if item:
            if item.kind == "image":
                try:
                    write_clipboard_image(self.store.image_path(item.image_path))
                except (ImportError, OSError, RuntimeError, ValueError) as error:
                    messagebox.showerror("画像履歴", str(error))
                    return
                if self.config["settings"].get("auto_paste", True):
                    self._send_paste()
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

    def _text_dialog(self, title: str, initial: str = "") -> str | None:
        dialog = tk.Toplevel(self.root)
        dialog.title(title)
        dialog.geometry("560x360")
        dialog.transient(self.root)
        dialog.grab_set()
        editor = tk.Text(dialog, wrap="word", font=("Yu Gothic UI", int(self.config["settings"].get("font_size", 11))))
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
        content = tk.Text(dialog, wrap="word", height=10, font=("Yu Gothic UI", 10))
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

    def _paste_text(self, text: str) -> None:
        self._set_clipboard(text)
        if not self.config["settings"].get("auto_paste", True):
            self.status_var.set("クリップボードへセットしました")
            return
        self._send_paste()

    def _send_paste(self) -> None:
        self.hide_window()

        def send_paste() -> None:
            time.sleep(0.12)
            try:
                from pynput.keyboard import Controller, Key

                keyboard = Controller()
                modifier = Key.cmd if platform.system() == "Darwin" else Key.ctrl
                with keyboard.pressed(modifier):
                    keyboard.press("v")
                    keyboard.release("v")
            except Exception:
                pass

        threading.Thread(target=send_paste, daemon=True).start()

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
        if settings["popup_hotkey"] != "ctrl+space":
            mappings[settings["popup_hotkey"]] = lambda: self.events.put(("show", None))
        for group in self.config["groups"]:
            for snippet in group.get("snippets", []):
                if snippet.get("hotkey"):
                    text = snippet["text"]
                    mappings[snippet["hotkey"]] = lambda value=text: self.events.put(("paste_text", value))
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
        for widget_name in ("history_list", "group_list", "fifo_list", "call_history_list", "call_snippet_list"):
            widget = getattr(self, widget_name, None)
            if widget:
                widget.configure(font=("Yu Gothic UI", size), background=colors["background"], foreground=colors["foreground"], selectbackground=colors["accent"])
        style = ttk.Style()
        style.configure("Treeview", font=("Yu Gothic UI", size), rowheight=max(24, size * 2 + 4))
        self._refresh_history()

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
        ctrl_space_callback = None
        if self.config["settings"].get("popup_hotkey") == "ctrl+space":
            ctrl_space_callback = lambda: self.events.put(("show", None))
        self.hotkeys = GlobalHotkeyService(self._hotkey_mappings(), double_ctrl_callback, ctrl_space_callback)
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
        self._refresh_call_window()
        self.call_window.deiconify()
        self.call_window.lift()
        self.call_window.attributes("-topmost", True)
        self.call_window.after(80, lambda: self.call_window.attributes("-topmost", False))
        self.call_window.focus_force()
        self._focus_call_list()

    def show_settings_window(self) -> None:
        self.root.deiconify()
        self.root.lift()
        self.root.attributes("-topmost", True)
        keep_top = bool(self.config["settings"].get("always_on_top", False))
        self.root.after(80, lambda: self.root.attributes("-topmost", keep_top))
        self.root.focus_force()

    def hide_call_window(self) -> None:
        self.call_window.withdraw()

    def hide_window(self) -> None:
        if hasattr(self, "call_window"):
            self.call_window.withdraw()
        self.root.withdraw()

    def toggle_monitor(self) -> None:
        self.monitor_enabled = not self.monitor_enabled
        self.monitor_status_var.set("監視中" if self.monitor_enabled else "監視停止中")
        self.status_var.set(f"クリップボード{self.monitor_status_var.get()}")

    def _start_tray(self) -> None:
        try:
            import pystray
            from PIL import Image

            image = Image.open(resource_path("assets/newclipboard.png"))
            menu = pystray.Menu(
                pystray.MenuItem("貼り付け候補を開く", lambda *_: self.events.put(("show", None)), default=True),
                pystray.MenuItem("設定・編集を開く", lambda *_: self.events.put(("show_settings", None))),
                pystray.MenuItem("FIFO 切替", lambda *_: self.events.put(("toggle_fifo", None))),
                pystray.MenuItem("LIFO 切替", lambda *_: self.events.put(("toggle_lifo", None))),
                pystray.MenuItem("クリップボード監視 切替", lambda *_: self.events.put(("toggle_monitor", None))),
                pystray.MenuItem("終了", lambda *_: self.events.put(("quit", None))),
            )
            self.tray = pystray.Icon("NewClipboard", image, "NewClipboard", menu)
            self.tray.run_detached()
        except Exception as error:
            self.status_var.set(f"タスクトレイを開始できません: {error}")

    def quit(self) -> None:
        self.closing = True
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
    parser = argparse.ArgumentParser(description="NewClipboard")
    parser.add_argument("--data-dir", type=Path, default=default_data_dir(), help="履歴などの保存先")
    parser.add_argument("--config", type=Path, help="共有する設定JSONのパス")
    return parser


def run(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    root = tk.Tk()
    NewClipboardApp(root, JsonStore(args.data_dir, args.config))
    root.mainloop()
