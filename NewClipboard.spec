# -*- mode: python ; coding: utf-8 -*-

import sys

app_icon = "assets/newclipboard.ico" if sys.platform == "win32" else "assets/newclipboard.png"

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
    name="NewClipboard",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    icon=app_icon,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
