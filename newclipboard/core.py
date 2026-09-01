from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import RLock
from typing import Iterable


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class HistoryItem:
    text: str
    copied_at: str
    kind: str = "text"
    image_path: str = ""
    width: int = 0
    height: int = 0

    def to_dict(self) -> dict[str, str]:
        value = {"kind": self.kind, "text": self.text, "copied_at": self.copied_at}
        if self.kind == "image":
            value.update({"image_path": self.image_path, "width": self.width, "height": self.height})
        return value

    @classmethod
    def from_dict(cls, value: dict) -> "HistoryItem":
        return cls(
            text=str(value.get("text", "")),
            copied_at=str(value.get("copied_at", "")),
            kind=str(value.get("kind", "text")),
            image_path=str(value.get("image_path", "")),
            width=int(value.get("width", 0)),
            height=int(value.get("height", 0)),
        )


class ClipboardHistory:
    """Newest-first, duplicate-collapsing text history."""

    def __init__(self, items: Iterable[HistoryItem] = (), limit: int = 1000):
        self._lock = RLock()
        self._items = list(items)
        self.limit = max(1, int(limit))
        self._trim()

    def add(self, text: str, copied_at: str | None = None) -> bool:
        if not text:
            return False
        with self._lock:
            self._items = [item for item in self._items if item.kind != "text" or item.text != text]
            self._items.insert(0, HistoryItem(text, copied_at or utc_now()))
            self._trim()
        return True

    def add_image(self, image_path: str, width: int, height: int, copied_at: str | None = None) -> bool:
        if not image_path:
            return False
        with self._lock:
            self._items = [item for item in self._items if item.kind != "image" or item.image_path != image_path]
            self._items.insert(0, HistoryItem("", copied_at or utc_now(), "image", image_path, width, height))
            self._trim()
        return True

    def clear(self) -> None:
        with self._lock:
            self._items.clear()

    def remove(self, index: int) -> None:
        with self._lock:
            del self._items[index]

    def search(self, query: str = "") -> list[HistoryItem]:
        with self._lock:
            items = list(self._items)
        needle = query.casefold().strip()
        if not needle:
            return items
        return [
            item for item in items
            if needle in item.text.casefold() or item.kind == "image" and needle in "画像 image".casefold()
        ]

    def to_list(self) -> list[dict[str, str]]:
        return [item.to_dict() for item in self.search()]

    def _trim(self) -> None:
        del self._items[self.limit :]


class FifoQueue:
    """Thread-safe FIFO/LIFO stock used by clipboard and hotkey threads."""

    def __init__(self, values: Iterable[str] = ()):
        self._lock = RLock()
        self._values = deque(str(v) for v in values if str(v))

    def append(self, text: str) -> None:
        if text:
            with self._lock:
                self._values.append(text)

    def pop(self, mode: str = "fifo") -> str | None:
        with self._lock:
            if not self._values:
                return None
            return self._values.pop() if mode == "lifo" else self._values.popleft()

    def undo(self, text: str, mode: str = "fifo") -> None:
        with self._lock:
            (self._values.append if mode == "lifo" else self._values.appendleft)(text)

    def insert(self, index: int, text: str) -> None:
        if text:
            with self._lock:
                self._values.insert(index, text)

    def edit(self, index: int, text: str) -> None:
        if not text:
            raise ValueError("キューのテキストは空にできません。")
        with self._lock:
            self._values[index] = text

    def remove(self, index: int) -> str:
        with self._lock:
            value = self._values[index]
            del self._values[index]
            return value

    def joined(self, separator: str = "\n") -> str:
        return separator.join(self.values())

    def clear(self) -> None:
        with self._lock:
            self._values.clear()

    def values(self) -> list[str]:
        with self._lock:
            return list(self._values)

    def __len__(self) -> int:
        with self._lock:
            return len(self._values)
