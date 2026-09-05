"""Segmenter tests: numbered, unnumbered, bullets, a table row, a TOC page.

Inline strings only. Nothing here touches the network, and no fixture on disk
is read or written.
"""
from __future__ import annotations

import importlib.util
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "examples" / "policy-reader"))

import segment as seg  # noqa: E402


def _load_ingest():
    spec = importlib.util.spec_from_file_location("ingest_mod", ROOT / "scripts" / "ingest.py")
    mod = importlib.util.module_from_spec(spec)
    # dataclasses resolve annotations via sys.modules[cls.__module__] on 3.9,
    # so the module must be registered before it is executed.
    sys.modules["ingest_mod"] = mod
    spec.loader.exec_module(mod)
    return mod


ingest = _load_ingest()


class Opts:
    def __init__(self, min_clause_chars=40, max_clause_chars=600, id_prefix="sec"):
        self.min_clause_chars = min_clause_chars
        self.max_clause_chars = max_clause_chars
        self.id_prefix = id_prefix


def run(text: str, opts: Opts = None, markdown: bool = True) -> list[dict]:
    opts = opts or Opts()
    blocks = ingest.blocks_from_lines(text.strip("\n").splitlines(), markdown=markdown)
    raws = ingest.segment(blocks, opts)
    raws = ingest.merge_and_split(raws, opts)
    return ingest.to_records(raws, opts)


NUMBERED = """
Section 1 Definitions
(a) The first subsection introduces terms that matter later in this agreement document.
(i) Bodily injury means harm, sickness, or disease, including the care that it requires.
(ii) Property damage means physical injury to or destruction of tangible property here.
Section 2 Coverage
(a) We cover direct physical loss to the property except where this agreement excludes it.
"""

UNNUMBERED = """
# Overview
This scheme provides a weekly payment to people who care for someone with a disability.
# Eligibility
You may qualify if you are over sixteen and provide at least thirty-five hours of care.

Your earnings after tax and expenses must not exceed the weekly limit set for the year.
"""

BULLETED = """
# Expenses
Expenses can include:
- half your pension contributions
- equipment you need
- travel costs between different workplaces that your employer does not reimburse you for
"""

TOC = """
Contents
Introduction 1
Definitions 2
Coverage A 3
Coverage B 4
Exclusions 5
Conditions 6
Endorsements 7
Schedules 8
Appendix A 9
Real body text begins here and is long enough to become a genuine clause on its own.
"""


class TestNumbered(unittest.TestCase):
    def test_hierarchy_and_ids(self):
        recs = run(NUMBERED)
        ids = [r["id"] for r in recs]
        self.assertIn("sec-1a-i", ids)
        self.assertIn("sec-1a-ii", ids)
        self.assertIn("sec-2a-i", ids)

    def test_section_title_from_heading_not_a_clause(self):
        recs = run(NUMBERED)
        self.assertEqual(recs[0]["section_title"], "Definitions")
        # "Section 1 Definitions" is a heading; it must not become clause text.
        self.assertFalse(any(r["text_display"].strip() == "Definitions" for r in recs))

    def test_path_is_recorded(self):
        recs = run(NUMBERED)
        by_id = {r["id"]: r for r in recs}
        self.assertEqual(by_id["sec-1a-ii"]["path"], "1(a)(ii)")
        self.assertEqual(by_id["sec-1a-ii"]["subsection"], "a")
        self.assertEqual(by_id["sec-1a-ii"]["item"], "ii")


class TestUnnumbered(unittest.TestCase):
    def test_headings_become_sections_and_ids_are_p_numbered(self):
        recs = run(UNNUMBERED)
        ids = [r["id"] for r in recs]
        self.assertEqual(ids[0], "sec-1-p1")
        self.assertIn("sec-2-p1", ids)
        self.assertIn("sec-2-p2", ids)

    def test_section_title_is_the_heading(self):
        recs = run(UNNUMBERED)
        self.assertEqual(recs[0]["section_title"], "Overview")
        self.assertEqual([r for r in recs if r["id"] == "sec-2-p1"][0]["section_title"], "Eligibility")

    def test_unnumbered_records_carry_no_path(self):
        for r in run(UNNUMBERED):
            self.assertNotIn("path", r)


class TestBullets(unittest.TestCase):
    def test_short_bullet_borrows_introducer_exactly_once(self):
        recs = run(BULLETED, Opts(min_clause_chars=1))
        texts = [r["text_display"] for r in recs]
        short = [t for t in texts if "half your pension" in t]
        self.assertEqual(len(short), 1)
        self.assertTrue(short[0].startswith("Expenses can include:"))
        # the introducer must not survive as its own clause as well
        self.assertNotIn("Expenses can include:", texts)
        # and it must appear only once inside the borrowed bullet
        self.assertEqual(short[0].count("Expenses can include:"), 1)

    def test_long_bullet_stands_alone(self):
        recs = run(BULLETED, Opts(min_clause_chars=1))
        longs = [t for t in (r["text_display"] for r in recs) if "travel costs" in t]
        self.assertEqual(len(longs), 1)
        self.assertFalse(longs[0].startswith("Expenses can include:"))


class TestTables(unittest.TestCase):
    def test_row_sentence_shape(self):
        self.assertEqual(ingest.row_sentence(["Band", "Rate"], ["A", "10%"]),
                         "Row: Band: A; Rate: 10%.")

    def test_missing_header_falls_back_to_column_n(self):
        self.assertEqual(ingest.row_sentence([], ["A", "B"]), "Row: column 1: A; column 2: B.")

    def test_table_row_kind_is_flagged(self):
        opts = Opts(min_clause_chars=1)
        blocks = [seg.Block("heading", "Rates", 2),
                  seg.Block("table_row", "Row: Band: A; Rate: ten percent.")]
        raws = ingest.merge_and_split(ingest.segment(blocks, opts), opts)
        recs = ingest.to_records(raws, opts)
        self.assertEqual(recs[0]["kind"], "table_row")


class TestCleanupPasses(unittest.TestCase):
    def test_toc_run_is_stripped(self):
        recs = run(TOC)
        texts = " ".join(r["text_display"] for r in recs)
        self.assertIn("Real body text begins here", texts)
        for gone in ("Introduction 1", "Definitions 2", "Appendix A 9"):
            self.assertNotIn(gone, texts)

    def test_short_toc_run_is_kept(self):
        lines = ["Alpha 1", "Beta 2", "Gamma 3"]
        self.assertEqual(seg.strip_toc(lines), lines)

    def test_hyphenated_linebreaks_rejoin(self):
        self.assertEqual(seg.join_hyphenated("compen-\nsation"), "compensation")

    def test_repeated_headers_dropped(self):
        pages = ["ACME LTD\nbody one", "ACME LTD\nbody two", "ACME LTD\nbody three"]
        out = seg.drop_repeated_lines(pages)
        self.assertTrue(all("ACME LTD" not in p for p in out))
        self.assertIn("body two", out[1])

    def test_page_numbers_dropped(self):
        self.assertEqual(seg.drop_page_numbers(["- 12 -", "Page 3 of 9", "real text"]), ["real text"])


class TestMergeAndSplit(unittest.TestCase):
    def test_runt_clause_merges_forward(self):
        text = "# H\nShort.\nThis following paragraph is comfortably longer than the minimum clause length."
        recs = run(text)
        self.assertEqual(len(recs), 1)
        self.assertTrue(recs[0]["text_display"].startswith("Short."))

    def test_long_clause_splits_at_sentence_boundary_with_suffixes(self):
        sent = "This sentence is used to pad the clause out beyond the configured maximum length. "
        recs = run("# H\n" + (sent * 6).strip(), Opts(max_clause_chars=200))
        self.assertGreater(len(recs), 1)
        self.assertTrue(recs[0]["id"].endswith("-a"))
        self.assertTrue(recs[1]["id"].endswith("-b"))
        for r in recs:
            self.assertTrue(r["text_display"].endswith("."))

    def test_duplicate_ids_are_suffixed(self):
        opts = Opts(min_clause_chars=1)
        blocks = [seg.Block("heading", "H", 2),
                  seg.Block("para", "Alpha text here."), seg.Block("para", "Beta text here.")]
        raws = ingest.merge_and_split(ingest.segment(blocks, opts), opts)
        for r in raws:
            r.id_parts = ["1", "p1"]                 # force a collision
        recs = ingest.to_records(raws, opts)
        self.assertEqual([r["id"] for r in recs], ["sec-1-p1", "sec-1-p1-2"])


class TestSchemaAndValidator(unittest.TestCase):
    REQUIRED = ("id", "index", "section", "section_title", "subsection", "item",
                "text_display", "text_spoken", "sentences", "spoken_map")

    def test_records_match_policy_json_schema(self):
        for recs in (run(NUMBERED), run(UNNUMBERED)):
            for r in recs:
                for k in self.REQUIRED:
                    self.assertIn(k, r)
                self.assertNotIn("text", r)

    def test_validator_accepts_generated_records(self):
        for src in (NUMBERED, UNNUMBERED, TOC):
            ingest.validate(run(src), Opts(), min_clauses=1)

    def test_validator_rejects_broken_span_map(self):
        recs = run(UNNUMBERED)
        recs[0]["spoken_map"] = [[0, 1, "x"]]
        with self.assertRaises(SystemExit):
            ingest.validate(recs, Opts(), min_clauses=1)

    def test_validator_rejects_non_contiguous_index(self):
        recs = run(UNNUMBERED)
        recs[0]["index"] = 7
        with self.assertRaises(SystemExit):
            ingest.validate(recs, Opts(), min_clauses=1)

    def test_sentences_cover_text_display(self):
        for r in run(UNNUMBERED):
            self.assertEqual(r["sentences"][0][0], 0)
            self.assertEqual(r["sentences"][-1][1], len(r["text_display"]))


class TestPIIScan(unittest.TestCase):
    def test_detects_email_phone_and_address(self):
        hits = ingest.scan_pii("Write to jo@example.com or call 555-123-4567, "
                               "at 12 Rosemount Street.")
        labels = {label for label, _ in hits}
        self.assertIn("email", labels)
        self.assertIn("phone", labels)
        self.assertIn("street_address", labels)

    def test_detects_name_beside_account_number(self):
        hits = ingest.scan_pii("Policyholder Jane Marchetti, policy 4820193774, is insured.")
        self.assertIn("name_with_account_number", {label for label, _ in hits})

    def test_clean_synthetic_text_passes(self):
        self.assertEqual(ingest.scan_pii("Coverage A is provided with a limit of $350,000."), [])

    def test_redaction_keeps_only_four_chars(self):
        self.assertEqual(ingest.redact("jo@example.com"), "jo@e…")


class TestSentenceSpansCopyIsIdentical(unittest.TestCase):
    """segment.sentence_spans is a deliberate copy of the one in build_fixture.py
    (which is frozen). If either drifts, fixtures stop being comparable."""

    @staticmethod
    def _extract(path: Path) -> str:
        src = path.read_text(encoding="utf-8")
        m = re.search(r"_SENT_END = re\.compile\((.*?)\)\n", src, re.S)
        body = re.search(r"def sentence_spans\(text: str\).*?\n    return spans\n", src, re.S)
        assert m and body, f"could not find sentence_spans in {path}"
        return m.group(1).strip() + "\n" + body.group(0)

    def test_copies_match(self):
        a = self._extract(ROOT / "examples" / "policy-reader" / "segment.py")
        b = self._extract(ROOT / "examples" / "policy-reader" / "fixtures" / "build_fixture.py")
        self.assertEqual(a, b)


if __name__ == "__main__":
    unittest.main()
