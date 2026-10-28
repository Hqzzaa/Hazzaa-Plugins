from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import shutil
import uuid
from pathlib import Path

from installer_automation import read_document

SUPPORTED_YEARS = tuple(str(year) for year in range(2026, 2018, -1))


def documents_directory() -> Path:

    if os.name == "nt":
        buffer = ctypes.create_unicode_buffer(32768)
        shell = ctypes.WinDLL("shell32", use_last_error=True)
        shell.SHGetFolderPathW.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p,
                                          ctypes.c_uint, ctypes.c_wchar_p)
        if shell.SHGetFolderPathW(None, 5, None, 0, buffer) == 0 and buffer.value:
            return Path(buffer.value)
    return Path.home() / "Documents"


def preset_directory(year: str, documents: Path | None = None) -> Path:
    if str(year) not in SUPPORTED_YEARS:
        raise ValueError("Choose an After Effects version from 2019 through 2026.")
    return (documents or documents_directory()) / "Adobe" / f"After Effects {year}" / "User Presets"


def local_drives() -> list[Path]:
    if os.name != "nt":
        return [Path.home()]
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetDriveTypeW.argtypes = (ctypes.c_wchar_p,)
    mask = kernel.GetLogicalDrives()
    return [Path(f"{chr(65 + index)}:/") for index in range(26)
            if mask & (1 << index) and kernel.GetDriveTypeW(f"{chr(65 + index)}:\\") in (2, 3)]


def is_user_presets(path: Path) -> bool:
    return re.sub(r"[\s_-]", "", path.name).casefold() in {"userpreset", "userpresets"}


def _is_link(path: Path) -> bool:
    return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())


def find_preset_folders(roots: list[Path], destination: Path, cancelled=lambda: False,
                        progress=lambda _count: None):

    skip = {"windows", "$recycle.bin", "system volume information", "$windows.~bt",
            "node_modules", ".git", ".svn"}
    destination = destination.resolve()
    stack = list(roots)
    visited = set()
    count = 0
    while stack and not cancelled():
        folder = stack.pop()
        try:
            if _is_link(folder):
                continue
            resolved = folder.resolve()
            if resolved in visited or resolved == destination or destination in resolved.parents:
                continue
            visited.add(resolved)
            if is_user_presets(folder):
                yield folder
                continue
            with os.scandir(folder) as entries:
                stack.extend(Path(entry.path) for entry in entries
                             if entry.name.casefold() not in skip
                             and entry.is_dir(follow_symlinks=False) and not entry.is_symlink())
            count += 1
            if count % 250 == 0:
                progress(count)
        except OSError:
            continue


def _same_file(left: Path, right: Path) -> bool:
    if not right.is_file() or left.stat().st_size != right.stat().st_size:
        return False
    def digest(path):
        value = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                value.update(chunk)
        return value.digest()
    return digest(left) == digest(right)


def copy_preset_file(source: Path, target: Path) -> bool:

    if _is_link(source) or _is_link(target) or any(_is_link(parent) for parent in target.parents):
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    if _same_file(source, target):
        return False
    candidate = target
    number = 2
    while candidate.exists():
        if _same_file(source, candidate):
            return False
        candidate = target.with_name(f"{target.stem} ({number}){target.suffix}")
        number += 1
    shutil.copy2(source, candidate)
    candidate.chmod(0o644)
    return True


def merge_presets(source: Path, destination: Path, cancelled=lambda: False,
                   on_error=lambda _path, _error: None) -> int:
    source, destination = source.resolve(), destination.resolve()
    if source == destination or source in destination.parents:
        return 0
    copied = 0
    for base, directories, files in os.walk(source, followlinks=False):
        if cancelled():
            break
        directories[:] = [name for name in directories if not _is_link(Path(base) / name)]
        for name in files:
            if cancelled():
                break
            file = Path(base) / name
            if not _is_link(file):
                try:
                    copied += copy_preset_file(file, destination / file.relative_to(source))
                except OSError as error:
                    on_error(file, error)
    return copied


def registry_backup_directory() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Hazzaa Plugins" / "Registry Backups"


def allowed_registry_key(key: str) -> bool:

    return bool(re.fullmatch(
        r"HKEY_(?:CURRENT_USER|LOCAL_MACHINE)\\SOFTWARE\\(?:WOW6432Node\\)?Adobe\\"
        r"(?:CSXS\.\d+(?:\\[^\r\n]*)?|After Effects(?:\\[^\r\n]*)?)", key, re.I))


def parse_adobe_registry(content: str) -> tuple[list[dict], int]:

    changes = []
    rejected = 0
    key = None
    for raw in content.splitlines():
        line = raw.strip().lstrip("\ufeff")
        if not line or line.startswith((";", "#", "Windows Registry Editor", "REGEDIT4")):
            continue
        if line.startswith("[") and line.endswith("]"):
            candidate = line[1:-1]
            key = candidate if allowed_registry_key(candidate) else None
            if key is None:
                rejected += 1
            continue
        match = re.fullmatch(r'"((?:\\.|[^"\\])*)"\s*=\s*(.*)', line)
        if not match:
            continue
        if key is None:
            rejected += 1
            continue
        name = match[1].replace('\\"', '"').replace('\\\\', '\\')
        encoded = match[2]
        if re.fullmatch(r"dword:[0-9a-fA-F]{1,8}", encoded):
            value, kind = int(encoded[6:], 16), 4
        elif re.fullmatch(r'"(?:\\.|[^"\\])*"', encoded):
            value, kind = encoded[1:-1].replace('\\"', '"').replace('\\\\', '\\'), 1
        else:
            rejected += 1
            continue
        changes.append({"key": key, "name": name, "type": kind, "value": value})

    if not changes:
        for match in re.finditer(r"HKEY_CURRENT_USER\\SOFTWARE\\Adobe\\CSXS\.\d+", content, re.I):
            nearby = content[max(0, match.start() - 150):match.end() + 300]
            if re.search(r'PlayerDebugMode[\s"\':=]*(?:dword:0{0,7})?1\b', nearby, re.I):
                changes.append({"key": match[0], "name": "PlayerDebugMode", "type": 1, "value": "1"})
    return changes, rejected


def _registry_location(key: str, registry):
    hive, subkey = key.split("\\", 1)
    return getattr(registry, hive.upper()), subkey


def apply_registry_changes(changes: list[dict], backup_dir: Path | None = None, registry=None) -> Path | None:

    if not changes:
        return None
    if registry is None:
        import winreg as registry
    unique = {}
    for item in changes:
        if not allowed_registry_key(item["key"]) or item["type"] not in (1, 4):
            raise ValueError("Registry change is outside the supported Adobe script settings.")
        identity = (item["key"].casefold(), item["name"].casefold())
        if identity in unique and (unique[identity]["type"], unique[identity]["value"]) != (item["type"], item["value"]):
            raise ValueError("Conflicting registry settings were found in the archive.")
        unique[identity] = item
    original = []
    for item in unique.values():
        root, key = _registry_location(item["key"], registry)
        entry = {"key": item["key"], "name": item["name"], "exists": False}
        try:
            with registry.OpenKey(root, key) as handle:
                value, kind = registry.QueryValueEx(handle, item["name"])
                if isinstance(value, bytes):
                    value = {"binary": value.hex()}
                entry.update(exists=True, value=value, type=kind)
        except FileNotFoundError:
            pass
        original.append(entry)
    directory = backup_dir or registry_backup_directory()
    directory.mkdir(parents=True, exist_ok=True)
    backup = directory / f"adobe-script-settings-{uuid.uuid4().hex}.json"
    backup.write_text(json.dumps({"format": "HAZZAA_REGISTRY_V1", "entries": original}, indent=2), encoding="utf-8")
    try:
        for item in unique.values():
            root, key = _registry_location(item["key"], registry)
            with registry.CreateKeyEx(root, key, 0, registry.KEY_SET_VALUE) as handle:
                registry.SetValueEx(handle, item["name"], 0, item["type"], item["value"])
    except OSError:
        restore_registry_backup(backup, registry)
        raise
    return backup


def restore_registry_backup(path: Path, registry=None) -> None:
    if registry is None:
        import winreg as registry
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if payload.get("format") != "HAZZAA_REGISTRY_V1":
        raise ValueError("Not a HAZZAA registry backup.")
    entries = payload["entries"]
    if any(not allowed_registry_key(entry["key"]) for entry in entries):
        raise ValueError("Backup contains keys outside Adobe script settings.")
    for item in entries:
        root, key = _registry_location(item["key"], registry)
        if item["exists"]:
            value = item["value"]
            if isinstance(value, dict) and "binary" in value:
                value = bytes.fromhex(value["binary"])
            with registry.CreateKeyEx(root, key, 0, registry.KEY_SET_VALUE) as handle:
                registry.SetValueEx(handle, item["name"], 0, item["type"], value)
        else:
            try:
                with registry.OpenKey(root, key, 0, registry.KEY_SET_VALUE) as handle:
                    registry.DeleteValue(handle, item["name"])
            except FileNotFoundError:
                pass
