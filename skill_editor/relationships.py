"""Evidence-backed maintenance relationships, never a runtime routing graph.

Reviewed relationships are editor data. Exact quotations are checked against the
currently supplied files on every read; no instructions or resources are run.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re


RELATION_TYPES = {
    "delegation", "overlap", "complementarity", "instruction-conflict",
    "reference", "scope-ambiguity",
}
LABELS = {
    "delegation": "委派", "overlap": "触发重叠", "complementarity": "互补",
    "instruction-conflict": "条件性指令冲突", "reference": "明确引用",
    "scope-ambiguity": "范围待厘清",
}


def _current_content(skill: dict | None) -> str | None:
    if not skill or skill.get("fileStatus", "present") != "present":
        return None
    content = skill.get("content")
    return content if isinstance(content, str) else None


def _check_evidence(spec: dict, by_id: dict[str, dict]) -> dict:
    identifier = spec["skillId"]
    skill = by_id.get(identifier)
    content = _current_content(skill)
    quote = spec["quote"]
    offset = content.find(quote) if content is not None else -1
    status = "unavailable" if content is None else "current" if offset >= 0 else "stale"
    baseline = spec.get("contentSha256")
    current_sha = hashlib.sha256(content.encode("utf-8")).hexdigest() if content is not None else None
    if status == "current" and baseline and baseline != current_sha:
        status = "stale"
    evidence = {
        "skillId": identifier,
        "path": skill.get("path", "") if skill else spec.get("path", ""),
        "line": content.count("\n", 0, offset) + 1 if offset >= 0 else None,
        "quote": quote,
        "status": status,
    }
    if baseline:
        evidence["contentSha256"] = baseline
        evidence["currentContentSha256"] = current_sha
    return evidence


def _reviewed_edges(project: Path, by_id: dict[str, dict]) -> list[dict]:
    try:
        document = json.loads((project / "data/relationships.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        # A missing/broken optional review record must not break inventory reads.
        return []
    records = document.get("relationships", []) if isinstance(document, dict) else document
    baselines = document.get("sourceBaselines", {}) if isinstance(document, dict) else {}
    if not isinstance(baselines, dict):
        baselines = {}
    if not isinstance(records, list):
        return []
    edges = []
    seen = set()
    for record in records:
        if not isinstance(record, dict):
            continue
        source, target = record.get("sourceId"), record.get("targetId")
        identifier, kind = record.get("id"), record.get("type")
        specs = record.get("evidence")
        if (not all(isinstance(value, str) and value for value in (source, target, identifier))
                or source == target or identifier in seen
                or not isinstance(kind, str) or kind not in RELATION_TYPES
                or not isinstance(specs, list) or not specs):
            continue
        if any(not isinstance(spec, dict)
               or not isinstance(spec.get("skillId"), str)
               or not isinstance(spec.get("quote"), str)
               or not spec["quote"].strip() for spec in specs):
            continue
        # Both endpoints need their own source evidence, not an all-pairs expansion
        # of a multi-skill finding or a keyword-grouping inference.
        if not {source, target}.issubset({spec["skillId"] for spec in specs}):
            continue
        evidence = []
        for spec in specs:
            checked = dict(spec)
            baseline = checked.get("contentSha256", baselines.get(spec["skillId"]))
            if isinstance(baseline, str) and re.fullmatch(r"[0-9a-f]{64}", baseline):
                checked["contentSha256"] = baseline
            else:
                checked.pop("contentSha256", None)
            evidence.append(_check_evidence(checked, by_id))
        statuses = {item["status"] for item in evidence}
        status = "unavailable" if "unavailable" in statuses else "stale" if "stale" in statuses else "current"
        edge = {
            "id": identifier, "sourceId": source, "targetId": target,
            "type": kind, "label": LABELS[kind],
            "summary": str(record.get("summary", "")),
            "evidence": evidence, "status": status, "method": "source-review",
            "validation": "content-sha256-and-quote" if all(item.get("contentSha256") for item in evidence) else "quote-presence-only",
        }
        if isinstance(record.get("condition"), str):
            edge["condition"] = record["condition"]
        if record.get("status") == "uncertain" and status == "current":
            edge["status"] = "uncertain"
        edges.append(edge)
        seen.add(identifier)
    return edges


def _reference_edges(skills: list[dict]) -> list[dict]:
    # A reused short name is ambiguous; never choose an arbitrary plugin version.
    owners: dict[str, set[str]] = {}
    for skill in skills:
        if _current_content(skill) is None:
            continue
        identifier = skill.get("id")
        if not isinstance(identifier, str) or not identifier:
            continue
        for alias in (identifier, skill.get("name")):
            if isinstance(alias, str) and len(alias) >= 3:
                owners.setdefault(alias, set()).add(identifier)
    aliases = {alias: next(iter(ids)) for alias, ids in owners.items() if len(ids) == 1}
    edges = []
    for skill in skills:
        source = skill.get("id")
        content = _current_content(skill)
        if content is None or not isinstance(source, str):
            continue
        references: dict[str, list[dict]] = {}
        for alias, target in sorted(aliases.items(), key=lambda pair: (-len(pair[0]), pair[0])):
            if target == source:
                continue
            # Hyphens and colons are name characters, so paper-reviewer cannot
            # match the tail of reliable-paper-reviewer or a plugin namespace.
            pattern = r"(?<![\w:-])" + re.escape(alias) + r"(?![\w:-])"
            for match in re.finditer(pattern, content):
                start = content.rfind("\n", 0, match.start()) + 1
                end = content.find("\n", match.end())
                end = len(content) if end < 0 else end
                line_text = content[start:end]
                if not any(char in alias for char in "-:"):
                    # Ordinary words such as "documents" are not enough to
                    # establish a skill reference without invocation/path cues.
                    relative = match.start() - start
                    before = line_text[:relative]
                    after = line_text[relative + len(alias):]
                    if not (before.endswith(("`", "$", "/"))
                            or after.startswith(("`", "/SKILL.md"))
                            or re.match(r"\s+skill\b", after, re.IGNORECASE)):
                        continue
                items = references.setdefault(target, [])
                if len(items) >= 3:
                    continue
                # Keep quotations bounded, with the name itself always visible.
                quote_start = max(start, match.start() - 150)
                quote_end = min(end, match.end() + 250)
                quote = content[quote_start:quote_end]
                if any(item["quote"] == quote for item in items):
                    continue
                items.append({"skillId": source, "path": skill.get("path", ""),
                              "line": content.count("\n", 0, start) + 1,
                              "quote": quote, "status": "current"})
        for target, evidence in references.items():
            token = hashlib.sha256((source + "\0" + target).encode("utf-8")).hexdigest()[:20]
            edges.append({
                "id": "reference-" + token, "sourceId": source, "targetId": target,
                "type": "reference", "label": LABELS["reference"],
                "summary": "原文明示引用另一个 skill；引用本身不证明委派或冲突。",
                "evidence": evidence, "status": "current", "method": "explicit-reference",
                "validation": "literal-reference-only",
            })
    return edges


def build_relationships(skills: list[dict], project: Path) -> list[dict]:
    """Return reviewed pairs and literal references using supplied content only.

    ``current`` means quotations still match, not that route selection or the
    interpretation has been experimentally validated. Invalid optional review
    records are skipped; absent source files never validate cached quotations.
    """
    by_id = {skill["id"]: skill for skill in skills
             if isinstance(skill, dict) and isinstance(skill.get("id"), str)}
    return _reviewed_edges(Path(project), by_id) + _reference_edges(list(by_id.values()))
