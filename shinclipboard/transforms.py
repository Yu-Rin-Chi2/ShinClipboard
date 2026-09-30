from __future__ import annotations

import re
from typing import Any, Iterable

# 整形方法ごとの表示名と、値1/値2 の意味（使わない値は None）
TRANSFORM_TYPES: dict[str, tuple[str, str | None, str | None]] = {
    "prefix_each_line": ("各行に挿入", "行頭に入れる文字", None),
    "surround_each_line": ("各行の前後に挿入", "行頭に入れる文字", "行末に入れる文字"),
    "number_lines": ("連番", "桁数", "区切り"),
    "regex": ("正規表現置換", "検索パターン", "置換後"),
    "trim": ("前後空白削除", None, None),
    "upper": ("大文字化", None, None),
    "lower": ("小文字化", None, None),
    "prefix_suffix": ("全文の前後に挿入", "先頭に入れる文字", "末尾に入れる文字"),
}


def transform_label(kind: str) -> str:
    return TRANSFORM_TYPES.get(kind, (kind, None, None))[0]


def transform_kind(label: str) -> str:
    return next((kind for kind, (name, _, _) in TRANSFORM_TYPES.items() if name == label), label)


def apply_transform(text: str, rule: dict[str, Any]) -> str:
    kind = rule.get("type", "")
    params = rule.get("params", {})
    lines = text.splitlines(keepends=False)
    trailing_newline = text.endswith(("\n", "\r"))

    if kind == "prefix_each_line":
        result = "\n".join(str(params.get("prefix", "")) + line for line in lines)
    elif kind == "surround_each_line":
        before, after = str(params.get("before", "")), str(params.get("after", ""))
        result = "\n".join(before + line + after for line in lines)
    elif kind == "number_lines":
        start = int(params.get("start", 1))
        width = max(1, int(params.get("width", 3)))
        separator = str(params.get("separator", ": "))
        result = "\n".join(f"{number:0{width}d}{separator}{line}" for number, line in enumerate(lines, start))
    elif kind == "regex":
        flags = re.MULTILINE
        if params.get("ignore_case"):
            flags |= re.IGNORECASE
        replacement = re.sub(r"\$(\d+)", r"\\g<\1>", str(params.get("replacement", "")))
        result = re.sub(str(params.get("pattern", "")), replacement, text, flags=flags)
        trailing_newline = False
    elif kind == "trim":
        return text.strip()
    elif kind == "upper":
        return text.upper()
    elif kind == "lower":
        return text.lower()
    elif kind == "prefix_suffix":
        return str(params.get("prefix", "")) + text + str(params.get("suffix", ""))
    else:
        raise ValueError(f"未対応の整形方法です: {kind}")

    return result + ("\n" if trailing_newline and lines else "")


def apply_enabled(text: str, rules: Iterable[dict[str, Any]]) -> str:
    result = text
    for rule in rules:
        if rule.get("enabled", True):
            result = apply_transform(result, rule)
    return result
