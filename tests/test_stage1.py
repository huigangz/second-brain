"""Stage 1 tests (spec/stage1-design.md), organised by the main spec §47 test matrix."""
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tools"))
import second_brain as sb  # noqa: E402

# Synthetic credentials for the secret-scanner tests. None is real, and each is assembled from pieces at
# runtime, so no secret-shaped literal appears in this file: the repo passes its own scanner and hosted
# secret scanning (push protection) never sees a candidate.
FAKE_AWS_KEY = "AKIA" + "ABCDEFGHIJKLMNOP"
FAKE_PASSWORD = "pass" + "word=" + "not-a-real-one-0000"           # credential-assignment rule
FAKE_PASSWORD_SPACED = "pass" + "word = " + "not-a-real-one-0000"
FAKE_API_KEY = "api" + "_key: " + "not-a-real-one-0000"
FAKE_PRIVATE_KEY = "-----BEGIN RSA " + "PRIVATE KEY-----"
FAKE_GITHUB_TOKEN = "gh" + "p_" + "0" * 36
FAKE_SLACK_TOKEN = "xo" + "xb-" + "0000000000-abc"
FAKE_BEARER = "Authorization: " + "Bearer " + "0" * 24
FAKE_JWT = "ey" + "J" + "a" * 12 + "." + "b" * 12 + "." + "c" * 12
FAKE_DB_URL = "postgres://app:" + "not-real-pw" + "@db:5432/x"
MEETING = "# Demo Meeting\n\nDate: 2026-09-18\n\nWe decided to use Redis.\n"


class ManagedVault(unittest.TestCase):
    def setUp(self):
        os.environ["SB_TODAY"] = "2026-09-24"
        self.tmp = tempfile.TemporaryDirectory()
        self.vault = Path(self.tmp.name)
        sb.init_vault(self.vault)
        self.n = 0

    def tearDown(self):
        for k in ("SB_TODAY", "SB_CRASH_AFTER"):
            os.environ.pop(k, None)
        self.tmp.cleanup()

    # -- helpers ---------------------------------------------------------------
    def raw(self, rel, text):
        p = self.vault / "raw" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return p

    def sources(self):
        return sb.State(self.vault).sources()

    def pages(self):
        return sb.State(self.vault).pages()

    def plan(self, ops, sids):
        self.n += 1
        srcs = self.sources()
        return {"schema_version": 2, "plan_id": f"plan-20260924-s{self.n:03d}", "source_ids": list(sids),
                "source_versions": {s: srcs[s]["content_hash"] for s in sids}, "summary": "t", "operations": ops}

    def write(self, p):
        path = self.vault / "plans" / "pending" / f"{p['plan_id']}.json"
        path.write_text(json.dumps(p, ensure_ascii=False), encoding="utf-8")
        return path

    def apply(self, p):
        path = self.write(p)
        return sb.apply(self.vault, path, sb.sha256_bytes(path.read_bytes()))

    def execute(self, p):
        return sb.execute(sb.Wiki(self.vault), p, False, sb.State(self.vault))

    def rejects(self, code, p):
        with self.assertRaises(sb.PlanError) as cm:
            self.execute(p)
        self.assertEqual(cm.exception.code, code, str(cm.exception))
        return cm.exception

    def page_hash(self, pid):
        return sb.sha256_text(sb.Wiki(self.vault).pages[pid].raw)

    def page_file(self, pid):
        return self.vault / self.pages()[pid]["path"]

    def source_op(self, sid, op_id="op-001"):
        rec = self.sources()[sid]
        return {"operation_id": op_id, "type": "create_page", "page_id": sid, "page_type": "source",
                "title": "Demo Meeting", "body": "## Summary\n\nRedis chosen.",
                "source": {"source_id": sid, "source_type": rec["source_type"], "raw_path": rec["path"],
                           "source_date": rec["source_date"], "date_confidence": rec["source_date_confidence"],
                           "date_basis": rec["source_date_basis"], "synthetic": False}}

    def ingest_demo(self):
        """raw → discover → plan (source page + entity with two sections) → apply."""
        self.raw("meetings/2026-09-18-demo-meeting.md", MEETING)
        sb.discover(self.vault)
        sid = next(iter(self.sources()))
        self.apply(self.plan([self.source_op(sid), {
            "operation_id": "op-002", "type": "create_page", "page_id": "redis", "page_type": "entity",
            "title": "Redis", "body": f"## Facts\n\n- chosen（[[{sid}|Demo Meeting]]）\n\n## Notes\n\n- none",
            "source_ids": [sid]}], [sid]))
        return sid

    def section_op(self, sid, section, content, **extra):
        return {"operation_id": "op-001", "type": "update_section", "page_id": "redis",
                "expected_hash": self.page_hash("redis"), "section_path": [section], "content": content,
                "source_ids": [sid], **extra}


class TestDiscover(ManagedVault):
    def test_new_source_id_date_and_status(self):
        self.raw("meetings/2026-09-18-demo-meeting.md", MEETING)
        self.raw("documents/Design Notes.html", "<p>no date</p>")
        report = sb.discover(self.vault)
        srcs = self.sources()
        self.assertIn("meeting-2026-09-18-demo-meeting", srcs)
        self.assertIn("document-undated-design-notes", srcs)
        rec = srcs["meeting-2026-09-18-demo-meeting"]
        self.assertEqual((rec["status"], rec["source_date_confidence"]), ("new", "high"))
        self.assertTrue(rec["content_hash"].startswith("sha256:"))
        self.assertEqual(srcs["document-undated-design-notes"]["source_date"], "unknown")
        self.assertTrue(any(line.startswith("NEW") for line in report))
        self.assertEqual(sb.discover(self.vault), ["no changes"])

    def test_pdf_companion_is_not_a_separate_source(self):
        self.raw("documents/data.pdf", "%PDF-1.4 \x00binary")
        self.raw("documents/data.pdf.txt", "extracted text")
        sb.discover(self.vault)
        self.assertEqual([r["path"] for r in self.sources().values()], ["raw/documents/data.pdf"])

    def test_companion_is_part_of_the_evidence(self):
        """review 2 P1: the .pdf.txt companion is what the agent reads and what preflight scans."""
        self.raw("documents/data.pdf", "%PDF-1.4 \x00binary")
        companion = self.raw("documents/data.pdf.txt", "clean text")
        sb.discover(self.vault)
        sid, rec = next(iter(self.sources().items()))
        self.assertEqual(rec["status"], "new")
        companion.write_text("clean text\n" + FAKE_PASSWORD + "\n", encoding="utf-8")
        report = sb.discover(self.vault)
        rec2 = self.sources()[sid]
        self.assertNotEqual(rec["content_hash"], rec2["content_hash"], report)
        self.assertEqual(rec2["status"], "blocked_secret")
        # a plan written against the earlier version no longer matches
        companion.write_text("clean text again", encoding="utf-8")
        sb.discover(self.vault)
        p = self.plan([self.source_op(sid)], [sid])
        p["source_versions"][sid] = rec["content_hash"]
        self.rejects("SOURCE_STALE", p)
        self.assertNotIn("secret_hits", self.sources()[sid])
        self.execute(self.plan([self.source_op(sid)], [sid]))  # the current version is accepted
        companion.write_text("edited after discover", encoding="utf-8")
        self.rejects("SOURCE_STALE", self.plan([self.source_op(sid)], [sid]))
        companion.unlink()  # without the companion the binary cannot be scanned
        sb.discover(self.vault)
        self.assertEqual(self.sources()[sid]["status"], "blocked_unscannable")

    def test_binary_without_companion_is_blocked(self):
        self.raw("documents/scan.pdf", "%PDF-1.4 \x00binary")
        report = sb.discover(self.vault)
        sid = next(iter(self.sources()))
        self.assertEqual(self.sources()[sid]["status"], "blocked_unscannable", report)
        self.rejects("SOURCE_NOT_READY", self.plan([self.source_op(sid)], [sid]))
        self.raw("documents/scan.pdf.txt", "extracted")
        sb.discover(self.vault)
        self.assertEqual(self.sources()[sid]["status"], "new")

    def test_changed_raw_is_redated(self):
        """review 3 P1: a changed raw file is dated again; the source page follows at the next apply."""
        sid = self.ingest_demo()  # Date: 2026-09-18 (structured → high)
        f = self.vault / self.sources()[sid]["path"]
        f.write_text(MEETING.replace("2026-09-18", "2026-09-20"), encoding="utf-8")
        report = sb.discover(self.vault)
        self.assertTrue(any(l.startswith(f"SOURCE_DATE_CHANGED {sid}: 2026-09-18") for l in report), report)
        rec = self.sources()[sid]
        self.assertEqual((rec["source_date"], rec["source_date_confidence"], rec["status"]),
                         ("2026-09-20", "high", "changed"))
        op = {"operation_id": "op-001", "type": "update_section", "page_id": sid, "expected_hash": self.page_hash(sid),
              "section_path": ["Summary"], "content": "Redis chosen (re-ingested).", "source_ids": [sid]}
        ex = self.apply(self.plan([op], [sid]))
        self.assertTrue(any(fl.startswith("SOURCE DATE CHANGED") for fl in ex.results[0].flags))
        self.assertEqual(sb.Wiki(self.vault).pages[sid].fm["source_date"], "2026-09-20")
        self.assertEqual(self.sources()[sid]["source_date"], "2026-09-20")

    def test_redated_future_source_is_flagged(self):
        """review 6 P2: FUTURE DATE uses the registry date, which is ahead of the page after a re-date."""
        sid = self.ingest_demo()
        f = self.vault / self.sources()[sid]["path"]
        f.write_text(MEETING.replace("2026-09-18", "2027-01-01"), encoding="utf-8")
        sb.discover(self.vault)
        op = {"operation_id": "op-001", "type": "update_section", "page_id": sid, "expected_hash": self.page_hash(sid),
              "section_path": ["Summary"], "content": "Redis chosen (re-ingested).", "source_ids": [sid]}
        ex = self.execute(self.plan([op], [sid]))
        self.assertIn("FUTURE DATE", ex.results[0].flags)

    def test_date_origin_is_explicit(self):
        """review 9 (standards + P2): the origin is a field, not guessed from the date_basis wording."""
        f = self.raw("documents/Design Notes.md", "# Notes\n\nno date here\n")
        sb.discover(self.vault)
        sid = next(iter(self.sources()))
        self.assertEqual(self.sources()[sid]["source_date_origin"], "discover")
        op = self.source_op(sid)
        op["source"].update(source_date="2026-09-01", date_confidence="medium",
                            date_basis="filename: date supplied by the ingest plan")  # looks like discover's wording
        self.apply(self.plan([op], [sid]))
        self.assertEqual(self.sources()[sid]["source_date_origin"], "plan")
        f.write_text("# Notes\n\nstill no date\n", encoding="utf-8")
        sb.discover(self.vault)
        self.assertEqual(self.sources()[sid]["source_date"], "2026-09-01")
        # a registry written before the field existed is migrated once, from the basis wording
        reg = json.loads((self.vault / "state/sources.json").read_text(encoding="utf-8"))
        del reg["sources"][sid]["source_date_origin"]
        (self.vault / "state/sources.json").write_text(json.dumps(reg), encoding="utf-8")
        sb.discover(self.vault)
        self.assertIn(self.sources()[sid]["source_date_origin"], ("discover", "plan"))

    def test_plan_supplied_date_survives_a_dateless_change(self):
        f = self.raw("documents/Design Notes.md", "# Notes\n\nno date here\n")
        sb.discover(self.vault)
        sid = next(iter(self.sources()))
        op = self.source_op(sid)
        op["source"].update(source_date="2026-09-01", date_confidence="medium", date_basis="body: sprint 12 start")
        self.apply(self.plan([op], [sid]))
        f.write_text("# Notes\n\nstill no date, more text\n", encoding="utf-8")
        report = sb.discover(self.vault)
        self.assertFalse(any("SOURCE_DATE_CHANGED" in l for l in report), report)
        self.assertEqual(self.sources()[sid]["source_date"], "2026-09-01")

    def test_title_area_date_is_level_3(self):
        """review 3 P2: main spec §28 level 3 — one clear date in the title area → medium."""
        cases = {"documents/a.md": ("# Quarterly review — September 18, 2026\n\nbody\n", "2026-09-18"),
                 "documents/b.html": ("<html><h1>Report 2026-09-17</h1><p>x</p></html>", "2026-09-17"),
                 "documents/c.md": ("# 评审\n\n2026年9月16日 会议纪要\n", "2026-09-16"),
                 "documents/d.md": ("# Plan\n\nfrom 2026-09-01 to 2026-09-30\n", "unknown"),
                 "documents/e.md": ("# Notes\n" + "\ntext\n" * 20 + "\ndeadline 2026-10-01\n", "unknown"),
                 "documents/f.md": ("# Notes\n\n2026-02-30 is not a date\n", "unknown")}
        for rel, (text, _) in cases.items():
            self.raw(rel, text)
        sb.discover(self.vault)
        got = {r["path"].split("/")[-1]: (r["source_date"], r["source_date_confidence"])
               for r in self.sources().values()}
        for rel, (_, date) in cases.items():
            name = rel.split("/")[-1]
            self.assertEqual(got[name], (date, "medium" if date != "unknown" else "low"), name)

    def test_link_target_must_be_a_raw_source_file(self):
        """review 3 P2: evidence comes only from raw/."""
        sid = self.ingest_demo()
        (self.vault / self.sources()[sid]["path"]).unlink()
        sb.discover(self.vault)  # missing: a link to a *good* path would be accepted, so only the path decides
        (self.vault.parent / f"outside-{self.vault.name}.md").write_text("x", encoding="utf-8")
        self.raw("documents/data.pdf", "%PDF \x00")
        self.raw("documents/data.pdf.txt", "text")
        for target in (f"../outside-{self.vault.name}.md", str(self.vault.parent / f"outside-{self.vault.name}.md"),
                       "raw/documents/data.pdf.txt", "wiki/index.md", "raw/meetings/../../wiki/index.md"):
            with self.assertRaises(sb.PlanError, msg=target):
                sb.discover(self.vault, (target, sid))
        self.assertEqual(self.sources()[sid]["path"], "raw/meetings/2026-09-18-demo-meeting.md")
        (self.vault.parent / f"outside-{self.vault.name}.md").unlink()

    def test_copy_of_a_live_twin_is_not_a_move(self):
        """review 8 P1: A and B share content, A vanishes, C is copied from B: C may be B's copy → no MOVED."""
        a = self.raw("documents/a.md", "# Same\n\nsame\n")
        b = self.raw("documents/b.md", "# Same\n\nsame\n")
        sb.discover(self.vault)
        a.unlink()
        shutil.copy(b, self.vault / "raw/documents/c.md")
        report = sb.discover(self.vault)
        self.assertTrue(any(l.startswith("AMBIGUOUS_MOVE raw/documents/c.md") and "document-undated-b" in l
                            for l in report), report)
        srcs = self.sources()
        self.assertEqual(srcs["document-undated-a"]["status"], "missing")
        self.assertEqual(srcs["document-undated-c"]["path"], "raw/documents/c.md")

    def test_generated_source_ids_stay_within_80_characters(self):
        """review 8 P2: the -n suffix must not push an id past the page_id limit."""
        for tail in ("1", "2", "3"):
            self.raw(f"documents/2026-09-18-{'a' * 70}{tail}.md", "# X\n")
        sb.discover(self.vault)
        for sid in self.sources():
            self.assertTrue(sb.valid_page_id(sid), (len(sid), sid))
        self.assertEqual(len(self.sources()), 3)

    def test_one_missing_source_two_identical_new_files(self):
        """review 6 P1: a move is automatic only if exactly one missing source meets exactly one new file."""
        sid = self.ingest_demo()
        old = self.vault / self.sources()[sid]["path"]
        (self.vault / "raw/documents").mkdir(parents=True, exist_ok=True)
        shutil.copy(old, self.vault / "raw/documents/a-copy.md")
        old.rename(self.vault / "raw/sessions/z-actual-move.md")
        report = sb.discover(self.vault)
        amb = [l for l in report if l.startswith("AMBIGUOUS_MOVE")]
        self.assertEqual(len(amb), 2, report)
        rec = self.sources()[sid]
        self.assertEqual((rec["status"], rec["source_type"]), ("missing", "meeting"))
        sb.discover(self.vault, ("raw/sessions/z-actual-move.md", sid))  # the human decides
        self.assertEqual(self.sources()[sid]["path"], "raw/sessions/z-actual-move.md")

    def test_only_known_text_formats_are_scanned_directly(self):
        """review 5 P1: a PDF / DOCX without NUL bytes is still binary; no companion → blocked_unscannable."""
        self.raw("documents/a.pdf", "%PDF-1.7 no nul bytes here")
        self.raw("documents/b.docx", "PK\x03\x04word/document.xml")
        (self.vault / "raw/documents/c.md").write_bytes(b"# Latin-1 \xe9\xe8 not utf-8\n")
        self.raw("documents/d.md", "# Fine\n")
        self.raw("documents/e.pdf", "%PDF-1.7")
        self.raw("documents/e.pdf.txt", "extracted text")
        sb.discover(self.vault)
        status = {r["path"].split("/")[-1]: r["status"] for r in self.sources().values()}
        self.assertEqual(status, {"a.pdf": "blocked_unscannable", "b.docx": "blocked_unscannable",
                                  "c.md": "blocked_unscannable", "d.md": "new", "e.pdf": "new"})

    def test_link_only_confirms_a_real_move(self):
        """review 5 P1: the linked source must be missing; the link then settles type / date / status."""
        sid = self.ingest_demo()  # raw/meetings/2026-09-18-demo-meeting.md
        old = self.vault / self.sources()[sid]["path"]
        self.raw("documents/copy.md", MEETING)
        sb.discover(self.vault)  # a copy: the original still exists
        with self.assertRaises(sb.PlanError) as cm:
            sb.discover(self.vault, ("raw/documents/copy.md", sid))
        self.assertEqual(cm.exception.code, "LINK_CONFLICT")
        old.unlink()
        sb.discover(self.vault)
        report = sb.discover(self.vault, ("raw/documents/copy.md", sid))
        rec = self.sources()[sid]
        self.assertEqual((rec["path"], rec["source_type"], rec["status"]),
                         ("raw/documents/copy.md", "document", "ingested"), report)

    def test_block_survives_rename_and_reappearance(self):
        """review 4 P1: only a scan of the current content lifts a BLOCK, never a rename or a missing/found cycle."""
        leaky = self.raw("sessions/s.md", "# S\n\nkey " + FAKE_AWS_KEY + "\n")
        binary = self.raw("documents/scan.pdf", "%PDF \x00")
        sb.discover(self.vault)
        ids = {r["path"]: s for s, r in self.sources().items()}
        sid_s, sid_b = ids["raw/sessions/s.md"], ids["raw/documents/scan.pdf"]
        leaky.rename(leaky.with_name("renamed.md"))
        binary.rename(binary.with_name("renamed.pdf"))
        report = sb.discover(self.vault)
        self.assertEqual(self.sources()[sid_s]["status"], "blocked_secret", report)
        self.assertEqual(self.sources()[sid_b]["status"], "blocked_unscannable", report)
        moved = self.vault / "raw/sessions/renamed.md"
        text = moved.read_bytes()
        moved.unlink()
        sb.discover(self.vault)
        self.assertEqual(self.sources()[sid_s]["status"], "missing")
        moved.write_bytes(text)
        sb.discover(self.vault)
        self.assertEqual(self.sources()[sid_s]["status"], "blocked_secret")
        self.rejects("SOURCE_NOT_READY", self.plan([self.source_op(sid_s)], [sid_s]))

    def test_ambiguous_move_is_not_guessed(self):
        """review 4 P1: two missing sources with the same content → no automatic MOVED."""
        a = self.raw("documents/a.md", "# Same\n\nsame\n")
        b = self.raw("documents/b.md", "# Same\n\nsame\n")
        sb.discover(self.vault)
        a.unlink()
        b.unlink()
        self.raw("documents/c.md", "# Same\n\nsame\n")
        report = sb.discover(self.vault)
        self.assertTrue(any(l.startswith("AMBIGUOUS_MOVE raw/documents/c.md") for l in report), report)
        srcs = self.sources()
        self.assertEqual((srcs["document-undated-a"]["status"], srcs["document-undated-b"]["status"]),
                         ("missing", "missing"))
        self.assertEqual(srcs["document-undated-c"]["path"], "raw/documents/c.md")
        sb.discover(self.vault, ("raw/documents/c.md", "document-undated-b"))
        srcs = self.sources()
        self.assertEqual(srcs["document-undated-b"]["path"], "raw/documents/c.md")
        self.assertNotIn("document-undated-c", srcs)

    def test_move_updates_registry_and_source_page(self):
        """review 4 P2: the registry and the source page follow a move (path, filename date, type)."""
        f = self.raw("meetings/2026-01-01-note.md", "# Note\n\nbody\n")
        sb.discover(self.vault)
        sid = next(iter(self.sources()))
        self.apply(self.plan([self.source_op(sid)], [sid]))
        page = self.page_file(sid)
        page.write_text(page.read_text(encoding="utf-8").replace("Redis chosen.", "Redis chosen. (my note)"),
                        encoding="utf-8")  # a human edit that must survive the sync
        new = self.vault / "raw/documents/2026-02-02-note.md"
        f.rename(new)
        report = sb.discover(self.vault)
        self.assertTrue(any(l.startswith("SOURCE_DATE_CHANGED") for l in report), report)
        self.assertTrue(any(l.startswith("SOURCE_TYPE_CHANGED") for l in report), report)
        rec = self.sources()[sid]
        self.assertEqual((rec["path"], rec["source_date"], rec["source_type"], rec["status"]),
                         ("raw/documents/2026-02-02-note.md", "2026-02-02", "document", "ingested"))
        report = sb.maintain(self.vault, fix=True)
        self.assertTrue(any(l.startswith(f"SOURCE_PAGE_OUTDATED    {sid}") for l in report), report)
        fm = sb.Wiki(self.vault).pages[sid].fm
        self.assertEqual((fm["raw_path"], fm["source_date"], fm["source_type"]),
                         ("raw/documents/2026-02-02-note.md", "2026-02-02", "document"))
        self.assertIn("(my note)", page.read_text(encoding="utf-8"))
        base = sb.Page.parse(sb.State(self.vault).baseline(sid))
        self.assertEqual(base.fm, fm)  # baseline got the same frontmatter change: the diff is only the human's
        fix_txn = next(p.stem for p in (self.vault / "state/txn").glob("*.json")
                       if json.loads(p.read_text(encoding="utf-8"))["kind"] == "maintain")
        self.assertEqual(self.pages()[sid]["last_txn"], fix_txn)  # review 5 P2: the page was written by it
        after = sb.maintain(self.vault)
        self.assertFalse(any("SOURCE_PAGE_OUTDATED" in l for l in after), after)
        self.assertTrue(any(l.startswith(f"HUMAN_EDITED            {sid}") for l in after), after)  # still visible

    def test_impossible_calendar_dates(self):
        """review 4 P2: shape-valid but impossible dates are not dates."""
        self.raw("documents/bad.md", "Date: 2026-99-99\n\n# Bad\n")
        self.raw("documents/2026-13-01-worse.md", "# Worse\n")
        sb.discover(self.vault)
        for rec in self.sources().values():
            self.assertEqual((rec["source_date"], rec["source_date_confidence"]), ("unknown", "low"), rec["path"])
        self.assertIn("document-undated-bad", self.sources())

    def test_source_rename_keeps_id(self):
        """§47 Source Rename: same hash + old path missing → same source_id."""
        sid = self.ingest_demo()
        old = self.vault / self.sources()[sid]["path"]
        new = old.with_name("renamed.md")
        old.rename(new)
        report = sb.discover(self.vault)
        self.assertTrue(any(line.startswith("MOVED") for line in report), report)
        rec = self.sources()[sid]
        self.assertEqual((rec["path"], rec["status"]), ("raw/meetings/renamed.md", "ingested"))
        self.assertEqual(len(self.sources()), 1)

    def test_source_copy_is_not_a_move(self):
        """§47 Source Copy: old path still exists → new source, original untouched."""
        sid = self.ingest_demo()
        shutil.copy(self.vault / self.sources()[sid]["path"], self.vault / "raw/meetings/copy.md")
        report = sb.discover(self.vault)
        self.assertTrue(any("DUPLICATE CONTENT" in line for line in report), report)
        self.assertEqual(self.sources()[sid]["path"], "raw/meetings/2026-09-18-demo-meeting.md")
        self.assertEqual(len(self.sources()), 2)

    def test_moved_and_modified_needs_link(self):
        sid = self.ingest_demo()
        old = self.vault / self.sources()[sid]["path"]
        old.unlink()
        self.raw("meetings/elsewhere.md", MEETING + "\nedited\n")
        report = sb.discover(self.vault)
        self.assertTrue(any("POSSIBLE MOVED+MODIFIED" in line for line in report), report)
        self.assertEqual(self.sources()[sid]["status"], "missing")
        new_sid = next(s for s in self.sources() if s != sid)
        # the user confirms: link the new file to the original id; the provisional record is dropped
        report = sb.discover(self.vault, ("raw/meetings/elsewhere.md", sid))
        rec = self.sources()[sid]
        self.assertEqual((rec["path"], rec["status"]), ("raw/meetings/elsewhere.md", "changed"), report)
        self.assertNotIn(new_sid, self.sources())
        self.assertTrue(any(l.startswith("DROPPED") for l in report), report)
        self.assertEqual(sb.discover(self.vault), ["no changes"])

    def test_link_refuses_to_drop_an_ingested_source(self):
        sid = self.ingest_demo()
        other = self.raw("meetings/other.md", "# Other\n\nDate: 2026-09-19\n")
        sb.discover(self.vault)
        oid = next(s for s in self.sources() if s != sid)
        self.apply(self.plan([{**self.source_op(oid), "title": "Other"}], [oid]))
        (self.vault / self.sources()[sid]["path"]).unlink()
        sb.discover(self.vault)  # sid is now missing: a link would be allowed if other.md were not ingested
        with self.assertRaises(sb.PlanError) as cm:
            sb.discover(self.vault, ("raw/meetings/other.md", sid))
        self.assertEqual(cm.exception.code, "LINK_CONFLICT")
        self.assertIn(oid, self.sources())


class TestIngestLifecycle(ManagedVault):
    def test_raw_immutability_and_registries(self):
        """§47 Raw Immutability + registries/baselines written by the transaction."""
        self.raw("meetings/2026-09-18-demo-meeting.md", MEETING)
        before = {p: p.read_bytes() for p in (self.vault / "raw").rglob("*") if p.is_file()}
        sb.discover(self.vault)
        sid = next(iter(self.sources()))
        self.apply(self.plan([self.source_op(sid)], [sid]))
        self.assertEqual(before, {p: p.read_bytes() for p in (self.vault / "raw").rglob("*") if p.is_file()})
        rec = self.sources()[sid]
        self.assertEqual((rec["status"], rec["ingested_hash"]), ("ingested", rec["content_hash"]))
        self.assertEqual(self.pages()[sid]["origin"], "plan")
        self.assertTrue((self.vault / "state/baselines" / f"{sid}.md.base").exists())
        events = [json.loads(l)["event"] for l in (self.vault / "state/events.jsonl").read_text().splitlines()]
        self.assertIn("txn_commit", events)

    def test_idempotency(self):
        """§47 Idempotency: unchanged source re-ingest → no wiki mutation."""
        sid = self.ingest_demo()
        e = self.rejects("SOURCE_ALREADY_INGESTED", self.plan([self.source_op(sid)], [sid]))
        self.assertIn("ingested", e.message)

    def test_source_versions_required_and_bound(self):
        sid = self.ingest_demo()
        p = self.plan([self.section_op(sid, "Notes", "- x")], [sid])
        p.pop("source_versions")
        self.rejects("SCHEMA", p)
        p = self.plan([self.section_op(sid, "Notes", "- x")], [sid])
        p["source_versions"][sid] = "sha256:" + "0" * 64
        self.rejects("SOURCE_STALE", p)

    def test_changed_source(self):
        """§47 Changed Source: detected as CHANGED; stale plans rejected; re-ingest by updating the source page."""
        sid = self.ingest_demo()
        stale = self.plan([self.section_op(sid, "Notes", "- x")], [sid])
        (self.vault / self.sources()[sid]["path"]).write_text(MEETING + "\nAddendum.\n", encoding="utf-8")
        self.rejects("SOURCE_STALE", stale)  # raw changed after discover
        report = sb.discover(self.vault)
        self.assertIn(f"CHANGED      {sid}", report)
        self.assertEqual(self.sources()[sid]["status"], "changed")
        self.rejects("SOURCE_STALE", stale)  # plan still bound to the old hash
        self.apply(self.plan([{"operation_id": "op-001", "type": "update_section", "page_id": sid,
                               "expected_hash": self.page_hash(sid), "section_path": ["Summary"],
                               "content": "Redis chosen. Addendum noted.", "source_ids": [sid]}], [sid]))
        rec = self.sources()[sid]
        self.assertEqual((rec["status"], rec["ingested_hash"]), ("ingested", rec["content_hash"]))

    def test_unregistered_source_and_missing(self):
        self.raw("meetings/2026-09-18-demo-meeting.md", MEETING)
        sb.discover(self.vault)
        sid = next(iter(self.sources()))
        p = self.plan([self.source_op(sid)], [sid])
        (self.vault / self.sources()[sid]["path"]).unlink()
        sb.discover(self.vault)
        self.rejects("SOURCE_NOT_READY", p)


class TestHumanEdits(ManagedVault):
    def edit(self, pid, old, new):
        f = self.page_file(pid)
        text = f.read_text(encoding="utf-8")
        self.assertIn(old, text)
        f.write_text(text.replace(old, new), encoding="utf-8")

    def test_change_in_different_section_is_allowed(self):
        """§47: human edits section A, AI updates section B → allowed, page marked human-maintained."""
        sid = self.ingest_demo()
        self.edit("redis", "- none", "- a human note")
        ex = self.apply(self.plan([self.section_op(sid, "Facts", "- chosen for locking")], [sid]))
        self.assertIn("HUMAN-MAINTAINED PAGE", ex.results[0].flags)
        self.assertTrue(self.pages()["redis"]["human_touched"])
        text = self.page_file("redis").read_text(encoding="utf-8")
        self.assertIn("- a human note", text)  # human text untouched
        self.assertIn("- chosen for locking", text)

    def test_change_in_same_section_is_blocked_then_overridable(self):
        """§47: baseline section != current section → blocked; human_override lets it through."""
        sid = self.ingest_demo()
        self.edit("redis", "- none", "- a human note")
        e = self.rejects("HUMAN_EDITED_SECTION", self.plan([self.section_op(sid, "Notes", "- ai text")], [sid]))
        self.assertIn("+- a human note", e.message)  # baseline → current diff is shown
        ok = self.plan([self.section_op(sid, "Notes", "- ai text",
                                        human_override={"confirmed_by_user": True, "note": "user agreed"})], [sid])
        ex = self.apply(ok)
        self.assertIn("HUMAN EDIT OVERRIDE", ex.results[0].flags)

    def test_meta_edit_blocks_meta_ops_and_sticky_flag(self):
        sid = self.ingest_demo()
        self.edit("redis", "tags: []", "tags: [human]")
        self.rejects("HUMAN_EDITED_META", self.plan([{
            "operation_id": "op-001", "type": "update_meta", "page_id": "redis",
            "expected_hash": self.page_hash("redis"), "set": {"aliases": ["redis db"]}, "source_ids": [sid]}], [sid]))
        # add_section never overwrites human text → allowed
        self.apply(self.plan([{"operation_id": "op-001", "type": "add_section", "page_id": "redis",
                               "expected_hash": self.page_hash("redis"), "parent_path": [], "heading": "More",
                               "content": "x", "source_ids": [sid]}], [sid]))
        # baseline now absorbs the edit (Q3), but human_touched stays set
        ex = self.apply(self.plan([self.section_op(sid, "Notes", "- later")], [sid]))
        self.assertIn("HUMAN-MAINTAINED PAGE", ex.results[0].flags)
        self.assertTrue(self.pages()["redis"]["human_touched"])

    def test_existing_human_page(self):
        """§47 Existing Human Page: not in pages.json → human_touched; every change needs an override."""
        sid = self.ingest_demo()
        f = self.vault / "wiki/concepts/handmade.md"
        f.write_text("---\npage_id: handmade\npage_type: concept\ntitle: Handmade\n---\n# Handmade\n\n## Body\n\ntext\n",
                     encoding="utf-8")
        report = sb.maintain(self.vault)
        self.assertTrue(any(l.startswith("UNREGISTERED") for l in report))
        sb.maintain(self.vault, fix=True)
        self.assertEqual((self.pages()["handmade"]["origin"], self.pages()["handmade"]["human_touched"]),
                         ("external", True))
        op = {"operation_id": "op-001", "type": "append_to_section", "page_id": "handmade",
              "expected_hash": self.page_hash("handmade"), "section_path": ["Body"], "content": "more",
              "source_ids": [sid]}
        self.rejects("HUMAN_EDITED_SECTION", self.plan([op], [sid]))
        add = {"operation_id": "op-001", "type": "add_section", "page_id": "handmade", "parent_path": [],
               "expected_hash": self.page_hash("handmade"), "heading": "Extra", "content": "x", "source_ids": [sid]}
        self.rejects("HUMAN_EDITED_SECTION", self.plan([add], [sid]))  # review 2026-09-23 P2: add_section too
        self.apply(self.plan([dict(op, human_override={"confirmed_by_user": True, "note": "ok"})], [sid]))


class TestTransactions(ManagedVault):
    def snapshot_tree(self):
        return {p.relative_to(self.vault).as_posix(): p.read_bytes()
                for p in self.vault.rglob("*") if p.is_file()
                and not p.relative_to(self.vault).as_posix().startswith(("state/txn", "state/snapshots", "state/events",
                                                                         "plans/"))}

    def test_crash_blocks_writes_and_rollback_restores(self):
        """§47 Transaction Crash: rollback restores modified files and deletes created ones."""
        sid = self.ingest_demo()
        before = self.snapshot_tree()
        p = self.plan([self.section_op(sid, "Notes", "- crash"), {
            "operation_id": "op-002", "type": "create_page", "page_id": "new-page", "page_type": "concept",
            "title": "New", "body": "x", "source_ids": [sid]}], [sid])
        os.environ["SB_CRASH_AFTER"] = "2"
        with self.assertRaises(RuntimeError):
            self.apply(p)
        os.environ.pop("SB_CRASH_AFTER")
        self.assertFalse((self.vault / "state/.lock").exists())
        txn = sb.State(self.vault).incomplete_txns()
        self.assertEqual(len(txn), 1)
        self.rejects("INCOMPLETE_TXN", self.plan([self.section_op(sid, "Notes", "- y")], [sid]))
        restored = sb.rollback(self.vault, txn[0])
        self.assertTrue(any(r.startswith("create") for r in restored))
        self.assertEqual(before, self.snapshot_tree())
        self.assertEqual(sb.State(self.vault).incomplete_txns(), [])

    def test_rollback_committed_only_without_conflict(self):
        sid = self.ingest_demo()
        before = self.snapshot_tree()
        ex = self.apply(self.plan([self.section_op(sid, "Notes", "- first")], [sid]))
        sb.rollback(self.vault, ex.txn_id)
        self.assertEqual(before, self.snapshot_tree())
        ex1 = self.apply(self.plan([self.section_op(sid, "Notes", "- a")], [sid]))
        self.apply(self.plan([self.section_op(sid, "Facts", "- b")], [sid]))
        with self.assertRaises(sb.PlanError) as cm:
            sb.rollback(self.vault, ex1.txn_id)
        self.assertEqual(cm.exception.code, "ROLLBACK_CONFLICT")

    def test_interrupted_rollback_blocks_writes_and_resumes(self):
        """review 2 P1: a rollback that dies halfway must stay visible as incomplete and be finishable."""
        sid = self.ingest_demo()
        before = self.snapshot_tree()
        ex = self.apply(self.plan([self.section_op(sid, "Notes", "- first")], [sid]))
        os.environ["SB_ROLLBACK_CRASH_AFTER"] = "1"
        try:
            with self.assertRaises(RuntimeError):
                sb.rollback(self.vault, ex.txn_id)
        finally:
            os.environ.pop("SB_ROLLBACK_CRASH_AFTER")
        self.assertEqual(sb.State(self.vault).incomplete_txns(), [ex.txn_id])
        self.rejects("INCOMPLETE_TXN", self.plan([self.section_op(sid, "Notes", "- y")], [sid]))
        sb.rollback(self.vault, ex.txn_id)  # resumes although the first file is already restored
        self.assertEqual(before, self.snapshot_tree())
        self.assertEqual(sb.State(self.vault).incomplete_txns(), [])

    def test_resumed_rollback_never_overwrites_later_edits(self):
        """review 3 P1: a file edited after an interrupted rollback restored it is a conflict, not overwritten."""
        sid = self.ingest_demo()
        before = self.snapshot_tree()
        ex = self.apply(self.plan([self.section_op(sid, "Notes", "- first")], [sid]))
        os.environ["SB_ROLLBACK_CRASH_AFTER"] = "1"
        try:
            with self.assertRaises(RuntimeError):
                sb.rollback(self.vault, ex.txn_id)
        finally:
            os.environ.pop("SB_ROLLBACK_CRASH_AFTER")
        manifest = json.loads((self.vault / f"state/txn/{ex.txn_id}.json").read_text(encoding="utf-8"))
        first = self.vault / manifest["files"][0]["path"]  # restored before the crash
        restored = first.read_bytes()
        first.write_bytes(restored + b"\nedited in Obsidian meanwhile\n")
        with self.assertRaises(sb.PlanError) as cm:
            sb.rollback(self.vault, ex.txn_id)
        self.assertEqual(cm.exception.code, "ROLLBACK_CONFLICT")
        self.assertIn(b"edited in Obsidian meanwhile", first.read_bytes())
        first.write_bytes(restored)  # the human settles it; the rollback can finish
        sb.rollback(self.vault, ex.txn_id)
        self.assertEqual(before, self.snapshot_tree())

    def test_writers_blocked_while_a_transaction_is_incomplete(self):
        """review 3 P1: reject-plan (and rollback of another txn) must not run next to an incomplete apply."""
        sid = self.ingest_demo()
        ingest_txn = next((self.vault / "state/txn").glob("*.json")).stem
        p = self.plan([self.section_op(sid, "Notes", "- crash")], [sid])
        os.environ["SB_CRASH_AFTER"] = "1"
        try:
            with self.assertRaises(RuntimeError):
                self.apply(p)
        finally:
            os.environ.pop("SB_CRASH_AFTER")
        pending = self.vault / f"plans/pending/{p['plan_id']}.json"
        for call in (lambda: sb.reject_plan(self.vault, pending, "no"), lambda: sb.rollback(self.vault, ingest_txn)):
            with self.assertRaises(sb.PlanError) as cm:
                call()
            self.assertEqual(cm.exception.code, "INCOMPLETE_TXN")
        self.assertTrue(pending.exists())

    def test_tampered_snapshot_is_never_restored(self):
        """review 4 P2: the snapshot must hash to the manifest's `before` before anything is restored."""
        sid = self.ingest_demo()
        ex = self.apply(self.plan([self.section_op(sid, "Notes", "- first")], [sid]))
        after = self.snapshot_tree()
        snap = next(p for p in (self.vault / "state/snapshots" / ex.txn_id).rglob("*.md") if "entities" in p.parts)
        snap.write_text("CORRUPTED", encoding="utf-8")
        with self.assertRaises(sb.PlanError) as cm:
            sb.rollback(self.vault, ex.txn_id)
        self.assertEqual(cm.exception.code, "ROLLBACK_CONFLICT")
        self.assertEqual(after, self.snapshot_tree())
        manifest = json.loads((self.vault / f"state/txn/{ex.txn_id}.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["status"], "committed")

    def test_committed_rollback_is_strict(self):
        """review 4 P2 / Q6: a committed transaction rolls back only if every file is still at `after`."""
        sid = self.ingest_demo()
        f = self.page_file("redis")
        old = f.read_bytes()
        ex = self.apply(self.plan([self.section_op(sid, "Notes", "- first")], [sid]))
        f.write_bytes(old)  # someone put the page back by hand
        with self.assertRaises(sb.PlanError) as cm:
            sb.rollback(self.vault, ex.txn_id)
        self.assertEqual(cm.exception.code, "ROLLBACK_CONFLICT")

    def test_concurrent_writer_is_locked_out(self):
        """§47 Concurrent Writer."""
        sid = self.ingest_demo()
        with sb.Lock(self.vault, "other writer"):
            with self.assertRaises(sb.PlanError) as cm:
                self.apply(self.plan([self.section_op(sid, "Notes", "- x")], [sid]))
            self.assertEqual(cm.exception.code, "LOCKED")
            self.assertIn("other writer", cm.exception.message)
        self.apply(self.plan([self.section_op(sid, "Notes", "- x")], [sid]))

    def test_stale_lock_hint(self):
        lock = self.vault / "state/.lock"
        lock.write_text(json.dumps({"pid": 999999, "host": sb._host(), "command": "apply-plan x",
                                    "started_at": "old", "epoch": 0}), encoding="utf-8")
        self.assertIn("STALE LOCK?", sb.describe_lock(lock))


class TestMaintain(ManagedVault):
    def test_obsidian_rename_is_detected_and_fixed(self):
        """§47 Obsidian Rename."""
        self.ingest_demo()
        f = self.page_file("redis")
        f.rename(f.with_name("Redis DB.md"))
        report = sb.maintain(self.vault)
        self.assertTrue(any(l.startswith("PAGE_ID_FILENAME_MISMATCH") for l in report), report)
        sb.maintain(self.vault, fix=True)
        self.assertTrue(f.exists())
        self.assertFalse(f.with_name("Redis DB.md").exists())
        self.assertEqual(sb.maintain(self.vault), ["no issues"])

    def test_broken_manual_link_is_reported(self):
        self.ingest_demo()
        f = self.page_file("redis")
        f.write_text(f.read_text(encoding="utf-8") + "\nsee [[Nowhere]]\n", encoding="utf-8")
        report = sb.maintain(self.vault)
        self.assertTrue(any(l.startswith("BROKEN_LINK") and "Nowhere" in l for l in report), report)
        self.assertTrue(any(l.startswith("HUMAN_EDITED") for l in report), report)


class TestSecrets(ManagedVault):
    def test_secret_in_raw_blocks_ingest(self):
        """§47 Secret handling — a synthetic credential is blocked before wiki propagation."""
        self.raw("sessions/2026-09-18-leaky.md", f"# Session\n\naws key {FAKE_AWS_KEY}\n")
        report = sb.discover(self.vault)
        sid = next(iter(self.sources()))
        rec = self.sources()[sid]
        self.assertEqual(rec["status"], "blocked_secret")
        self.assertIn("aws-access-key@line3", rec["secret_hits"])
        self.assertNotIn(FAKE_AWS_KEY, json.dumps(rec) + "\n".join(report))  # the secret itself is never stored
        self.rejects("SOURCE_NOT_READY", self.plan([self.source_op(sid)], [sid]))
        # sanitising the raw file and re-running discover unblocks it
        (self.vault / rec["path"]).write_text("# Session\n\naws key [REDACTED]\n", encoding="utf-8")
        sb.discover(self.vault)
        self.assertEqual(self.sources()[sid]["status"], "new")

    def test_secret_in_plan_content(self):
        sid = self.ingest_demo()
        self.rejects("SECRET_IN_PLAN", self.plan([self.section_op(sid, "Notes", "- " + FAKE_PASSWORD_SPACED)], [sid]))

    def test_plan_cannot_weaken_the_discovered_date(self):
        """review 2 P2 (AGENTS.md §4): refine unknown dates, never downgrade what discover found."""
        self.raw("meetings/2026-09-18-demo-meeting.md", MEETING)  # structured "Date:" → 2026-09-18 / high
        self.raw("documents/Design Notes.md", "# Notes\n\nno date here\n")
        sb.discover(self.vault)
        dated, undated = "meeting-2026-09-18-demo-meeting", "document-undated-design-notes"
        base = self.source_op(dated)
        for change in ({"source_date": "unknown", "date_confidence": "low"},
                       {"source_date": "2026-09-17"}, {"date_confidence": "medium"}):
            op = {**base, "source": {**base["source"], **change}}
            self.rejects("DATE_DOWNGRADE", self.plan([op], [dated]))
        u = self.source_op(undated)
        self.rejects("DATE_DOWNGRADE", self.plan([{**u, "source": {**u["source"], "source_date": "2026-09-01",
                                                                     "date_confidence": "high"}}], [undated]))
        self.execute(self.plan([{**u, "source": {**u["source"], "source_date": "2026-09-01",
                                                  "date_confidence": "medium", "date_basis": "body"}}], [undated]))

    def test_page_type_must_match_its_directory(self):
        """review 2 P2: wiki/<type-dir>/<page_id>.md."""
        sid = self.ingest_demo()
        op = self.section_op(sid, "Notes", "- x")
        wrong = self.vault / "wiki/concepts/wrong.md"
        wrong.write_text("---\npage_id: wrong\npage_type: source\ntitle: Wrong\n---\n# Wrong\n", encoding="utf-8")
        report = sb.maintain(self.vault, fix=True)
        self.assertTrue(any("does not belong in wiki/concepts/" in l for l in report), report)
        self.assertNotIn("wrong", self.pages())
        self.rejects("SCHEMA", self.plan([op], [sid]))

    def test_resulting_pages_must_be_valid(self):
        """review 2026-09-23 P2: a plan that validates must not produce an unaddressable / unmanageable page."""
        sid = self.ingest_demo()
        page = {"operation_id": "op-001", "type": "create_page", "page_id": "cache", "page_type": "concept",
                "title": "Cache", "body": "## A\n\nx\n\n## A\n\ny", "source_ids": [sid]}
        self.rejects("PATH_AMBIGUOUS", self.plan([page], [sid]))
        for bad in ({"title": "Two\nlines"}, {"title": "  "}, {"meta": {"aliases": ["a\nb"]}},
                    {"meta": {"scope": "x\r\ny"}}, {"meta": {"tags": "not-a-list"}}):
            self.rejects("SCHEMA", self.plan([{**page, "body": "## A\n\nx", **bad}], [sid]))
        self.execute(self.plan([{**page, "body": "## A\n\nx"}], [sid]))

    def test_secret_in_nested_fields(self):
        """review 2026-09-23 P1: meta / set / override notes are written too, so they are scanned too."""
        sid = self.ingest_demo()
        page = {"operation_id": "op-001", "type": "create_page", "page_id": "cache", "page_type": "concept",
                "title": "Cache", "body": "## Notes\n\n- x", "source_ids": [sid]}
        for op in ({**page, "meta": {"scope": FAKE_PASSWORD}},
                   {**page, "meta": {"aliases": ["ok", FAKE_AWS_KEY]}},
                   {"operation_id": "op-001", "type": "update_meta", "page_id": "redis",
                    "expected_hash": self.page_hash("redis"), "set": {"tags": [FAKE_AWS_KEY]}, "source_ids": [sid]},
                   self.section_op(sid, "Notes", "- y", human_override={"confirmed_by_user": True,
                                                                        "note": FAKE_API_KEY})):
            err = self.rejects("SECRET_IN_PLAN", self.plan([op], [sid]))
            self.assertNotIn(FAKE_AWS_KEY, str(err))

    def test_rules(self):
        cases = {"private-key": FAKE_PRIVATE_KEY, "github-token": FAKE_GITHUB_TOKEN, "slack-token": FAKE_SLACK_TOKEN,
                 "bearer-token": FAKE_BEARER, "jwt": FAKE_JWT, "connection-string": FAKE_DB_URL,
                 "credential-assignment": FAKE_PASSWORD, "aws-access-key": FAKE_AWS_KEY}
        for rule, text in cases.items():
            self.assertIn(rule, [r for r, _ in sb.scan_secrets(text)], rule)
        for clean in ("the passphrase for the key didn't work", "refresh_token rotation", "Password policy"):
            self.assertEqual(sb.scan_secrets(clean), [], clean)


class TestDeleteAndRetract(ManagedVault):
    def two_source_wiki(self):
        """Source A backs page `redis` and decision `use-redis`; source B also cites `redis`."""
        a = self.ingest_demo()
        self.raw("sessions/2026-09-19-follow-up.md", "# Follow-up\n\nDate: 2026-09-19\n\nRedis confirmed.\n")
        sb.discover(self.vault)
        b = next(s for s in self.sources() if s != a)
        self.apply(self.plan([self.source_op(b), {
            "operation_id": "op-002", "type": "append_to_section", "page_id": "redis",
            "expected_hash": self.page_hash("redis"), "section_path": ["Facts"],
            "content": f"- confirmed（[[{b}|Follow-up]]）", "source_ids": [b]}], [b]))
        self.apply(self.plan([{
            "operation_id": "op-001", "type": "create_page", "page_id": "use-redis", "page_type": "decision",
            "title": "Use Redis", "decision": {"status": "active", "decided_on": "2026-09-18", "date_confidence": "high"},
            "body": f"## Decision\n\nUse [[redis|Redis]]（[[{a}|Demo Meeting]]）\n\n## History\n\n- decided",
            "source_ids": [a]}], [a]))
        return a, b

    def test_source_created_later_in_the_plan_cannot_be_cited_first(self):
        """review 7 P2: a History line must link its source; so the source page comes first in the plan."""
        a, _ = self.two_source_wiki()
        self.raw("meetings/2026-09-20-review.md", "# Review\n\nDate: 2026-09-20\n\nRedis dropped.\n")
        sb.discover(self.vault)
        c = "meeting-2026-09-20-review"
        revoke = {"operation_id": "op-001", "type": "decision_change", "page_id": "use-redis",
                  "expected_hash": self.page_hash("use-redis"), "new_status": "revoked",
                  "history_note": "dropped", "source_ids": [c]}
        create = {**self.source_op(c, "op-002"), "title": "Review"}
        self.rejects("SCHEMA", self.plan([revoke, create], [c]))
        self.apply(self.plan([{**create, "operation_id": "op-001"}, {**revoke, "operation_id": "op-002"}], [c]))
        self.assertIn(f"[[{c}|", self.page_file("use-redis").read_text(encoding="utf-8").split("## History")[1])

    def test_retracting_origins_one_plan_at_a_time(self):
        """review 9 P1: a decision whose origins are retracted in separate plans must still be revoked."""
        self.raw("meetings/2026-09-18-m1.md", "# M1\n\nDate: 2026-09-18\n")
        self.raw("meetings/2026-09-19-m2.md", "# M2\n\nDate: 2026-09-19\n")
        sb.discover(self.vault)
        a, b = "meeting-2026-09-18-m1", "meeting-2026-09-19-m2"
        self.apply(self.plan([{**self.source_op(a), "title": "M1"}, {**self.source_op(b, "op-002"), "title": "M2"}],
                             [a, b]))
        self.apply(self.plan([{
            "operation_id": "op-001", "type": "create_page", "page_id": "d", "page_type": "decision", "title": "D",
            "decision": {"status": "active", "decided_on": "2026-09-19", "date_confidence": "high"},
            "body": f"## Decision\n\nx（[[{a}|M1]]、[[{b}|M2]]）\n\n## History\n\n- decided", "source_ids": [a, b]}],
            [a, b]))

        def retract(sid, keep):
            fix = {"operation_id": "op-001", "type": "update_section", "page_id": "d",
                   "expected_hash": self.page_hash("d"), "section_path": ["Decision"],
                   "content": f"x（[[{keep}|kept]]）" if keep else "x", "source_ids": [sid]}
            ret = {"operation_id": "op-002", "type": "retract_source", "page_id": sid,
                   "expected_hash": self.page_hash(sid), "reason": "mistake", "source_ids": [sid]}
            return self.plan([fix, ret], [sid])

        self.apply(retract(a, b))                        # d still rests on b
        self.assertIn("ONLY origin", sb.cmd_trace(self.vault, b))  # a is already retracted
        self.rejects("DECISION_FROM_RETRACTED", retract(b, None))

    def test_trace_names_exact_links_only(self):
        """review 9 P2: [[<sid>-long]] and a link inside a code span are not links to <sid>."""
        sid = self.ingest_demo()
        (self.vault / "wiki/concepts/other.md").write_text(
            f"---\npage_id: other\npage_type: concept\ntitle: Other\n---\n# Other\n\n"
            f"[[{sid}-long|Other]] `[[{sid}|Code only]]`\n\n## Refs\n\n"  # preamble and a section
            f"[[{sid}-long|Other]]\n`[[{sid}|Code only]]`\n", encoding="utf-8")
        self.assertNotIn("  other  [", sb.cmd_trace(self.vault, sid))

    def test_delete_page_needs_links_removed_first(self):
        a, _ = self.two_source_wiki()
        delete = {"operation_id": "op-002", "type": "delete_page", "page_id": "redis",
                  "expected_hash": self.page_hash("redis"), "reason": "merged elsewhere", "source_ids": []}
        e = self.rejects("BROKEN_LINK", self.plan([dict(delete, operation_id="op-001")], []))
        self.assertIn("use-redis", e.message)
        unlink = {"operation_id": "op-001", "type": "update_section", "page_id": "use-redis",
                  "expected_hash": self.page_hash("use-redis"), "section_path": ["Decision"],
                  "content": f"Use Redis（[[{a}|Demo Meeting]]）", "source_ids": [a]}
        path = self.page_file("redis")
        ex = self.apply(self.plan([unlink, delete], [a]))
        self.assertEqual(ex.deleted, ["redis"])
        self.assertFalse(path.exists())
        self.assertNotIn("redis", self.pages())
        self.assertFalse((self.vault / "state/baselines/redis.md.base").exists())
        self.assertNotIn("[[redis|", (self.vault / "wiki/index.md").read_text(encoding="utf-8"))
        # a deletion is an ordinary transaction: roll it back and the page is back
        sb.rollback(self.vault, ex.txn_id)
        self.assertTrue(path.exists())
        self.assertIn("redis", self.pages())

    def test_delete_forbidden_for_decisions_and_sources(self):
        a, _ = self.two_source_wiki()
        self.rejects("DELETE_FORBIDDEN", self.plan([{
            "operation_id": "op-001", "type": "delete_page", "page_id": "use-redis",
            "expected_hash": self.page_hash("use-redis"), "reason": "x", "source_ids": []}], []))
        self.rejects("DELETE_FORBIDDEN", self.plan([{
            "operation_id": "op-001", "type": "delete_page", "page_id": a,
            "expected_hash": self.page_hash(a), "reason": "x", "source_ids": []}], []))

    def test_delete_human_edited_page_needs_override(self):
        self.ingest_demo()
        f = self.page_file("redis")
        f.write_text(f.read_text(encoding="utf-8").replace("- none", "- human"), encoding="utf-8")
        op = {"operation_id": "op-001", "type": "delete_page", "page_id": "redis",
              "expected_hash": self.page_hash("redis"), "reason": "x", "source_ids": []}
        self.rejects("HUMAN_EDITED_SECTION", self.plan([op], []))

    def test_trace_and_retract_source(self):
        a, b = self.two_source_wiki()
        trace = sb.cmd_trace(self.vault, a)
        self.assertIn("redis", trace)
        self.assertIn("use-redis", trace)
        self.assertIn("ONLY origin: revoke it", trace)
        self.assertIn("links in: Facts", trace)
        retract = {"operation_id": "op-009", "type": "retract_source", "page_id": a,
                   "expected_hash": self.page_hash(a), "reason": "ingested by mistake", "source_ids": [a]}
        # alone: pages still link to / cite the source
        self.rejects("BROKEN_LINK", self.plan([retract], [a]))
        fix_redis = {"operation_id": "op-001", "type": "update_section", "page_id": "redis",
                     "expected_hash": self.page_hash("redis"), "section_path": ["Facts"],
                     "content": f"- confirmed（[[{b}|Follow-up]]）", "source_ids": [a]}
        fix_decision = {"operation_id": "op-002", "type": "update_section", "page_id": "use-redis",
                        "expected_hash": self.page_hash("use-redis"), "section_path": ["Decision"],
                        "content": "Use [[redis|Redis]] (source retracted)", "source_ids": [a]}
        e = self.rejects("DECISION_FROM_RETRACTED", self.plan([fix_redis, fix_decision, retract], [a]))
        self.assertIn("use-redis", e.message)
        revoke = {"operation_id": "op-003", "type": "decision_change", "page_id": "use-redis",
                  "expected_hash": self.page_hash("use-redis"), "new_status": "revoked",
                  "history_note": "its only source was retracted", "source_ids": [a]}
        # the History line the tool writes names the retracted source instead of linking to it
        ex = self.apply(self.plan([fix_redis, fix_decision, revoke, retract], [a]))
        self.assertEqual(ex.retracted, [a])
        self.assertIn(f"{a}（retracted）", self.page_file("use-redis").read_text(encoding="utf-8"))
        self.assertEqual(self.sources()[a]["status"], "retracted")
        self.assertEqual(self.page_file("redis").exists(), True)
        self.assertEqual(sb.Wiki(self.vault).pages["redis"].fm["sources"], [b])
        self.assertNotIn(a, sb.Wiki(self.vault).pages)
        self.assertIn("retracted", sb.cmd_status(self.vault))
        # a retracted source can't be cited or re-ingested, and discover leaves it alone
        self.rejects("SOURCE_NOT_READY", self.plan([self.source_op(a)], [a]))
        self.assertEqual(sb.discover(self.vault), ["no changes"])

    def test_retract_requires_all_citing_pages_updated(self):
        a, b = self.two_source_wiki()
        fix_redis = {"operation_id": "op-001", "type": "update_section", "page_id": "redis",
                     "expected_hash": self.page_hash("redis"), "section_path": ["Facts"],
                     "content": f"- confirmed（[[{b}|Follow-up]]）", "source_ids": [a]}
        retract = {"operation_id": "op-002", "type": "retract_source", "page_id": a,
                   "expected_hash": self.page_hash(a), "reason": "x", "source_ids": [a]}
        e = self.rejects("BROKEN_LINK", self.plan([fix_redis, retract], [a]))
        self.assertIn("use-redis", e.message)


class TestRevert(ManagedVault):
    """maintain --revert: undo what an agent wrote outside a plan (e.g. Codex apply_patch, 2026-09-23 smoke test)."""

    def snapshot(self):
        return {p.relative_to(self.vault).as_posix(): p.read_bytes() for p in (self.vault / "wiki").rglob("*.md")}

    def test_revert_direct_edit_and_undo_the_revert(self):
        self.ingest_demo()
        clean = self.snapshot()
        f = self.page_file("redis")
        f.write_text(f.read_text(encoding="utf-8") + "\nsmoke-test\n", encoding="utf-8")
        edited = f.read_bytes()
        report = sb.maintain(self.vault)
        self.assertTrue(any(l.startswith("HUMAN_EDITED") and "maintain --revert redis" in l for l in report), report)
        out = sb.revert_pages(self.vault, ["redis"])
        self.assertTrue(out[0].startswith("REVERTED    redis"), out)
        self.assertEqual(clean, self.snapshot())
        self.assertFalse(self.pages()["redis"]["human_touched"])  # the reverted edit was not the human's
        self.assertEqual(self.pages()["redis"]["last_txn"], out[-1].split()[1])  # review 6 P2
        self.assertEqual(sb.maintain(self.vault), ["no issues"])
        # the revert is a transaction: rolling it back brings the unauthorized content back for inspection
        txn = out[-1].split()[1]
        sb.rollback(self.vault, txn)
        self.assertEqual(f.read_bytes(), edited)

    def test_unchanged_page_is_a_no_op(self):
        self.ingest_demo()
        self.assertEqual(sb.revert_pages(self.vault, ["redis"]), ["UNCHANGED   redis"])
        self.assertEqual(len(list((self.vault / "state/txn").glob("*.json"))), 1)  # only the ingest txn

    def test_deleted_page_is_restored(self):
        self.ingest_demo()
        clean = self.snapshot()
        self.page_file("redis").unlink()
        sb.revert_pages(self.vault, ["redis"])
        self.assertEqual(clean, self.snapshot())

    def test_renamed_page_is_moved_back(self):
        self.ingest_demo()
        clean = self.snapshot()
        f = self.page_file("redis")
        f.rename(f.with_name("Redis DB.md"))
        sb.revert_pages(self.vault, ["redis"])
        self.assertEqual(clean, self.snapshot())

    def test_unregistered_page_is_removed_and_index_regenerated(self):
        sid = self.ingest_demo()
        clean = self.snapshot()
        rogue = self.vault / "wiki/concepts/rogue.md"
        rogue.write_text("---\npage_id: rogue\npage_type: concept\ntitle: Rogue\n---\n# Rogue\n\nwritten by an agent\n",
                         encoding="utf-8")
        out = sb.revert_pages(self.vault, ["rogue"])
        self.assertTrue(out[0].startswith("REMOVED     rogue"), out)
        self.assertFalse(rogue.exists())
        self.assertEqual(clean, self.snapshot())
        self.assertIn(f"[[{sid}|", (self.vault / "wiki/index.md").read_text(encoding="utf-8"))

    def test_external_and_unknown_pages_are_refused(self):
        self.ingest_demo()
        (self.vault / "wiki/concepts/handmade.md").write_text(
            "---\npage_id: handmade\npage_type: concept\ntitle: Handmade\n---\n# Handmade\n", encoding="utf-8")
        sb.maintain(self.vault, fix=True)
        for pid, code in (("handmade", "REVERT_UNAVAILABLE"), ("nope", "UNKNOWN_PAGE")):
            with self.assertRaises(sb.PlanError) as cm:
                sb.revert_pages(self.vault, [pid])
            self.assertEqual(cm.exception.code, code)

    def test_revert_needs_the_lock(self):
        self.ingest_demo()
        with sb.Lock(self.vault, "other"):
            with self.assertRaises(sb.PlanError) as cm:
                sb.revert_pages(self.vault, ["redis"])
            self.assertEqual(cm.exception.code, "LOCKED")

    def test_edit_on_human_maintained_page_is_still_reported(self):
        self.ingest_demo()
        f = self.page_file("redis")
        f.write_text(f.read_text(encoding="utf-8").replace("- none", "- mine"), encoding="utf-8")
        sb.maintain(self.vault, fix=True)                      # human edit acknowledged → human_touched
        self.assertTrue(any(l.startswith("HUMAN_EDITED") for l in sb.maintain(self.vault)))

    def test_duplicate_page_id_blocks_fix_and_revert_drops_every_copy(self):
        """review 2026-09-23 P1: two files claiming one page_id must never overwrite each other."""
        self.ingest_demo()
        clean = self.snapshot()
        f = self.page_file("redis")
        for name in ("a-redis.md", "z-redis.md"):
            f.with_name(name).write_text(f.read_text(encoding="utf-8").replace("- none", "- other"), encoding="utf-8")
        before = self.snapshot()
        report = sb.maintain(self.vault, fix=True)
        self.assertTrue(any(l.startswith("DUPLICATE_PAGE_ID") for l in report), report)
        self.assertTrue(any(l.startswith("FIX_SKIPPED") for l in report), report)
        self.assertEqual(before, self.snapshot())
        sb.revert_pages(self.vault, ["redis"])
        self.assertEqual(clean, self.snapshot())

    def test_rename_back_never_overwrites_another_page(self):
        sid = self.ingest_demo()
        f = self.page_file("redis")
        text = f.read_text(encoding="utf-8")
        f.write_text(text.replace("page_id: redis", "page_id: cache"), encoding="utf-8")
        (f.parent / "cache.md").write_text("---\npage_id: other\npage_type: entity\ntitle: Other\n---\n# Other\n",
                                           encoding="utf-8")
        report = sb.maintain(self.vault, fix=True)
        self.assertTrue(any("is taken by another page" in l for l in report), report)
        self.assertIn("page_id: other", (f.parent / "other.md").read_text(encoding="utf-8"))  # its own rename
        self.assertIn("page_id: cache", f.read_text(encoding="utf-8"))                     # left in place

    def test_maintain_uses_the_full_page_validator(self):
        """review 3 P2: H1 ≠ title, frontmatter outside the subset, bad page_id → NOT_MANAGEABLE, never registered."""
        self.ingest_demo()
        bad = {"h1": "---\npage_id: h1\npage_type: concept\ntitle: Expected\n---\n# Wrong\n",
               "extra": "---\npage_id: extra\npage_type: concept\ntitle: Extra\nowner: me\n---\n# Extra\n",
               "notitle": "---\npage_id: notitle\npage_type: concept\n---\n# Notitle\n",
               "Bad_ID": "---\npage_id: Bad_ID\npage_type: concept\ntitle: Bad\n---\n# Bad\n"}
        for name, text in bad.items():
            (self.vault / f"wiki/concepts/{name}.md").write_text(text, encoding="utf-8")
        report = sb.maintain(self.vault, fix=True)
        for name in bad:
            self.assertTrue(any(l.startswith("NOT_MANAGEABLE") and f"{name}.md" in l for l in report), (name, report))
            self.assertNotIn(name, self.pages())

    def test_page_validator_checks_value_types(self):
        """review 4 P2: wrong frontmatter types are NOT_MANAGEABLE (no crash, never registered)."""
        self.ingest_demo()
        head = "---\npage_id: {pid}\npage_type: concept\ntitle: T\n{extra}---\n# T\n"
        cases = {"list-type": "---\npage_id: list-type\npage_type: [entity]\ntitle: T\n---\n# T\n",
                 "src": head.format(pid="src", extra="sources: not-a-list\n"),
                 "made": head.format(pid="made", extra="created: false\n"),
                 "upd": head.format(pid="upd", extra="updated: 2026-02-30\n"),
                 "tg": head.format(pid="tg", extra="tags: wrong\n"),
                 "al": head.format(pid="al", extra="aliases: wrong\n"),
                 "dec": head.format(pid="dec", extra="status: active\n"),  # decision key on a concept page
                 # review 5: length, source identity, provenance
                 "x" * 81: head.format(pid="x" * 81, extra=""),
                 "ghost": head.format(pid="ghost", extra="sources: [does-not-exist]\n"),
                 "src-mismatch": "---\npage_id: src-mismatch\npage_type: source\ntitle: S\nsource_id: other\n"
                                 "source_type: meeting\nraw_path: raw/meetings/x.md\nsource_date: unknown\n"
                                 "date_confidence: low\ndate_basis: none\nsynthetic: false\n---\n# S\n",
                 # review 8: date_basis is required; an unterminated single quote is outside the YAML subset
                 "no-basis": "---\npage_id: no-basis\npage_type: source\ntitle: S\nsource_id: no-basis\n"
                             "source_type: meeting\nraw_path: raw/meetings/x.md\nsource_date: unknown\n"
                             "date_confidence: low\nsynthetic: false\n---\n# S\n",
                 "quote": "---\npage_id: quote\npage_type: concept\ntitle: 'Bad Quote\n---\n# 'Bad Quote\n"}
        for pid, text in cases.items():
            folder = "sources" if "page_type: source" in text else "concepts"
            (self.vault / f"wiki/{folder}/{pid}.md").write_text(text, encoding="utf-8")
        report = sb.maintain(self.vault, fix=True)
        for pid in cases:
            self.assertTrue(any(l.startswith("NOT_MANAGEABLE") and f"/{pid}.md" in l for l in report), (pid, report))
            self.assertNotIn(pid, self.pages())

    def test_malformed_plan_values_are_schema_errors(self):
        sid = self.ingest_demo()
        op = self.section_op(sid, "Notes", "- x")
        dec = {"operation_id": "op-001", "type": "create_page", "page_id": "d", "page_type": "decision",
               "title": "D", "body": "## Decision\n\nx", "source_ids": [sid],
               "decision": {"status": "active", "decided_on": "2026-09-18", "date_confidence": "high"}}
        for bad in ({**op, "type": []}, {**op, "type": {}},
                    {**dec, "decision": {**dec["decision"], "date_confidence": ["high"]}},
                    {**dec, "decision": {**dec["decision"], "decided_on": "2026-02-30"}}):
            self.rejects("SCHEMA", self.plan([bad], [sid]))
        # review 5 P2: the source block is checked with the source-page frontmatter rules
        self.raw("documents/n.md", "# N\n")
        sb.discover(self.vault)
        nid = "document-undated-n"
        src = self.source_op(nid)
        for field, value in (("date_basis", {"not": "a string"}), ("date_basis", ""), ("raw_path", ["x"]),
                             ("source_id", 7)):
            self.rejects("SCHEMA", self.plan([{**src, "source": {**src["source"], field: value}}], [nid]))

    def test_new_source_page_is_bound_by_source_versions(self):
        """review 6 P1: a created source page must be a plan-level source (and so version-checked)."""
        self.raw("meetings/2026-09-18-demo-meeting.md", MEETING)
        sb.discover(self.vault)
        sid = next(iter(self.sources()))
        self.rejects("SCHEMA", self.plan([self.source_op(sid)], []))

    def test_only_body_changes_complete_a_reingest(self):
        """review 6 P1: a tag (or a no-op rewrite) on the source page does not re-ingest a changed source."""
        sid = self.ingest_demo()
        f = self.vault / self.sources()[sid]["path"]
        f.write_text(MEETING + "\nNew critical fact.\n", encoding="utf-8")
        sb.discover(self.vault)
        tag = {"operation_id": "op-001", "type": "update_meta", "page_id": sid, "expected_hash": self.page_hash(sid),
               "set": {"tags": ["x"]}, "source_ids": [sid]}
        ex = self.apply(self.plan([tag], [sid]))
        self.assertTrue(any(w.startswith(f"SOURCE STILL CHANGED: {sid}") for w in ex.warnings), ex.warnings)
        self.assertEqual(self.sources()[sid]["status"], "changed")
        # review 7: an edit that only touches another page, or edits that cancel out, do not re-ingest either
        entity = self.section_op(sid, "Notes", "- new fact")
        ex = self.apply(self.plan([entity], [sid]))
        self.assertTrue(any(w.startswith("SOURCE STILL CHANGED") for w in ex.warnings), ex.warnings)
        h = self.page_hash(sid)
        there = {"operation_id": "op-001", "type": "update_section", "page_id": sid, "expected_hash": h,
                 "section_path": ["Summary"], "content": "Temporary.", "source_ids": [sid]}
        back = {**there, "operation_id": "op-002", "content": "Redis chosen."}
        self.apply(self.plan([there, back], [sid]))
        self.assertEqual(self.sources()[sid]["status"], "changed")
        # review 8: the title (and so the H1) is metadata too
        title = {**tag, "expected_hash": self.page_hash(sid), "set": {"title": "Renamed Demo"}}
        ex = self.apply(self.plan([title], [sid]))
        self.assertEqual((ex.ingested, self.sources()[sid]["status"]), ([], "changed"))
        same = {"operation_id": "op-001", "type": "update_section", "page_id": sid,
                "expected_hash": self.page_hash(sid), "section_path": ["Summary"], "content": "Redis chosen.",
                "source_ids": [sid]}
        self.apply(self.plan([same], [sid]))
        self.assertEqual(self.sources()[sid]["status"], "changed")
        real = {**same, "expected_hash": self.page_hash(sid), "content": "Redis chosen.\n\nNew critical fact."}
        self.apply(self.plan([real], [sid]))
        rec = self.sources()[sid]
        self.assertEqual((rec["status"], rec["ingested_hash"]), ("ingested", rec["content_hash"]))

    def test_renamed_file_is_never_overwritten_by_create_page(self):
        """review 7 P1: redis.md renamed to cache.md in Obsidian; create_page(cache) must not land on it."""
        sid = self.ingest_demo()
        f = self.page_file("redis")
        text = f.read_bytes()
        cache = {"operation_id": "op-001", "type": "create_page", "page_id": "cache", "page_type": "entity",
                 "title": "Cache", "body": "## Notes\n\nx", "source_ids": [sid]}
        f.rename(f.with_name("cache.md"))
        self.rejects("SCHEMA", self.plan([cache], [sid]))  # the loader refuses a file name ≠ page_id
        self.assertEqual(f.with_name("cache.md").read_bytes(), text)
        sb.maintain(self.vault, fix=True)                   # renamed back
        # last guard: a file that appears at the target after the wiki was loaded (e.g. created in Obsidian
        # between render and apply) is never overwritten
        wiki = sb.Wiki(self.vault)
        (f.parent / "cache.md").write_text("human notes", encoding="utf-8")
        with self.assertRaises(sb.PlanError) as cm:
            sb.execute(wiki, self.plan([cache], [sid]), False, sb.State(self.vault))
        self.assertEqual(cm.exception.code, "PAGE_EXISTS")

    def test_malformed_identity_never_crashes_maintain(self):
        """review 7 P2: list-valued page_id / source_id are NOT_MANAGEABLE in maintain, SCHEMA for plans."""
        sid = self.ingest_demo()
        p = self.plan([self.section_op(sid, "Notes", "- x")], [sid])
        (self.vault / "wiki/concepts/bad.md").write_text(
            "---\npage_id: [bad]\npage_type: concept\ntitle: B\n---\n# B\n", encoding="utf-8")
        (self.vault / "wiki/sources/src.md").write_text(
            "---\npage_id: src\npage_type: source\ntitle: S\nsource_id: [bad]\nsource_type: meeting\n"
            "raw_path: raw/x.md\nsource_date: unknown\ndate_confidence: low\ndate_basis: none\nsynthetic: false\n"
            "---\n# S\n",
            encoding="utf-8")
        report = sb.maintain(self.vault, fix=True)
        for name in ("concepts/bad.md", "sources/src.md"):
            self.assertTrue(any(l.startswith("NOT_MANAGEABLE") and name in l for l in report), (name, report))
        self.rejects("SCHEMA", p)

    def test_code_spans_of_any_length_hide_links(self):
        """review 7 P2: CommonMark code spans (`` `` ``, ``` ` ```, ...) are not links."""
        for text in ("`[[nope]]`", "``[[nope]]``", "`` a ` [[nope]] ``", "```[[nope]]```"):
            sb.check_links(text, {"redis"}, "op-001")
        for text in ("``[[nope]]`", "[[nope]] `code`"):
            with self.assertRaises(sb.PlanError, msg=text):
                sb.check_links(text, {"redis"}, "op-001")
        # review 8: a fence line with trailing text does not close the block (one scanner for headings and links)
        # review 9: a code span may wrap inside a paragraph, but not across a blank line
        sb.check_links("`code starts\n[[nope]]\ncode ends`", {"redis"}, "op-001")
        for items in ("- `open\n- [[nope]]\n- close`", "1. `open\n2. [[nope]]\n3. close`",  # review 10
                      "`open\n***\n[[nope]]\nclose`", "`open\n===\n[[nope]]\nclose`",       # review 11
                      "`open\n> [[nope]]\nclose`", "`open\n| [[nope]] |\nclose`"):
            with self.assertRaises(sb.PlanError, msg=items):
                sb.check_links(items, {"redis"}, "op-001")
        with self.assertRaises(sb.PlanError):
            sb.check_links("`open\n\n[[nope]]\n`", {"redis"}, "op-001")
        block = "```\n``` not-a-closing-fence\n[[nope]]\n## Not a heading\n```\n[[redis]]"
        sb.check_links(block, {"redis"}, "op-001")
        self.assertEqual(sb.wikilink_targets(block.split("\n")), {"redis"})
        self.assertEqual(sb.scan_headings(block.split("\n")), [])

    def test_frontmatter_codec_round_trips(self):
        """review 9 (standards): one quoted-scalar codec; parse(dump(v)) == v for scalars and list items."""
        import random
        rng = random.Random(7)
        alphabet = list("ab \"'\\,[]{}:#-?|>&*!%@`") + ["中", "é"]
        for _ in range(3000):
            v = "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 8)))
            for value in (v, [v, v + "x"]):
                self.assertEqual(sb.parse_frontmatter("k: " + sb._dump_value(value) + "\n")["k"], value, repr(value))
        self.assertEqual(sb.parse_frontmatter("k: [team's source, 'x''y']\n")["k"], ["team's source", "x'y"])
        for bad in ('k: "abc\\"', 'k: "a" b', "k: 'open", 'k: ["a\\q"]'):
            with self.assertRaises(ValueError, msg=bad):
                sb.parse_frontmatter(bad)

    def test_apply_never_overwrites_an_edit_made_during_the_apply(self):
        """review 9 P1: a human edit between validation and commit rejects the transaction."""
        from unittest import mock
        sid = self.ingest_demo()
        f = self.page_file("redis")
        path = self.write(self.plan([self.section_op(sid, "Notes", "- plan")], [sid]))
        original = sb.Transaction.commit

        def racing(txn, outputs, expected=None):
            f.write_text(f.read_text(encoding="utf-8") + "\nhuman, meanwhile\n", encoding="utf-8")
            return original(txn, outputs, expected)

        with mock.patch.object(sb.Transaction, "commit", racing):
            with self.assertRaises(sb.PlanError) as cm:
                sb.apply(self.vault, path, sb.sha256_bytes(path.read_bytes()))
        self.assertEqual(cm.exception.code, "PLAN_STALE")
        self.assertIn("human, meanwhile", f.read_text(encoding="utf-8"))
        self.assertTrue(path.exists())
        self.assertEqual(sb.State(self.vault).incomplete_txns(), [])

    def test_every_output_and_the_raw_evidence_are_checked_before_commit(self):
        """review 10 P1/S1: index, registries, baselines and the raw evidence (+ companion) are all covered."""
        from unittest import mock
        sid = self.ingest_demo()
        raw = self.vault / self.sources()[sid]["path"]
        edits = {"wiki/index.md": lambda p: p.write_text(p.read_text(encoding="utf-8") + "\nmine\n", encoding="utf-8"),
                 "state/pages.json": lambda p: p.write_text(p.read_text(encoding="utf-8") + " ", encoding="utf-8"),
                 "state/baselines/redis.md.base": lambda p: p.write_text("x", encoding="utf-8"),
                 raw.relative_to(self.vault).as_posix(): lambda p: p.write_text(MEETING + "\nlate fact\n",
                                                                                encoding="utf-8"),
                 raw.relative_to(self.vault).as_posix() + ".txt": lambda p: p.write_text("companion", encoding="utf-8")}
        original = sb.Transaction.commit
        for rel, edit in edits.items():
            path = self.write(self.plan([self.section_op(sid, "Notes", f"- {rel}")], [sid]))
            target = self.vault / rel
            keep = target.read_bytes() if target.exists() else None

            def racing(txn, outputs, before):
                edit(target)
                return original(txn, outputs, before)

            with mock.patch.object(sb.Transaction, "commit", racing):
                with self.assertRaises(sb.PlanError, msg=rel) as cm:
                    sb.apply(self.vault, path, sb.sha256_bytes(path.read_bytes()))
            self.assertEqual(cm.exception.code, "PLAN_STALE", rel)
            self.assertIn(rel, cm.exception.message)
            self.assertEqual(self.sources()[sid]["status"], "ingested")
            if keep is None:
                target.unlink()
            else:
                target.write_bytes(keep)
            path.unlink()
        with self.assertRaises(RuntimeError):
            sb.Fingerprint(self.vault).expected("raw/meetings/x.md")  # outputs never go outside the tool's areas

    def test_edit_inside_the_commit_window_is_never_overwritten(self):
        """review 11 P1: an edit after the pre-check (at txn_begin) or between two file writes aborts the
        transaction and undoes whatever it had already written."""
        from unittest import mock
        sid = self.ingest_demo()
        page, index = self.page_file("redis"), self.vault / "wiki/index.md"
        clean = {p: p.read_bytes() for p in (page, index)}
        original_event, original_write = sb.State.event, sb._atomic_write

        def at_txn_begin(state, event, **kw):
            if event == "txn_begin":
                page.write_text(page.read_text(encoding="utf-8") + "\nObsidian edit\n", encoding="utf-8")
            return original_event(state, event, **kw)

        def between_writes(path, text):
            original_write(path, text)
            if Path(path) == page:  # the page is written first; the index is edited before its turn
                index.write_text("# Index\n\nObsidian edit\n", encoding="utf-8")

        for name, target, patch in (("txn_begin", page, mock.patch.object(sb.State, "event", at_txn_begin)),
                                    ("mid-write", index, mock.patch.object(sb, "_atomic_write", between_writes))):
            path = self.write(self.plan([self.section_op(sid, "Notes", f"- {name}")], [sid]))
            with patch:
                with self.assertRaises(sb.PlanError, msg=name) as cm:
                    sb.apply(self.vault, path, sb.sha256_bytes(path.read_bytes()))
            self.assertEqual(cm.exception.code, "PLAN_STALE", name)
            self.assertIn(b"Obsidian edit", target.read_bytes(), name)          # the human edit survived
            other = index if target == page else page
            self.assertEqual(other.read_bytes(), clean[other], name)            # the commit left nothing behind
            self.assertTrue(path.exists(), name)
            self.assertEqual(sb.State(self.vault).incomplete_txns(), [], name)
            txn = max((self.vault / "state/txn").glob("*.json"), key=lambda p: p.stat().st_mtime)
            self.assertEqual(json.loads(txn.read_text(encoding="utf-8"))["status"], "aborted", name)
            with self.assertRaises(sb.PlanError) as cm:
                sb.rollback(self.vault, txn.stem)
            self.assertEqual(cm.exception.code, "UNKNOWN_TXN")  # aborted: nothing of it to roll back
            for p, data in clean.items():
                p.write_bytes(data)
            path.unlink()
        # a page the plan does not write, edited at txn_begin: only the full re-check of the read set sees it
        source_page = self.page_file(sid)
        keep = source_page.read_bytes()

        def other_page_at_txn_begin(state, event, **kw):
            if event == "txn_begin":
                source_page.write_text(source_page.read_text(encoding="utf-8") + "\n[[redis|Redis]]\n",
                                       encoding="utf-8")
            return original_event(state, event, **kw)

        path = self.write(self.plan([self.section_op(sid, "Notes", "- late")], [sid]))
        with mock.patch.object(sb.State, "event", other_page_at_txn_begin):
            with self.assertRaises(sb.PlanError) as cm:
                sb.apply(self.vault, path, sb.sha256_bytes(path.read_bytes()))
        self.assertEqual(cm.exception.code, "PLAN_STALE")
        self.assertEqual(page.read_bytes(), clean[page])
        source_page.write_bytes(keep)

    def test_read_set_is_rechecked_after_the_write_loop(self):
        """review 12 P1: raw evidence or a page the plan does not write, changed after the first file is
        written, aborts the transaction; everything it wrote is restored and nothing is marked ingested."""
        from unittest import mock
        sid = self.ingest_demo()
        page, raw, source_page = self.page_file("redis"), self.vault / self.sources()[sid]["path"], self.page_file(sid)
        clean = {p: p.read_bytes() for p in (page, raw, source_page, self.vault / "wiki/index.md",
                                            self.vault / "state/sources.json")}
        original_write = sb._atomic_write
        injections = {"raw": lambda: raw.write_text(MEETING + "\nlate fact\n", encoding="utf-8"),
                      "unwritten page": lambda: source_page.write_text(
                          source_page.read_text(encoding="utf-8") + "\n[[nope|Nope]]\n", encoding="utf-8"),
                      "new page": lambda: (self.vault / "wiki/concepts/late.md").write_text(
                          "---\npage_id: late\npage_type: concept\ntitle: Late\n---\n# Late\n", encoding="utf-8")}
        for name, inject in injections.items():
            path = self.write(self.plan([self.section_op(sid, "Notes", f"- {name}")], [sid]))
            fired = []

            def after_first_write(p, text):
                original_write(p, text)
                if Path(p) == page and not fired:
                    fired.append(1)
                    inject()

            with mock.patch.object(sb, "_atomic_write", after_first_write):
                with self.assertRaises(sb.PlanError, msg=name) as cm:
                    sb.apply(self.vault, path, sb.sha256_bytes(path.read_bytes()))
            self.assertEqual(cm.exception.code, "PLAN_STALE", name)
            self.assertEqual(page.read_bytes(), clean[page], name)                      # written output restored
            self.assertEqual(self.sources()[sid],                                       # registry restored too
                             json.loads(clean[self.vault / "state/sources.json"])["sources"][sid], name)
            self.assertTrue(path.exists(), name)
            txn = max((self.vault / "state/txn").glob("*.json"), key=lambda p: p.stat().st_mtime)
            self.assertEqual(json.loads(txn.read_text(encoding="utf-8"))["status"], "aborted", name)
            if name == "raw":
                self.assertIn("late fact", raw.read_text(encoding="utf-8"))            # the raw change is kept
            for p, data in clean.items():
                p.write_bytes(data)
            (self.vault / "wiki/concepts/late.md").unlink(missing_ok=True)
            path.unlink()

    def test_the_whole_wiki_is_the_read_set(self):
        """review 11 P1: validation reads every page (links, index), so a change to a page the plan does not
        write — or a page added — also makes the apply stale."""
        from unittest import mock
        sid = self.ingest_demo()
        source_page = self.page_file(sid)
        edits = (lambda: source_page.write_text(source_page.read_text(encoding="utf-8") + "\n[[redis|Redis]]\n",
                                                encoding="utf-8"),
                 lambda: (self.vault / "wiki/concepts/new.md").write_text(
                     "---\npage_id: new\npage_type: concept\ntitle: New\n---\n# New\n", encoding="utf-8"))
        original = sb.Transaction.commit
        for edit in edits:
            keep = source_page.read_bytes()
            path = self.write(self.plan([self.section_op(sid, "Notes", "- x")], [sid]))

            def racing(txn, outputs, before):
                edit()
                return original(txn, outputs, before)

            with mock.patch.object(sb.Transaction, "commit", racing):
                with self.assertRaises(sb.PlanError) as cm:
                    sb.apply(self.vault, path, sb.sha256_bytes(path.read_bytes()))
            self.assertEqual(cm.exception.code, "PLAN_STALE")
            source_page.write_bytes(keep)
            (self.vault / "wiki/concepts/new.md").unlink(missing_ok=True)
            path.unlink()

    def test_read_only_maintain_takes_no_fingerprint(self):
        """review 11 S1."""
        from unittest import mock
        self.ingest_demo()
        with mock.patch.object(sb, "Fingerprint", side_effect=AssertionError("fingerprint in a read-only run")):
            self.assertEqual(sb.maintain(self.vault), ["no issues"])

    def test_fingerprint_is_taken_before_the_plan_is_read(self):
        """review 10: a plan edited between fingerprint and read must never be archived over (or deleted)."""
        from unittest import mock
        sid = self.ingest_demo()
        path = self.write(self.plan([self.section_op(sid, "Notes", "- approved")], [sid]))
        approved = sb.sha256_bytes(path.read_bytes())
        original = sb.Fingerprint.__init__

        def edit_then_fingerprint(fp, vault, inputs=()):
            path.write_text(path.read_text(encoding="utf-8").replace("approved", "edited"), encoding="utf-8")
            original(fp, vault, inputs)

        with mock.patch.object(sb.Fingerprint, "__init__", edit_then_fingerprint):
            with self.assertRaises(sb.PlanError):
                sb.apply(self.vault, path, approved)
        self.assertIn("edited", path.read_text(encoding="utf-8"))  # the edited plan is still there, unapplied
        self.assertNotIn("- approved", self.page_file("redis").read_text(encoding="utf-8"))

    def test_stale_apply_writes_no_event(self):
        """review 10 P2: a PLAN_STALE apply leaves no human_edit_detected (or any other) event behind."""
        from unittest import mock
        sid = self.ingest_demo()
        f = self.page_file("redis")
        f.write_text(f.read_text(encoding="utf-8").replace("## Notes", "## Mine\n\nhuman\n\n## Notes"),
                     encoding="utf-8")
        path = self.write(self.plan([self.section_op(sid, "Notes", "- x")], [sid]))
        events = self.vault / "state/events.jsonl"
        before = events.read_bytes()
        original = sb.Transaction.commit

        def racing(txn, outputs, fp):
            (self.vault / "wiki/index.md").write_text("# Index\n\nmine\n", encoding="utf-8")
            return original(txn, outputs, fp)

        with mock.patch.object(sb.Transaction, "commit", racing):
            with self.assertRaises(sb.PlanError):
                sb.apply(self.vault, path, sb.sha256_bytes(path.read_bytes()))
        self.assertEqual(events.read_bytes(), before)

    def test_fix_never_writes_a_page_it_cannot_read_back(self):
        """review 9 P1: maintain --fix re-parses every page it would write (last guard for the codec)."""
        from unittest import mock
        f = self.raw("meetings/2026-01-01-note.md", "# Note\n\nbody\n")
        sb.discover(self.vault)
        sid = next(iter(self.sources()))
        self.apply(self.plan([self.source_op(sid)], [sid]))
        page = self.page_file(sid)
        page.write_text(page.read_text(encoding="utf-8").replace("aliases: []", "aliases: ['team''s source']"),
                        encoding="utf-8")
        f.rename(f.with_name("2026-01-02-note.md"))
        sb.discover(self.vault)
        before = page.read_bytes()
        real_dump = sb._dump_str
        broken = lambda value, in_list=False: '"' + value if in_list else real_dump(value)  # a codec bug
        with mock.patch.object(sb, "_dump_str", broken):
            with self.assertRaises(sb.PlanError):
                sb.maintain(self.vault, fix=True)
        self.assertEqual(page.read_bytes(), before)
        sb.maintain(self.vault, fix=True)  # with the real codec the sync works and keeps the alias
        self.assertEqual(sb.Wiki(self.vault).pages[sid].fm["aliases"], ["team's source"])

    def test_unaddressable_headings_are_rejected(self):
        """review 9 P2: `## __preamble__` and empty headings cannot be named by a section_path."""
        sid = self.ingest_demo()
        page = {"operation_id": "op-001", "type": "create_page", "page_id": "cache", "page_type": "concept",
                "title": "Cache", "source_ids": [sid]}
        for body in ("## __preamble__\n\nx", "## A\n\n## \n\nx"):
            self.rejects("HEADING_LEVEL", self.plan([{**page, "body": body}], [sid]))

    def test_trailing_spaces_on_the_last_line_are_content(self):
        """review 6 P2 (schema §1.4): only trailing blank lines are dropped."""
        self.assertNotEqual(sb.sha256_text("final  "), sb.sha256_text("final"))
        self.assertEqual(sb.sha256_text("final  \n\n  \n"), sb.sha256_text("final  "))
        self.assertEqual(sb.sha256_text("a\r\nb\r\n"), sb.sha256_text("a\nb"))

    def test_malformed_identity_is_a_schema_error_not_a_crash(self):
        """review 6 P2: the Wiki loader reports bad page files as SCHEMA (validate/render show no traceback)."""
        sid = self.ingest_demo()
        p = self.plan([self.section_op(sid, "Notes", "- x")], [sid])
        for text in ("---\npage_id: [bad]\npage_type: concept\ntitle: B\n---\n# B\n",
                     "---\npage_id: bad\npage_type: concept\ntitle: {x}\n---\n# B\n",
                     "---\npage_id: Bad Id\npage_type: concept\ntitle: B\n---\n# B\n"):
            (self.vault / "wiki/concepts/bad.md").write_text(text, encoding="utf-8")
            self.rejects("SCHEMA", p)

    def test_plan_summary_is_scanned_for_secrets(self):
        """review 5 P2: the whole plan file is kept in plans/applied/, so all of it is scanned."""
        sid = self.ingest_demo()
        p = {**self.plan([self.section_op(sid, "Notes", "- x")], [sid]), "summary": FAKE_PASSWORD}
        err = self.rejects("SECRET_IN_PLAN", p)
        self.assertIn("summary", str(err))

    def test_clean_fix_writes_no_transaction(self):
        """review 4 P3."""
        self.ingest_demo()
        txns = len(list((self.vault / "state/txn").glob("*.json")))
        self.assertEqual(sb.maintain(self.vault, fix=True), ["no issues"])
        self.assertEqual(len(list((self.vault / "state/txn").glob("*.json"))), txns)

    def test_hash_reports_human_and_baseline_state(self):
        """review 3 P3 (stage1-design §10)."""
        self.ingest_demo()
        out = sb.cmd_hash(self.vault, "redis", False)
        self.assertIn("human_touched: false", out)
        self.assertIn("baseline: same", out)
        f = self.page_file("redis")
        f.write_text(f.read_text(encoding="utf-8").replace("- none", "- mine"), encoding="utf-8")
        self.assertIn("baseline: DIFFERS", sb.cmd_hash(self.vault, "redis", False))
        sb.maintain(self.vault, fix=True)
        self.assertIn("human_touched: true", sb.cmd_hash(self.vault, "redis", False))
        (self.vault / "wiki/concepts/handmade.md").write_text(
            "---\npage_id: handmade\npage_type: concept\ntitle: Handmade\n---\n# Handmade\n", encoding="utf-8")
        self.assertIn("baseline: unregistered", sb.cmd_hash(self.vault, "handmade", False))
        sb.maintain(self.vault, fix=True)
        self.assertIn("baseline: none", sb.cmd_hash(self.vault, "handmade", False))

    def test_index_and_log_edits_are_detected_and_reverted(self):
        self.ingest_demo()
        clean = self.snapshot()
        log = self.vault / "wiki/log.md"
        (self.vault / "wiki/index.md").write_text("# Index\n\nwiped\n", encoding="utf-8")
        log.write_text(log.read_text(encoding="utf-8") + "\n## forged entry\n", encoding="utf-8")
        report = sb.maintain(self.vault)
        self.assertTrue(any(l.startswith("INDEX_OUT_OF_DATE") for l in report), report)
        self.assertTrue(any(l.startswith("LOG_EDITED") for l in report), report)
        out = sb.revert_pages(self.vault, ["index", "log"])
        self.assertEqual(clean, self.snapshot())
        self.assertEqual(sb.maintain(self.vault), ["no issues"])
        sb.rollback(self.vault, out[-1].split()[1])
        self.assertIn("forged entry", log.read_text(encoding="utf-8"))

    def test_unchanged_index_and_log_are_a_no_op(self):
        self.ingest_demo()
        self.assertEqual(sb.revert_pages(self.vault, ["index", "log", "redis"]),
                         ["UNCHANGED   log", "UNCHANGED   redis", "UNCHANGED   index"])
        self.assertEqual(len(list((self.vault / "state/txn").glob("*.json"))), 1)


class TestCommands(ManagedVault):
    def test_status_source_and_reject(self):
        sid = self.ingest_demo()
        out = sb.cmd_status(self.vault)
        self.assertIn("ingested", out)
        self.assertIn("lock: free", out)
        self.assertIn("content_hash: sha256:", sb.cmd_source(self.vault, sid))
        path = self.write(self.plan([self.section_op(sid, "Notes", "- x")], [sid]))
        dest = sb.reject_plan(self.vault, path, "review: wrong section")
        self.assertTrue(dest.exists() and dest.parent.name == "rejected")
        events = (self.vault / "state/events.jsonl").read_text(encoding="utf-8")
        self.assertIn("plan_rejected", events)

    def test_reject_takes_the_write_lock(self):
        """review 2026-09-23 P2: reject must not race an apply of the same plan."""
        sid = self.ingest_demo()
        path = self.write(self.plan([self.section_op(sid, "Notes", "- x")], [sid]))
        with sb.Lock(self.vault, "apply-plan"):
            with self.assertRaises(sb.PlanError) as cm:
                sb.reject_plan(self.vault, path, "r")
            self.assertEqual(cm.exception.code, "LOCKED")
        self.assertTrue(path.exists())
        sb.apply(self.vault, path, sb.sha256_bytes(path.read_bytes()))
        with self.assertRaises(sb.PlanError):
            sb.reject_plan(self.vault, path, "too late")
        with self.assertRaises(sb.PlanError):
            sb.reject_plan(self.vault, self.vault / "plans/pending/nope.json", "missing")

    def test_only_pending_plans_run_and_history_is_write_once(self):
        """review 2 P1: rejected plans cannot be applied; applied/ is never overwritten; archiving is transactional."""
        sid = self.ingest_demo()
        p = self.plan([self.section_op(sid, "Notes", "- x")], [sid])
        rejected = sb.reject_plan(self.vault, self.write(p), "no")
        with self.assertRaises(sb.PlanError) as cm:
            sb.apply(self.vault, rejected, sb.sha256_bytes(rejected.read_bytes()))
        self.assertEqual(cm.exception.code, "PLAN_NOT_PENDING")
        # the file name must be the plan_id
        p2 = self.plan([self.section_op(sid, "Notes", "- y")], [sid])
        odd = self.vault / "plans/pending/other.json"
        odd.write_text(json.dumps(p2), encoding="utf-8")
        with self.assertRaises(sb.PlanError):
            sb.apply(self.vault, odd, sb.sha256_bytes(odd.read_bytes()))
        odd.unlink()
        # applied history is write-once
        ex = self.apply(p2)
        applied = self.vault / f"plans/applied/{p2['plan_id']}.json"
        record = applied.read_bytes()
        again = self.write({**p2, "summary": "same id, other content"})
        with self.assertRaises(sb.PlanError) as cm:
            sb.apply(self.vault, again, sb.sha256_bytes(again.read_bytes()))
        self.assertEqual(cm.exception.code, "PLAN_EXISTS")
        self.assertEqual(applied.read_bytes(), record)
        again.unlink()
        # rolling the apply back puts the plan back in pending
        sb.rollback(self.vault, ex.txn_id)
        self.assertFalse(applied.exists())
        self.assertEqual((self.vault / f"plans/pending/{p2['plan_id']}.json").read_bytes(), record)

    def test_reject_takes_only_plan_files(self):
        """review 8 P2: rejected/ holds plans only (plans/pending/<plan_id>.json), like applied/."""
        self.ingest_demo()
        junk = self.vault / "plans/pending/not-a-plan.txt"
        junk.write_text("not json", encoding="utf-8")
        odd = self.vault / "plans/pending/other.json"
        odd.write_text(json.dumps({"plan_id": "plan-20260924-abcd"}), encoding="utf-8")
        named = self.vault / "plans/pending/plan-20260924-zzzz.json"  # a plan's name, but not a plan
        named.write_text("not json", encoding="utf-8")
        for path in (junk, odd, named):
            with self.assertRaises(sb.PlanError, msg=path.name):
                sb.reject_plan(self.vault, path, "junk")
            self.assertTrue(path.exists())

    def test_init_twice_fails(self):
        with self.assertRaises(sb.PlanError):
            sb.init_vault(self.vault)


class TestKit(unittest.TestCase):
    """install / upgrade: the project is the single source of every vault's rule files and tools."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.project, self.vault = root / "project", root / "vault"
        shutil.copytree(REPO / "vault-template", self.project / "vault-template")
        (self.project / "tools").mkdir()
        for name in sb.KIT_TOOLS:
            shutil.copy(REPO / "tools" / name, self.project / "tools" / name)

    def tearDown(self):
        self.tmp.cleanup()

    def kit_file(self, rel):
        return self.project / ("tools" if rel.startswith("tools/") else "vault-template") / rel.split("tools/")[-1]

    def test_install_creates_a_complete_vault(self):
        sb.install_vault(self.vault, self.project)
        for rel in sb.kit_files(self.project):
            self.assertEqual((self.vault / rel).read_bytes(), self.kit_file(rel).read_bytes(), rel)
        self.assertTrue(sb.State(self.vault).managed)
        self.assertEqual(sb.upgrade_vault(self.vault, project=self.project), ["up to date"])
        with self.assertRaises(sb.PlanError):
            sb.install_vault(self.vault, self.project)  # already a vault

    def test_install_never_overwrites(self):
        self.vault.mkdir()
        (self.vault / "AGENTS.md").write_text("my own rules", encoding="utf-8")
        with self.assertRaises(sb.PlanError):
            sb.install_vault(self.vault, self.project)
        self.assertEqual((self.vault / "AGENTS.md").read_text(encoding="utf-8"), "my own rules")
        self.assertFalse((self.vault / "tools").exists())  # nothing written

    def test_upgrade_updates_adds_removes_and_leaves_data_alone(self):
        sb.install_vault(self.vault, self.project)
        (self.vault / "raw/meetings/m.md").write_text("# M\n", encoding="utf-8")
        data = {p: p.read_bytes() for d in ("raw", "wiki", "plans") for p in (self.vault / d).rglob("*") if p.is_file()}
        (self.project / "vault-template/AGENTS.md").write_text("# new rules\n", encoding="utf-8")
        (self.project / "vault-template/NEW.md").write_text("new\n", encoding="utf-8")
        (self.project / "vault-template/CLAUDE.md").unlink()
        dry = sb.upgrade_vault(self.vault, dry_run=True, project=self.project)
        self.assertEqual(sorted(l.split()[0] for l in dry if not l.startswith("(")), ["ADD", "REMOVE", "UPDATE"])
        self.assertTrue((self.vault / "CLAUDE.md").exists())  # dry run wrote nothing
        sb.upgrade_vault(self.vault, project=self.project)
        self.assertEqual((self.vault / "AGENTS.md").read_text(encoding="utf-8"), "# new rules\n")
        self.assertTrue((self.vault / "NEW.md").exists())
        self.assertFalse((self.vault / "CLAUDE.md").exists())
        self.assertEqual(data, {p: p.read_bytes() for d in ("raw", "wiki", "plans")
                                for p in (self.vault / d).rglob("*") if p.is_file()})
        self.assertEqual(sb.upgrade_vault(self.vault, project=self.project), ["up to date"])
        # the record follows every upgrade: a second upstream change is a plain UPDATE, not a conflict
        (self.project / "vault-template/AGENTS.md").write_text("# newer rules\n", encoding="utf-8")
        self.assertEqual(sb.upgrade_vault(self.vault, project=self.project), ["UPDATE   AGENTS.md"])

    def test_a_dropped_file_changed_in_the_vault_is_kept(self):
        sb.install_vault(self.vault, self.project)
        (self.vault / "CLAUDE.md").write_text("my notes\n", encoding="utf-8")
        (self.project / "vault-template/CLAUDE.md").unlink()
        sb.upgrade_vault(self.vault, force=True, project=self.project)
        self.assertEqual((self.vault / "CLAUDE.md").read_text(encoding="utf-8"), "my notes\n")

    def test_a_change_made_in_the_vault_is_a_conflict(self):
        sb.install_vault(self.vault, self.project)
        (self.vault / "INGEST.md").write_text("edited in the vault\n", encoding="utf-8")
        (self.project / "vault-template/INGEST.md").write_text("new upstream\n", encoding="utf-8")
        (self.project / "vault-template/AGENTS.md").write_text("# new rules\n", encoding="utf-8")
        with self.assertRaises(sb.PlanError) as cm:
            sb.upgrade_vault(self.vault, project=self.project)
        self.assertEqual(cm.exception.code, "KIT_CONFLICT")
        self.assertEqual((self.vault / "INGEST.md").read_text(encoding="utf-8"), "edited in the vault\n")
        self.assertNotEqual((self.vault / "AGENTS.md").read_text(encoding="utf-8"), "# new rules\n")  # all or nothing
        sb.upgrade_vault(self.vault, force=True, project=self.project)
        self.assertEqual((self.vault / "INGEST.md").read_text(encoding="utf-8"), "new upstream\n")

    def test_must_run_from_the_project(self):
        sb.install_vault(self.vault, self.project)
        with self.assertRaises(sb.PlanError):
            sb.upgrade_vault(self.vault, project=self.vault)  # the vault's own copy has no vault-template/


if __name__ == "__main__":
    unittest.main()
