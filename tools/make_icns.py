"""Build assets/shinclipboard.icns from the master PNG.

macOS lays app icons out on a fixed grid: the artwork occupies an 824pt square
inside a 1024pt canvas, so a picture that fills its canvas edge to edge stands
visibly larger than everything beside it in the Dock. The master PNG is drawn for
Windows, where icons do fill their canvas, so the inset is applied here instead
of being baked into the file both platforms share.

Re-run after changing assets/shinclipboard.png:

    python tools/make_icns.py
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image


ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "assets" / "shinclipboard.png"
TARGET = ROOT / "assets" / "shinclipboard.icns"

CANVAS = 1024
BODY = 824  # the Big Sur icon grid: an 824pt body centred in a 1024pt canvas
# Every size macOS asks for, at both resolutions.
VARIANTS = (
    (16, "icon_16x16.png"),
    (32, "icon_16x16@2x.png"),
    (32, "icon_32x32.png"),
    (64, "icon_32x32@2x.png"),
    (128, "icon_128x128.png"),
    (256, "icon_128x128@2x.png"),
    (256, "icon_256x256.png"),
    (512, "icon_256x256@2x.png"),
    (512, "icon_512x512.png"),
    (1024, "icon_512x512@2x.png"),
)


def build_master() -> Image.Image:
    """The artwork trimmed to its own edges, then re-inset onto the macOS grid."""
    with Image.open(SOURCE) as opened:
        source = opened.convert("RGBA")
    bounds = source.getbbox()
    if bounds:
        source = source.crop(bounds)
    body = source.resize((BODY, BODY), Image.Resampling.LANCZOS)
    canvas = Image.new("RGBA", (CANVAS, CANVAS), (0, 0, 0, 0))
    canvas.paste(body, ((CANVAS - BODY) // 2, (CANVAS - BODY) // 2))
    return canvas


def main() -> int:
    if shutil.which("iconutil") is None:
        print("iconutil が見つかりません。macOS で実行してください。", file=sys.stderr)
        return 1
    if not SOURCE.exists():
        print(f"元画像がありません: {SOURCE}", file=sys.stderr)
        return 1
    master = build_master()
    with tempfile.TemporaryDirectory() as workspace:
        iconset = Path(workspace) / "shinclipboard.iconset"
        iconset.mkdir()
        for size, name in VARIANTS:
            master.resize((size, size), Image.Resampling.LANCZOS).save(iconset / name)
        result = subprocess.run(
            ["iconutil", "--convert", "icns", str(iconset), "--output", str(TARGET)],
            capture_output=True,
            text=True,
        )
    if result.returncode != 0:
        print(result.stderr.strip() or "iconutil に失敗しました。", file=sys.stderr)
        return result.returncode
    print(f"{TARGET.relative_to(ROOT)} を書き出しました（{TARGET.stat().st_size:,} バイト）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
