"""Loopback HTTP contract tests; use an authorized CPU environment."""
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from skill_editor.core import SkillStore, digest
from skill_editor.server import make_handler


class RefreshEndpointTests(unittest.TestCase):
    def setUp(self):
        self.store = Mock()
        self.store.inventory.return_value = {"skills": [], "relationships": []}
        self.store.refresh_inventory.return_value = {
            "skills": [], "relationships": [], "delta": {"added": [{"id": "new"}]},
            "refresh": {"complete": True, "sessionCatalogRefreshed": False},
        }
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.store))
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(timeout=2)

    def request(self, method, path, body=None, headers=None):
        connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_reading_inventory_does_not_rescan_or_write(self):
        status, payload = self.request("GET", "/api/inventory")
        self.assertEqual(status, 200)
        self.assertEqual(payload["relationships"], [])
        self.store.refresh_inventory.assert_not_called()

    def test_explicit_json_refresh_returns_delta_and_preserves_catalog_contract(self):
        status, payload = self.request("POST", "/api/inventory/refresh", "{}", {"Content-Type": "application/json"})
        self.assertEqual(status, 200)
        self.assertEqual(payload["delta"]["added"][0]["id"], "new")
        self.assertFalse(payload["refresh"]["sessionCatalogRefreshed"])
        self.store.refresh_inventory.assert_called_once_with()

    def test_cross_site_refresh_is_rejected_before_scan(self):
        status, _ = self.request("POST", "/api/inventory/refresh", "{}", {
            "Content-Type": "application/json", "Origin": "https://untrusted.example",
        })
        self.assertEqual(status, 403)
        self.store.refresh_inventory.assert_not_called()

    def test_non_json_refresh_is_rejected_before_scan(self):
        status, _ = self.request("POST", "/api/inventory/refresh", "{}", {"Content-Type": "text/plain"})
        self.assertEqual(status, 415)
        self.store.refresh_inventory.assert_not_called()


class CaseEndpointTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name).resolve()
        personal = self.project / "personal"
        self.skill_path = personal / "sample/SKILL.md"
        self.skill_path.parent.mkdir(parents=True)
        self.original = '---\nname: sample\ndescription: Old trigger\n---\nProtected workflow.\n'
        self.skill_path.write_text(self.original)
        (self.project / "data").mkdir()
        (self.project / "data/inventory.json").write_text(json.dumps({"skills": [{
            "id": "sample", "name": "sample", "path": str(self.skill_path), "source": "personal",
            "editable": True, "description": "Old trigger", "sha256": digest(self.original),
        }]}))
        self.store = SkillStore(self.project, personal)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.store))
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(timeout=2)
        self.temp.cleanup()

    def request(self, method, path, body=None, headers=None):
        connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def json_request(self, method, path, payload, **headers):
        return self.request(method, path, json.dumps(payload), {"Content-Type": "application/json", **headers})

    def create_case(self):
        status, document = self.json_request("POST", "/api/cases", {
            "title": "Boundary case", "problem": "Only the requested scope", "skillIds": ["sample"],
            "authorExpectation": {"status": "draft", "goal": "Preserve workflow", "invariants": []},
            "prompts": [{"id": "p1", "text": "Use the sample task", "kind": "positive",
                         "expectation": {"required": ["sample"], "allowed": [], "forbidden": [], "protectedOutputs": []}}],
        })
        self.assertEqual(status, 201)
        return document

    def test_case_crud_rejects_stale_and_forged_prediction_writes(self):
        document = self.create_case()
        identifier = document["id"]
        status, updated = self.json_request("PUT", "/api/case?id=" + identifier, {"baselineSha": document["sha"], "patch": {"title": "Reviewed boundary"}})
        self.assertEqual(status, 200)
        self.assertEqual(updated["revision"], 2)
        status, _ = self.json_request("PUT", "/api/case?id=" + identifier, {"baselineSha": document["sha"], "patch": {"title": "Stale"}})
        self.assertEqual(status, 409)
        status, _ = self.json_request("PUT", "/api/case?id=" + identifier, {"baselineSha": updated["sha"], "patch": {"predictions": [{"type": "native", "result": "pass"}]}})
        self.assertEqual(status, 400)
        status, listing = self.request("GET", "/api/cases")
        self.assertEqual(status, 200)
        self.assertEqual(listing["cases"][0]["title"], "Reviewed boundary")

    def test_case_preview_is_read_only_and_cross_site_writes_are_blocked(self):
        document = self.create_case()
        status, updated = self.json_request("PUT", "/api/case?id=" + document["id"], {
            "baselineSha": document["sha"], "patch": {"candidates": [{"id": "sample", "description": "Narrow trigger", "sourceSha": digest(self.original)}]},
        })
        self.assertEqual(status, 200)
        status, preview = self.json_request("POST", "/api/case/preview", {"caseId": document["id"], "skillId": "sample", "baselineSha": updated["sha"]})
        self.assertEqual(status, 200)
        self.assertTrue(preview["protectedBodyUnchanged"])
        self.assertFalse(preview["applied"])
        self.assertEqual(self.skill_path.read_text(), self.original)
        status, _ = self.json_request("POST", "/api/cases", {"skillIds": ["sample"]}, Origin="https://untrusted.example")
        self.assertEqual(status, 403)

    def test_unconfigured_case_analysis_never_calls_provider(self):
        document = self.create_case()
        with patch.dict("os.environ", {}, clear=True), patch("skill_editor.case_ai._complete") as complete:
            status, _ = self.json_request("POST", "/api/case/generate", {"caseId": document["id"], "baselineSha": document["sha"]})
            self.assertEqual(status, 503)
            status, _ = self.json_request("POST", "/api/case/predict", {"caseId": document["id"], "baselineSha": document["sha"]})
            self.assertEqual(status, 409)
            complete.assert_not_called()


if __name__ == "__main__":
    unittest.main()
