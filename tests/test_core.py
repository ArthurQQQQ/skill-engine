"""Run on an authorized CPU environment: python3 -m unittest discover -s tests."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from skill_editor.core import EditorError, SkillStore, digest, replace_description


class DescriptionTests(unittest.TestCase):
    def test_preserves_body_metadata_and_newlines(self):
        old = '---\r\nname: sample\r\ndescription: "Old trigger"\r\nmetadata:\r\n  owner: Example\r\n---\r\n# Body\r\nDo exactly this.\r\n'
        new = replace_description(old, "New trigger")
        self.assertEqual(new, old.replace('description: "Old trigger"', 'description: "New trigger"'))

    def test_replaces_folded_field_without_touching_other_fields(self):
        old = '---\nname: example\ndescription: >-\n  An old\n  trigger.\nmetadata:\n  short-description: Keep me\n---\nProtected instructions\n'
        new = replace_description(old, 'Use for "this" request.')
        self.assertTrue(new.endswith('metadata:\n  short-description: Keep me\n---\nProtected instructions\n'))
        self.assertIn(r'description: "Use for \"this\" request."', new)

    def test_preserves_yaml_comments_and_key_spacing(self):
        old = "---\nname: example\ndescription  :  'Old # literal'  # Keep this comment\n  # Detached comment\nmetadata:\n  owner: Example\n---\nBody\n"
        self.assertEqual(replace_description(old, "New"), old.replace("'Old # literal'", '"New"'))

    def test_indented_block_delimiter_is_description_text(self):
        old = "---\nname: example\ndescription: |-  # Keep this comment\n  Old\n  ---\n  scope\nmetadata:\n  owner: Example\n---\nBody\n"
        new = replace_description(old, "New")
        self.assertEqual(new, '---\nname: example\ndescription: "New"  # Keep this comment\nmetadata:\n  owner: Example\n---\nBody\n')

    def test_rejects_ambiguous_comment_in_multiline_scalar(self):
        old = "---\nname: example\ndescription: Old\n  continued # Keep this comment\n---\nBody\n"
        with self.assertRaises(EditorError):
            replace_description(old, "New")

    def test_preserves_inline_comment_after_empty_scalar_header(self):
        old = "---\nname: example\ndescription: # Keep this comment\n  Old trigger\n---\nBody\n"
        self.assertEqual(replace_description(old, "New"), '---\nname: example\ndescription: "New" # Keep this comment\n---\nBody\n')

    def test_rejects_ambiguous_frontmatter(self):
        with self.assertRaises(EditorError):
            replace_description('---\nname: x\ndescription: a\ndescription: b\n---\nBody', "new")


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        # macOS /var may be a symlink; use the physical temporary root.
        self.root = Path(self.temp.name).resolve()
        self.personal = self.root / "personal"
        self.skill_dir = self.personal / "example"
        self.skill_dir.mkdir(parents=True)
        self.path = self.skill_dir / "SKILL.md"
        self.original = '---\nname: example\ndescription: "Old"\n---\nProtected\n'
        self.path.write_text(self.original, encoding="utf-8")
        (self.root / "data").mkdir()
        self.inventory = {"skills": [{"id": "example", "name": "example", "description": "Old", "path": str(self.path), "source": "personal", "editable": True}]}
        (self.root / "data/inventory.json").write_text(json.dumps(self.inventory), encoding="utf-8")
        self.store = SkillStore(self.root, self.personal)

    def tearDown(self):
        self.temp.cleanup()

    def test_preview_is_read_only_then_apply_has_backup(self):
        preview = self.store.preview("example", "New")
        self.assertEqual(self.path.read_text(), self.original)
        result = self.store.apply_description("example", "New", preview["baselineSha"])
        self.assertEqual(Path(result["backupPath"]).read_text(), self.original)
        self.assertEqual(self.path.read_text(), self.original.replace('"Old"', '"New"'))
        self.assertEqual(result["sha"], digest(self.path.read_text()))

    def test_stale_write_leaves_newer_file_untouched(self):
        sha = self.store.preview("example", "New")["baselineSha"]
        newer = self.original + "Someone else's change\n"
        self.path.write_text(newer)
        with self.assertRaises(EditorError) as caught:
            self.store.apply_description("example", "New", sha)
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(self.path.read_text(), newer)

    def test_symlinked_skill_is_never_writable(self):
        other = self.root / "other.md"
        other.write_text(self.original)
        self.path.unlink()
        self.path.symlink_to(other)
        with self.assertRaises(EditorError):
            self.store.apply_description("example", "New", digest(self.original))
        self.assertEqual(other.read_text(), self.original)

    def test_read_only_skill_rejected(self):
        self.inventory["skills"][0]["source"] = "plugin"
        (self.root / "data/inventory.json").write_text(json.dumps(self.inventory))
        with self.assertRaises(EditorError):
            self.store.preview("example", "New")

    def test_editor_intent_is_outside_runtime_skill(self):
        self.store.save_intent("example", "# Problem\nPreserve this intent.")
        self.assertEqual(self.path.read_text(), self.original)
        self.assertIn("Preserve this intent", self.store.intent("example"))

    def test_oversized_skill_is_not_reclassified_present_by_inventory_read(self):
        self.path.write_bytes(b"x" * 2_000_001)
        result = self.store.inventory()
        self.assertEqual(result["skills"][0]["fileStatus"], "unavailable-or-unsupported")
        with self.assertRaises(EditorError) as caught:
            self.store.read("example")
        self.assertEqual(caught.exception.status, 413)

    def test_single_skill_read_does_not_build_inventory_or_read_unrelated_skills(self):
        self.inventory["skills"][0].update({"body": "Stale snapshot body", "frontmatter": {"description": "Old"}})
        unrelated = self.personal / "unrelated/SKILL.md"
        unrelated.parent.mkdir()
        unrelated.write_text(self.original.replace("example", "unrelated"))
        self.inventory["skills"].append({"id": "unrelated", "name": "unrelated", "path": str(unrelated), "source": "personal", "editable": True})
        (self.root / "data/inventory.json").write_text(json.dumps(self.inventory))
        current = self.original.replace('"Old"', '"Externally updated"') + "New workflow detail\n"
        self.path.write_text(current)
        with patch.object(self.store, "inventory", side_effect=AssertionError("Single reads must not build inventory")), patch("skill_editor.relationships.build_relationships", side_effect=AssertionError("Single reads must not analyze relationships")), patch.object(self.store, "_read_content", wraps=self.store._read_content) as read_content:
            detail = self.store.read("example")
        self.assertEqual([call.args[0] for call in read_content.call_args_list], [self.path])
        self.assertEqual(detail["sha"], digest(current))
        self.assertEqual(detail["skill"]["sha256"], detail["sha"])
        self.assertEqual(detail["skill"]["bytes"], len(current.encode("utf-8")))
        self.assertEqual(detail["skill"]["description"], "Externally updated")
        self.assertEqual(detail["skill"]["frontmatter"]["description"], "Externally updated")
        self.assertTrue(detail["skill"]["body"].endswith("New workflow detail\n"))

    def test_metadata_inventory_is_fresh_without_analysis_and_default_still_analyzes(self):
        current = self.original.replace('"Old"', '"Updated metadata"')
        self.path.write_text(current)
        self.inventory.update({"findings": [{"id": "stale"}], "relationships": [{"id": "stale"}]})
        (self.root / "data/inventory.json").write_text(json.dumps(self.inventory))
        with patch("skill_editor.relationships.build_relationships", side_effect=AssertionError("Metadata read must skip relationships")), patch("skill_editor.core.conservative_findings", side_effect=AssertionError("Metadata read must skip findings")):
            metadata = self.store.inventory(include_analysis=False)
        self.assertEqual(metadata["skills"][0]["description"], "Updated metadata")
        self.assertEqual(metadata["skills"][0]["sha256"], digest(current))
        self.assertNotIn("relationships", metadata)
        self.assertNotIn("findings", metadata)
        with patch("skill_editor.relationships.build_relationships", return_value=[{"id": "fresh"}]) as relationships:
            analyzed = self.store.inventory()
        relationships.assert_called_once()
        self.assertEqual(analyzed["relationships"], [{"id": "fresh"}])


if __name__ == "__main__":
    unittest.main()
