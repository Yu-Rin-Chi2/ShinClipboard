from __future__ import annotations

import csv
import io
import json
import zipfile
from pathlib import Path
from typing import Any
from uuid import uuid4


CSV_HEADERS = ["定型文グループ", "定型文", "メモ", "ホットキー"]
# `images/` holds the clipboard history, `library/` the registered image library.
PICTURE_FOLDERS = ("images", "library")

# What a settings package carries between machines: everything the user set up
# by hand, and nothing that belongs to one computer. The history stays local,
# and so does a folder path that may not exist elsewhere.
PACKAGE_COLLECTIONS = ("groups", "color_groups", "image_groups", "transforms")
MACHINE_SETTINGS = {"screenshot_save_dir"}
# Which field says two items are "the same thing" when packages are merged.
_ITEM_IDENTITY = {"groups": ("snippets", "text"), "color_groups": ("colors", "value"), "image_groups": ("images", "image_path")}


def export_settings_package(config: dict[str, Any], data_dir: Path, destination: Path) -> int:
    """Write the portable settings and the library pictures they refer to. Returns the picture count."""
    payload = {
        "schema_version": config.get("schema_version", 1),
        "settings": {key: value for key, value in config.get("settings", {}).items() if key not in MACHINE_SETTINGS},
    }
    for key in PACKAGE_COLLECTIONS:
        payload[key] = config.get(key, [])
    pictures = 0
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("config.json", json.dumps(payload, ensure_ascii=False, indent=2))
        for group in config.get("image_groups", []):
            for item in group.get("images", []):
                relative = str(item.get("image_path", ""))
                source = Path(data_dir) / relative
                if _is_picture_entry(relative) and relative.startswith("library/") and source.is_file():
                    archive.write(source, relative)
                    pictures += 1
    return pictures


def import_settings_package(config: dict[str, Any], data_dir: Path, source: Path, mode: str = "merge") -> dict[str, int]:
    """Bring a package into `config` in place, extracting its pictures into `data_dir`.

    `replace` swaps the collections and portable settings for the package's;
    `merge` adds what the package has and the local config lacks, matching
    groups by name and items by content, so importing twice changes nothing.
    Returns how many groups and items were added or updated.
    """
    if mode not in ("replace", "merge"):
        raise ValueError(f"不明な取り込み方法です: {mode}")
    with zipfile.ZipFile(source, "r") as archive:
        names = set(archive.namelist())
        if "config.json" not in names:
            raise ValueError("ShinClipboardの引き継ぎパッケージではありません。")
        try:
            package = json.loads(archive.read("config.json").decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("パッケージの設定データが壊れています。") from error
        if not isinstance(package, dict) or "schema_version" not in package:
            raise ValueError("パッケージの設定データが壊れています。")
        if package["schema_version"] != config.get("schema_version", 1):
            raise ValueError("このパッケージのバージョンには対応していません。")
        for name in names:
            if name.startswith("library/") and _is_picture_entry(name):
                target = Path(data_dir) / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.read(name))
    if mode == "replace":
        for key in PACKAGE_COLLECTIONS:
            config[key] = package.get(key, [])
        settings = config.setdefault("settings", {})
        settings.update({key: value for key, value in package.get("settings", {}).items() if key not in MACHINE_SETTINGS})
        return {"groups": sum(len(package.get(key, [])) for key in PACKAGE_COLLECTIONS if key != "transforms"), "items": 0, "updated": 0}
    return _merge_package(config, package)


def _merge_package(config: dict[str, Any], package: dict[str, Any]) -> dict[str, int]:
    added_groups = added = updated = 0
    for key, (items_key, identity) in _ITEM_IDENTITY.items():
        local_groups = config.setdefault(key, [])
        by_name = {group.get("name", ""): group for group in local_groups}
        for group in package.get(key, []):
            target = by_name.get(group.get("name", ""))
            if target is None:
                target = {"id": str(uuid4()), "name": group.get("name", ""), items_key: []}
                local_groups.append(target)
                by_name[target["name"]] = target
                added_groups += 1
            existing = {str(item.get(identity, "")): item for item in target.setdefault(items_key, [])}
            for item in group.get(items_key, []):
                match = existing.get(str(item.get(identity, "")))
                if match is None:
                    copy = {**item, "id": str(uuid4())}
                    target[items_key].append(copy)
                    existing[str(copy.get(identity, ""))] = copy
                    added += 1
                elif any(match.get(field) != item.get(field) for field in item if field != "id"):
                    match.update({field: value for field, value in item.items() if field != "id"})
                    updated += 1
    local_rules = config.setdefault("transforms", [])
    by_name = {rule.get("name", ""): rule for rule in local_rules}
    for rule in package.get("transforms", []):
        match = by_name.get(rule.get("name", ""))
        if match is None:
            local_rules.append({**rule, "id": str(uuid4())})
            added += 1
        elif any(match.get(field) != rule.get(field) for field in rule if field != "id"):
            match.update({field: value for field, value in rule.items() if field != "id"})
            updated += 1
    return {"groups": added_groups, "items": added, "updated": updated}


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
        for folder in PICTURE_FOLDERS:
            picture_dir = history_path.parent / folder
            if picture_dir.exists():
                for image_path in picture_dir.glob("*.png"):
                    archive.write(image_path, f"{folder}/{image_path.name}")


def restore_backup(source: Path, config_path: Path, history_path: Path) -> None:
    with zipfile.ZipFile(source, "r") as archive:
        names = set(archive.namelist())
        allowed = {"config.json", "history.json"}
        allowed.update(name for name in names if _is_picture_entry(name))
        if "config.json" not in names or names - allowed:
            raise ValueError("ShinClipboardのバックアップ形式ではありません。")
        config = json.loads(archive.read("config.json").decode("utf-8-sig"))
        if not isinstance(config, dict) or "schema_version" not in config:
            raise ValueError("設定データが壊れています。")
        config_path.write_bytes(archive.read("config.json"))
        if "history.json" in names:
            history_path.write_bytes(archive.read("history.json"))
        for name in names:
            if _is_picture_entry(name):
                target = history_path.parent / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.read(name))


def _is_picture_entry(name: str) -> bool:
    """`images/<file>.png` or `library/<file>.png`, one level deep and nothing else."""
    folder, _, file_name = name.partition("/")
    return folder in PICTURE_FOLDERS and Path(file_name).name == file_name and file_name.endswith(".png")
