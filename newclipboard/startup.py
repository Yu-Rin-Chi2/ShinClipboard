from __future__ import annotations

import os
import plistlib
import sys
from pathlib import Path


APP_NAME = "NewClipboard"


def _command() -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable]
    return [sys.executable, str((Path(__file__).resolve().parent.parent / "main.py"))]


def startup_enabled() -> bool:
    if os.name == "nt":
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run") as key:
                winreg.QueryValueEx(key, APP_NAME)
            return True
        except OSError:
            return False
    return (Path.home() / "Library" / "LaunchAgents" / "com.newclipboard.app.plist").exists()


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
    target = Path.home() / "Library" / "LaunchAgents" / "com.newclipboard.app.plist"
    if enabled:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("wb") as stream:
            plistlib.dump({"Label": "com.newclipboard.app", "ProgramArguments": command, "RunAtLoad": True}, stream)
    elif target.exists():
        target.unlink()
