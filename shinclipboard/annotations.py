from __future__ import annotations

import platform
from copy import deepcopy
from dataclasses import dataclass, field
from functools import lru_cache
from math import atan2, cos, hypot, radians, sin
from uuid import uuid4

from PIL import Image, ImageColor, ImageDraw, ImageFilter, ImageFont

from .fonts import CATALOG, JAPANESE_PROBE, FontFace, has_glyphs  # noqa: F401 - has_glyphs is re-exported


Point = tuple[float, float]
Rect = tuple[int, int, int, int]

# Shapes drawn as outlines on top of the picture. They stay editable forever.
VECTOR_KINDS = ("arrow", "rect", "ellipse", "freehand", "text", "number")
# Shapes that rewrite the pixels underneath. Tk canvas items cannot blend, so
# these are baked into the background bitmap instead of drawn as canvas items.
RASTER_KINDS = ("highlight", "blur", "pixelate")
KINDS = VECTOR_KINDS + RASTER_KINDS

# Floors for the blur and mosaic tools. They make a weak setting harder to read,
# but neither tool is a secrecy guarantee: blur is a reversible convolution and a
# mosaic block still leaks its average colour. They also only touch the picture
# underneath, so a text annotation drawn on top stays legible. The only way to
# truly remove content is an opaque filled rectangle, which the editor's tool
# hints point users to.
MIN_PIXEL_BLOCK = 6
MIN_BLUR_RADIUS = 5

HIGHLIGHT_ALPHA = 96
PALETTE = ("#e02424", "#f59e0b", "#facc15", "#22c55e", "#0ea5e9", "#2563eb", "#a855f7", "#111827")

_FONT_CANDIDATES = {
    "Windows": ("YuGothM.ttc", "meiryo.ttc", "msgothic.ttc", "segoeui.ttf"),
    # Full paths on macOS. Pillow resolves a bare name by walking the font
    # folders and comparing file names, and macOS keeps the Hiragino names in
    # decomposed form (NFD), so the name as typed here never matched. The lookup
    # then fell through to AppleGothic, a Korean face with no "ー".
    "Darwin": (
        "/System/Library/Fonts/ヒラギノ角ゴシック W4.ttc",
        "/System/Library/Fonts/Hiragino Sans GB.ttc",
        "/Library/Fonts/Arial Unicode.ttf",
        "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
        "/System/Library/Fonts/AppleSDGothicNeo.ttc",
    ),
}
_FONT_FALLBACK = ("DejaVuSans.ttf", "arial.ttf")
TEXT_LINE_SPACING = 4  # ImageDraw's default gap between the lines of a text


@dataclass
class Shape:
    """One annotation. Coordinates are always in source-image pixels."""

    kind: str
    points: list[Point]
    id: str = field(default_factory=lambda: uuid4().hex)
    color: str = PALETTE[0]
    width: int = 4
    filled: bool = False
    text: str = ""
    font_size: int = 24
    font: str = ""  # a family from the font catalog; empty for the platform's default face
    strength: int = 12
    number: int = 1

    def bounds(self) -> tuple[float, float, float, float]:
        xs = [point[0] for point in self.points]
        ys = [point[1] for point in self.points]
        return min(xs), min(ys), max(xs), max(ys)

    def moved(self, dx: float, dy: float) -> "Shape":
        clone = deepcopy(self)
        clone.points = [(x + dx, y + dy) for x, y in self.points]
        return clone


@dataclass
class AnnotationDocument:
    shapes: list[Shape] = field(default_factory=list)
    crop: Rect | None = None

    def find(self, shape_id: str) -> Shape | None:
        return next((shape for shape in self.shapes if shape.id == shape_id), None)

    def replace(self, shape: Shape) -> None:
        for index, existing in enumerate(self.shapes):
            if existing.id == shape.id:
                self.shapes[index] = shape
                return
        self.shapes.append(shape)

    def remove(self, shape_id: str) -> None:
        self.shapes = [shape for shape in self.shapes if shape.id != shape_id]

    def next_number(self) -> int:
        used = [shape.number for shape in self.shapes if shape.kind == "number"]
        return max(used) + 1 if used else 1


class AnnotationHistory:
    """Snapshot-based undo/redo. Callers push the document *before* changing it."""

    def __init__(self, limit: int = 50):
        self.limit = max(1, limit)
        self._undo: list[AnnotationDocument] = []
        self._redo: list[AnnotationDocument] = []

    def push(self, document: AnnotationDocument) -> None:
        self._undo.append(deepcopy(document))
        del self._undo[: -self.limit]
        self._redo.clear()

    def undo(self, current: AnnotationDocument) -> AnnotationDocument | None:
        if not self._undo:
            return None
        self._redo.append(deepcopy(current))
        return self._undo.pop()

    def redo(self, current: AnnotationDocument) -> AnnotationDocument | None:
        if not self._redo:
            return None
        self._undo.append(deepcopy(current))
        return self._redo.pop()

    def can_undo(self) -> bool:
        return bool(self._undo)

    def can_redo(self) -> bool:
        return bool(self._redo)


def resolve_font(size: int, family: str = "") -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """The font for a text annotation: the chosen family when the catalog has it, else the default.

    The family lookup is not cached: the catalog fills in the background, and a
    miss recorded before it finished would stick.
    """
    size = max(6, int(size))
    face = CATALOG.face(family) if family else None
    if face is not None:
        font = _load_face(face, size)
        if font is not None:
            return font
    return _default_font(size)


@lru_cache(maxsize=128)
def _load_face(face: FontFace, size: int) -> ImageFont.FreeTypeFont | None:
    try:
        return face.load(size)
    except (OSError, ValueError):
        return None  # the file went away since the scan


@lru_cache(maxsize=64)
def _default_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """A font that can draw Japanese, falling back until something loads.

    Cached per size: every text annotation asks for one on every export, and
    proving a face has the glyphs means rasterising a few of them.
    """
    first_loaded = None
    for name in _FONT_CANDIDATES.get(platform.system(), ()) + _FONT_FALLBACK:
        try:
            font = ImageFont.truetype(name, size)
        except (OSError, ValueError):
            continue
        if has_glyphs(font, JAPANESE_PROBE):
            return font
        first_loaded = first_loaded or font
    if first_loaded is not None:
        return first_loaded  # Latin only, but still a scalable face
    try:
        return ImageFont.load_default(size)
    except TypeError:  # Pillow < 9.2 has no size argument
        return ImageFont.load_default()


def text_line_height(size: int, family: str = "") -> float:
    """Distance between the lines of a multi-line text annotation, in pixels.

    This is what `ImageDraw.multiline_text` uses with its default spacing, so
    the preview can lay lines out at the same pitch.
    """
    font = resolve_font(size, family)
    try:
        return font.getbbox("A")[3] + TEXT_LINE_SPACING
    except (AttributeError, TypeError):
        return size * 1.2 + TEXT_LINE_SPACING


def effective_blur_radius(strength: int, region: tuple[float, float]) -> int:
    """Blur weak enough to be reversed by sharpening is worse than no blur at all."""
    shortest = min(abs(region[0]), abs(region[1]))
    return int(max(int(strength), MIN_BLUR_RADIUS, shortest // 20))


def effective_pixel_block(strength: int) -> int:
    return int(max(int(strength), MIN_PIXEL_BLOCK))


def render_raster(base: Image.Image, document: AnnotationDocument) -> Image.Image:
    """The picture with only the pixel-rewriting effects and the crop applied."""
    return _apply_crop(_apply_raster(base, document), document)


def render(base: Image.Image, document: AnnotationDocument) -> Image.Image:
    """The finished image. `base` is never modified."""
    image = _apply_raster(base, document)
    _draw_vectors(image, document)
    return _apply_crop(image, document)


def hit_test(document: AnnotationDocument, x: float, y: float, tolerance: float = 6.0) -> Shape | None:
    """Topmost shape near the point, or None. Later shapes are drawn on top."""
    for shape in reversed(document.shapes):
        left, top, right, bottom = shape.bounds()
        if shape.kind == "text":
            right = max(right, left + shape.font_size * max(1, len(shape.text)) * 0.6)
            bottom = max(bottom, top + shape.font_size * (shape.text.count("\n") + 1) * 1.2)
        elif shape.kind == "number":
            radius = badge_radius(shape)
            left, top, right, bottom = left - radius, top - radius, right + radius, bottom + radius
        if left - tolerance <= x <= right + tolerance and top - tolerance <= y <= bottom + tolerance:
            return shape
    return None


# ----- internals ------------------------------------------------------------------


def _rgb(color: str) -> tuple[int, int, int]:
    value = ImageColor.getrgb(color)
    return (value[0], value[1], value[2])


def _box(shape: Shape, size: tuple[int, int]) -> Rect | None:
    """Clamp a two-point shape to a non-empty pixel box inside the image."""
    left, top, right, bottom = shape.bounds()
    box = (
        max(0, int(round(left))),
        max(0, int(round(top))),
        min(size[0], int(round(right))),
        min(size[1], int(round(bottom))),
    )
    return box if box[2] > box[0] and box[3] > box[1] else None


def _apply_raster(base: Image.Image, document: AnnotationDocument) -> Image.Image:
    image = base.convert("RGBA")
    if image is base:  # convert() returns self when the mode already matches
        image = base.copy()
    for shape in document.shapes:
        if shape.kind not in RASTER_KINDS:
            continue
        box = _box(shape, image.size)
        if box is None:
            continue
        region = image.crop(box)
        width, height = region.size
        if shape.kind == "blur":
            region = region.filter(ImageFilter.GaussianBlur(effective_blur_radius(shape.strength, (width, height))))
        elif shape.kind == "pixelate":
            block = effective_pixel_block(shape.strength)
            small = region.resize((max(1, width // block), max(1, height // block)), Image.Resampling.BOX)
            region = small.resize((width, height), Image.Resampling.NEAREST)
        else:  # highlight
            overlay = Image.new("RGBA", (width, height), _rgb(shape.color) + (HIGHLIGHT_ALPHA,))
            region = Image.alpha_composite(region, overlay)
        image.paste(region, box)
    return image


def _draw_vectors(image: Image.Image, document: AnnotationDocument) -> None:
    draw = ImageDraw.Draw(image)
    for shape in document.shapes:
        if shape.kind == "arrow":
            _draw_arrow(draw, shape)
        elif shape.kind == "rect":
            _draw_box(draw, shape, draw.rectangle)
        elif shape.kind == "ellipse":
            _draw_box(draw, shape, draw.ellipse)
        elif shape.kind == "freehand":
            if len(shape.points) >= 2:
                draw.line(shape.points, fill=_rgb(shape.color), width=max(1, shape.width), joint="curve")
        elif shape.kind == "text":
            if shape.text:
                draw.text(
                    shape.points[0], shape.text, font=resolve_font(shape.font_size, shape.font), fill=_rgb(shape.color)
                )
        elif shape.kind == "number":
            _draw_number(draw, shape)


def _draw_box(draw: ImageDraw.ImageDraw, shape: Shape, method) -> None:
    left, top, right, bottom = shape.bounds()
    if right - left < 1 or bottom - top < 1:
        return
    fill = _rgb(shape.color) if shape.filled else None
    method([(left, top), (right, bottom)], outline=_rgb(shape.color), width=max(1, shape.width), fill=fill)


def _draw_arrow(draw: ImageDraw.ImageDraw, shape: Shape) -> None:
    (x0, y0), (x1, y1) = shape.points[0], shape.points[-1]
    length = hypot(x1 - x0, y1 - y0)
    if length < 1:
        return
    color = _rgb(shape.color)
    head = max(shape.width * 3.5, 12.0)
    angle = atan2(y1 - y0, x1 - x0)
    spread = radians(26)
    wing_a = (x1 - head * cos(angle - spread), y1 - head * sin(angle - spread))
    wing_b = (x1 - head * cos(angle + spread), y1 - head * sin(angle + spread))
    # Stop the shaft inside the head so the outline stays clean on thick arrows.
    shaft = min(length, head * 0.75)
    draw.line(
        [(x0, y0), (x1 - shaft * cos(angle), y1 - shaft * sin(angle))],
        fill=color,
        width=max(1, shape.width),
    )
    draw.polygon([(x1, y1), wing_a, wing_b], fill=color)


def badge_radius(shape: Shape) -> float:
    """Shared by the PIL renderer and the canvas preview so the badge matches."""
    return max(12.0, shape.font_size * 0.9)


def _draw_number(draw: ImageDraw.ImageDraw, shape: Shape) -> None:
    x, y = shape.points[0]
    radius = badge_radius(shape)
    draw.ellipse([(x - radius, y - radius), (x + radius, y + radius)], fill=_rgb(shape.color))
    draw.text((x, y), str(shape.number), font=resolve_font(shape.font_size), fill=(255, 255, 255), anchor="mm")


def _apply_crop(image: Image.Image, document: AnnotationDocument) -> Image.Image:
    if not document.crop:
        return image
    left, top, right, bottom = document.crop
    box = (
        max(0, min(int(left), image.width)),
        max(0, min(int(top), image.height)),
        max(0, min(int(right), image.width)),
        max(0, min(int(bottom), image.height)),
    )
    if box[2] <= box[0] or box[3] <= box[1]:
        return image
    return image.crop(box)
