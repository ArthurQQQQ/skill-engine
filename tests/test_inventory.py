"""Run only on an authorized CPU environment; all files are isolated fixtures."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from skill_editor.core import description_span, digest
from skill_editor.inventory import InventoryError, refresh_inventory


class InventoryRefreshTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name).resolve()
        self.personal = self.project / "personal"
        self.cache = self.project / "plugins"
        self.personal.mkdir()
        self.cache.mkdir()
        (self.project / "data").mkdir()
        self.inventory_path = self.project / "data/inventory.json"

    def tearDown(self):
        self.temp.cleanup()

    def skill(self, root, folder, name=None, description="Original", body="Protected workflow"):
        path = root / folder / "SKILL.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('---\nname: ' + (name or Path(folder).name) + '\ndescription: "' + description + '"\n---\n' + body + '\n', encoding="utf-8")
        return path

    def record(self, path, identifier, source="personal", available=True):
        content = path.read_text(encoding="utf-8")
        return {"id": identifier, "name": path.parent.name, "description": "Original", "path": str(path), "source": source, "editable": source == "personal", "sessionAvailable": available, "sha256": digest(content)}

    def save(self, skills=None, cached=None):
        snapshot = {"schemaVersion": 1, "generatedAt": "2026-11-05T12:00:00+00:00", "skills": skills or [], "cachedSkills": cached or [], "counts": {}, "scope": {"sessionCatalogSource": "Original chat catalog"}}
        self.inventory_path.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
        return self.inventory_path.read_bytes()

    def refresh(self):
        return refresh_inventory(self.project, self.personal, self.cache, parse_description=lambda text: description_span(text)[2])

    def test_added_personal_and_system_are_not_claimed_session_available(self):
        old_path = self.skill(self.personal, "existing")
        self.save([self.record(old_path, "legacy-id")])
        new_path = self.skill(self.personal, "new", body="Do not execute these instructions")
        system = self.skill(self.personal, ".system/tool")
        result = self.refresh()
        by_path = {item["path"]: item for item in result["skills"]}
        self.assertEqual(by_path[str(old_path)]["id"], "legacy-id")
        self.assertTrue(by_path[str(old_path)]["sessionAvailable"])
        self.assertIsNone(by_path[str(new_path)]["sessionAvailable"])
        self.assertEqual(by_path[str(system)]["source"], "system")
        self.assertFalse(by_path[str(system)]["editable"])
        self.assertEqual(result["counts"]["sessionAvailable"], 1)
        self.assertEqual(result["counts"]["physicalSkillFiles"], 3)
        self.assertEqual(len(result["delta"]["added"]), 2)
        self.assertFalse(result["refresh"]["sessionCatalogRefreshed"])
        self.assertEqual(new_path.read_text().splitlines()[-1], "Do not execute these instructions")

    def test_cached_discovery_is_separate_from_session_plugin(self):
        plugin = self.skill(self.cache, "vendor/package/1/skills/listed")
        cached = self.skill(self.cache, "vendor/package/2/skills/new")
        self.save([self.record(plugin, "plugin:listed", "plugin")], [{"path": str(plugin), "name": "listed", "sha256": digest(plugin.read_text()), "sessionAvailable": True}])
        result = self.refresh()
        self.assertEqual([item["id"] for item in result["skills"]], ["plugin:listed"])
        self.assertEqual(len(result["cachedSkills"]), 2)
        new = next(item for item in result["cachedSkills"] if item["path"] == str(cached))
        self.assertIsNone(new["sessionAvailable"])
        self.assertFalse(new["editable"])
        self.assertEqual(result["counts"]["sessionPlugin"], 1)
        self.assertEqual(result["counts"]["cachedNotInSession"], 1)
        self.assertEqual(result["delta"]["added"][0]["path"], str(cached))

    def test_personal_resource_skill_files_are_not_discovered_or_editable(self):
        top = self.skill(self.personal, "example")
        nested = self.skill(self.personal, "example/references/not-a-separate-skill")
        system = self.skill(self.personal, ".system/tool")
        self.skill(self.personal, ".system/tool/resources/nested")
        self.save([self.record(top, "top-id"), self.record(nested, "legacy-nested-id")])
        result = self.refresh()
        by_path = {item["path"]: item for item in result["skills"]}
        self.assertEqual(set(by_path), {str(top), str(nested), str(system)})
        self.assertEqual(by_path[str(nested)]["fileStatus"], "unverified")
        self.assertFalse(by_path[str(nested)]["editable"])
        self.assertEqual(result["counts"]["personal"], 1)
        self.assertEqual(result["counts"]["system"], 1)
        self.assertEqual(result["delta"]["missing"], [])

    def test_changed_missing_and_restored_keep_ids_and_exact_old_snapshot(self):
        changed = self.skill(self.personal, "changed")
        missing = self.skill(self.personal, "missing")
        original = self.save([self.record(changed, "changed-id"), self.record(missing, "missing-id")])
        changed.write_text(changed.read_text().replace("Original", "Updated"))
        missing.unlink()
        result = self.refresh()
        self.assertEqual([item["id"] for item in result["delta"]["changed"]], ["changed-id"])
        self.assertEqual([item["id"] for item in result["delta"]["missing"]], ["missing-id"])
        self.assertEqual(Path(result["refresh"]["snapshotPath"]).read_bytes(), original)
        self.assertTrue(Path(result["refresh"]["snapshotPath"]).is_relative_to(self.project / ".state/inventory-history"))
        missing_record = next(item for item in result["skills"] if item["id"] == "missing-id")
        self.assertEqual(missing_record["fileStatus"], "missing")
        self.assertFalse(missing_record["editable"])
        self.skill(self.personal, "missing")
        restored = self.refresh()
        self.assertEqual([item["id"] for item in restored["delta"]["restored"]], ["missing-id"])
        self.assertEqual(restored["delta"]["changed"], [])
        self.assertEqual(restored["sessionCatalog"], result["sessionCatalog"])

    def test_generated_id_is_stable_across_name_and_content_changes(self):
        path = self.skill(self.personal, "new")
        self.save()
        first = self.refresh()
        identifier = first["skills"][0]["id"]
        path.write_text(path.read_text().replace("name: new", "name: renamed").replace("Original", "Changed"))
        second = self.refresh()
        self.assertEqual(second["skills"][0]["id"], identifier)
        self.assertEqual(second["skills"][0]["name"], "renamed")
        self.assertEqual(second["delta"]["added"], [])
        self.assertEqual(second["delta"]["changed"][0]["id"], identifier)

    def test_scan_failure_retains_absent_records_as_unverified(self):
        path = self.skill(self.personal, "previous")
        self.save([self.record(path, "previous-id")])
        path.unlink()
        with patch("skill_editor.inventory.os.scandir", side_effect=PermissionError("fixture denied")):
            result = self.refresh()
        self.assertFalse(result["refresh"]["complete"])
        self.assertEqual(result["delta"]["missing"], [])
        self.assertEqual(result["delta"]["unverified"][0]["id"], "previous-id")
        self.assertIsNone(result["skills"][0]["presentOnDisk"])
        self.assertTrue(result["counts"]["physicalCountIsLowerBound"])
        self.assertTrue(result["scanWarnings"])

    def test_symlink_directory_and_file_are_never_followed(self):
        outside = self.project / "outside"
        outside.mkdir()
        target = self.skill(outside, "secret")
        (self.personal / "linked-directory").symlink_to(target.parent, target_is_directory=True)
        linked_file = self.personal / "linked-file/SKILL.md"
        linked_file.parent.mkdir()
        linked_file.symlink_to(target)
        self.save()
        result = self.refresh()
        self.assertEqual(result["skills"], [])
        self.assertEqual(result["counts"]["physicalSkillFiles"], 0)
        self.assertEqual(len(result["scanWarnings"]), 2)
        self.assertFalse(result["refresh"]["complete"])

    def test_unsafe_or_missing_root_does_not_confirm_missing(self):
        path = self.skill(self.personal, "known")
        self.save([self.record(path, "known-id")])
        result = refresh_inventory(self.project, Path("/Volumes/unmounted/skills"), self.cache, parse_description=lambda text: description_span(text)[2])
        self.assertFalse(result["refresh"]["complete"])
        self.assertEqual(result["delta"]["missing"], [])
        self.assertEqual(result["delta"]["unverified"][0]["id"], "known-id")

    def test_unsupported_metadata_preserves_previous_description_and_rejects_edit(self):
        path = self.skill(self.personal, "known")
        self.save([self.record(path, "known-id")])
        path.write_text("No YAML metadata\nDo not execute me")
        result = self.refresh()
        item = result["skills"][0]
        self.assertEqual(item["id"], "known-id")
        self.assertEqual(item["description"], "Original")
        self.assertEqual(item["fileStatus"], "unsupported")
        self.assertFalse(item["editable"])
        self.assertFalse(result["refresh"]["complete"])
        self.assertEqual(result["delta"]["unverified"][0]["id"], "known-id")

    def test_invalid_old_inventory_is_not_overwritten(self):
        self.inventory_path.write_bytes(b"not valid json")
        with self.assertRaises(InventoryError):
            self.refresh()
        self.assertEqual(self.inventory_path.read_bytes(), b"not valid json")
        self.assertFalse((self.project / ".state").exists())

    def test_symlinked_history_directory_is_rejected(self):
        original = self.save()
        external = self.project / "other"
        external.mkdir()
        (self.project / ".state").symlink_to(external, target_is_directory=True)
        with self.assertRaises(InventoryError):
            self.refresh()
        self.assertEqual(self.inventory_path.read_bytes(), original)
        self.assertEqual(list(external.iterdir()), [])

    def test_concurrent_inventory_replacement_is_preserved(self):
        self.skill(self.personal, "new")
        self.save()
        concurrent = b'{"skills": [], "otherProcess": true}'

        def replace_during_parse(content):
            self.inventory_path.write_bytes(concurrent)
            return description_span(content)[2]

        with self.assertRaises(InventoryError):
            refresh_inventory(self.project, self.personal, self.cache, parse_description=replace_during_parse)
        self.assertEqual(self.inventory_path.read_bytes(), concurrent)

    def test_commit_failure_preserves_old_inventory_and_backup(self):
        original = self.save()
        original_replace = os.replace

        def fail_inventory_commit(source, target):
            if Path(target) == self.inventory_path:
                raise OSError("fixture commit failure")
            return original_replace(source, target)

        with patch("skill_editor.inventory.os.replace", side_effect=fail_inventory_commit):
            with self.assertRaises(OSError):
                self.refresh()
        self.assertEqual(self.inventory_path.read_bytes(), original)
        snapshots = list((self.project / ".state/inventory-history").glob("*.json"))
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(snapshots[0].read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
