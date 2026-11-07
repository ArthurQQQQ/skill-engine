"""Editor-only maintenance cases. Skill contents and quoted evidence are data.

No method executes a skill, calls a model, or applies an installed-file edit.
Predictions are recorded separately from explicitly manual observations.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import stat
import time
import uuid

from .core import EditorError, digest, replace_description


MAX_CASE_BYTES = 20_000_000
SHA_PATTERN = re.compile(r"[0-9a-f]{64}")
ID_PATTERN = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,249}")
CASE_ID_PATTERN = re.compile(r"case-[a-f0-9]{32}")
MUTABLE_FIELDS = {"title", "problem", "authorExpectation", "prompts", "candidates", "observations"}


def _now():
    return datetime.now(timezone.utc).isoformat()


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def context_sha(case):
    """Normative prediction context; histories and volatile read/time fields are excluded."""
    context = {key: copy.deepcopy(case.get(key)) for key in ("problem", "skillIds", "baseline", "catalogSnapshot", "authorExpectation", "prompts", "candidates")}
    if isinstance(context["authorExpectation"], dict):
        context["authorExpectation"].pop("confirmedAt", None)
    # read() annotates quotations against today's files; those annotations are
    # warnings about the saved context, not changes to its original inputs.
    for candidate in context.get("candidates") or []:
        for evidence in candidate.get("evidence", []):
            if not isinstance(evidence, dict):
                continue
            for key in ("currentSha", "currentLineStart", "currentLineEnd"):
                evidence.pop(key, None)
            if evidence.get("method") == "source-quote":
                evidence["status"] = "current" if evidence.get("baselineMatched") else "unmatched"
            elif evidence.get("method") == "manual-unverified":
                evidence["status"] = "unverified"
    return digest(_canonical(context))


def _text(value, label, limit=6000, allow_empty=True):
    if not isinstance(value, str) or len(value) > limit or (not allow_empty and not value.strip()):
        raise EditorError(f"{label} must be text under {limit} characters.")
    if any(ord(char) < 32 and char not in "\n\t\r" for char in value):
        raise EditorError(f"{label} contains unsupported control characters.")
    return value


def _strings(value, label, limit=100, text_limit=4000):
    if not isinstance(value, list) or len(value) > limit:
        raise EditorError(f"{label} must be a list of at most {limit} strings.")
    return [_text(item, label, text_limit) for item in value]


def _identifier(value, label="Identifier"):
    if not isinstance(value, str) or not ID_PATTERN.fullmatch(value):
        raise EditorError(f"{label} is invalid.")
    return value


def _sha(value, label="SHA"):
    if not isinstance(value, str) or not SHA_PATTERN.fullmatch(value):
        raise EditorError(f"{label} must be a SHA-256 digest.")
    return value


class CaseStore:
    """Wrap an existing SkillStore, sharing its lock and internal-path checks."""

    def __init__(self, store):
        self.store = store

    def _directory(self, relative=".state/cases"):
        return self.store._state_directory(relative)

    def _path(self, case_id):
        if not isinstance(case_id, str) or not CASE_ID_PATTERN.fullmatch(case_id):
            raise EditorError("A valid maintenance case id is required.")
        return self.store._physical_internal(self._directory() / (case_id + ".json"))

    def _raw(self, path):
        path = self.store._physical_internal(path)
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise EditorError("Case files must be regular files.", 403)
            raw = handle.read(MAX_CASE_BYTES + 1)
        if len(raw) > MAX_CASE_BYTES:
            raise EditorError("Maintenance case exceeds the size limit.", 413)
        return raw

    def _load(self, case_id):
        try:
            raw = self._raw(self._path(case_id))
        except FileNotFoundError:
            raise EditorError("Maintenance case was not found.", 404) from None
        try:
            document = json.loads(raw)
        except (ValueError, UnicodeError):
            raise EditorError("Maintenance case is invalid JSON; no replacement was written.", 409) from None
        if not isinstance(document, dict) or document.get("id") != case_id or not isinstance(document.get("revision"), int) or isinstance(document.get("revision"), bool) or document["revision"] < 1:
            raise EditorError("Maintenance case has an unsupported structure.", 409)
        if any(not isinstance(document.get(key), list) for key in ("skillIds", "baseline", "catalogSnapshot", "prompts", "candidates", "evidence", "predictions", "observations")) or not isinstance(document.get("authorExpectation"), dict):
            raise EditorError("Maintenance case has an unsupported structure.", 409)
        return document, raw

    @staticmethod
    def _catalog(inventory):
        records = []
        for item in inventory.get("skills", []):
            if item.get("fileStatus") != "present" and not ("fileStatus" not in item and item.get("sha256")):
                continue
            records.append({key: item.get(key) for key in ("id", "name", "description", "sha256", "source")})
        return sorted(records, key=lambda item: str(item["id"]))

    def _capture(self, skill_ids):
        if not isinstance(skill_ids, list) or not 1 <= len(skill_ids) <= 6 or not all(isinstance(item, str) for item in skill_ids) or len(set(skill_ids)) != len(skill_ids):
            raise EditorError("A case must select one to six distinct skill ids.")
        baseline = []
        for identifier in skill_ids:
            _identifier(identifier, "Skill id")
            current = self.store.read(identifier)
            baseline.append({
                "id": identifier, "name": current["skill"]["name"], "sha": current["sha"],
                "description": current["description"], "content": current["content"],
                "editorIntent": current["intent"], "editorIntentSha": digest(current["intent"]),
                "path": current["skill"]["path"], "source": current["skill"].get("source"),
                "editable": bool(current["skill"].get("editable") and current["skill"].get("source") == "personal"),
            })
        catalog = self._catalog(self.store.inventory(include_analysis=False))
        by_id = {item["id"]: item for item in catalog}
        if any(item["id"] not in by_id or by_id[item["id"]].get("sha256") != item["sha"] for item in baseline):
            raise EditorError("A selected source changed during case capture. Reload before creating the case.", 409)
        return baseline, catalog

    @staticmethod
    def _author(value, force_draft=False):
        if not isinstance(value, dict):
            raise EditorError("authorExpectation must be an object.")
        unknown = set(value) - {"status", "goal", "invariants", "notes", "confirmedAt"}
        if unknown:
            raise EditorError("authorExpectation contains unsupported fields.")
        status = "draft" if force_draft else value.get("status", "draft")
        if status not in ("draft", "confirmed"):
            raise EditorError("Author expectation status must be draft or confirmed.")
        goal = _text(value.get("goal", ""), "Author goal", 6000)
        invariants = _strings(value.get("invariants", []), "Author invariants", 50)
        if status == "confirmed" and not goal.strip():
            raise EditorError("A confirmed author expectation needs a goal.")
        result = {"status": status, "goal": goal, "invariants": invariants, "notes": _text(value.get("notes", ""), "Author notes")}
        if status == "confirmed":
            result["confirmedAt"] = _now()
        return result

    @staticmethod
    def _prompts(value, skill_ids):
        if not isinstance(value, list) or len(value) > 100:
            raise EditorError("prompts must contain at most 100 cases.")
        result = []
        seen = set()
        for raw in value:
            if not isinstance(raw, dict) or set(raw) - {"id", "text", "kind", "expectation"}:
                raise EditorError("Each prompt needs id, text, kind and expectation only.")
            identifier = _identifier(raw.get("id") or "prompt-" + uuid.uuid4().hex, "Prompt id")
            if identifier in seen:
                raise EditorError("Prompt ids must be distinct.")
            seen.add(identifier)
            if raw.get("kind") not in ("positive", "negative", "boundary", "combination"):
                raise EditorError("Unsupported prompt kind.")
            expectation = raw.get("expectation", {})
            if not isinstance(expectation, dict) or set(expectation) - {"required", "allowed", "forbidden", "protectedOutputs"}:
                raise EditorError("Prompt expectation contains unsupported fields.")
            normalized = {key: _strings(expectation.get(key, []), "Prompt " + key, 100) for key in ("required", "allowed", "forbidden", "protectedOutputs")}
            for key in ("required", "allowed", "forbidden"):
                if any(identifier not in skill_ids for identifier in normalized[key]) or len(set(normalized[key])) != len(normalized[key]):
                    raise EditorError("Expected skill ids must be distinct members of this case.")
            if set(normalized["forbidden"]) & (set(normalized["required"]) | set(normalized["allowed"])):
                raise EditorError("A skill cannot be both expected and forbidden in the same prompt.")
            result.append({"id": identifier, "text": _text(raw.get("text"), "Prompt text", 10000, False), "kind": raw["kind"], "expectation": normalized})
        return result

    @staticmethod
    def _candidates(value, baseline):
        if not isinstance(value, list) or len(value) > len(baseline):
            raise EditorError("Candidates must contain at most one description proposal per selected skill.")
        by_id = {item["id"]: item for item in baseline}
        result = []
        seen = set()
        allowed_keys = {"id", "description", "sourceSha", "reason", "evidence", "intentPreservation", "risks"}
        for raw in value:
            if not isinstance(raw, dict) or set(raw) - allowed_keys or not isinstance(raw.get("id"), str) or raw.get("id") not in by_id or raw["id"] in seen:
                raise EditorError("Only selected skills may receive one description-only candidate.")
            base = by_id[raw["id"]]
            if not base.get("editable"):
                raise EditorError("System and plugin skills cannot receive edit candidates.", 403)
            source_sha = _sha(raw.get("sourceSha", base["sha"]), "Candidate sourceSha")
            if source_sha != base["sha"]:
                raise EditorError("Candidate sourceSha differs from the captured case baseline.", 409)
            description = _text(raw.get("description"), "Candidate description", 4000, False).strip()
            replace_description(base["content"], description)
            raw_evidence = raw.get("evidence", [])
            if isinstance(raw_evidence, str):
                raw_evidence = [raw_evidence] if raw_evidence else []
            if not isinstance(raw_evidence, list) or len(raw_evidence) > 50:
                raise EditorError("Candidate evidence must contain at most 50 quotations or manual notes.")
            evidence = []
            for item in raw_evidence:
                if isinstance(item, str):
                    evidence.append({"quote": _text(item, "Candidate evidence note", 20000), "status": "unverified", "method": "manual-unverified"})
                elif isinstance(item, dict) and item.get("method") == "manual-unverified" and "skillId" not in item:
                    evidence.append({"quote": _text(item.get("quote"), "Candidate evidence note", 20000), "status": "unverified", "method": "manual-unverified"})
                elif isinstance(item, dict):
                    quoted = CaseStore._evidence([item], baseline)[0]
                    if not quoted["baselineMatched"]:
                        raise EditorError("Candidate evidence quote does not match the captured source.")
                    evidence.append({**quoted, "method": "source-quote"})
                else:
                    raise EditorError("Candidate evidence must be source quotations or manual notes.")
            result.append({
                "id": raw["id"], "description": description, "sourceSha": source_sha,
                "reason": _text(raw.get("reason", ""), "Candidate reason"), "evidence": evidence,
                "intentPreservation": _text(raw.get("intentPreservation", ""), "Intent preservation explanation"),
                "risks": _strings(raw.get("risks", []), "Candidate risks", 50),
            })
            seen.add(raw["id"])
        return result

    @staticmethod
    def _evidence(value, baseline):
        if not isinstance(value, list) or len(value) > 100:
            raise EditorError("Evidence must contain at most 100 quotations.")
        by_id = {item["id"]: item for item in baseline}
        result = []
        seen = set()
        for raw in value:
            if not isinstance(raw, dict) or not isinstance(raw.get("skillId"), str) or raw.get("skillId") not in by_id:
                raise EditorError("Evidence must quote a selected skill.")
            base = by_id[raw["skillId"]]
            identifier = _identifier(raw.get("id") or "evidence-" + uuid.uuid4().hex, "Evidence id")
            if identifier in seen:
                raise EditorError("Evidence ids must be distinct.")
            seen.add(identifier)
            quote = _text(raw.get("quote"), "Evidence quote", 20000, False)
            offset = base["content"].find(quote)
            line = base["content"].count("\n", 0, offset) + 1 if offset >= 0 else None
            result.append({
                "id": identifier, "skillId": raw["skillId"], "path": base["path"], "quote": quote,
                "baselineSha": base["sha"], "baselineMatched": offset >= 0,
                "lineStart": line, "lineEnd": line + quote.count("\n") if line else None,
                "status": "current" if offset >= 0 else "unmatched",
                "reviewStatus": "needs-author-confirmation",
                "claim": _text(raw.get("claim", ""), "Evidence claim"),
                "condition": _text(raw.get("condition", ""), "Evidence condition"),
            })
        return result

    @staticmethod
    def _context(document):
        return {
            "skillIds": document["skillIds"], "problem": document["problem"],
            "baselineShas": {item["id"]: item["sha"] for item in document["baseline"]},
            "baselineIntentShas": {item["id"]: item["editorIntentSha"] for item in document["baseline"]},
            "catalogSnapshotSha": document["catalogSnapshotSha"],
            "authorExpectation": document["authorExpectation"], "prompts": document["prompts"],
            "candidates": document["candidates"],
        }

    @classmethod
    def _context_sha(cls, document):
        return context_sha(document)

    @classmethod
    def _observations(cls, value, document):
        if not isinstance(value, list) or len(value) > 200:
            raise EditorError("Observations must contain at most 200 manual records.")
        prompt_ids = {item["id"] for item in document["prompts"]}
        catalog_ids = {item["id"] for item in document["catalogSnapshot"]} | set(document["skillIds"])
        result = []
        seen = set()
        allowed_keys = {"id", "promptId", "source", "status", "selectedSkillIds", "result", "notes", "evidence", "contextSha", "recordedAt", "contextStatus"}
        for raw in value:
            if not isinstance(raw, dict) or set(raw) - allowed_keys or not isinstance(raw.get("promptId"), str) or raw.get("promptId") not in prompt_ids:
                raise EditorError("Each observation must reference a case prompt and manual observation fields.")
            identifier = _identifier(raw.get("id") or "observation-" + uuid.uuid4().hex, "Observation id")
            if identifier in seen:
                raise EditorError("Observation ids must be distinct.")
            seen.add(identifier)
            source = raw.get("source", "unknown")
            status = raw.get("status", "not-run")
            verdict = raw.get("result", "unknown")
            if source not in ("manual", "unknown") or status not in ("recorded", "not-run") or verdict not in ("pass", "fail", "unknown"):
                raise EditorError("Observations may only be manual or unknown; native execution is not supported.")
            evidence = _text(raw.get("evidence", ""), "Manual observation evidence", 20000)
            if source == "unknown" or status == "not-run":
                verdict = "unknown"
            elif verdict != "unknown" and not evidence.strip():
                raise EditorError("A manual pass/fail requires recorded supporting evidence.")
            selected = _strings(raw.get("selectedSkillIds", []), "Observed skill ids", 100, 250)
            if len(set(selected)) != len(selected) or any(identifier not in catalog_ids for identifier in selected):
                raise EditorError("Observed skill ids must be distinct catalog entries.")
            result.append({
                "id": identifier, "promptId": raw["promptId"], "source": source, "status": status,
                "selectedSkillIds": selected, "result": verdict, "evidence": evidence,
                "notes": _text(raw.get("notes", ""), "Observation notes", 10000),
                "contextSha": _sha(raw.get("contextSha", cls._context_sha(document)), "Observation contextSha"),
                "recordedAt": _text(raw.get("recordedAt", _now()), "Observation recordedAt", 100),
            })
        return result

    def _templates(self):
        path = self.store._physical_internal(self.store.project / "data/case-templates.json")
        if not path.exists():
            return []
        try:
            loaded = json.loads(self._raw(path))
        except (OSError, ValueError, UnicodeError):
            raise EditorError("Case templates could not be read as JSON.", 409) from None
        templates = loaded.get("templates", []) if isinstance(loaded, dict) else []
        if not isinstance(templates, list) or any(not isinstance(item, dict) or not isinstance(item.get("id"), str) for item in templates):
            raise EditorError("Case templates have an unsupported structure.", 409)
        if len({item["id"] for item in templates}) != len(templates):
            raise EditorError("Case template ids must be distinct.", 409)
        return templates

    def list(self):
        with self.store._lock:
            summaries = []
            warnings = []
            for path in sorted(self._directory().glob("*.json")):
                try:
                    document, raw = self._load(path.stem)
                    summaries.append({key: document.get(key) for key in ("id", "title", "problem", "skillIds", "revision", "createdAt", "updatedAt", "templateId")})
                    summaries[-1].update({"sha": digest(raw.decode("utf-8")), "authorStatus": document["authorExpectation"]["status"], "predictionCount": len(document["predictions"]), "observationCount": len(document["observations"])})
                except (EditorError, OSError, UnicodeError, KeyError, TypeError):
                    warnings.append({"path": str(path), "message": "Case could not be read safely; it was left unchanged."})
            try:
                templates = self._templates()
            except EditorError as exc:
                templates = []
                warnings.append({"path": str(self.store.project / "data/case-templates.json"), "message": str(exc)})
            return {"cases": sorted(summaries, key=lambda item: item.get("updatedAt", ""), reverse=True), "warnings": warnings, "templates": templates}

    def _write(self, document, previous_raw=None):
        path = self._path(document["id"])
        encoded = (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        if len(encoded) > MAX_CASE_BYTES:
            raise EditorError("Maintenance case exceeds the size limit.", 413)
        if previous_raw is not None:
            try:
                current = self._raw(path)
            except FileNotFoundError:
                current = None
            if current != previous_raw:
                raise EditorError("Maintenance case changed before saving. Reload before editing.", 409)
            backups = self._directory(".state/cases/backups")
            backup = self.store._physical_internal(backups / (document["id"] + "-" + str(time.time_ns()) + ".json"))
            self.store._atomic_write(backup, previous_raw.decode("utf-8"), 0o600)
            try:
                current = self._raw(path)
            except FileNotFoundError:
                current = None
            if current != previous_raw:
                raise EditorError("Maintenance case changed while backing up. Reload before editing.", 409)
        elif path.exists():
            raise EditorError("Maintenance case id already exists.", 409)
        self.store._physical_internal(path)
        self.store._atomic_write(path, encoded.decode("utf-8"), 0o600)

    def create(self, payload):
        if not isinstance(payload, dict):
            raise EditorError("Case creation payload must be an object.")
        allowed = {"templateId", "title", "problem", "skillIds", "authorExpectation", "prompts", "candidates", "evidence", "observations"}
        if set(payload) - allowed:
            raise EditorError("Case creation contains unsupported or server-owned fields.")
        with self.store._lock:
            template = {}
            if payload.get("templateId") is not None:
                matches = [item for item in self._templates() if item["id"] == payload["templateId"]]
                if len(matches) != 1:
                    raise EditorError("Maintenance case template was not found.", 404)
                template = copy.deepcopy(matches[0])
            data = {**template, **copy.deepcopy(payload)}
            skill_ids = data.get("skillIds")
            baseline, catalog = self._capture(skill_ids)
            now = _now()
            document = {
                "schemaVersion": 1, "id": "case-" + uuid.uuid4().hex, "revision": 1, "createdAt": now, "updatedAt": now,
                "title": _text(data.get("title", "新维护案例"), "Case title", 200, False),
                "problem": _text(data.get("problem", ""), "Case problem", 10000), "skillIds": skill_ids,
                "baseline": baseline, "catalogSnapshot": catalog, "catalogSnapshotSha": digest(_canonical(catalog)),
                "authorExpectation": self._author(data.get("authorExpectation", {}), force_draft=bool(template)),
                "prompts": self._prompts(data.get("prompts", []), skill_ids),
                "candidates": self._candidates(data.get("candidates", []), baseline),
                "evidence": self._evidence(data.get("evidence", []), baseline), "predictions": [], "observations": [],
            }
            if template:
                document.update({"templateId": template["id"], "templateNotes": _text(template.get("notes", ""), "Template notes", 10000)})
            document["observations"] = self._observations(data.get("observations", []), document)
            self._write(document)
            return self.read(document["id"])

    def read(self, case_id):
        with self.store._lock:
            document, raw = self._load(case_id)
            result = copy.deepcopy(document)
            result["sha"] = digest(raw.decode("utf-8"))
            result["contextSha"] = self._context_sha(document)
            source_status = []
            currents = {}
            warnings = []
            for base in document["baseline"]:
                entry = {"id": base["id"], "name": base["name"], "baselineSha": base["sha"], "currentSha": None, "status": "unavailable", "baselineIntentSha": base["editorIntentSha"], "currentIntentSha": None, "intentStatus": "unavailable"}
                try:
                    current = self.store.read(base["id"])
                    currents[base["id"]] = current
                    entry.update({"currentSha": current["sha"], "status": "current" if current["sha"] == base["sha"] else "changed", "currentIntentSha": digest(current["intent"])})
                    entry["intentStatus"] = "current" if entry["currentIntentSha"] == base["editorIntentSha"] else "changed"
                except (EditorError, OSError, UnicodeError):
                    pass
                if entry["status"] != "current" or entry["intentStatus"] != "current":
                    warnings.append({"skillId": base["id"], "message": "Captured source or editor intent differs from the current file, or is unavailable; re-review the case evidence."})
                source_status.append(entry)
            all_evidence = [*result["evidence"], *(evidence for candidate in result["candidates"] for evidence in candidate.get("evidence", []) if isinstance(evidence, dict) and evidence.get("method") == "source-quote")]
            for evidence in all_evidence:
                current = currents.get(evidence["skillId"])
                if not current:
                    evidence["status"] = "unavailable"
                    evidence["currentSha"] = None
                    evidence["currentLineStart"] = None
                    evidence["currentLineEnd"] = None
                else:
                    evidence["currentSha"] = current["sha"]
                    offset = current["content"].find(evidence["quote"])
                    line = current["content"].count("\n", 0, offset) + 1 if offset >= 0 else None
                    evidence["currentLineStart"] = line
                    evidence["currentLineEnd"] = line + evidence["quote"].count("\n") if line else None
                    evidence["status"] = "unmatched" if not evidence["baselineMatched"] else "current" if current["sha"] == evidence["baselineSha"] and offset >= 0 else "stale"
                if evidence["status"] != "current":
                    warnings.append({"evidenceId": evidence["id"], "skillId": evidence["skillId"], "message": "Evidence does not match the captured/current source; it is not proof of a current conflict."})
            try:
                current_catalog = self._catalog(self.store.inventory(include_analysis=False))
                catalog_status = "current" if digest(_canonical(current_catalog)) == document["catalogSnapshotSha"] else "changed"
            except (EditorError, OSError, UnicodeError):
                catalog_status = "unavailable"
            if catalog_status != "current":
                warnings.append({"message": "The metadata catalog differs from the captured prediction catalog, or is unavailable."})
            sources_current = catalog_status == "current" and all(item["status"] == "current" and item["intentStatus"] == "current" for item in source_status)
            for prediction in result["predictions"]:
                prediction["contextStatus"] = "current" if sources_current and prediction.get("contextSha") == result["contextSha"] else "stale"
            for observation in result["observations"]:
                observation["contextStatus"] = "current" if sources_current and observation.get("contextSha") == result["contextSha"] else "stale"
            manual_current = [item for item in result["observations"] if item["source"] == "manual" and item["status"] == "recorded" and item["contextStatus"] == "current" and item["evidence"].strip()]
            result.update({
                "sourceStatus": source_status, "catalogStatus": catalog_status, "warnings": warnings,
                "validationSummary": {"predictionCount": len(result["predictions"]), "manualRecorded": len(manual_current), "manualPassCount": sum(item["result"] == "pass" for item in manual_current) if document["authorExpectation"]["status"] == "confirmed" else 0, "nativeRuns": 0},
            })
            return result

    def update(self, case_id, patch, baseline_sha):
        _sha(baseline_sha, "Case baselineSha")
        if not isinstance(patch, dict) or set(patch) - MUTABLE_FIELDS:
            raise EditorError("Only editable case fields may be patched; predictions and source snapshots are server-owned.")
        with self.store._lock:
            document, raw = self._load(case_id)
            if digest(raw.decode("utf-8")) != baseline_sha:
                raise EditorError("Maintenance case changed since it was opened. Reload before editing.", 409)
            updated = copy.deepcopy(document)
            if "title" in patch:
                updated["title"] = _text(patch["title"], "Case title", 200, False)
            if "problem" in patch:
                updated["problem"] = _text(patch["problem"], "Case problem", 10000)
            if "authorExpectation" in patch:
                updated["authorExpectation"] = self._author(patch["authorExpectation"])
            if "prompts" in patch:
                updated["prompts"] = self._prompts(patch["prompts"], document["skillIds"])
            if ("prompts" in patch or "problem" in patch) and not (isinstance(patch.get("authorExpectation"), dict) and patch["authorExpectation"].get("status") == "confirmed"):
                updated["authorExpectation"]["status"] = "draft"
                updated["authorExpectation"].pop("confirmedAt", None)
            if "candidates" in patch:
                updated["candidates"] = self._candidates(patch["candidates"], document["baseline"])
            if "observations" in patch:
                updated["observations"] = self._observations(patch["observations"], updated)
            elif {item["promptId"] for item in updated["observations"]} - {item["id"] for item in updated["prompts"]}:
                raise EditorError("Removing a prompt requires explicitly updating its manual observations.")
            updated["revision"] += 1
            updated["updatedAt"] = _now()
            self._write(updated, raw)
            return self.read(case_id)

    def preview(self, case_id, skill_id, baseline_sha=None):
        with self.store._lock:
            document, raw = self._load(case_id)
            if baseline_sha is not None and _sha(baseline_sha, "Case baselineSha") != digest(raw.decode("utf-8")):
                raise EditorError("Maintenance case changed before preview. Reload before editing.", 409)
            current_case = self.read(case_id)
            if current_case["sha"] != digest(raw.decode("utf-8")):
                raise EditorError("Maintenance case changed while checking preview sources. Reload before editing.", 409)
            if current_case["catalogStatus"] != "current" or any(item["status"] != "current" or item["intentStatus"] != "current" for item in current_case["sourceStatus"]):
                raise EditorError("Case sources, editor intent or catalog changed before preview. Create a fresh case.", 409)
            candidates = [item for item in document["candidates"] if item["id"] == skill_id]
            if len(candidates) != 1:
                raise EditorError("A saved description candidate was not found for this skill.", 404)
            candidate = candidates[0]
            preview = self.store.preview(skill_id, candidate["description"], candidate["sourceSha"])
            return {**preview, "caseId": case_id, "caseSha": digest(raw.decode("utf-8")), "caseRevision": document["revision"], "applied": False}

    def append_prediction(self, case_id, record, baseline_sha):
        """Server-only call after an OpenRouter response; generic PUT cannot call it."""
        _sha(baseline_sha, "Case baselineSha")
        if not isinstance(record, dict):
            raise EditorError("Prediction record must be an object.")
        with self.store._lock:
            document, raw = self._load(case_id)
            if digest(raw.decode("utf-8")) != baseline_sha:
                raise EditorError("Maintenance case changed while predicting. No prediction was recorded.", 409)
            if document["authorExpectation"]["status"] != "confirmed":
                raise EditorError("Explicitly confirm the author expectation before running a prediction.", 409)
            current_case = self.read(case_id)
            if current_case["catalogStatus"] != "current" or any(item["status"] != "current" or item["intentStatus"] != "current" for item in current_case["sourceStatus"]):
                raise EditorError("Case sources or catalog changed before the prediction was recorded. Create a fresh case.", 409)
            if not document["prompts"]:
                raise EditorError("At least one prompt is required for prediction.")
            if record.get("type", "OpenRouter-prediction") != "OpenRouter-prediction" or record.get("provider", "OpenRouter") != "OpenRouter":
                raise EditorError("This store records OpenRouter predictions only; it cannot record native execution.")
            context_sha = self._context_sha(document)
            if "contextSha" in record and record["contextSha"] != context_sha:
                raise EditorError("Prediction context differs from the current case.", 409)
            prediction = copy.deepcopy(record)
            prediction.update({
                "id": _identifier(record.get("id") or "prediction-" + uuid.uuid4().hex, "Prediction id"),
                "type": "OpenRouter-prediction", "provider": "OpenRouter", "model": _text(record.get("model"), "Prediction model", 250, False),
                "createdAt": _now(), "caseRevision": document["revision"], "caseSha": baseline_sha, "contextSha": context_sha,
                "inputSnapshot": copy.deepcopy(self._context(document)), "applied": False, "nativeExecution": False,
                "protectedBehaviorStatus": "not-evaluated", "actualCodexStatus": "not-run",
            })
            if any(item["id"] == prediction["id"] for item in document["predictions"]):
                raise EditorError("Prediction id already exists.")
            try:
                prediction_bytes = _canonical(prediction).encode("utf-8")
            except (ValueError, TypeError):
                raise EditorError("Prediction record must be JSON data.") from None
            if len(prediction_bytes) > 1_000_000:
                raise EditorError("Prediction record exceeds the size limit.", 413)
            if len(document["predictions"]) >= 100:
                raise EditorError("A case may contain at most 100 prediction records.", 413)
            updated = copy.deepcopy(document)
            updated["predictions"].append(prediction)
            updated["revision"] += 1
            updated["updatedAt"] = _now()
            self._write(updated, raw)
            return self.read(case_id)
