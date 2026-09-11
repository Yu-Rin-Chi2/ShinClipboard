"""The faces a text annotation can be drawn in.

PIL draws the export and needs a file; Tk draws the preview and needs a family
name. Both come out of one scan of the font folders, keyed by the family each
file declares, which is also the name Tk knows the face by on macOS and
Windows. Only faces that draw Japanese are kept: PIL has no per-glyph fallback,
so a Latin-only face would export every kana as a box.
"""

from __future__ import annotations

import json
import os
import platform
import threading
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from pathlib import Path

from PIL import ImageFont


FONT_SUFFIXES = (".ttf", ".ttc", ".otf")
MAX_FACES_PER_FILE = 32
# A face is only trusted with Japanese once it draws these. The prolonged sound
# mark is the one that Korean and Chinese faces most often leave out.
JAPANESE_PROBE = "ーあ漢"
# When a family ships several weights, the one offered under its name. Tk's
# "normal" weight resolves to the same face for the families checked, so the
# preview and the export stay the same weight.
PREFERRED_STYLES = ("regular", "w4", "w3", "medium", "normal", "book", "roman", "w5")
CACHE_VERSION = 1


@dataclass(frozen=True)
class FontFace:
    family: str
    style: str
    path: str
    index: int = 0  # face within a .ttc collection

    def load(self, size: int) -> ImageFont.FreeTypeFont:
        return ImageFont.truetype(self.path, max(1, int(size)), index=self.index)


def has_glyphs(font: ImageFont.FreeTypeFont, text: str, unknown: bool = True) -> bool:
    """Whether the face draws every character of `text` rather than its missing-glyph box.

    Pillow has no glyph lookup, so each character is rasterised and compared with
    what the font draws for a code point no font assigns. `unknown` is the answer
    when the face cannot be rasterised at all.
    """
    try:
        missing = _glyph_bitmap(font, "\U0010ffff")
        return all(_glyph_bitmap(font, char) != missing for char in text)
    except (OSError, ValueError, TypeError, AttributeError):
        return unknown


def _glyph_bitmap(font: ImageFont.FreeTypeFont, char: str) -> tuple[tuple[int, int], bytes]:
    mask = font.getmask(char)
    return (mask.size, bytes(mask))


def font_directories() -> list[Path]:
    """Where this platform keeps its fonts; only the folders that exist."""
    system = platform.system()
    if system == "Darwin":
        roots = [
            "/System/Library/Fonts",  # Supplemental sits inside
            "/Library/Fonts",
            "~/Library/Fonts",
            # The Japanese faces macOS installs on demand (Yu Gothic, Klee,
            # Tsukushi, Toppan Bunkyu, ...) are kept in an asset store.
            "/System/Library/AssetsV2/com_apple_MobileAsset_Font7",
        ]
    elif system == "Windows":
        roots = [
            os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"),
            os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "Windows", "Fonts"),
        ]
    else:
        roots = ["/usr/share/fonts", "/usr/local/share/fonts", "~/.fonts", "~/.local/share/fonts"]
    return [Path(root).expanduser() for root in roots if root and Path(root).expanduser().is_dir()]


def scan_fonts(directories: Iterable[Path] | None = None) -> list[FontFace]:
    """Every Japanese-capable face under the given folders (or the platform's)."""
    faces: list[FontFace] = []
    for directory in font_directories() if directories is None else directories:
        for path in sorted(Path(directory).rglob("*")):
            if path.suffix.lower() in FONT_SUFFIXES and path.is_file():
                faces.extend(faces_in(path))
    return faces


def faces_in(path: Path) -> list[FontFace]:
    faces: list[FontFace] = []
    for index in range(MAX_FACES_PER_FILE):
        try:
            font = ImageFont.truetype(str(path), 20, index=index)
        except (OSError, ValueError):
            break  # past the last face of a collection, or not a font at all
        family, style = font.getname()
        # macOS hides its internal faces behind a leading dot. A face FreeType
        # cannot rasterise is left out rather than offered and then exported blank.
        if family and not family.startswith(".") and has_glyphs(font, JAPANESE_PROBE, unknown=False):
            faces.append(FontFace(family, style or "Regular", str(path), index))
    return faces


class FontCatalog:
    """The faces on this machine, found once and remembered on disk.

    The scan opens every font file and takes a few seconds, so it runs in a
    background thread the first time and comes from the cache file after that.
    The cache is dropped when a font folder's modification time changes, which
    is what installing or removing a font does to it.
    """

    def __init__(self, faces: Iterable[FontFace] | None = None):
        self._faces: list[FontFace] | None = None
        self._by_family: dict[str, FontFace] = {}
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        if faces is not None:
            self._install(list(faces))

    @property
    def ready(self) -> bool:
        return self._faces is not None

    def faces(self) -> list[FontFace]:
        return list(self._faces or [])

    def families(self) -> list[str]:
        return sorted({face.family for face in self.faces()}, key=str.lower)

    def face(self, family: str) -> FontFace | None:
        """The face offered under a family name, or None while unknown or not yet scanned."""
        return self._by_family.get(family.strip().lower()) if family else None

    def start(self, cache_path: Path | None = None) -> None:
        """Load in the background; `ready` turns true once the list can be used."""
        if self.ready or (self._thread is not None and self._thread.is_alive()):
            return
        self._thread = threading.Thread(
            target=self._load_quietly, args=(cache_path,), daemon=True, name="font-scan"
        )
        self._thread.start()

    def load(self, cache_path: Path | None = None, directories: Iterable[Path] | None = None) -> list[FontFace]:
        """Load now, from the cache when it still matches the folders, else by scanning."""
        directories = font_directories() if directories is None else [Path(d) for d in directories]
        signature = _signature(directories)
        faces = _read_cache(cache_path, signature) if cache_path else None
        if faces is None:
            faces = scan_fonts(directories)
            if cache_path:
                _write_cache(cache_path, signature, faces)
        self._install(faces)
        return faces

    def _load_quietly(self, cache_path: Path | None) -> None:
        try:
            self.load(cache_path)
        except Exception:  # noqa: BLE001 - a failed scan means "no choice", never a crash
            self._install([])

    def _install(self, faces: list[FontFace]) -> None:
        by_family: dict[str, FontFace] = {}
        for face in faces:
            key = face.family.lower()
            if key not in by_family or _preference(face) < _preference(by_family[key]):
                by_family[key] = face
        with self._lock:
            self._faces = faces
            self._by_family = by_family


def _preference(face: FontFace) -> int:
    style = face.style.lower()
    return PREFERRED_STYLES.index(style) if style in PREFERRED_STYLES else len(PREFERRED_STYLES)


def _signature(directories: list[Path]) -> list[list]:
    signature = []
    for directory in directories:
        try:
            signature.append([str(directory), os.stat(directory).st_mtime_ns])
        except OSError:
            continue
    return signature


def _read_cache(path: Path, signature: list[list]) -> list[FontFace] | None:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if data.get("version") != CACHE_VERSION or data.get("signature") != signature:
            return None
        return [FontFace(**entry) for entry in data["faces"]]
    except (OSError, ValueError, TypeError, KeyError):
        return None


def _write_cache(path: Path, signature: list[list], faces: list[FontFace]) -> None:
    payload = {"version": CACHE_VERSION, "signature": signature, "faces": [asdict(face) for face in faces]}
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass  # the scan simply runs again next time


# The one catalog the renderer and the editor share.
CATALOG = FontCatalog()
