"""Tests for tools/second_brain.py (spec/write-plan-schema-v1.md, incl. v1.1 §9). Run: python -m unittest discover tests"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tools"))
import second_brain as sb  # noqa: E402

SRC = "meeting-2026-09-18-demo"


def source_op(op_id="op-001", sid=SRC, synthetic=False, date="2026-09-18", conf="high"):
    return {"operation_id": op_id, "type": "create_page", "page_id": sid, "page_type": "source",
            "title": f"Source {sid}", "body": "## Summary\n\nsummary",
            "source": {"source_id": sid, "source_type": "meeting", "raw_path": f"raw/meetings/{sid}.md",
                       "source_date": date, "date_confidence": conf, "date_basis": "metadata",
                       "synthetic": synthetic}}


def plan(ops, sources=(SRC,), plan_id="plan-20260922-abcd"):
    return {"schema_version": 2, "plan_id": plan_id, "created_at": "2026-09-22T10:00:00+08:00",
            "source_ids": list(sources), "summary": "test", "operations": ops}


class VaultTest(unittest.TestCase):
    def setUp(self):
        os.environ["SB_TODAY"] = "2026-09-22"
        self.tmp = tempfile.TemporaryDirectory()
        self.vault = Path(self.tmp.name)
        (self.vault / "plans" / "pending").mkdir(parents=True)
        (self.vault / "wiki").mkdir()
        self.counter = 0

    def tearDown(self):
        self.tmp.cleanup()
        os.environ.pop("SB_TODAY", None)

    def write_plan(self, p):
        self.counter += 1
        p = dict(p, plan_id=f"plan-20260922-t{self.counter:03d}")
        path = self.vault / "plans" / "pending" / f"{p['plan_id']}.json"
        path.write_text(json.dumps(p, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def apply(self, p, allow_restructure=False):
        path = self.write_plan(p)
        return sb.apply(self.vault, path, sb.sha256_bytes(path.read_bytes()), allow_restructure)

    def execute(self, p, allow_restructure=False):
        return sb.execute(sb.Wiki(self.vault), p, allow_restructure)

    def assertRejects(self, code, p, **kw):
        with self.assertRaises(sb.PlanError) as cm:
            self.execute(p, **kw)
        self.assertEqual(cm.exception.code, code, str(cm.exception))
        return cm.exception

    def page(self, pid):
        return sb.Wiki(self.vault).pages[pid]

    def hash(self, pid):
        p = self.page(pid)
        return sb.sha256_text(p.raw)

    def seed(self):
        """A source page and an entity page with nested sections."""
        self.apply(plan([source_op(), {
            "operation_id": "op-002", "type": "create_page", "page_id": "orbit", "page_type": "entity",
            "title": "Orbit", "meta": {"tags": ["system"], "aliases": ["mainnet"]},
            "body": "Preamble text.\n\n## Facts\n\n- fact one\n\n### Detail\n\ndetail text\n\n## History\n\n- created",
            "source_ids": [SRC]}]))


class TestNormalizeAndHash(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(sb.normalize(sb.BOM + "a\r\nb  \rc\n\n\n  "), "a\nb  \nc\n")

    def test_hash_ignores_line_endings_but_not_trailing_spaces(self):
        self.assertEqual(sb.sha256_text("a\r\nb\n"), sb.sha256_text("a\nb"))
        self.assertNotEqual(sb.sha256_text("a  \nb"), sb.sha256_text("a\nb"))


class TestFrontmatter(unittest.TestCase):
    def test_round_trip(self):
        fm = {"page_id": "x", "page_type": "entity", "sources": ["a", "b"], "created": "2026-09-22",
              "updated": "2026-09-22", "title": "Beacon: API", "tags": [], "aliases": ["10:30 standup", "a, b", '"q"'],
              "scope": "null"}
        text = sb.dump_frontmatter(fm)
        self.assertEqual(sb.parse_frontmatter(text), fm)
        self.assertEqual(sb.dump_frontmatter(sb.parse_frontmatter(text)), text)

    def test_rejects_unsupported_yaml(self):
        for bad in ("key:\n  - item", "key: {a: 1}", "key: |", "- item", "a: 1\na: 2"):
            with self.assertRaises(ValueError, msg=bad):
                sb.parse_frontmatter(bad)


class TestSectionTree(unittest.TestCase):
    def lines(self, text):
        return text.split("\n")

    def test_tree_and_fences(self):
        t = sb.build_tree(self.lines("# T\n\npre\n\n## A\n\n```\n## not a heading\n```\n\n### A1\n\n## B"))
        self.assertEqual(t.errors, [])
        self.assertEqual([c.text for c in t.root.children], ["A", "B"])
        self.assertEqual([c.text for c in t.root.children[0].children], ["A1"])
        self.assertEqual(t.preamble, (1, 4))

    def test_structure_errors(self):
        self.assertEqual(sb.build_tree(self.lines("no title\n## A")).errors[0][0], "HEADING_LEVEL")
        self.assertEqual(sb.build_tree(self.lines("# T\n# U")).errors[0][0], "HEADING_LEVEL")
        self.assertEqual(sb.build_tree(self.lines("# T\n## A\n#### deep")).errors[0][0], "HEADING_LEVEL")
        self.assertEqual(sb.build_tree(self.lines("# T\n## A\n## A")).errors[0][0], "PATH_AMBIGUOUS")


class TestOperations(VaultTest):
    def test_create_and_generated_files(self):
        self.seed()
        p = self.page("orbit")
        self.assertEqual(p.fm["sources"], [SRC])
        self.assertEqual((p.fm["created"], p.fm["updated"]), ("2026-09-22", "2026-09-22"))
        self.assertEqual(p.lines[0], "# Orbit")
        index = (self.vault / "wiki" / "index.md").read_text(encoding="utf-8")
        self.assertIn("- [[orbit|Orbit]] — system", index)
        log = (self.vault / "wiki" / "log.md").read_text(encoding="utf-8")
        self.assertIn("apply | plan-20260922-t001", log)
        self.assertTrue((self.vault / "plans" / "applied" / "plan-20260922-t001.json").exists())
        self.assertFalse((self.vault / "plans" / "pending" / "plan-20260922-t001.json").exists())

    def test_page_round_trip(self):
        self.seed()
        path = self.vault / "wiki" / "entities" / "orbit.md"
        raw = path.read_text(encoding="utf-8")
        self.assertEqual(sb.Page.parse(raw).text(), raw)

    def test_update_section_replaces_subtree(self):
        self.seed()
        ex = self.apply(plan([{"operation_id": "op-001", "type": "update_section", "page_id": "orbit",
                               "expected_hash": self.hash("orbit"), "section_path": ["Facts"],
                               "content": "- new fact", "source_ids": [SRC]}]))
        body = "\n".join(self.page("orbit").lines)
        self.assertIn("## Facts\n\n- new fact\n\n## History", body)
        self.assertIn("SUBSECTION REMOVED", ex.results[0].flags)

    def test_update_preamble(self):
        self.seed()
        self.apply(plan([{"operation_id": "op-001", "type": "update_section", "page_id": "orbit",
                          "expected_hash": self.hash("orbit"), "section_path": ["__preamble__"],
                          "content": "New preamble.", "source_ids": [SRC]}]))
        self.assertEqual(self.page("orbit").lines[:4], ["# Orbit", "", "New preamble.", ""])

    def test_append_goes_to_end_of_subtree_and_continues_lists(self):
        self.seed()
        h = self.hash("orbit")
        self.apply(plan([
            {"operation_id": "op-001", "type": "append_to_section", "page_id": "orbit", "expected_hash": h,
             "section_path": ["Facts"], "content": "appended paragraph", "source_ids": [SRC]},
            {"operation_id": "op-002", "type": "append_to_section", "page_id": "orbit", "expected_hash": h,
             "section_path": ["History"], "content": "- second", "source_ids": [SRC]}]))
        body = "\n".join(self.page("orbit").lines)
        self.assertIn("detail text\n\nappended paragraph\n\n## History", body)
        self.assertIn("- created\n- second", body)

    def test_update_section_body_keeps_children(self):
        """schema v1.1 / S1: replace only the section's own text."""
        self.seed()
        h = self.hash("orbit")
        ex = self.apply(plan([{"operation_id": "op-001", "type": "update_section_body", "page_id": "orbit",
                               "expected_hash": h, "section_path": ["Facts"], "content": "- rewritten fact",
                               "source_ids": [SRC]}]))
        body = "\n".join(self.page("orbit").lines)
        self.assertIn("## Facts\n\n- rewritten fact\n\n### Detail\n\ndetail text\n\n## History", body)
        self.assertNotIn("SUBSECTION REMOVED", ex.results[0].flags)
        self.assertRejects("CONTENT_HEADING", plan([{
            "operation_id": "op-001", "type": "update_section_body", "page_id": "orbit",
            "expected_hash": self.hash("orbit"), "section_path": ["Facts"], "content": "### New child",
            "source_ids": [SRC]}]))

    def test_links_must_target_page_ids(self):
        """schema v1.1 / T4 option A: [[page_id|Title]]; unknown targets are rejected, code is ignored."""
        self.seed()
        ok = {"operation_id": "op-002", "type": "create_page", "page_id": "beacon", "page_type": "entity",
              "title": "Beacon", "body": "See [[orbit|Orbit]], [[orbit#Facts|facts]], [[later|Later]] "
                                        "and `[[Not A Link]]`.\n\n```\n[[also ignored]]\n```",
              "source_ids": [SRC]}
        later = {"operation_id": "op-001", "type": "create_page", "page_id": "later", "page_type": "concept",
                 "title": "Later", "body": "", "source_ids": [SRC]}
        self.execute(plan([ok, dict(later, operation_id="op-003")]))
        e = self.assertRejects("BROKEN_LINK", plan([dict(ok, body="See [[Orbit]].")]))
        self.assertIn("Orbit", e.message)
        self.assertRejects("BROKEN_LINK", plan([{
            "operation_id": "op-001", "type": "append_to_section", "page_id": "orbit",
            "expected_hash": self.hash("orbit"), "section_path": ["Facts"], "content": "- [[nope|Nope]]",
            "source_ids": [SRC]}]))

    def test_add_section_positions(self):
        self.seed()
        h = self.hash("orbit")
        self.apply(plan([
            {"operation_id": "op-001", "type": "add_section", "page_id": "orbit", "expected_hash": h,
             "parent_path": [], "heading": "Aliases", "position": {"after": "Facts"}, "content": "text",
             "source_ids": [SRC]},
            {"operation_id": "op-002", "type": "add_section", "page_id": "orbit", "expected_hash": h,
             "parent_path": ["Facts"], "heading": "First", "position": "start", "content": "x",
             "source_ids": [SRC]}]))
        tree = self.page("orbit").tree()
        self.assertEqual([c.text for c in tree.root.children], ["Facts", "Aliases", "History"])
        self.assertEqual([c.text for c in tree.root.children[0].children], ["First", "Detail"])

    def test_update_meta_title_syncs_h1_and_flags_duplicates(self):
        self.seed()
        self.apply(plan([{"operation_id": "op-001", "type": "create_page", "page_id": "beacon",
                          "page_type": "entity", "title": "Beacon", "body": "", "source_ids": [SRC]}]))
        ex = self.apply(plan([{"operation_id": "op-001", "type": "update_meta", "page_id": "beacon",
                               "expected_hash": self.hash("beacon"),
                               "set": {"title": "Beacon SaaS", "aliases": ["MAINNET"]}, "source_ids": [SRC]}]))
        self.assertEqual(self.page("beacon").lines[0], "# Beacon SaaS")
        self.assertTrue(any(f.startswith("POSSIBLE DUPLICATE: orbit") for f in ex.results[0].flags))

    def test_restructure_needs_flag(self):
        self.seed()
        op = {"operation_id": "op-001", "type": "restructure_page", "page_id": "orbit",
              "expected_hash": self.hash("orbit"), "body": "## Only\n\nall new", "reason": "merge",
              "source_ids": [SRC]}
        self.assertRejects("RESTRUCTURE_DISABLED", plan([op]))
        self.apply(plan([op]), allow_restructure=True)
        self.assertEqual([c.text for c in self.page("orbit").tree().root.children], ["Only"])


class TestDecisions(VaultTest):
    def decision_op(self, op_id, pid, status="active", date="2026-09-18", conf="high", sources=(SRC,)):
        return {"operation_id": op_id, "type": "create_page", "page_id": pid, "page_type": "decision",
                "title": pid.replace("-", " ").title(), "meta": {"scope": "demo"},
                "decision": {"status": status, "decided_on": date, "date_confidence": conf},
                "body": "## Decision\n\ntext\n\n## History\n\n- decided", "source_ids": list(sources)}

    def test_partial_supersession_in_one_plan(self):
        """0A 06b pattern: split out what still holds, trim the old page, supersede it (spec §3.8)."""
        old_src = "meeting-undated-billing-sync"
        new_src = "meeting-2026-09-29-follow-up"
        self.apply(plan([source_op("op-001", old_src, date="unknown", conf="low"),
                         self.decision_op("op-002", "sample-data-first", date="unknown", conf="low",
                                          sources=(old_src,))], sources=(old_src,)))
        self.apply(plan([source_op("op-001", new_src, synthetic=True, date="2026-09-29"),
                         self.decision_op("op-002", "read-only-data-path", date="2026-09-29",
                                          sources=(new_src,))], sources=(new_src,)))
        h = self.hash("sample-data-first")
        base = [
            self.decision_op("op-001", "phase-1-goal", date="unknown", conf="low", sources=(new_src,)),
            {"operation_id": "op-002", "type": "update_section", "page_id": "sample-data-first",
             "expected_hash": h, "section_path": ["Decision"], "content": "only the superseded part",
             "source_ids": [new_src]},
            {"operation_id": "op-003", "type": "decision_change", "page_id": "sample-data-first",
             "expected_hash": h, "new_status": "superseded", "superseded_by": "read-only-data-path",
             "history_note": "partial; goal split out", "source_ids": [new_src]}]
        e = self.assertRejects("TEMPORAL_UNCERTAINTY", plan(base, sources=(new_src,)))
        self.assertEqual(e.op_id, "op-003")
        base[2]["temporal_override"] = {"confirmed_by_user": True, "note": "user confirmed order"}
        ex = self.apply(plan(base, sources=(new_src,)))
        old = self.page("sample-data-first")
        self.assertEqual((old.fm["status"], old.fm["superseded_by"]), ("superseded", "read-only-data-path"))
        self.assertIn("- 2026-09-22（applied）status active → superseded：partial; goal split out"
                      "（[[meeting-2026-09-29-follow-up|Source meeting-2026-09-29-follow-up]]）", old.lines)
        # T1: render detail shows the old decision's provenance before this plan touched it
        old_detail = next(d for d in ex.results[2].detail if d.startswith("old:"))
        self.assertIn(f"sources=['{old_src}']", old_detail)
        self.assertNotIn("SYNTHETIC", old_detail)
        new_detail = next(d for d in ex.results[2].detail if d.startswith("new:"))
        self.assertIn("SYNTHETIC", new_detail)
        # T2: a source page cites only itself even when a later source edits it
        self.apply(plan([{"operation_id": "op-001", "type": "append_to_section", "page_id": old_src,
                          "expected_hash": self.hash(old_src), "section_path": ["Summary"],
                          "content": "later note", "source_ids": [new_src]}], sources=(new_src,)))
        self.assertEqual(self.page(old_src).fm["sources"], [old_src])
        flags = ex.results[2].flags
        self.assertIn("TEMPORAL OVERRIDE", flags)
        self.assertIn("SYNTHETIC SOURCE", flags)
        self.assertIn("FUTURE DATE", flags)
        self.assertEqual(self.page("phase-1-goal").fm["status"], "active")
        self.assertIn("(superseded)", (self.vault / "wiki" / "index.md").read_text(encoding="utf-8"))

    def test_origin_sources_are_fixed_at_creation(self):
        """schema v1.1 / T5: a later plan citing another source must not change a decision's provenance."""
        old_src, new_src = "meeting-undated-old", "meeting-2026-09-29-new"
        self.apply(plan([source_op("op-001", old_src, date="unknown", conf="low"),
                         self.decision_op("op-002", "old-decision", date="unknown", conf="low", sources=(old_src,))],
                        sources=(old_src,)))
        self.apply(plan([source_op("op-001", new_src, synthetic=True, date="2026-09-29"),
                         self.decision_op("op-002", "new-decision", date="2026-09-29", sources=(new_src,)),
                         {"operation_id": "op-003", "type": "append_to_section", "page_id": "old-decision",
                          "expected_hash": self.hash("old-decision"), "section_path": ["Decision"],
                          "content": "> warning", "source_ids": [new_src]}], sources=(new_src,)))
        old = self.page("old-decision")
        self.assertEqual(old.fm["origin_sources"], [old_src])
        self.assertEqual(old.fm["sources"], sorted([old_src, new_src]))
        ex = self.execute(plan([{"operation_id": "op-001", "type": "decision_change", "page_id": "old-decision",
                                 "expected_hash": self.hash("old-decision"), "new_status": "superseded",
                                 "superseded_by": "new-decision", "history_note": "x",
                                 "temporal_override": {"confirmed_by_user": True, "note": "ok"},
                                 "source_ids": [new_src]}], sources=(new_src,)))
        old_detail = next(d for d in ex.results[0].detail if d.startswith("old:"))
        self.assertIn(f"sources=['{old_src}']", old_detail)
        self.assertNotIn("SYNTHETIC", old_detail)
        self.assertRejects("FORBIDDEN_META", plan([{
            "operation_id": "op-001", "type": "update_meta", "page_id": "old-decision",
            "expected_hash": self.hash("old-decision"), "set": {"origin_sources": ["x"]},
            "source_ids": [new_src]}], sources=(new_src,)))

    def test_temporal_order_and_transitions(self):
        self.apply(plan([source_op(), self.decision_op("op-002", "newer", date="2026-09-20"),
                         self.decision_op("op-003", "older", date="2026-09-10")]))
        change = {"operation_id": "op-001", "type": "decision_change", "page_id": "newer",
                  "expected_hash": self.hash("newer"), "new_status": "superseded", "superseded_by": "older",
                  "history_note": "x", "source_ids": [SRC]}
        self.assertRejects("TEMPORAL_ORDER", plan([change]))
        revoke = {k: v for k, v in change.items() if k != "superseded_by"}
        self.apply(plan([dict(revoke, new_status="revoked")]))
        self.assertEqual(self.page("newer").fm["status"], "revoked")
        reopen = dict(revoke, new_status="active", expected_hash=self.hash("newer"))
        self.assertRejects("BAD_TRANSITION", plan([reopen]))

    def test_proposed_to_active_and_superseded_by_must_be_active(self):
        self.apply(plan([source_op(), self.decision_op("op-002", "draft", status="proposed"),
                         self.decision_op("op-003", "other", status="proposed")]))
        bad = {"operation_id": "op-001", "type": "decision_change", "page_id": "draft",
               "expected_hash": self.hash("draft"), "new_status": "superseded", "superseded_by": "other",
               "history_note": "x", "source_ids": [SRC]}
        self.assertRejects("BAD_TRANSITION", plan([bad]))
        self.apply(plan([{"operation_id": "op-001", "type": "decision_change", "page_id": "draft",
                          "expected_hash": self.hash("draft"), "new_status": "active",
                          "history_note": "accepted", "source_ids": [SRC]}]))
        self.assertEqual(self.page("draft").fm["status"], "active")

    def test_decision_change_on_non_decision_page(self):
        self.apply(plan([source_op(), {"operation_id": "op-002", "type": "create_page", "page_id": "e",
                                       "page_type": "entity", "title": "E", "body": "", "source_ids": [SRC]}]))
        self.assertRejects("SCHEMA", plan([{"operation_id": "op-001", "type": "decision_change", "page_id": "e",
                                           "expected_hash": self.hash("e"), "new_status": "revoked",
                                           "history_note": "x", "source_ids": [SRC]}]))


class TestErrorCodes(VaultTest):
    def test_schema(self):
        self.assertRejects("SCHEMA", {"schema_version": 2})
        self.assertRejects("SCHEMA", plan([], sources=(SRC,)))
        self.assertRejects("SCHEMA", plan([{"operation_id": "op-1", "type": "update_meta"}]))

    def test_unknown_source_and_undeclared_source(self):
        self.assertRejects("UNKNOWN_SOURCE", plan([{"operation_id": "op-001", "type": "create_page",
                                                    "page_id": "x", "page_type": "entity", "title": "X",
                                                    "body": "", "source_ids": [SRC]}]))
        self.assertRejects("SCHEMA", plan([source_op(), {"operation_id": "op-002", "type": "create_page",
                                                         "page_id": "x", "page_type": "entity", "title": "X",
                                                         "body": "", "source_ids": ["other"]}]))

    def test_unknown_page_and_use_before_create(self):
        self.seed()
        self.assertRejects("UNKNOWN_PAGE", plan([{"operation_id": "op-001", "type": "append_to_section",
                                                  "page_id": "nope", "section_path": ["A"], "content": "x",
                                                  "source_ids": [SRC]}]))
        self.assertRejects("UNKNOWN_PAGE", plan([
            {"operation_id": "op-001", "type": "append_to_section", "page_id": "late", "section_path": ["A"],
             "content": "x", "source_ids": [SRC]},
            {"operation_id": "op-002", "type": "create_page", "page_id": "late", "page_type": "entity",
             "title": "Late", "body": "## A", "source_ids": [SRC]}]))

    def test_page_exists(self):
        self.seed()
        self.assertRejects("PAGE_EXISTS", plan([{"operation_id": "op-001", "type": "create_page",
                                                 "page_id": "orbit", "page_type": "entity", "title": "M",
                                                 "body": "", "source_ids": [SRC]}]))

    def test_plan_stale_and_hash_mismatch(self):
        self.seed()
        h = self.hash("orbit")
        path = self.vault / "wiki" / "entities" / "orbit.md"
        op = {"operation_id": "op-001", "type": "append_to_section", "page_id": "orbit", "expected_hash": h,
              "section_path": ["Facts"], "content": "x", "source_ids": [SRC]}
        self.assertRejects("HASH_MISMATCH_IN_PLAN", plan([op, dict(op, operation_id="op-002",
                                                                    expected_hash="sha256:" + "0" * 64)]))
        path.write_text(path.read_text(encoding="utf-8").replace("tags: [system]", "tags: [system, edited]"),
                        encoding="utf-8")
        self.assertRejects("PLAN_STALE", plan([op]))

    def test_line_endings_do_not_make_plan_stale(self):
        self.seed()
        h = self.hash("orbit")
        path = self.vault / "wiki" / "entities" / "orbit.md"
        path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
        self.execute(plan([{"operation_id": "op-001", "type": "append_to_section", "page_id": "orbit",
                            "expected_hash": h, "section_path": ["Facts"], "content": "x",
                            "source_ids": [SRC]}]))

    def test_path_errors(self):
        self.seed()
        h = self.hash("orbit")
        self.assertRejects("PATH_NOT_FOUND", plan([{"operation_id": "op-001", "type": "update_section",
                                                    "page_id": "orbit", "expected_hash": h,
                                                    "section_path": ["Detail"], "content": "x",
                                                    "source_ids": [SRC]}]))
        self.assertRejects("PATH_AMBIGUOUS", plan([{"operation_id": "op-001", "type": "add_section",
                                                    "page_id": "orbit", "expected_hash": h, "parent_path": [],
                                                    "heading": "Facts", "content": "x", "source_ids": [SRC]}]))
        path = self.vault / "wiki" / "entities" / "orbit.md"
        path.write_text(path.read_text(encoding="utf-8") + "\n#### skipped\n", encoding="utf-8")
        self.assertRejects("HEADING_LEVEL", plan([{"operation_id": "op-001", "type": "append_to_section",
                                                   "page_id": "orbit", "expected_hash": self.hash("orbit"),
                                                   "section_path": ["Facts"], "content": "x",
                                                   "source_ids": [SRC]}]))

    def test_content_heading(self):
        self.seed()
        h = self.hash("orbit")
        cases = [("update_section", ["Facts"], "## Same level"),
                 ("update_section", ["Facts"], "#### Skips"),
                 ("update_section", ["__preamble__"], "## In preamble"),
                 ("append_to_section", ["Facts"], "### Any heading")]
        for t, path, content in cases:
            self.assertRejects("CONTENT_HEADING", plan([{"operation_id": "op-001", "type": t, "page_id": "orbit",
                                                         "expected_hash": h, "section_path": path,
                                                         "content": content, "source_ids": [SRC]}]))
        self.assertRejects("CONTENT_HEADING", plan([source_op("op-001", "s2"), {
            "operation_id": "op-002", "type": "create_page", "page_id": "y", "page_type": "entity",
            "title": "Y", "body": "# Second H1", "source_ids": ["s2"]}], sources=("s2",)))

    def test_forbidden_meta(self):
        self.seed()
        self.assertRejects("FORBIDDEN_META", plan([{"operation_id": "op-001", "type": "update_meta",
                                                    "page_id": "orbit", "expected_hash": self.hash("orbit"),
                                                    "set": {"status": "active"}, "source_ids": [SRC]}]))
        self.assertRejects("FORBIDDEN_META", plan([{"operation_id": "op-001", "type": "create_page",
                                                    "page_id": "z", "page_type": "entity", "title": "Z",
                                                    "meta": {"sources": ["x"]}, "body": "", "source_ids": [SRC]}]))

    def test_approval_mismatch(self):
        self.seed()
        path = self.write_plan(plan([{"operation_id": "op-001", "type": "append_to_section", "page_id": "orbit",
                                      "expected_hash": self.hash("orbit"), "section_path": ["Facts"],
                                      "content": "x", "source_ids": [SRC]}]))
        approved = sb.sha256_bytes(path.read_bytes())
        path.write_text(path.read_text(encoding="utf-8").replace('"x"', '"tampered"'), encoding="utf-8")
        with self.assertRaises(sb.PlanError) as cm:
            sb.apply(self.vault, path, approved)
        self.assertEqual(cm.exception.code, "APPROVAL_MISMATCH")

    def test_rejected_plan_writes_nothing(self):
        self.seed()
        before = {p: p.read_bytes() for p in (self.vault / "wiki").rglob("*.md")}
        h = self.hash("orbit")
        path = self.write_plan(plan([
            {"operation_id": "op-001", "type": "append_to_section", "page_id": "orbit", "expected_hash": h,
             "section_path": ["Facts"], "content": "ok", "source_ids": [SRC]},
            {"operation_id": "op-002", "type": "update_section", "page_id": "orbit", "expected_hash": h,
             "section_path": ["Missing"], "content": "x", "source_ids": [SRC]}]))
        with self.assertRaises(sb.PlanError):
            sb.apply(self.vault, path, sb.sha256_bytes(path.read_bytes()))
        self.assertEqual(before, {p: p.read_bytes() for p in (self.vault / "wiki").rglob("*.md")})


class TestCli(VaultTest):
    def test_render_and_hash_commands(self):
        self.seed()
        path = self.write_plan(plan([{"operation_id": "op-001", "type": "append_to_section", "page_id": "orbit",
                                      "expected_hash": self.hash("orbit"), "section_path": ["Facts"],
                                      "content": "- rendered", "source_ids": [SRC]}]))
        from io import StringIO
        from contextlib import redirect_stdout
        buf = StringIO()
        with redirect_stdout(buf):
            code = sb.main(["--vault", str(self.vault), "render-plan", str(path)])
        out = buf.getvalue()
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("PLAN SHA256: " + sb.sha256_bytes(path.read_bytes())))
        self.assertIn("+- rendered", out)
        buf = StringIO()
        with redirect_stdout(buf):
            sb.main(["--vault", str(self.vault), "hash", "orbit", "--sections"])
        self.assertIn("Facts > Detail  sha256:", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
