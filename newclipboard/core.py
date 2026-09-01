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

    def to_dict(self) -> dict[str, str]:
        return {"text": self.text, "copied_at": self.copied_at}

    @classmethod
    def from_dict(cls, value: dict) -> "HistoryItem":
        return cls(text=str(value.get("text", "")), copied_at=str(value.get("copied_at", "")))


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
            self._items = [item for item in self._items if item.text != text]
            self._items.insert(0, HistoryItem(text, copied_at or utc_now()))
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
        return items if not needle else [item for item in items if needle in item.text.casefold()]

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
