from __future__ import annotations

import csv
import io
import json
import zipfile
from pathlib import Path
from typing import Any
from uuid import uuid4


CSV_HEADERS = ["定型文グループ", "定型文", "メモ", "ホットキー"]


def export_snippets_csv(config: dict[str, Any], destination: Path) -> None:
    with Path(destination).open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=CSV_HEADERS)
        writer.writeheader()
        for group in config.get("groups", []):
            for snippet in group.get("snippets", []):
                writer.writerow({
                    "定型文グループ": group.get("name", ""),
                    "定型文": snippet.get("text", ""),
                    "メモ": snippet.get("memo", "") or snippet.get("title", ""),
                    "ホットキー": snippet.get("hotkey", ""),
                })


def import_snippets_csv(config: dict[str, Any], source: Path, clear: bool = False) -> tuple[int, int]:
    if clear:
        config["groups"] = []
    groups = {group["name"]: group for group in config.get("groups", [])}
    added = updated = 0
    raw = Path(source).read_bytes()
    try:
        content = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        content = raw.decode("cp932")
    with io.StringIO(content, newline="") as stream:
        for row in csv.DictReader(stream):
            group_name = (row.get("定型文グループ") or row.get("group") or "").strip()
            text = row.get("定型文") or row.get("text") or ""
            if not group_name or not text:
                continue
            group = groups.get(group_name)
            if group is None:
                group = {"id": str(uuid4()), "name": group_name, "snippets": []}
                config["groups"].append(group)
                groups[group_name] = group
            existing = next((item for item in group["snippets"] if item.get("text") == text), None)
            values = {
                "title": (row.get("title") or row.get("メモ") or row.get("memo") or text.splitlines()[0][:40]).strip(),
                "text": text,
                "memo": row.get("メモ") or row.get("memo") or "",
                "hotkey": (row.get("ホットキー") or row.get("hotkey") or "").strip().lower(),
            }
            if existing:
                existing.update(values)
                updated += 1
            else:
                group["snippets"].append({"id": str(uuid4()), **values})
                added += 1
    return added, updated


def create_backup(config_path: Path, history_path: Path, destination: Path) -> None:
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.write(config_path, "config.json")
        if history_path.exists():
            archive.write(history_path, "history.json")


def restore_backup(source: Path, config_path: Path, history_path: Path) -> None:
    with zipfile.ZipFile(source, "r") as archive:
        names = set(archive.namelist())
        if "config.json" not in names or names - {"config.json", "history.json"}:
            raise ValueError("NewClipboardのバックアップ形式ではありません。")
        config = json.loads(archive.read("config.json").decode("utf-8-sig"))
        if not isinstance(config, dict) or "schema_version" not in config:
            raise ValueError("設定データが壊れています。")
        config_path.write_bytes(archive.read("config.json"))
        if "history.json" in names:
            history_path.write_bytes(archive.read("history.json"))
