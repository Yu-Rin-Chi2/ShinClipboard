# -*- mode: python ; coding: utf-8 -*-

import re
import sys
from pathlib import Path

app_icon = "assets/shinclipboard.ico" if sys.platform == "win32" else "assets/shinclipboard.png"

APP_NAME = "ShinClipboard"
COPYRIGHT = "Copyright (c) 2026 Yu-Rin-Chi2. MIT License."
DESCRIPTION = "ShinClipboard - クリップボード履歴・定型文管理"


def _version() -> str:
    """Read __version__ so the exe metadata never drifts from the package."""
    source = Path(SPECPATH, "shinclipboard", "__init__.py").read_text(encoding="utf-8")
    return re.search(r'__version__\s*=\s*"([^"]+)"', source).group(1)


def _version_resource(version: str):
    """Build the Windows version resource shown in the file properties.

    Without it the exe has an empty publisher and product name, which makes an
    unsigned download look more suspicious than it needs to.
    """
    from PyInstaller.utils.win32.versioninfo import (
        FixedFileInfo,
        StringFileInfo,
        StringStruct,
        StringTable,
        VarFileInfo,
        VarStruct,
        VSVersionInfo,
    )

    parts = tuple(int(piece) for piece in version.split("."))
    numeric = (parts + (0, 0, 0, 0))[:4]
    return VSVersionInfo(
        ffi=FixedFileInfo(filevers=numeric, prodvers=numeric, mask=0x3F, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0),
        kids=[
            StringFileInfo([
                StringTable("040904B0", [
                    StringStruct("CompanyName", "Yu-Rin-Chi2"),
                    StringStruct("FileDescription", DESCRIPTION),
                    StringStruct("FileVersion", version),
                    StringStruct("InternalName", APP_NAME),
                    StringStruct("LegalCopyright", COPYRIGHT),
                    StringStruct("OriginalFilename", f"{APP_NAME}.exe"),
                    StringStruct("ProductName", APP_NAME),
                    StringStruct("ProductVersion", version),
                ]),
            ]),
            VarFileInfo([VarStruct("Translation", [0x0409, 1200])]),
        ],
    )


# PyInstaller drops the resource on non-Windows targets, but the import above is
# Windows-only, so it is never reached when building the macOS app.
version_resource = _version_resource(_version()) if sys.platform == "win32" else None

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=[],
    datas=[("assets", "assets")],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="ShinClipboard",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    icon=app_icon,
    version=version_resource,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
