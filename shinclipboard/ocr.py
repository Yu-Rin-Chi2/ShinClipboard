"""Read the text in an image with the OCR engine built into Windows.

Windows.Media.Ocr ships with the OS and works offline, recognising whichever
languages the user has installed. Nothing here is available on other platforms;
`is_available()` is how the caller finds out.
"""

from __future__ import annotations

import asyncio
import functools
import io
import re
import sys

from PIL import Image


class OcrError(Exception):
    """The image could not be read, or held no text."""


# Windows OCR returns Japanese as space separated "words" of one character each.
# A space is dropped when both of its neighbours are CJK, and kept between Latin
# words, so "日 本 語 の Hello World" becomes "日本語の Hello World".
_CJK = r"　-ヿ㐀-䶿一-鿿豈-﫿！-｠｡-ﾟ"
_CJK_GAP = re.compile(rf"(?<=[{_CJK}]) +(?=[{_CJK}])")
# The katakana long vowel mark "ー" comes back as a dash; between two katakana
# a dash is never meant, so it is put back.
_KATAKANA_DASH = re.compile(r"(?<=[ァ-ヺー]) *[-－‐―─一] *(?=[ァ-ヺ])")

# Below this the engine tends to see nothing; small crops are upscaled first.
_MIN_SIDE = 40
_UPSCALE = 2


def normalize_line(text: str) -> str:
    return _CJK_GAP.sub("", _KATAKANA_DASH.sub("ー", text)).strip()


def _engine():
    from winrt.windows.media.ocr import OcrEngine

    return OcrEngine.try_create_from_user_profile_languages()


@functools.lru_cache(maxsize=1)
def is_available() -> bool:
    if sys.platform != "win32":
        return False
    try:
        return _engine() is not None
    except Exception:  # missing winrt packages or an OS without the API
        return False


def _prepare(image: Image.Image, max_side: int) -> Image.Image:
    # UI text in a screenshot is about 12px tall, below what the engine reads
    # well; doubling it turned "1 ↓ べ替え" into "並べ替え" and "A4 tate- bac に"
    # into "A4_tate-back. ai" on a real capture. Going further made it worse.
    image = image.convert("RGB")
    width, height = image.size
    scale = max(_UPSCALE, _MIN_SIDE / max(1, min(width, height)))
    scale = min(scale, max_side / max(width, height))
    if scale != 1:
        image = image.resize((max(1, int(width * scale)), max(1, int(height * scale))), Image.LANCZOS)
    return image


async def _recognize_async(image: Image.Image) -> list[str]:
    from winrt.windows.graphics.imaging import BitmapDecoder
    from winrt.windows.media.ocr import OcrEngine
    from winrt.windows.storage.streams import DataWriter, InMemoryRandomAccessStream

    engine = _engine()
    if engine is None:
        raise OcrError("OCR に使える言語が Windows にインストールされていません")
    buffer = io.BytesIO()
    _prepare(image, OcrEngine.max_image_dimension).save(buffer, "PNG")
    stream = InMemoryRandomAccessStream()
    writer = DataWriter(stream)
    writer.write_bytes(buffer.getvalue())
    await writer.store_async()
    writer.detach_stream()
    stream.seek(0)
    decoder = await BitmapDecoder.create_async(stream)
    bitmap = await decoder.get_software_bitmap_async()
    result = await engine.recognize_async(bitmap)
    return [line.text for line in result.lines]


def recognize(image: Image.Image) -> str:
    """The text in `image`, one recognised line per line. Blocks; call it off the UI thread."""
    if sys.platform != "win32":
        raise OcrError("OCR は Windows でのみ使えます")
    try:
        lines = asyncio.run(_recognize_async(image))
    except OcrError:
        raise
    except Exception as error:
        raise OcrError(f"OCR に失敗しました: {error}") from error
    text = "\n".join(line for line in (normalize_line(raw) for raw in lines) if line)
    if not text:
        raise OcrError("文字を認識できませんでした")
    return text
