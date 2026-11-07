"""Skill reads, conservative findings, and description-only transactions.

No installed skill is edited until apply_description is explicitly called.
Editor intent files live outside skill discovery directories.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import threading
import time


MAX_SKILL_BYTES = 2_000_000


class EditorError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def digest(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _frontmatter_lines(content: str) -> tuple[list[str], int]:
    lines = content.splitlines(keepends=True)
    if not lines or not re.fullmatch(r"---[ \t]*", lines[0].rstrip("\r\n")):
        raise EditorError("SKILL.md must start with YAML frontmatter.")
    closing = next((i for i in range(1, len(lines)) if re.fullmatch(r"---[ \t]*", lines[i].rstrip("\r\n"))), None)
    if closing is None:
        raise EditorError("SKILL.md has no closing frontmatter delimiter.")
    return lines, closing


def _scalar_and_suffix(raw: str) -> tuple[str, str]:
    """Separate a YAML scalar from its trailing whitespace/comment, losslessly."""
    if raw.startswith(('"', "'")):
        quote = raw[0]
        i = 1
        while i < len(raw):
            if quote == '"' and raw[i] == "\\":
                i += 2
                continue
            if raw[i] == quote:
                if quote == "'" and i + 1 < len(raw) and raw[i + 1] == "'":
                    i += 2
                    continue
                trailing = raw[i + 1:]
                if trailing.strip() and not (trailing[0].isspace() and trailing.lstrip().startswith("#")):
                    raise EditorError("Ambiguous YAML description suffix requires a manual edit.")
                return raw[:i + 1], trailing
            i += 1
        raise EditorError("Multiline quoted descriptions require a manual edit.")
    for i, char in enumerate(raw):
        if char == "#" and (i == 0 or raw[i - 1].isspace()):
            value = raw[:i].rstrip()
            return value, raw[len(value):]
    value = raw.rstrip()
    return value, raw[len(value):]


def _description_details(content: str) -> tuple[int, int, str, str, str, str]:
    """Find the top-level YAML description without rewriting other fields.

    This intentionally supports scalar descriptions only. Complex YAML constructs
    are rejected instead of passing through a lossy hand-written YAML serializer.
    """
    lines, closing = _frontmatter_lines(content)
    matches = [i for i in range(1, closing) if re.match(r"^description[ \t]*:", lines[i])]
    if len(matches) != 1:
        raise EditorError("Expected exactly one top-level description field.")
    start_line = matches[0]
    first_line = lines[start_line].rstrip("\r\n")
    prefix = re.match(r"^description[ \t]*:[ \t]*", first_line).group(0)
    raw, suffix = _scalar_and_suffix(first_line[len(prefix):])
    newline = lines[start_line][len(first_line):]
    if raw.startswith(("&", "*", "!", "{", "[")):
        raise EditorError("Complex YAML descriptions need a manual edit.")
    end_line = start_line + 1
    block = raw.startswith(("|", ">"))
    while end_line < closing:
        line = lines[end_line]
        if line.strip() and not line.startswith((" ", "\t")):
            break
        if not block and line.lstrip().startswith("#"):
            # This is a YAML comment, not description text. Preserve it in place.
            for following in lines[end_line + 1:closing]:
                if following.strip() and not following.startswith((" ", "\t")):
                    break
                if following.strip() and not following.lstrip().startswith("#"):
                    raise EditorError("Interrupted multiline descriptions require a manual edit.")
            break
        end_line += 1
    # Blank separator lines are not part of the description.
    while end_line > start_line + 1 and not lines[end_line - 1].strip():
        end_line -= 1
    continuations = [line.strip() for line in lines[start_line + 1:end_line] if line.strip()]
    if block:
        value = " ".join(continuations)
    elif raw.startswith('"'):
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            raise EditorError("Non-JSON YAML quoting requires a manual edit.")
        if continuations:
            raise EditorError("Multiline quoted descriptions require a manual edit.")
    elif raw.startswith("'"):
        if not raw.endswith("'") or continuations:
            raise EditorError("Multiline quoted descriptions require a manual edit.")
        value = raw[1:-1].replace("''", "'")
    else:
        if any(_scalar_and_suffix(line)[1].lstrip().startswith("#") for line in continuations):
            raise EditorError("Commented multiline descriptions require a manual edit.")
        value = " ".join([raw, *continuations]).strip()
    if not isinstance(value, str):
        raise EditorError("Description must be a string.")
    start = sum(len(line) for line in lines[:start_line])
    end = sum(len(line) for line in lines[:end_line])
    return start, end, value, prefix, suffix, newline


def description_span(content: str) -> tuple[int, int, str]:
    return _description_details(content)[:3]


def replace_description(content: str, description: str) -> str:
    if not isinstance(description, str) or not description.strip():
        raise EditorError("Description cannot be empty.")
    if len(description) > 4000 or any(ord(c) < 32 and c not in "\n\t\r" for c in description):
        raise EditorError("Description must be at most 4000 characters without control characters.")
    start, end, _, prefix, suffix, newline = _description_details(content)
    if suffix.startswith("#"):
        # An empty scalar may have a comment immediately after the key spacing.
        suffix = " " + suffix
    line = prefix + json.dumps(description.strip(), ensure_ascii=False) + suffix + newline
    return content[:start] + line + content[end:]


def conservative_findings(skills: list[dict]) -> list[dict]:
    """Report exact evidence, never claim to emulate Codex skill selection."""
    findings = []
    by_name: dict[str, list[dict]] = {}
    by_description: dict[str, list[dict]] = {}
    ids = {str(s.get("name", "")): str(s.get("id", "")) for s in skills}
    for skill in skills:
        name = str(skill.get("name", ""))
        description = str(skill.get("description", ""))
        by_name.setdefault(name.casefold(), []).append(skill)
        if description.strip():
            by_description.setdefault(" ".join(description.casefold().split()), []).append(skill)
        if len(description) > 800:
            findings.append({"id": "long-" + str(skill["id"]), "type": "long-description", "severity": "info", "skillIds": [skill["id"]], "evidence": f"Description has {len(description)} characters.", "message": "触发描述较长；可检查是否把执行流程放进了触发条件。", "suggestion": "保留何时适用与必要边界，将执行细节放在正文。", "method": "heuristic"})
    for groups, kind, message in ((by_name, "duplicate-name", "多个 skill 使用同一名称。"), (by_description, "duplicate-description", "多个 skill 使用相同触发描述。")):
        for key, group in groups.items():
            if key and len(group) > 1:
                findings.append({"id": kind + "-" + digest(key)[:12], "type": kind, "severity": "warning", "skillIds": [s["id"] for s in group], "evidence": key, "message": message, "suggestion": "核对来源、可用状态和适用范围，避免把缓存版本当作独立能力。", "method": "heuristic"})
    # Literal cross-skill mentions imply a relationship, not a contradiction.
    for skill in skills:
        description = str(skill.get("description", ""))
        references = [sid for name, sid in ids.items() if name and sid != skill["id"] and name in description]
        if references:
            findings.append({"id": "reference-" + str(skill["id"]), "type": "routing-reference", "severity": "info", "skillIds": [skill["id"], *references], "evidence": description, "message": "触发描述引用了其他 skill，需要检查路由与职责是否清楚。", "suggestion": "查看原文中的委派或排除边界；这条引用本身不代表冲突。", "method": "heuristic"})
    return findings


class SkillStore:
    def __init__(self, project: Path, personal_root: Path | None = None, plugin_cache_root: Path | None = None):
        self.project = Path(project).absolute()
        self.personal_root = Path(personal_root or Path.home() / ".codex/skills").absolute()
        self.plugin_cache_root = Path(plugin_cache_root or Path.home() / ".codex/plugins/cache").absolute()
        self.inventory_path = self.project / "data/inventory.json"
        self._lock = threading.RLock()

    @staticmethod
    def _physical_internal(path: Path) -> Path:
        path = path.absolute()
        if ".." in path.parts or path.resolve() != path:
            raise EditorError("Symlinks and relative traversal are not editable.", 403)
        if path == Path("/Volumes") or Path("/Volumes") in path.parents:
            raise EditorError("Editor state and writable skills must stay on the internal disk.", 403)
        return path

    def inventory(self, include_analysis: bool = True) -> dict:
        try:
            result = json.loads(self.inventory_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"skills": [], "counts": {}, "findings": [], "notice": "Inventory has not been generated yet."}
        for skill in result.get("skills", []):
            try:
                content = self._read_content(Path(skill["path"]))
                skill["description"] = description_span(content)[2]
                skill["content"] = content
                skill["sha256"] = digest(content)
                skill["bytes"] = len(content.encode("utf-8"))
                if isinstance(skill.get("frontmatter"), dict):
                    skill["frontmatter"]["description"] = skill["description"]
                lines, closing = _frontmatter_lines(content)
                skill["rawFrontmatter"] = "".join(lines[1:closing]).strip("\r\n")
                skill["body"] = "".join(lines[closing + 1:]).lstrip("\r\n")
                skill["fileStatus"] = "present"
            except FileNotFoundError:
                skill["fileStatus"] = "missing"
            except (OSError, UnicodeError, EditorError):
                skill["fileStatus"] = "unavailable-or-unsupported"
        if not include_analysis:
            # Metadata callers still receive fresh content and SHA values;
            # relationship analysis is unnecessary for source freshness checks.
            result.pop("findings", None)
            result.pop("relationships", None)
            return result
        from .relationships import build_relationships

        result["findings"] = conservative_findings(result.get("skills", []))
        review_path = self.project / "data/review-findings.json"
        if review_path.is_file():
            reviewed = json.loads(review_path.read_text(encoding="utf-8"))
            entries = reviewed.get("findings", []) if isinstance(reviewed, dict) else reviewed
            if isinstance(entries, list):
                result["findings"] = [*entries, *result["findings"]]
        result["relationships"] = build_relationships(result.get("skills", []), self.project)
        return result

    def _read_content(self, path: Path) -> str:
        with self._physical_internal(path).open("rb") as handle:
            raw = handle.read(MAX_SKILL_BYTES + 1)
        if len(raw) > MAX_SKILL_BYTES:
            raise EditorError("Skill exceeds the 2 MB editor read limit.", 413)
        return raw.decode("utf-8")

    def refresh_inventory(self) -> dict:
        """Explicit disk scan; it never claims to refresh the session catalog."""
        from .inventory import InventoryError, refresh_inventory

        with self._lock:
            self._physical_internal(self.project)
            try:
                refresh_inventory(
                    self.project, self.personal_root, self.plugin_cache_root,
                    parse_description=lambda content: description_span(content)[2],
                )
            except InventoryError as exc:
                raise EditorError(str(exc), 409 if "changed during refresh" in str(exc) else 400) from None
            return self.inventory()

    def skill(self, identifier: str) -> dict:
        if not isinstance(identifier, str) or not identifier or len(identifier) > 250:
            raise EditorError("A valid skill id is required.")
        try:
            inventory = json.loads(self.inventory_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            inventory = {"skills": []}
        # Resolve membership from the saved directory only. Reading one skill
        # must not reread every installed file or rebuild the relationship map.
        matches = [s for s in inventory.get("skills", []) if s.get("id") == identifier]
        if len(matches) != 1:
            raise EditorError("Skill id was not found or is ambiguous.", 404)
        return matches[0]

    def writable_path(self, skill: dict) -> Path:
        if skill.get("source") != "personal" or skill.get("editable") is not True:
            raise EditorError("Only personal skills marked editable can be changed.", 403)
        root = self._physical_internal(self.personal_root)
        path = self._physical_internal(Path(skill["path"]))
        if root not in path.parents or path.name != "SKILL.md" or ".system" in path.relative_to(root).parts:
            raise EditorError("Skill path is outside the editable personal skill root.", 403)
        if not path.is_file() or not stat.S_ISREG(path.stat().st_mode):
            raise EditorError("Skill path is not a regular file.", 403)
        return path

    def read(self, identifier: str) -> dict:
        skill = self.skill(identifier)
        path = Path(skill["path"])
        # Read-only plugin and system skills are allowed by inventory path only.
        try:
            content = self._read_content(path)
        except (OSError, UnicodeError):
            raise EditorError("The skill file is unavailable or is not UTF-8.", 404)
        description_supported = True
        try:
            description = description_span(content)[2]
        except EditorError:
            description_supported = False
            description = skill.get("description", "")
        sha = digest(content)
        skill.update({"description": description, "content": content, "sha256": sha, "bytes": len(content.encode("utf-8")), "fileStatus": "present" if description_supported else "unavailable-or-unsupported"})
        if isinstance(skill.get("frontmatter"), dict):
            skill["frontmatter"]["description"] = description
        try:
            lines, closing = _frontmatter_lines(content)
            skill["rawFrontmatter"] = "".join(lines[1:closing]).strip("\r\n")
            skill["body"] = "".join(lines[closing + 1:]).lstrip("\r\n")
        except EditorError:
            skill["fileStatus"] = "unavailable-or-unsupported"
        return {"skill": skill, "content": content, "sha": sha, "description": description, "intent": self.intent(identifier)}

    def preview(self, identifier: str, description: str, baseline_sha: str | None = None) -> dict:
        current = self.read(identifier)
        before = current["content"]
        if baseline_sha and baseline_sha != current["sha"]:
            raise EditorError("Skill changed since it was opened. Reload before editing.", 409)
        self.writable_path(current["skill"])
        after = replace_description(before, description)
        diff = "".join(difflib.unified_diff(before.splitlines(keepends=True), after.splitlines(keepends=True), fromfile="before/SKILL.md", tofile="after/SKILL.md"))
        return {"id": identifier, "before": before, "after": after, "diff": diff, "baselineSha": current["sha"], "protectedBodyUnchanged": True, "editable": True}

    def _state_directory(self, relative: str) -> Path:
        self._physical_internal(self.project)
        target = self._physical_internal(self.project / relative)
        target.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._physical_internal(target)
        return target

    def _intent_path(self, identifier: str) -> Path:
        self.skill(identifier)
        # Personal folder ids stay readable; namespaces are hashed, never paths.
        filename = identifier if re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", identifier) else digest(identifier)[:32]
        return self._state_directory(".skill-editor/intents") / (filename + ".md")

    def intent(self, identifier: str) -> str:
        path = self._intent_path(identifier)
        self._physical_internal(path)
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def save_intent(self, identifier: str, markdown: str) -> dict:
        if not isinstance(markdown, str) or len(markdown) > 100_000:
            raise EditorError("Intent must be Markdown under 100,000 characters.")
        with self._lock:
            path = self._intent_path(identifier)
            self._physical_internal(path)
            self._atomic_write(path, markdown, 0o600)
        return {"id": identifier, "markdown": markdown, "runtimePromptIncluded": False}

    @staticmethod
    def _atomic_write(path: Path, content: str, mode: int) -> None:
        fd, tmp = tempfile.mkstemp(prefix=".skill-editor-", dir=path.parent)
        try:
            os.fchmod(fd, mode)
            with os.fdopen(fd, "wb") as handle:
                handle.write(content.encode("utf-8"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def apply_description(self, identifier: str, description: str, baseline_sha: str) -> dict:
        if not isinstance(baseline_sha, str) or not re.fullmatch(r"[0-9a-f]{64}", baseline_sha):
            raise EditorError("Apply requires the baselineSha returned by preview.")
        with self._lock:
            preview = self.preview(identifier, description, baseline_sha)
            path = self.writable_path(self.skill(identifier))
            if digest(self._read_content(path)) != baseline_sha:
                raise EditorError("Skill changed during preview. Reload before editing.", 409)
            directory = self._state_directory(".state/backups")
            backup = directory / (str(time.time_ns()) + "-" + digest(identifier)[:16] + ".md")
            self._atomic_write(backup, preview["before"], 0o600)
            self._atomic_write(path, preview["after"], stat.S_IMODE(path.stat().st_mode))
            # inventory() re-reads current files; preserve the original disk snapshot.
            return {"ok": True, "id": identifier, "sha": digest(preview["after"]), "backupPath": str(backup), "protectedBodyUnchanged": True}
