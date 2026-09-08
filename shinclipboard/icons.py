from __future__ import annotations

from functools import lru_cache

from PIL import Image, ImageDraw, ImageFilter

from .annotations import Shape, _draw_arrow, _draw_number, resolve_font

ICON_SIZE = 22
# Icons are drawn at this multiple and shrunk so the edges come out anti-aliased;
# ImageDraw itself only produces hard pixels.
_SUPERSAMPLE = 4
_INK = "#1f2937"
_SOFT = "#9ca3af"


@lru_cache(maxsize=None)
def tool_icon(name: str, size: int = ICON_SIZE, ink: str = _INK) -> Image.Image:
    """A crisp RGBA glyph for one editor tool, drawn with the tool's own primitives.

    Drawing the icons at runtime keeps the executable free of image assets and
    guarantees the arrow, badge and shapes look like what the tool will draw.
    """
    scale = _SUPERSAMPLE
    canvas = Image.new("RGBA", (size * scale, size * scale), (0, 0, 0, 0))
    painter = _PAINTERS.get(name)
    if painter is not None:
        painter(canvas, ImageDraw.Draw(canvas), size * scale, ink)
    return canvas.resize((size, size), Image.Resampling.LANCZOS)


def _stroke(side: int) -> int:
    return max(2, side // 11)


def _select(image: Image.Image, draw: ImageDraw.ImageDraw, side: int, ink: str) -> None:
    unit = side / 24
    cursor = [(6, 3), (6, 19), (10, 15), (13, 21), (16, 20), (13, 14), (18, 14)]
    draw.polygon([(x * unit, y * unit) for x, y in cursor], fill=ink, outline=ink)


def _arrow(image: Image.Image, draw: ImageDraw.ImageDraw, side: int, ink: str) -> None:
    margin = side * 0.2
    _draw_arrow(draw, Shape("arrow", [(margin, side - margin), (side - margin, margin)], color=ink, width=_stroke(side)))


def _rect(image: Image.Image, draw: ImageDraw.ImageDraw, side: int, ink: str) -> None:
    margin = side * 0.18
    draw.rectangle([(margin, margin * 1.4), (side - margin, side - margin * 1.4)], outline=ink, width=_stroke(side))


def _ellipse(image: Image.Image, draw: ImageDraw.ImageDraw, side: int, ink: str) -> None:
    margin = side * 0.16
    draw.ellipse([(margin, margin * 1.4), (side - margin, side - margin * 1.4)], outline=ink, width=_stroke(side))


def _freehand(image: Image.Image, draw: ImageDraw.ImageDraw, side: int, ink: str) -> None:
    unit = side / 24
    path = [(3, 17), (6, 8), (9, 15), (12, 6), (15, 16), (18, 9), (21, 14)]
    draw.line([(x * unit, y * unit) for x, y in path], fill=ink, width=_stroke(side), joint="curve")


def _text(image: Image.Image, draw: ImageDraw.ImageDraw, side: int, ink: str) -> None:
    draw.text((side / 2, side / 2), "A", font=resolve_font(int(side * 0.8)), fill=ink, anchor="mm")


def _number(image: Image.Image, draw: ImageDraw.ImageDraw, side: int, ink: str) -> None:
    _draw_number(draw, Shape("number", [(side / 2, side / 2)], color=ink, font_size=int(side * 0.5), number=1))


def _highlight(image: Image.Image, draw: ImageDraw.ImageDraw, side: int, ink: str) -> None:
    unit = side / 24
    for y in (8, 12, 16):
        draw.line([(4 * unit, y * unit), (20 * unit, y * unit)], fill=_SOFT, width=_stroke(side))
    marker = Image.new("RGBA", image.size, (0, 0, 0, 0))
    ImageDraw.Draw(marker).rectangle([(3 * unit, 10 * unit), (21 * unit, 14 * unit)], fill=(250, 204, 21, 170))
    image.alpha_composite(marker)


def _blur(image: Image.Image, draw: ImageDraw.ImageDraw, side: int, ink: str) -> None:
    margin = side * 0.3
    draw.ellipse([(margin, margin), (side - margin, side - margin)], fill=ink)
    blurred = image.filter(ImageFilter.GaussianBlur(side * 0.08))
    image.paste(blurred)


def _pixelate(image: Image.Image, draw: ImageDraw.ImageDraw, side: int, ink: str) -> None:
    cells = 4
    cell = side * 0.7 / cells
    origin = side * 0.15
    for row in range(cells):
        for column in range(cells):
            shade = ink if (row + column) % 2 == 0 else _SOFT
            x, y = origin + column * cell, origin + row * cell
            draw.rectangle([(x, y), (x + cell, y + cell)], fill=shade)


def _crop(image: Image.Image, draw: ImageDraw.ImageDraw, side: int, ink: str) -> None:
    unit = side / 24
    width = _stroke(side)
    # The two interlocking L shapes of the usual crop symbol.
    draw.line([(7 * unit, 2 * unit), (7 * unit, 17 * unit), (22 * unit, 17 * unit)], fill=ink, width=width)
    draw.line([(2 * unit, 7 * unit), (17 * unit, 7 * unit), (17 * unit, 22 * unit)], fill=ink, width=width)


_PAINTERS = {
    "select": _select,
    "arrow": _arrow,
    "rect": _rect,
    "ellipse": _ellipse,
    "freehand": _freehand,
    "text": _text,
    "number": _number,
    "highlight": _highlight,
    "blur": _blur,
    "pixelate": _pixelate,
    "crop": _crop,
}
