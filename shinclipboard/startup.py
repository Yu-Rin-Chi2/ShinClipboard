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


# Passed by the autostart entry so a login brings the app up in the tray rather
# than putting the settings window in front of the desktop.
BACKGROUND_FLAG = "--background"


def _command() -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, BACKGROUND_FLAG]
    return [sys.executable, str((Path(__file__).resolve().parent.parent / "main.py")), BACKGROUND_FLAG]


def _registry_command(command: list[str]) -> str:
    return " ".join(f'"{part}"' for part in command)


def _registry_value(name: str) -> str | None:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run") as key:
            value, _kind = winreg.QueryValueEx(key, name)
        return str(value)
    except OSError:
        return None


def _registry_entry_exists(name: str) -> bool:
    return _registry_value(name) is not None


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
                winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, _registry_command(command))
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


def refresh_startup() -> bool:
    """Point an existing autostart entry at this executable, with today's arguments.

    The entry stores a path, so moving or replacing the exe, or an entry written
    by an older build, would otherwise keep launching something else or nothing
    at all. Returns True when the entry had to be rewritten.

    Only a built app does this: a run from source must not take over the entry
    that the installed exe relies on.
    """
    if not getattr(sys, "frozen", False):
        return False
    try:
        if os.name == "nt":
            current = _registry_value(APP_NAME)
            if current is None or current == _registry_command(_command()):
                return False
        else:
            target = _plist_path(BUNDLE_ID)
            if not target.exists():
                return False
            with target.open("rb") as stream:
                if plistlib.load(stream).get("ProgramArguments") == _command():
                    return False
        set_startup(True)
        return True
    except (OSError, ValueError, plistlib.InvalidFileException):
        return False


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
