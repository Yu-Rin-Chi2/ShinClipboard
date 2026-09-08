from __future__ import annotations

import re
from typing import Any, Iterable


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
