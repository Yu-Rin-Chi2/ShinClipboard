from __future__ import annotations

import os
import plistlib
import sys
from pathlib import Path


APP_NAME = "ShinClipboard"
BUNDLE_ID = "com.shinclipboard.app"
# The app was called NewClipboard until 0.3.0. Autostart entries written by
# that build point at the old executable, so they are migrated, not left behind.
LEGACY_APP_NAME = "NewClipboard"
LEGACY_BUNDLE_ID = "com.newclipboard.app"


def _plist_path(bundle_id: str) -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{bundle_id}.plist"


def _command() -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable]
    return [sys.executable, str((Path(__file__).resolve().parent.parent / "main.py"))]


def _registry_entry_exists(name: str) -> bool:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run") as key:
            winreg.QueryValueEx(key, name)
        return True
    except OSError:
        return False


def startup_enabled() -> bool:
    if os.name == "nt":
        return _registry_entry_exists(APP_NAME)
    return _plist_path(BUNDLE_ID).exists()


def set_startup(enabled: bool) -> None:
    command = _command()
    if os.name == "nt":
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0,
            winreg.KEY_SET_VALUE,
        ) as key:
            if enabled:
                winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, " ".join(f'"{part}"' for part in command))
            else:
                try:
                    winreg.DeleteValue(key, APP_NAME)
                except FileNotFoundError:
                    pass
        return
    target = _plist_path(BUNDLE_ID)
    if enabled:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("wb") as stream:
            plistlib.dump({"Label": BUNDLE_ID, "ProgramArguments": command, "RunAtLoad": True}, stream)
    elif target.exists():
        target.unlink()


def migrate_legacy_startup() -> bool:
    """Move a NewClipboard autostart entry onto the current executable.

    Returns True when a legacy entry was found and replaced. Leaving it in
    place would keep launching the old build at login.
    """
    try:
        if os.name == "nt":
            if not _registry_entry_exists(LEGACY_APP_NAME):
                return False
            set_startup(True)
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Run",
                0,
                winreg.KEY_SET_VALUE,
            ) as key:
                try:
                    winreg.DeleteValue(key, LEGACY_APP_NAME)
                except FileNotFoundError:
                    pass
            return True
        legacy = _plist_path(LEGACY_BUNDLE_ID)
        if not legacy.exists():
            return False
        set_startup(True)
        legacy.unlink()
        return True
    except OSError:
        return False
