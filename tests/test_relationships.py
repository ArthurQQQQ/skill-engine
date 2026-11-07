"""Relationship evidence checks. Run only on an authorized CPU environment."""
import json
import hashlib
from pathlib import Path
import tempfile
import unittest

from skill_editor.relationships import build_relationships


def skill(identifier, content, **extra):
    return {"id": identifier, "name": identifier, "content": content,
            "path": "/skills/" + identifier + "/SKILL.md", "fileStatus": "present", **extra}


class RelationshipTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "data").mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def reviewed(self, **overrides):
        record = {"id": "review-pair", "sourceId": "router-a", "targetId": "reviewer-b",
                  "type": "delegation", "summary": "A delegates B", "condition": "Only for venue B",
                  "evidence": [{"skillId": "router-a", "quote": "Use the specialist."},
                               {"skillId": "reviewer-b", "quote": "Specialist workflow."}]}
        record.update(overrides)
        (self.root / "data/relationships.json").write_text(json.dumps({"relationships": [record]}))

    def test_no_shared_topic_or_name_grouping_edges(self):
        result = build_relationships([skill("paper-reviewer", "Review a paper."),
                                      skill("other-reviewer", "Review a paper.")], self.root)
        self.assertEqual(result, [])

    def test_review_keeps_direction_condition_and_current_line(self):
        self.reviewed()
        result = build_relationships([skill("router-a", "# Header\n\nUse the specialist."),
                                      skill("reviewer-b", "Specialist workflow.")], self.root)
        self.assertEqual(len(result), 1)
        edge = result[0]
        self.assertEqual((edge["sourceId"], edge["targetId"]), ("router-a", "reviewer-b"))
        self.assertEqual(edge["condition"], "Only for venue B")
        self.assertEqual(edge["status"], "current")
        self.assertEqual(edge["evidence"][0]["line"], 3)
        self.assertEqual(edge["validation"], "quote-presence-only")

    def test_changed_quote_downgrades_review_without_keeping_old_line(self):
        self.reviewed()
        edge = build_relationships([skill("router-a", "A changed workflow."),
                                    skill("reviewer-b", "Specialist workflow.")], self.root)[0]
        self.assertEqual(edge["status"], "stale")
        self.assertEqual(edge["evidence"][0]["status"], "stale")
        self.assertIsNone(edge["evidence"][0]["line"])

    def test_unreadable_skill_never_validates_cached_content(self):
        self.reviewed()
        edge = build_relationships([skill("router-a", "Use the specialist.", fileStatus="missing"),
                                    skill("reviewer-b", "Specialist workflow.")], self.root)[0]
        self.assertEqual(edge["status"], "unavailable")
        self.assertIsNone(edge["evidence"][0]["line"])

    def test_context_change_is_stale_even_when_quote_remains(self):
        self.reviewed(evidence=[
            {"skillId": "router-a", "quote": "Use the specialist.",
             "contentSha256": hashlib.sha256(b"Use the specialist.").hexdigest()},
            {"skillId": "reviewer-b", "quote": "Specialist workflow."},
        ])
        edge = build_relationships([skill("router-a", "Except for venue B: Use the specialist."),
                                    skill("reviewer-b", "Specialist workflow.")], self.root)[0]
        self.assertEqual(edge["status"], "stale")
        self.assertEqual(edge["evidence"][0]["line"], 1)
        self.assertNotEqual(edge["evidence"][0]["contentSha256"], edge["evidence"][0]["currentContentSha256"])

    def test_explicit_reference_is_not_delegation_or_conflict(self):
        result = build_relationships([skill("router-a", "Compare `reviewer-b` before proceeding."),
                                      skill("reviewer-b", "Protected workflow.")], self.root)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["type"], "reference")
        self.assertEqual(result[0]["method"], "explicit-reference")
        self.assertEqual(result[0]["evidence"][0]["line"], 1)

    def test_reference_name_boundaries_do_not_match_suffixes(self):
        result = build_relationships([skill("router-a", "Read reliable-paper-reviewer."),
                                      skill("paper-reviewer", "A"),
                                      skill("reliable-paper-reviewer", "B")], self.root)
        self.assertEqual([edge["targetId"] for edge in result], ["reliable-paper-reviewer"])

    def test_ambiguous_short_names_do_not_choose_arbitrary_plugin(self):
        result = build_relationships([skill("router-a", "Read `shared-tool`."),
                                      skill("plugin-a:shared-tool", "A", name="shared-tool"),
                                      skill("plugin-b:shared-tool", "B", name="shared-tool")], self.root)
        self.assertEqual(result, [])

    def test_ordinary_words_need_skill_reference_context(self):
        result = build_relationships([skill("router-a", "Read documents and export a pdf."),
                                      skill("documents", "A"), skill("pdf", "B")], self.root)
        self.assertEqual(result, [])
        result = build_relationships([skill("router-a", "Use `documents` and pdf/SKILL.md."),
                                      skill("documents", "A"), skill("pdf", "B")], self.root)
        self.assertEqual({edge["targetId"] for edge in result}, {"documents", "pdf"})

    def test_review_requires_evidence_for_both_endpoints(self):
        self.reviewed(evidence=[{"skillId": "router-a", "quote": "Use the specialist."}])
        self.assertEqual(build_relationships([skill("router-a", "Use the specialist."),
                                             skill("reviewer-b", "Specialist workflow.")], self.root), [])

    def test_missing_or_broken_optional_review_file_does_not_break_references(self):
        (self.root / "data/relationships.json").write_text("{broken")
        edges = build_relationships([skill("router-a", "Use reviewer-b."),
                                     skill("reviewer-b", "Workflow")], self.root)
        self.assertEqual(len(edges), 1)
        self.assertEqual(edges[0]["type"], "reference")

    def test_missing_source_is_not_used_to_generate_reference(self):
        edges = build_relationships([skill("router-a", "Use reviewer-b.", fileStatus="unverified"),
                                     skill("reviewer-b", "Workflow")], self.root)
        self.assertEqual(edges, [])

    def test_missing_target_is_not_used_to_generate_reference(self):
        edges = build_relationships([skill("router-a", "Use reviewer-b."),
                                     skill("reviewer-b", "Workflow", fileStatus="missing")], self.root)
        self.assertEqual(edges, [])


if __name__ == "__main__":
    unittest.main()
