# -*- mode: python ; coding: utf-8 -*-

import os
import re
import subprocess
import sys
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"

APP_NAME = "ShinClipboard"
BUNDLE_ID = "com.shinclipboard.app"
COPYRIGHT = "Copyright (c) 2026 Yu-Rin-Chi2. MIT License."
DESCRIPTION = "ShinClipboard - クリップボード履歴・定型文管理"

if IS_WINDOWS:
    app_icon = "assets/shinclipboard.ico"
elif IS_MAC:
    app_icon = "assets/shinclipboard.icns"
else:
    app_icon = "assets/shinclipboard.png"


def _version() -> str:
    """Read __version__ so the build metadata never drifts from the package."""
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


def _codesign_identity():
    """The identity to sign the macOS bundle with, or None for an ad-hoc signature.

    macOS records the Accessibility and Screen Recording grants against the
    app's code signature. An ad-hoc signature is a hash of the binary, so every
    rebuild is a stranger that has to be granted all over again, while the
    toggles in System Settings keep showing it as allowed. A certificate gives
    the app the same identity across builds. `SHINCLIPBOARD_CODESIGN_IDENTITY`
    names one explicitly ("-" forces ad-hoc); otherwise the first valid
    code-signing identity in the keychain is used, and having none falls back
    to ad-hoc.
    """
    if not IS_MAC:
        return None
    explicit = os.environ.get("SHINCLIPBOARD_CODESIGN_IDENTITY", "").strip()
    if explicit:
        return None if explicit == "-" else explicit
    try:
        listing = subprocess.run(
            ["security", "find-identity", "-v", "-p", "codesigning"],
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r'^\s*\d+\)\s+[0-9A-F]{40}\s+"([^"]+)"', listing, re.MULTILINE)
    return match.group(1) if match else None


def _info_plist(version: str) -> dict:
    """What Finder, the menu bar and the permission prompts read about the app."""
    return {
        "CFBundleName": APP_NAME,
        "CFBundleDisplayName": APP_NAME,
        "CFBundleIdentifier": BUNDLE_ID,
        "CFBundleShortVersionString": version,
        "CFBundleVersion": version,
        "NSHumanReadableCopyright": COPYRIGHT,
        "NSHighResolutionCapable": True,
        "LSMinimumSystemVersion": "11.0",
        # The app lives in the menu bar. A Dock icon and a menu bar of its own
        # would only compete with the application the user is really typing in,
        # and the popup is summoned by a shortcut rather than by clicking a tile.
        "LSUIElement": True,
    }


version = _version()
# PyInstaller drops the resource on non-Windows targets, but the import inside is
# Windows-only, so it is never reached when building the macOS app.
version_resource = _version_resource(version) if IS_WINDOWS else None
codesign_identity = _codesign_identity()
macos_entitlements = str(Path(SPECPATH, "assets", "macos-entitlements.plist")) if IS_MAC else None
if IS_MAC:
    print(f"macOS code signing identity: {codesign_identity or 'ad-hoc (permissions will not survive a rebuild)'}")

hidden_imports = []
if IS_MAC:
    # The tray and the hotkeys pick their backend at run time, so nothing in the
    # source tree names these modules for PyInstaller to follow.
    hidden_imports += ["pystray._darwin", "pynput.keyboard._darwin", "pynput.mouse._darwin"]

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=[],
    datas=[("assets", "assets")],
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

if IS_MAC:
    # A .app is a directory, so the binaries stay beside the executable instead
    # of being packed into it. That keeps launches fast and, more importantly,
    # gives macOS a stable path to hang the Accessibility and Screen Recording
    # permissions on - a self-extracting build asks for them again every run.
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name=APP_NAME,
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,  # UPX corrupts Mach-O binaries
        console=False,
        icon=app_icon,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=codesign_identity,
        entitlements_file=macos_entitlements,
    )
    collected = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name=APP_NAME)
    app = BUNDLE(
        collected,
        name=f"{APP_NAME}.app",
        icon=app_icon,
        bundle_identifier=BUNDLE_ID,
        version=version,
        info_plist=_info_plist(version),
        codesign_identity=codesign_identity,
        entitlements_file=macos_entitlements,
    )
else:
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        [],
        name=APP_NAME,
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
