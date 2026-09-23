"""Colours and the ttk theme: one palette for every window, light or dark.

On Windows the ttk widgets are drawn by Sun Valley (sv-ttk), the Windows 11
look, and the plain Tk widgets the app uses next to them - lists, the editor's
tool buttons and canvas - are painted from the same palette so the two never
disagree. macOS keeps Aqua, which already looks native there.
"""

from __future__ import annotations

import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk

from .platform_support import IS_MAC, IS_WINDOWS, UI_FONT_FAMILY

_LIGHT = {
    "mode": "light",
    "window": "#fafafa",  # what sv-ttk paints frames in
    "background": "#ffffff",
    "stripe": "#f3f5f8",
    "foreground": "#1c1c1c",
    "muted": "#6b7280",
    "border": "#e0e0e0",
    "canvas": "#e8e9ec",
    "tool_hover": "#eceef2",
    "tooltip": "#ffffff",
    "swatch_ring": "#c8c8c8",  # outline of an unchosen colour swatch
}
_DARK = {
    "mode": "dark",
    "window": "#1c1c1c",
    "background": "#232323",
    "stripe": "#2b2b2b",
    "foreground": "#f3f3f3",
    "muted": "#a3a3a3",
    "border": "#3a3a3a",
    "canvas": "#161616",
    "tool_hover": "#333333",
    "tooltip": "#2b2b2b",
    "swatch_ring": "#6b6b6b",
}

THEMES = {
    "blue": {**_LIGHT, "accent": "#005fb8", "tool_selected": "#dbe8f8"},
    "dark": {**_DARK, "accent": "#2f60d8", "tool_selected": "#26374f"},
    "green": {**_LIGHT, "accent": "#047857", "tool_selected": "#d9efe6"},
}
# Follows the OS: the dark palette while it is in dark mode, blue otherwise.
SYSTEM_THEME = "system"
THEME_NAMES = (SYSTEM_THEME, *THEMES)
THEME_LABELS = {
    SYSTEM_THEME: "OSに合わせる",
    "blue": "ライト（青）",
    "green": "ライト（緑）",
    "dark": "ダーク",
}

_PERSONALIZE_KEY = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
_DWMWA_USE_IMMERSIVE_DARK_MODE = 20


def theme_colors(name: str, dark: bool) -> dict[str, str]:
    """The palette for a theme setting, resolving "system" by the OS appearance."""
    if name == SYSTEM_THEME:
        name = "dark" if dark else "blue"
    return THEMES.get(name, THEMES["blue"])


def is_dark(colors: dict[str, str]) -> bool:
    return colors.get("mode") == "dark"


def windows_dark_mode() -> bool:
    """Whether Windows is set to dark mode for apps; False wherever it cannot say."""
    if not IS_WINDOWS:
        return False
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _PERSONALIZE_KEY) as key:
            value, _kind = winreg.QueryValueEx(key, "AppsUseLightTheme")
    except (ImportError, OSError):
        return False
    return value == 0


def apply_ttk_theme(root: tk.Tk, colors: dict[str, str]) -> None:
    """Switch the ttk theme to match the palette and restyle what the app names."""
    style = ttk.Style(root)
    if not IS_MAC:
        try:
            import sv_ttk

            sv_ttk.set_theme("dark" if is_dark(colors) else "light", root)
            # The switch recolours every plain Tk widget through tk_setPalette,
            # but only once the idle loop gets to <<ThemeChanged>>. Run it now,
            # or it lands after the lists have been painted and undoes them.
            root.update_idletasks()
            # Sun Valley draws in Segoe UI Variable, which has no Japanese; the
            # fallback Windows picks for the kana is a size off and looks pasted
            # in. The app's own face covers both.
            for name in ("SunValleyBodyFont", "SunValleyCaptionFont", "SunValleyBodyStrongFont"):
                tkfont.nametofont(name, root).configure(family=UI_FONT_FAMILY)
        except (ImportError, tk.TclError):
            if "vista" in style.theme_names():
                style.theme_use("vista")
    style.configure("Title.TLabel", font=(UI_FONT_FAMILY, 16, "bold"))
    style.configure("Subtitle.TLabel", font=(UI_FONT_FAMILY, 11, "bold"))
    style.configure("Muted.TLabel", foreground=colors["muted"])
    style.configure("Hint.TLabel", foreground=colors["muted"], font=(UI_FONT_FAMILY, 9))
    style.configure("Status.TLabel", foreground=colors["muted"])


def set_title_bar_dark(window: tk.Misc, dark: bool) -> None:
    """Ask Windows 10/11 to draw a toplevel's title bar dark or light."""
    if not IS_WINDOWS:
        return
    try:
        import ctypes

        window.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(window.winfo_id())
        value = ctypes.c_int(1 if dark else 0)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            hwnd, _DWMWA_USE_IMMERSIVE_DARK_MODE, ctypes.byref(value), ctypes.sizeof(value)
        )
    except (AttributeError, OSError, tk.TclError):
        pass


def style_list(widget: tk.Listbox | tk.Text, colors: dict[str, str]) -> None:
    """Flat, borderless list surface in the palette's colours.

    The border is drawn in the background colour, which is the only way to give
    a Listbox's rows some room from its edge; the thin outline is the highlight
    ring, which turns accent-coloured while the list has the focus.
    """
    options = {
        "background": colors["background"],
        "foreground": colors["foreground"],
        "selectbackground": colors["accent"],
        "selectforeground": "#ffffff",
        "relief": "flat",
        "borderwidth": 6,
        "highlightthickness": 1,
        "highlightbackground": colors["border"],
        "highlightcolor": colors["accent"],
    }
    if IS_MAC:
        # Aqua draws its own list frame; only the colours are the app's.
        options = {key: options[key] for key in ("background", "foreground", "selectbackground")}
    elif isinstance(widget, tk.Listbox):
        options["activestyle"] = "none"
        options["selectborderwidth"] = 0
    widget.configure(**options)
