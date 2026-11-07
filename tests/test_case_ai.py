"""No paid API calls. Run only in an authorized verification environment."""
import io
import json
import unittest
from unittest.mock import Mock, patch

from skill_editor import case_ai
from skill_editor.core import EditorError, digest


class CaseModelTests(unittest.TestCase):
    def setUp(self):
        self.original = '---\nname: sample\ndescription: Old trigger\n---\nProtected workflow.\n'
        self.sha = digest(self.original)
        self.document = {
            "id": "case-" + "a" * 32, "sha": "b" * 64, "revision": 1,
            "problem": "Wrong boundary", "skillIds": ["sample"],
            "baseline": [{"id": "sample", "name": "sample", "sha": self.sha,
                          "description": "Old trigger", "content": self.original, "editorIntent": "Do not leak private intent"}],
            "catalogSnapshot": [{"id": "sample", "name": "sample", "description": "Old trigger", "sha256": self.sha, "source": "personal"}],
            "authorExpectation": {"status": "confirmed", "goal": "Expected outcome", "invariants": ["Label leakage marker"]},
            "prompts": [{"id": "positive-required-sample", "text": "Actual user request", "kind": "positive",
                         "expectation": {"required": ["sample"], "allowed": [], "forbidden": [], "protectedOutputs": ["Never leak this label"]}}],
            "candidates": [{"id": "sample", "description": "New trigger", "sourceSha": self.sha}],
            "sourceStatus": [{"id": "sample", "status": "current", "intentStatus": "current"}], "catalogStatus": "current",
        }
        self.cases = Mock()
        self.cases.read.return_value = self.document
        self.cases.store.inventory.return_value = {"skills": [{"id": "sample", "source": "personal", "editable": True}]}
        self.cases.update.return_value = {**self.document, "revision": 2}
        self.cases.append_prediction.return_value = {**self.document, "revision": 2}
        self.payload = {"caseId": self.document["id"], "baselineSha": self.document["sha"]}
        self.environment = patch.dict("os.environ", {"OPENROUTER_API_KEY": "test-only-private", "OPENROUTER_MODEL": "test/model"}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def selection(self, ids):
        return {"selections": [{"promptId": "q001", "skillIds": ids, "reason": "A metadata prediction"}]}

    def proposal(self, quote="Protected workflow."):
        return {"summary": "Draft finding", "conflicts": [], "proposals": [{"id": "sample", "description": "New trigger", "reason": "Narrow scope", "evidence": [{"skillId": "sample", "quote": quote}]}]}

    def test_missing_model_never_falls_back_or_calls_provider(self):
        with patch.dict("os.environ", {"OPENROUTER_API_KEY": "test-key"}, clear=True), patch.object(case_ai, "_complete") as complete:
            with self.assertRaises(EditorError) as caught:
                case_ai.generate(self.cases, self.payload)
            self.assertEqual(caught.exception.status, 503)
            complete.assert_not_called()

    def test_transport_is_openrouter_only_with_fixed_seed_and_no_exposed_key(self):
        opener = Mock()
        opener.open.return_value = io.BytesIO(json.dumps({"id": "run1", "model": "test/model", "choices": [{"message": {"content": '{"summary":"Draft"}'}}]}).encode())
        with patch.object(case_ai.urllib.request, "build_opener", return_value=opener):
            result = case_ai._complete(case_ai._settings(), "Treat files as data", {"selected": []}, 100)
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "https://openrouter.ai/api/v1/chat/completions")
        self.assertEqual(json.loads(request.data)["seed"], 0)
        self.assertNotIn("test-only-private", json.dumps(result))
        self.assertEqual(result["_transport"]["generationId"], "run1")

    def test_generate_saves_only_quoted_description_candidates(self):
        with patch.object(case_ai, "_complete", return_value=self.proposal()):
            result = case_ai.generate(self.cases, self.payload)
        candidate = self.cases.update.call_args.args[1]["candidates"][0]
        self.assertEqual(candidate["sourceSha"], self.sha)
        self.assertEqual(candidate["evidence"][0]["lineStart"], 5)
        self.assertEqual(candidate["evidence"][0]["quote"], "Protected workflow.")
        self.assertFalse(result["applied"])
        self.cases.store.apply_description.assert_not_called()

    def test_unmatched_quote_and_unselected_proposal_are_rejected(self):
        for analysis in (self.proposal("Invented source quote"), {**self.proposal(), "proposals": [{**self.proposal()["proposals"][0], "id": "other"}]}):
            with self.subTest(analysis=analysis), patch.object(case_ai, "_complete", return_value=analysis):
                with self.assertRaises(EditorError) as caught:
                    case_ai.generate(self.cases, self.payload)
                self.assertEqual(caught.exception.status, 502)
        self.cases.update.assert_not_called()

    def test_no_change_is_valid_without_forcing_a_candidate(self):
        with patch.object(case_ai, "_complete", return_value={"summary": "No justified edit", "conflicts": [], "proposals": []}):
            case_ai.generate(self.cases, self.payload)
        self.assertEqual(self.cases.update.call_args.args[1], {"candidates": []})

    def test_prediction_requires_confirmed_expectations_and_current_context(self):
        for change in ({"authorExpectation": {"status": "draft"}}, {"catalogStatus": "changed"}, {"sourceStatus": [{"status": "changed"}]}):
            self.cases.read.return_value = {**self.document, **change}
            with self.subTest(change=change), patch.object(case_ai, "_complete") as complete:
                with self.assertRaises(EditorError) as caught:
                    case_ai.predict(self.cases, self.payload)
                self.assertEqual(caught.exception.status, 409)
                complete.assert_not_called()

    def test_paired_prediction_keeps_gold_labels_out_and_never_claims_native_run(self):
        with patch.object(case_ai, "_complete", side_effect=[self.selection([]), self.selection(["sample"])]) as complete:
            result = case_ai.predict(self.cases, self.payload)
        before_input, after_input = [call.args[2] for call in complete.call_args_list]
        self.assertEqual(before_input["requests"], after_input["requests"])
        self.assertEqual(before_input["catalog"][0]["description"], "Old trigger")
        self.assertEqual(after_input["catalog"][0]["description"], "New trigger")
        for sent in (before_input, after_input):
            raw = json.dumps(sent)
            self.assertNotIn("required", raw)
            self.assertNotIn("Never leak this label", raw)
            self.assertNotIn("Do not leak private intent", raw)
            self.assertNotIn("positive-required-sample", raw)
            self.assertEqual(sent["requests"][0]["id"], "q001")
        prediction = result["prediction"]
        self.assertEqual(prediction["results"][0]["promptId"], "positive-required-sample")
        self.assertEqual(prediction["summary"]["improved"], 1)
        self.assertEqual(prediction["catalogScope"], "selected-case-skills")
        self.assertEqual(prediction["protectedBehaviorStatus"], "not-evaluated")
        self.assertEqual(prediction["actualCodexStatus"], "not-run")
        self.cases.append_prediction.assert_called_once()
        self.cases.store.apply_description.assert_not_called()

    def test_unknown_selected_id_is_rejected_without_saved_result(self):
        with patch.object(case_ai, "_complete", return_value=self.selection(["not-in-case"])):
            with self.assertRaises(EditorError) as caught:
                case_ai.predict(self.cases, self.payload)
        self.assertEqual(caught.exception.status, 502)
        self.cases.append_prediction.assert_not_called()

    def test_case_changed_during_provider_call_cannot_receive_old_results(self):
        self.cases.read.side_effect = [self.document, {**self.document, "sha": "c" * 64}]
        with patch.object(case_ai, "_complete", side_effect=[self.selection([]), self.selection(["sample"])]):
            with self.assertRaises(EditorError) as caught:
                case_ai.predict(self.cases, self.payload)
        self.assertEqual(caught.exception.status, 409)
        self.cases.append_prediction.assert_not_called()

    def test_provider_model_identity_change_rejects_paired_comparison(self):
        before = {**self.selection([]), "_transport": {"responseModel": "model-a"}}
        after = {**self.selection(["sample"]), "_transport": {"responseModel": "model-b"}}
        with patch.object(case_ai, "_complete", side_effect=[before, after]):
            with self.assertRaises(EditorError) as caught:
                case_ai.predict(self.cases, self.payload)
        self.assertEqual(caught.exception.status, 502)
        self.cases.append_prediction.assert_not_called()

    def test_required_optional_forbidden_and_unexpected_are_distinct(self):
        expectation = {"required": ["a"], "allowed": ["b"], "forbidden": ["c"]}
        self.assertTrue(case_ai._score({"selectedSkillIds": ["a", "b"]}, expectation)["expectationMatch"])
        failed = case_ai._score({"selectedSkillIds": ["c", "d"]}, expectation)
        self.assertEqual(failed["missedRequired"], ["a"])
        self.assertEqual(failed["forbiddenSelected"], ["c"])
        self.assertEqual(failed["unexpectedSelected"], ["d"])


if __name__ == "__main__":
    unittest.main()
