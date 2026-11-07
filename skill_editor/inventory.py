"""Non-executing refreshes of explicit roots, separate from session catalogs.

Personal discovery is <root>/<skill>/SKILL.md, with system skills in
<root>/.system/<skill>/SKILL.md. Files nested in a skill's resources are not new
personal skills. Plugin caches are recursive so versioned and template packages
are inventoried without treating their presence as activation evidence.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import time
from typing import Callable


class InventoryError(ValueError):
    """Refresh failed before a replacement inventory could be committed."""


def _hash(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _internal(path: Path) -> Path:
    path = Path(path).absolute()
    if ".." in path.parts or path == Path("/Volumes") or Path("/Volumes") in path.parents:
        raise InventoryError("Inventory paths must stay on the internal disk without traversal.")
    # Inspect each component without following a symlink, including ancestors.
    for component in (*reversed(path.parents), path):
        try:
            if stat.S_ISLNK(component.lstat().st_mode):
                raise InventoryError("Inventory paths must not contain symbolic links.")
        except FileNotFoundError:
            continue
    return path


def _atomic_write(path: Path, raw: bytes) -> None:
    _internal(path)
    fd, temporary = tempfile.mkstemp(prefix=".skill-editor-inventory-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        _internal(path)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _identity(item: dict) -> tuple[str, str]:
    return str(item.get("source", "plugin")), str(item.get("path", ""))


def _new_id(source: str, path: Path) -> str:
    return "disk-" + source + "-" + _hash((source + "\0" + str(path)).encode("utf-8"))[:24]


def _personal_source(path: Path, root: Path) -> str | None:
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        return None
    if len(parts) == 2 and parts[0] != ".system" and parts[-1] == "SKILL.md":
        return "personal"
    if len(parts) == 3 and parts[0] == ".system" and parts[-1] == "SKILL.md":
        return "system"
    return None


def _open_directory(path: Path) -> int:
    """Do not follow a replaced ancestor between validation and opening a root."""
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for name in path.parts[1:]:
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def _metadata(content: str, fallback_name: str, parse_description: Callable[[str], str]) -> dict:
    lines = content.splitlines(keepends=True)
    if not lines or not re.fullmatch(r"---[ \t]*", lines[0].rstrip("\r\n")):
        raise ValueError("Missing frontmatter")
    closing = next((i for i in range(1, len(lines)) if re.fullmatch(r"---[ \t]*", lines[i].rstrip("\r\n"))), None)
    if closing is None:
        raise ValueError("Unclosed frontmatter")
    raw_name = next((line.split(":", 1)[1].strip() for line in lines[1:closing] if re.match(r"^name[ \t]*:", line)), fallback_name)
    if raw_name.startswith('"'):
        try:
            name = json.loads(raw_name)
        except ValueError:
            name = fallback_name
    elif raw_name.startswith("'") and raw_name.endswith("'"):
        name = raw_name[1:-1].replace("''", "'")
    else:
        name = re.split(r"\s+#", raw_name, maxsplit=1)[0].strip()
    description = parse_description(content)
    if not isinstance(description, str):
        raise ValueError("Non-text description")
    return {
        "name": name if isinstance(name, str) and name else fallback_name,
        "description": description,
        "rawFrontmatter": "".join(lines[1:closing]).strip("\r\n"),
        "body": "".join(lines[closing + 1:]).lstrip("\r\n"),
        "content": content,
    }


def _scan_root(root: Path, personal_root: Path, source: str, parse_description: Callable[[str], str]) -> tuple[list[dict], list[dict]]:
    """Use directory descriptors and O_NOFOLLOW for files and nested directories."""
    records: list[dict] = []
    warnings: list[dict] = []

    def warn(path: Path, message: str):
        warnings.append({"path": str(path), "message": message})

    def visit(directory: Path, fd: int):
        try:
            with os.scandir(fd) as iterator:
                names = sorted(entry.name for entry in iterator)
        except OSError:
            warn(directory, "Directory could not be listed; prior records are retained as unverified.")
            return
        for name in names:
            path = directory / name
            try:
                info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode):
                    warn(path, "Symbolic link skipped without following its target.")
                    continue
                if stat.S_ISDIR(info.st_mode):
                    if source == "personal":
                        parts = path.relative_to(personal_root).parts
                        if len(parts) > 1 and not (len(parts) == 2 and parts[0] == ".system"):
                            continue
                    child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                    try:
                        visit(path, child)
                    finally:
                        os.close(child)
                    continue
                if name != "SKILL.md":
                    continue
                if source == "personal" and _personal_source(path, personal_root) is None:
                    continue
                if not stat.S_ISREG(info.st_mode):
                    warn(path, "Non-regular SKILL.md skipped.")
                    continue
                file_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
                with os.fdopen(file_fd, "rb") as handle:
                    before_read = os.fstat(handle.fileno())
                    if not stat.S_ISREG(before_read.st_mode):
                        warn(path, "SKILL.md changed file type during scanning.")
                        continue
                    raw = handle.read(2_000_001)
                    after_read = os.fstat(handle.fileno())
                kind = _personal_source(path, personal_root) if source == "personal" else source
                record = {"path": str(path), "source": kind, "sha256": _hash(raw), "bytes": len(raw), "fileStatus": "present", "presentOnDisk": True}
                if (before_read.st_size, before_read.st_mtime_ns, before_read.st_ctime_ns) != (after_read.st_size, after_read.st_mtime_ns, after_read.st_ctime_ns):
                    record.pop("sha256")
                    record.update({"fileStatus": "unverified", "scanError": "Skill changed while being read; prior metadata is retained."})
                    warn(path, record["scanError"])
                elif len(raw) > 2_000_000:
                    record.pop("sha256")
                    record.update({"fileStatus": "unverified", "scanError": "Skill exceeds the 2 MB scan limit."})
                    warn(path, record["scanError"])
                else:
                    try:
                        record.update(_metadata(raw.decode("utf-8"), path.parent.name, parse_description))
                    except (ValueError, UnicodeError):
                        record.update({"fileStatus": "unsupported", "scanError": "Skill metadata could not be parsed; prior metadata is retained."})
                        warn(path, record["scanError"])
                records.append(record)
            except OSError:
                warn(path, "Path could not be read safely; prior records are retained as unverified.")

    try:
        _internal(root)
        fd = _open_directory(root)
        try:
            visit(root, fd)
        finally:
            os.close(fd)
    except (OSError, InventoryError):
        warn(root, "Configured root is unavailable or unsafe; it was not followed.")
    return records, warnings


def refresh_inventory(project: Path, personal_root: Path, plugin_cache_root: Path, *, parse_description: Callable[[str], str]) -> dict:
    """Persist a disk snapshot and return it, preserving old ids and catalog evidence.

    Call under the SkillStore lock. ``parse_description`` is a data parser, e.g.
    ``lambda content: description_span(content)[2]``; this module imports no store.
    A warning makes the scan incomplete, so no previously known file is newly
    confirmed missing. All previous records remain available for comparison.
    """
    project = _internal(project)
    personal_root = Path(personal_root).absolute()
    plugin_cache_root = Path(plugin_cache_root).absolute()
    inventory_path = _internal(project / "data/inventory.json")
    try:
        previous_raw = inventory_path.read_bytes()
    except FileNotFoundError:
        previous_raw = None
        previous = {"schemaVersion": 1, "skills": [], "cachedSkills": [], "counts": {}, "scope": {}}
    else:
        try:
            previous = json.loads(previous_raw)
        except (ValueError, UnicodeError):
            raise InventoryError("Existing inventory is invalid JSON; it was not replaced.") from None
        if not isinstance(previous, dict) or not isinstance(previous.get("skills", []), list) or not isinstance(previous.get("cachedSkills", []), list) or not isinstance(previous.get("scope", {}), dict) or not isinstance(previous.get("counts", {}), dict):
            raise InventoryError("Existing inventory has an unsupported structure; it was not replaced.")
    result = copy.deepcopy(previous)
    old_main = previous.get("skills", [])
    old_cache = previous.get("cachedSkills", [])
    if any(not isinstance(item, dict) or not isinstance(item.get("path"), str) or not item.get("path") for item in [*old_main, *old_cache]):
        raise InventoryError("Existing inventory has invalid records; it was not replaced.")
    catalog = previous.get("sessionCatalog")
    if not isinstance(catalog, dict):
        catalog = {
            "capturedAt": previous.get("generatedAt"),
            "source": previous.get("scope", {}).get("sessionCatalogSource", "Legacy session catalog snapshot; not refreshed by disk scanning."),
            "entries": [{key: item.get(key) for key in ("id", "name", "path", "source")} for item in old_main if item.get("sessionAvailable") is True],
            "isLive": False,
        }
    result["sessionCatalog"] = copy.deepcopy(catalog)
    listed = {str(item.get("path")) for item in catalog.get("entries", []) if isinstance(item, dict)}
    old_by_identity: dict[tuple[str, str], dict] = {}
    for item in [*old_cache, *old_main]:
        old_by_identity[_identity(item)] = item
    scanned, warnings = _scan_root(personal_root, personal_root, "personal", parse_description)
    plugin_records, plugin_warnings = _scan_root(plugin_cache_root, personal_root, "plugin", parse_description)
    scanned.extend(plugin_records)
    warnings.extend(plugin_warnings)
    complete = not warnings
    now = datetime.now(timezone.utc).isoformat()
    delta: dict[str, list[dict]] = {key: [] for key in ("added", "missing", "changed", "restored", "unverified")}
    merged: dict[tuple[str, str], dict] = {}

    def summary(item: dict, **extra) -> dict:
        return {"id": item["id"], "name": item.get("name", Path(item["path"]).parent.name), "source": item.get("source"), "path": item["path"], **extra}

    used_ids = {str(item["id"]) for item in old_by_identity.values() if item.get("id")}
    for found in scanned:
        identity = _identity(found)
        old = old_by_identity.get(identity)
        item = copy.deepcopy(old or {})
        item.update(found)
        if not item.get("id"):
            identifier = _new_id(found["source"], Path(found["path"]))
            # Stable full path identity is independent of skill name or file content.
            if identifier in used_ids:
                raise InventoryError("A generated inventory id collided; inventory was not replaced.")
            item["id"] = identifier
            used_ids.add(identifier)
        item.setdefault("name", Path(item["path"]).parent.name)
        item.setdefault("description", "")
        item["editable"] = item["source"] == "personal" and item["fileStatus"] == "present"
        item["sessionAvailable"] = old.get("sessionAvailable") if old else None
        item["sessionCatalogMembership"] = "listed-in-snapshot" if item["path"] in listed else "not-listed-in-snapshot" if old is not None else "unknown"
        item["sessionCatalogAt"] = catalog.get("capturedAt")
        if "scanError" not in found:
            item.pop("scanError", None)
        if isinstance(item.get("frontmatter"), dict) and "description" in found:
            item["frontmatter"]["description"] = found["description"]
        if old is None:
            delta["added"].append(summary(item))
        elif old.get("fileStatus") == "missing" and found["fileStatus"] == "present":
            delta["restored"].append(summary(item))
        if old and old.get("sha256") and found.get("sha256") and old["sha256"] != found["sha256"]:
            delta["changed"].append(summary(item, beforeSha=old["sha256"], afterSha=found["sha256"]))
        if found["fileStatus"] != "present":
            delta["unverified"].append(summary(item))
        merged[identity] = item
    for identity, old in old_by_identity.items():
        if identity in merged:
            continue
        item = copy.deepcopy(old)
        item.setdefault("id", _new_id(identity[0], Path(identity[1])))
        item.setdefault("source", identity[0])
        path = Path(item["path"])
        in_scope = (identity[0] in ("personal", "system") and _personal_source(path, personal_root) == identity[0]) or (identity[0] == "plugin" and plugin_cache_root in path.parents)
        if complete and in_scope:
            item.update({"fileStatus": "missing", "presentOnDisk": False, "editable": False})
            if old.get("fileStatus") != "missing":
                delta["missing"].append(summary(item))
        else:
            item.update({"fileStatus": "unverified", "presentOnDisk": None, "editable": False})
            delta["unverified"].append(summary(item))
        merged[identity] = item
    main_identities = {_identity(item) for item in old_main}
    cache_identities = {_identity(item) for item in old_cache}
    result["skills"] = [item for identity, item in merged.items() if identity[0] in ("personal", "system") or identity in main_identities]
    result["cachedSkills"] = [copy.deepcopy(item) for identity, item in merged.items() if identity[0] == "plugin" or identity in cache_identities]
    result["skills"].sort(key=lambda item: (item.get("source", ""), item.get("name", ""), item["path"]))
    result["cachedSkills"].sort(key=lambda item: item["path"])
    result["generatedAt"] = now
    result["delta"] = delta
    result["scanWarnings"] = warnings
    result.setdefault("scope", {}).update({
        "personalRoot": str(personal_root), "systemRoot": str(personal_root / ".system"), "pluginCacheRoot": str(plugin_cache_root),
        "scannedRoots": [str(personal_root), str(plugin_cache_root)], "externalVolumesScanned": False,
        "scanLayout": {"personal": "<personalRoot>/<skill>/SKILL.md", "system": "<personalRoot>/.system/<skill>/SKILL.md", "pluginCache": "Recursive SKILL.md files; cache presence is not session availability."},
        "enabledState": "unknown beyond the preserved session catalog snapshot", "installedSkillsModified": False,
        "physicalCountDefinition": "SKILL.md files observed in the personal/system discovery layout plus recursive plugin caches; caches do not imply enabled skills. An incomplete scan is a lower bound.",
    })
    session_entries = [entry for entry in catalog.get("entries", []) if isinstance(entry, dict)]
    observed = [item for item in merged.values() if item.get("presentOnDisk") is True]
    result.setdefault("counts", {}).update({
        "personal": sum(item.get("source") == "personal" for item in observed),
        "system": sum(item.get("source") == "system" for item in observed),
        "sessionSystem": sum(item.get("source") == "system" for item in session_entries),
        "sessionPlugin": sum(item.get("source") == "plugin" for item in session_entries),
        "sessionAvailable": len(session_entries),
        "cachedSkillFiles": sum(item.get("source") == "plugin" for item in observed),
        "cachedNotInSession": sum(item.get("source") == "plugin" and item["path"] not in listed for item in observed),
        "physicalSkillFiles": len(observed), "mainInventoryRecords": len(result["skills"]),
        "diskCountsComplete": complete, "physicalCountIsLowerBound": not complete,
    })
    history = _internal(project / ".state/inventory-history")
    history.mkdir(parents=True, exist_ok=True, mode=0o700)
    _internal(history)
    snapshot_path = None
    if previous_raw is not None:
        snapshot_path = history / (str(time.time_ns()) + "-" + _hash(previous_raw)[:16] + ".json")
        _atomic_write(snapshot_path, previous_raw)
    result["refresh"] = {"at": now, "complete": complete, "sessionCatalogRefreshed": False, "snapshotPath": str(snapshot_path) if snapshot_path else None, "scannedRoots": [str(personal_root), str(plugin_cache_root)]}
    replacement = (json.dumps(result, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    inventory_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _internal(inventory_path.parent)
    # Do not overwrite an inventory another process replaced during the scan.
    try:
        current_raw = inventory_path.read_bytes()
    except FileNotFoundError:
        current_raw = None
    if current_raw != previous_raw:
        raise InventoryError("Inventory changed during refresh; reload and try again.")
    _atomic_write(inventory_path, replacement)
    return result
