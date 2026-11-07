"""Isolated case persistence tests; run only in an authorized CPU environment."""
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from skill_editor.cases import CaseStore, context_sha
from skill_editor.core import EditorError, SkillStore, digest


class CaseStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name).resolve()
        self.personal = self.project / "personal"
        self.personal.mkdir()
        (self.project / "data").mkdir()
        self.files = {}
        records = []
        for identifier, source in (("structure", "personal"), ("reviewer", "personal"), ("plugin", "plugin")):
            path = self.personal / identifier / "SKILL.md"
            path.parent.mkdir()
            path.write_text('---\nname: ' + identifier + '\ndescription: "Original ' + identifier + '"\n---\nKeep every old subsection.\nQuoted evidence.\n', encoding="utf-8")
            self.files[identifier] = path
            records.append({"id": identifier, "name": identifier, "description": "Original " + identifier, "path": str(path), "source": source, "editable": source == "personal"})
        (self.project / "data/inventory.json").write_text(json.dumps({"skills": records}), encoding="utf-8")
        self.store = SkillStore(self.project, self.personal)
        self.cases = CaseStore(self.store)

    def tearDown(self):
        self.temp.cleanup()

    def payload(self, confirmed=False):
        return {
            "title": "Structure-only boundary", "problem": "Which skill should handle an outline migration?",
            "skillIds": ["structure", "reviewer"],
            "authorExpectation": {"status": "confirmed" if confirmed else "draft", "goal": "Return an outline and a complete migration map.", "invariants": ["Preserve all old subsections."]},
            "prompts": [
                {"id": "outline", "text": "Reorder sections without acceptance scoring.", "kind": "positive", "expectation": {"required": ["structure"], "allowed": [], "forbidden": ["reviewer"], "protectedOutputs": ["Complete migration map"]}},
                {"id": "review", "text": "Review the paper's evidence.", "kind": "negative", "expectation": {"required": ["reviewer"], "allowed": [], "forbidden": ["structure"], "protectedOutputs": []}},
            ],
            "evidence": [{"id": "quote", "skillId": "structure", "path": "/untrusted/path", "line": 999, "quote": "Keep every old subsection."}],
        }

    def create(self, confirmed=False):
        return self.cases.create(self.payload(confirmed))

    def candidate(self):
        return {"id": "structure", "description": "Use for outline reordering and complete subsection migration.", "reason": "Clarify the requested product.", "evidence": "Keep every old subsection."}

    def prediction(self, case):
        return {
            "id": "prediction-one", "type": "OpenRouter-prediction", "provider": "OpenRouter", "model": "fixture/model", "seed": 0,
            "catalogScope": "selected-case-skills", "contextSha": context_sha(case), "results": [],
            "summary": {"total": 0}, "protectedBehaviorStatus": "not-evaluated", "actualCodexStatus": "not-run",
        }

    def test_create_captures_current_sources_and_metadata_catalog_without_execution(self):
        originals = {identifier: path.read_bytes() for identifier, path in self.files.items()}
        created = self.create()
        self.assertEqual(created["revision"], 1)
        self.assertEqual(created["authorExpectation"]["status"], "draft")
        self.assertEqual(created["baseline"][0]["content"], originals["structure"].decode())
        self.assertEqual(created["baseline"][0]["sha"], digest(originals["structure"].decode()))
        self.assertEqual(len(created["catalogSnapshot"]), 3)
        self.assertEqual(set(created["catalogSnapshot"][0]), {"id", "name", "description", "sha256", "source"})
        self.assertEqual(created["sourceStatus"][0]["status"], "current")
        self.assertEqual(created["validationSummary"]["nativeRuns"], 0)
        self.assertEqual({identifier: path.read_bytes() for identifier, path in self.files.items()}, originals)

    def test_case_freshness_checks_skip_relationship_analysis_but_detect_catalog_changes(self):
        with patch("skill_editor.relationships.build_relationships", side_effect=AssertionError("Case checks must not analyze relationships")), patch("skill_editor.core.conservative_findings", side_effect=AssertionError("Case checks must not analyze findings")):
            created = self.create()
            self.assertEqual(created["catalogStatus"], "current")
            self.files["plugin"].write_text(self.files["plugin"].read_text().replace("Original plugin", "Externally changed plugin"))
            current = self.cases.read(created["id"])
        self.assertEqual(current["catalogStatus"], "changed")
        self.assertTrue(all(item["status"] == "current" for item in current["sourceStatus"]))
        self.assertEqual(current["sha"], created["sha"])

    def test_template_creation_never_confirms_inferred_expectations(self):
        template = {**self.payload(True), "id": "section-template", "notes": "Hypothesis, not an observed routing failure."}
        (self.project / "data/case-templates.json").write_text(json.dumps({"templates": [template]}))
        listed = self.cases.list()
        self.assertEqual(listed["templates"][0]["id"], "section-template")
        created = self.cases.create({"templateId": "section-template", "authorExpectation": {"status": "confirmed", "goal": "An explicit override is still a template draft."}})
        self.assertEqual(created["authorExpectation"]["status"], "draft")
        self.assertNotIn("confirmedAt", created["authorExpectation"])
        self.assertEqual(len(created["prompts"]), 2)
        self.assertEqual(created["evidence"][0]["path"], str(self.files["structure"]))
        self.assertEqual(created["evidence"][0]["lineStart"], 5)
        self.assertEqual(created["evidence"][0]["status"], "current")

    def test_goal_text_does_not_imply_author_confirmation(self):
        payload = self.payload()
        payload["authorExpectation"].pop("status")
        created = self.cases.create(payload)
        self.assertEqual(created["authorExpectation"]["status"], "draft")
        confirmed = self.cases.update(created["id"], {"authorExpectation": {"status": "confirmed", "goal": "Author explicitly chose this output.", "invariants": []}}, created["sha"])
        self.assertEqual(confirmed["authorExpectation"]["status"], "confirmed")
        unconfirmed = self.cases.update(confirmed["id"], {"authorExpectation": {"goal": "Changed without a new explicit confirmation."}}, confirmed["sha"])
        self.assertEqual(unconfirmed["authorExpectation"]["status"], "draft")

    def test_update_requires_fresh_case_sha_and_preserves_exact_backup(self):
        created = self.create()
        case_path = self.project / ".state/cases" / (created["id"] + ".json")
        original = case_path.read_bytes()
        updated = self.cases.update(created["id"], {"title": "Updated case title"}, created["sha"])
        self.assertEqual(updated["revision"], 2)
        self.assertNotEqual(updated["sha"], created["sha"])
        backups = list((self.project / ".state/cases/backups").glob("*.json"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), original)
        with self.assertRaises(EditorError) as caught:
            self.cases.update(created["id"], {"title": "Stale edit"}, created["sha"])
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(self.cases.read(created["id"])["title"], "Updated case title")

    def test_server_owned_snapshots_predictions_and_body_edits_are_rejected(self):
        created = self.create()
        for field in ("baseline", "catalogSnapshot", "evidence", "predictions", "revision", "skillIds"):
            with self.subTest(field=field), self.assertRaises(EditorError):
                self.cases.update(created["id"], {field: []}, created["sha"])
        with self.assertRaises(EditorError):
            self.cases.create({**self.payload(), "predictions": [self.prediction(created)]})
        with self.assertRaises(EditorError):
            self.cases.update(created["id"], {"candidates": [{**self.candidate(), "body": "Unsolicited workflow rewrite"}]}, created["sha"])
        with self.assertRaises(EditorError):
            self.cases.update(created["id"], {"candidates": [{**self.candidate(), "sourceSha": "0" * 64}]}, created["sha"])

    def test_candidate_preview_is_description_only_and_stale_source_is_rejected(self):
        created = self.create()
        saved = self.cases.update(created["id"], {"candidates": [self.candidate()]}, created["sha"])
        original = self.files["structure"].read_bytes()
        with patch.object(self.store, "apply_description", side_effect=AssertionError("Must never apply")):
            preview = self.cases.preview(saved["id"], "structure", saved["sha"])
        self.assertFalse(preview["applied"])
        self.assertTrue(preview["protectedBodyUnchanged"])
        self.assertIn("+description:", preview["diff"])
        self.assertEqual(self.files["structure"].read_bytes(), original)
        self.files["structure"].write_bytes(original + b"External edit\n")
        with self.assertRaises(EditorError) as caught:
            self.cases.preview(saved["id"], "structure", saved["sha"])
        self.assertEqual(caught.exception.status, 409)

    def test_candidate_quotations_are_verified_instead_of_trusting_provider_status(self):
        created = self.create()
        quoted = {"skillId": "structure", "quote": "Keep every old subsection.", "baselineSha": "0" * 64, "lineStart": 999, "status": "current", "method": "provider-claim"}
        saved = self.cases.update(created["id"], {"candidates": [{**self.candidate(), "evidence": [quoted]}]}, created["sha"])
        evidence = saved["candidates"][0]["evidence"][0]
        self.assertEqual(evidence["baselineSha"], created["baseline"][0]["sha"])
        self.assertEqual(evidence["lineStart"], 5)
        self.assertEqual(evidence["method"], "source-quote")
        self.assertEqual(context_sha(saved), saved["contextSha"])
        self.files["structure"].write_text(self.files["structure"].read_text() + "External edit\n")
        changed = self.cases.read(saved["id"])
        self.assertEqual(changed["candidates"][0]["evidence"][0]["status"], "stale")
        self.assertEqual(context_sha(changed), saved["contextSha"])
        with self.assertRaises(EditorError):
            self.cases.update(changed["id"], {"candidates": [{**self.candidate(), "evidence": [{"skillId": "structure", "quote": "Provider invented this instruction.", "status": "current"}]}]}, changed["sha"])

    def test_preview_rejects_changed_comparison_source_intent_and_catalog(self):
        for changed_part in ("comparison-source", "editor-intent", "unselected-catalog-source"):
            with self.subTest(changed_part=changed_part):
                created = self.create()
                candidate = {**self.candidate(), "evidence": [{"skillId": "reviewer", "quote": "Keep every old subsection."}]}
                saved = self.cases.update(created["id"], {"candidates": [candidate]}, created["sha"])
                target_original = self.files["structure"].read_bytes()
                if changed_part == "comparison-source":
                    self.files["reviewer"].write_text(self.files["reviewer"].read_text() + "Changed comparison scope\n")
                elif changed_part == "editor-intent":
                    self.store.save_intent("reviewer", "Changed editor-only purpose after the candidate was saved.")
                else:
                    self.files["plugin"].write_text(self.files["plugin"].read_text() + "Changed frozen catalog source\n")
                # Target skill and case JSON remain unchanged in all three cases.
                self.assertEqual(self.files["structure"].read_bytes(), target_original)
                self.assertEqual(self.cases.read(saved["id"])["sha"], saved["sha"])
                with patch.object(self.store, "preview") as preview:
                    with self.assertRaises(EditorError) as caught:
                        self.cases.preview(saved["id"], "structure", saved["sha"])
                    self.assertEqual(caught.exception.status, 409)
                    preview.assert_not_called()

    def test_system_and_plugin_candidates_are_read_only(self):
        payload = self.payload()
        payload.update({"skillIds": ["plugin"], "prompts": [], "evidence": []})
        created = self.cases.create(payload)
        with self.assertRaises(EditorError) as caught:
            self.cases.update(created["id"], {"candidates": [{"id": "plugin", "description": "Must not write managed files"}]}, created["sha"])
        self.assertEqual(caught.exception.status, 403)

    def test_read_reports_changed_missing_and_unmatched_evidence_without_repair_claim(self):
        payload = self.payload()
        payload["evidence"].append({"id": "unmatched", "skillId": "reviewer", "quote": "A removed old instruction."})
        created = self.cases.create(payload)
        self.assertEqual(created["evidence"][1]["status"], "unmatched")
        self.files["structure"].write_text(self.files["structure"].read_text() + "Unrelated external edit\n")
        self.files["reviewer"].unlink()
        read = self.cases.read(created["id"])
        self.assertEqual([item["status"] for item in read["sourceStatus"]], ["changed", "unavailable"])
        self.assertEqual(read["evidence"][0]["status"], "stale")
        self.assertEqual(read["evidence"][1]["status"], "unavailable")
        self.assertTrue(read["warnings"])
        self.assertEqual(read["sha"], created["sha"])

    def test_context_hash_ignores_read_and_history_fields_but_tracks_prediction_inputs(self):
        created = self.create()
        modified = copy.deepcopy(created)
        modified.update({"revision": 500, "updatedAt": "later", "sha": "0" * 64, "warnings": ["dynamic"], "predictions": [{"fake": "history"}], "observations": [{"fake": "manual history"}]})
        modified["authorExpectation"]["confirmedAt"] = "volatile timestamp"
        self.assertEqual(context_sha(modified), context_sha(created))
        for field, value in (("problem", "Different premise"), ("candidates", [self.candidate()]), ("catalogSnapshot", [])):
            changed = {**created, field: value}
            self.assertNotEqual(context_sha(changed), context_sha(created))

    def test_prediction_append_requires_confirmation_and_keeps_same_input_context(self):
        draft = self.create()
        with self.assertRaises(EditorError):
            self.cases.append_prediction(draft["id"], self.prediction(draft), draft["sha"])
        confirmed = self.cases.update(draft["id"], {"authorExpectation": {**draft["authorExpectation"], "status": "confirmed"}}, draft["sha"])
        record = self.prediction(confirmed)
        record.update({"actualCodexStatus": "passed", "protectedBehaviorStatus": "passed", "nativeExecution": True})
        saved = self.cases.append_prediction(confirmed["id"], record, confirmed["sha"])
        prediction = saved["predictions"][0]
        self.assertEqual(prediction["type"], "OpenRouter-prediction")
        self.assertEqual(prediction["contextSha"], context_sha(confirmed))
        self.assertEqual(prediction["contextStatus"], "current")
        self.assertEqual(prediction["caseSha"], confirmed["sha"])
        self.assertEqual(prediction["inputSnapshot"]["prompts"], confirmed["prompts"])
        self.assertEqual(context_sha(saved), context_sha(confirmed))
        self.assertEqual(prediction["actualCodexStatus"], "not-run")
        self.assertEqual(prediction["protectedBehaviorStatus"], "not-evaluated")
        self.assertFalse(prediction["nativeExecution"])
        self.assertEqual(saved["validationSummary"]["manualPassCount"], 0)

    def test_prediction_context_and_sources_cannot_go_stale_during_recording(self):
        confirmed = self.create(True)
        stale_context = {**self.prediction(confirmed), "contextSha": "0" * 64}
        with self.assertRaises(EditorError):
            self.cases.append_prediction(confirmed["id"], stale_context, confirmed["sha"])
        self.files["structure"].write_text(self.files["structure"].read_text() + "External edit\n")
        with self.assertRaises(EditorError) as caught:
            self.cases.append_prediction(confirmed["id"], self.prediction(confirmed), confirmed["sha"])
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(self.cases.read(confirmed["id"])["predictions"], [])

    def test_saved_prediction_becomes_stale_after_candidate_or_source_changes(self):
        confirmed = self.create(True)
        predicted = self.cases.append_prediction(confirmed["id"], self.prediction(confirmed), confirmed["sha"])
        changed = self.cases.update(predicted["id"], {"candidates": [self.candidate()]}, predicted["sha"])
        self.assertEqual(changed["predictions"][0]["contextStatus"], "stale")
        other = self.create(True)
        predicted_other = self.cases.append_prediction(other["id"], self.prediction(other), other["sha"])
        self.store.save_intent("structure", "New external editor intent")
        read = self.cases.read(other["id"])
        self.assertEqual(read["sourceStatus"][0]["intentStatus"], "changed")
        self.assertEqual(read["predictions"][0]["contextStatus"], "stale")
        self.assertEqual(read["sha"], predicted_other["sha"])

    def test_not_run_observation_cannot_count_as_pass_and_manual_evidence_is_required(self):
        created = self.create(True)
        observation = {"id": "manual-one", "promptId": "outline", "source": "unknown", "status": "not-run", "result": "pass"}
        unknown = self.cases.update(created["id"], {"observations": [observation]}, created["sha"])
        self.assertEqual(unknown["observations"][0]["result"], "unknown")
        self.assertEqual(unknown["validationSummary"]["manualPassCount"], 0)
        observation.update({"source": "manual", "status": "recorded"})
        with self.assertRaises(EditorError):
            self.cases.update(unknown["id"], {"observations": [observation]}, unknown["sha"])
        observation.update({"selectedSkillIds": ["structure"], "evidence": "Author manually recorded selection: structure, and checked the output."})
        manual = self.cases.update(unknown["id"], {"observations": [observation]}, unknown["sha"])
        self.assertEqual(manual["validationSummary"]["manualPassCount"], 1)
        self.assertEqual(manual["validationSummary"]["nativeRuns"], 0)
        with self.assertRaises(EditorError):
            self.cases.update(manual["id"], {"observations": [{**observation, "source": "Codex-native"}]}, manual["sha"])

    def test_changing_prompts_requires_explicit_reconfirmation(self):
        created = self.create(True)
        changed_prompts = copy.deepcopy(created["prompts"])
        changed_prompts[0]["text"] = "A different user request."
        changed = self.cases.update(created["id"], {"prompts": changed_prompts}, created["sha"])
        self.assertEqual(changed["authorExpectation"]["status"], "draft")
        reconfirmed = self.cases.update(changed["id"], {"prompts": changed_prompts, "authorExpectation": {**changed["authorExpectation"], "status": "confirmed"}}, changed["sha"])
        self.assertEqual(reconfirmed["authorExpectation"]["status"], "confirmed")
        changed_problem = self.cases.update(reconfirmed["id"], {"problem": "A different problem requiring renewed author confirmation."}, reconfirmed["sha"])
        self.assertEqual(changed_problem["authorExpectation"]["status"], "draft")

    def test_invalid_unhashable_skill_selection_is_a_validation_error(self):
        with self.assertRaises(EditorError):
            self.cases.create({**self.payload(), "skillIds": [{"id": "structure"}]})

    def test_symlinked_case_and_case_root_are_rejected(self):
        created = self.create()
        path = self.project / ".state/cases" / (created["id"] + ".json")
        outside = self.project / "other-case.json"
        path.rename(outside)
        path.symlink_to(outside)
        with self.assertRaises(EditorError):
            self.cases.read(created["id"])
        listed = self.cases.list()
        self.assertEqual(listed["cases"], [])
        self.assertEqual(len(listed["warnings"]), 1)
        with self.assertRaises(EditorError):
            self.cases.read("../../outside")

    def test_failed_commit_keeps_old_case_and_exact_backup(self):
        created = self.create()
        path = self.project / ".state/cases" / (created["id"] + ".json")
        original = path.read_bytes()
        original_replace = os.replace

        def fail_case_commit(source, target):
            if Path(target) == path:
                raise OSError("fixture commit failure")
            return original_replace(source, target)

        with patch("skill_editor.core.os.replace", side_effect=fail_case_commit):
            with self.assertRaises(OSError):
                self.cases.update(created["id"], {"title": "Never committed"}, created["sha"])
        self.assertEqual(path.read_bytes(), original)
        backups = list((self.project / ".state/cases/backups").glob("*.json"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
