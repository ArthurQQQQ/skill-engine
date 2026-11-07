"""Network-free configuration checks; no provider calls are made."""
import json
import io
import unittest
from unittest.mock import Mock, patch

from skill_editor import ai
from skill_editor.core import EditorError


class ConfigurationTests(unittest.TestCase):
    def test_configuration_never_returns_key(self):
        with patch.dict("os.environ", {"OPENROUTER_API_KEY": "test-private-value", "OPENROUTER_MODEL": "test/model"}, clear=True):
            result = ai.config()
        self.assertTrue(result["aiConfigured"])
        self.assertNotIn("test-private-value", json.dumps(result))

    def test_missing_model_is_not_silently_substituted(self):
        with patch.dict("os.environ", {"OPENROUTER_API_KEY": "test-private-value"}, clear=True):
            self.assertFalse(ai.config()["aiConfigured"])
            with self.assertRaises(EditorError) as caught:
                ai.analyze(None, {"id": "example"})
        self.assertEqual(caught.exception.status, 503)


class ProposalTests(unittest.TestCase):
    def store(self):
        store = Mock()
        store.read.return_value = {"skill": {"name": "sample"}, "content": "Protected workflow", "intent": "Preserve the underlying problem"}
        store.inventory.return_value = {"skills": [
            {"id": "sample", "name": "sample", "description": "Old", "editable": True},
            {"id": "other", "name": "other", "description": "Different purpose", "editable": True},
        ]}
        return store

    def run_analysis(self, store, target):
        analysis = {"summary": "Draft only", "conflicts": [], "proposals": [{"id": target, "description": "New trigger"}]}
        envelope = {"choices": [{"message": {"content": json.dumps(analysis)}}]}
        opener = Mock()
        opener.open.return_value = io.BytesIO(json.dumps(envelope).encode())
        with patch.dict("os.environ", {"OPENROUTER_API_KEY": "test-key", "OPENROUTER_MODEL": "test/model"}, clear=True), patch.object(ai.urllib.request, "build_opener", return_value=opener):
            result = ai.analyze(store, {"id": "sample"})
        return result, opener

    def test_selected_proposal_is_advisory_and_uses_openrouter(self):
        store = self.store()
        result, opener = self.run_analysis(store, "sample")
        self.assertFalse(result["applied"])
        req = opener.open.call_args.args[0]
        self.assertEqual(req.full_url, "https://openrouter.ai/api/v1/chat/completions")
        payload = json.loads(req.data)
        selected = json.loads(payload["messages"][1]["content"])["selected"]
        self.assertEqual(selected[0]["editorOnlyIntent"], "Preserve the underlying problem")
        store.apply_description.assert_not_called()

    def test_unselected_skill_cannot_be_rewritten_from_description_only(self):
        store = self.store()
        with self.assertRaises(EditorError) as caught:
            self.run_analysis(store, "other")
        self.assertEqual(caught.exception.status, 502)
        store.apply_description.assert_not_called()

    def test_multiple_selected_skills_include_full_context_and_individual_drafts(self):
        store = self.store()
        store.read.side_effect = lambda identifier: {
            "skill": {"name": identifier}, "content": "Protected body: " + identifier,
            "intent": "Purpose: " + identifier,
        }
        analysis = {"summary": "Compare two", "conflicts": [], "proposals": []}
        envelope = {"choices": [{"message": {"content": json.dumps(analysis)}}]}
        opener = Mock()
        opener.open.return_value = io.BytesIO(json.dumps(envelope).encode())
        with patch.dict("os.environ", {"OPENROUTER_API_KEY": "test-key", "OPENROUTER_MODEL": "test/model"}, clear=True), patch.object(ai.urllib.request, "build_opener", return_value=opener):
            ai.analyze(store, {"ids": ["sample", "other"], "descriptions": {"sample": "Sample draft", "other": "Other draft"}})
        selected = json.loads(json.loads(opener.open.call_args.args[0].data)["messages"][1]["content"])["selected"]
        self.assertEqual([(s["id"], s["draftDescription"]) for s in selected], [("sample", "Sample draft"), ("other", "Other draft")])
        self.assertEqual(selected[1]["content"], "Protected body: other")
        self.assertEqual(selected[1]["editorOnlyIntent"], "Purpose: other")
        store.apply_description.assert_not_called()

    def test_unselected_draft_and_duplicate_selection_never_call_provider(self):
        with patch.dict("os.environ", {"OPENROUTER_API_KEY": "test-key", "OPENROUTER_MODEL": "test/model"}, clear=True), patch.object(ai.urllib.request, "build_opener") as opener:
            for payload in ({"ids": ["sample"], "descriptions": {"other": "Unselected draft"}}, {"ids": ["sample", "sample"]}):
                with self.subTest(payload=payload), self.assertRaises(EditorError):
                    ai.analyze(self.store(), payload)
            opener.assert_not_called()

    def test_invalid_conflict_record_rejects_entire_provider_response(self):
        envelope = {"choices": [{"message": {"content": json.dumps({"summary": "Malformed", "conflicts": [None], "proposals": []})}}]}
        opener = Mock()
        opener.open.return_value = io.BytesIO(json.dumps(envelope).encode())
        with patch.dict("os.environ", {"OPENROUTER_API_KEY": "test-key", "OPENROUTER_MODEL": "test/model"}, clear=True), patch.object(ai.urllib.request, "build_opener", return_value=opener):
            with self.assertRaises(EditorError) as caught:
                ai.analyze(self.store(), {"id": "sample"})
        self.assertEqual(caught.exception.status, 502)


if __name__ == "__main__":
    unittest.main()
