from __future__ import annotations

import re

from PIL import Image, ImageDraw


# Only the `#` forms count as a colour. A bare `123456` is far more often a
# number than a colour, and `0x...` is a number that merely looks like one.
_HEX_COLOR = re.compile(r"^#([0-9a-fA-F]{3}|[0-9a-fA-F]{4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")

SWATCH_SIZE = (28, 16)
_CHECK = 4  # checkerboard cell behind translucent colours
_BORDER = (138, 148, 163, 255)  # keeps a white swatch visible on a white list


def parse_hex_color(text: str) -> tuple[int, int, int, int] | None:
    """RGBA for `#RGB`, `#RGBA`, `#RRGGBB` or `#RRGGBBAA` (whitespace around is fine), else None."""
    match = _HEX_COLOR.match(text.strip())
    if not match:
        return None
    digits = match.group(1)
    if len(digits) <= 4:
        digits = "".join(char * 2 for char in digits)
    values = [int(digits[index : index + 2], 16) for index in range(0, len(digits), 2)]
    if len(values) == 3:
        values.append(255)
    return values[0], values[1], values[2], values[3]


def is_hex_color(text: str) -> bool:
    return parse_hex_color(text) is not None


def normalize_hex_color(text: str) -> str | None:
    """`#rrggbb` (or `#rrggbbaa` when translucent) for any accepted spelling, else None."""
    rgba = parse_hex_color(text)
    if rgba is None:
        return None
    red, green, blue, alpha = rgba
    value = f"#{red:02x}{green:02x}{blue:02x}"
    return value if alpha == 255 else f"{value}{alpha:02x}"


def swatch_image(rgba: tuple[int, int, int, int], size: tuple[int, int] = SWATCH_SIZE) -> Image.Image:
    """A bordered block of the colour; translucent ones show over a checkerboard."""
    width, height = size
    image = Image.new("RGBA", size, (255, 255, 255, 255))
    draw = ImageDraw.Draw(image)
    if rgba[3] < 255:
        for y in range(0, height, _CHECK):
            for x in range(0, width, _CHECK):
                if (x // _CHECK + y // _CHECK) % 2:
                    draw.rectangle((x, y, x + _CHECK - 1, y + _CHECK - 1), fill=(204, 204, 204, 255))
    image.alpha_composite(Image.new("RGBA", size, rgba))
    draw.rectangle((0, 0, width - 1, height - 1), outline=_BORDER)
    return image
