"""The Docling structure pass, on the smallest source document.

Skipped when docling is not installed: ingestion is build-time only and runs
on the developer's machine, never in CI. With docling present this converts
fixtures/source/fixture_home_loan_mitc.docx (the smallest of the five) and
checks the invariants the reader depends on, then runs the whole ingest twice
and compares bytes.
"""
from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "examples" / "policy-reader"))

SOURCE = ROOT / "examples" / "policy-reader" / "fixtures" / "source"
SMALLEST = SOURCE / "fixture_home_loan_mitc.docx"
NO_REGISTER = ("--no-register",)
HAVE_DOCLING = importlib.util.find_spec("docling") is not None
_SENTENCE = re.compile(r"[^.!?]{80,}\.\s*$")


def _ingest(src: Path, out: Path, extra=()) -> subprocess.CompletedProcess:
    cmd = [sys.executable, str(ROOT / "scripts" / "ingest.py"), str(src), "--source", "pdf",
           "--out", str(out), "--name", out.stem, *extra]
    return subprocess.run(cmd, capture_output=True, text=True, cwd=str(ROOT), timeout=900)


@unittest.skipUnless(HAVE_DOCLING and SMALLEST.exists(),
                     "docling not installed or no source document (build-time only)")
class TestStructurePass(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        out = Path(cls.tmp.name) / "mitc.json"
        r = _ingest(SMALLEST, out, NO_REGISTER)
        if r.returncode != 0:
            raise RuntimeError(f"ingest failed:\n{r.stderr[-2000:]}")
        cls.out = out
        cls.stderr = r.stderr
        cls.doc = json.loads(out.read_text(encoding="utf-8"))
        cls.clauses = cls.doc["clauses"]

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def kinds(self, k):
        return [c for c in self.clauses if c.get("kind", "body") == k]

    def test_every_clause_has_a_kind_from_the_taxonomy(self):
        allowed = {"heading", "body", "definition", "table_stub", "table_row", "boilerplate"}
        for c in self.clauses:
            self.assertIn(c.get("kind", "body"), allowed, c["id"])

    def test_no_boilerplate_clause_is_a_long_sentence(self):
        # Guards against body text being demoted: page furniture is short and
        # does not read as an 80+ char sentence ending in a full stop.
        for c in self.kinds("boilerplate"):
            self.assertIsNone(_SENTENCE.search(c["text_display"]),
                              f"{c['id']} looks like body text: {c['text_display'][:100]!r}")

    def test_every_heading_has_at_least_one_child(self):
        heads = self.kinds("heading")
        self.assertTrue(heads)
        for h in heads:
            self.assertGreaterEqual(h.get("children", 0), 1, f"{h['id']} {h['text_display']!r}")
            self.assertRegex(h["text_display"], r"\. \d+ items?\.$|\.$")

    def test_every_table_row_has_a_parent_stub(self):
        stubs = {c["id"] for c in self.kinds("table_stub")}
        for r in self.kinds("table_row"):
            self.assertIn(r.get("parent"), stubs, r["id"])
            self.assertTrue(r.get("spoken_on_request"), r["id"])

    def test_ids_unique_and_index_monotonic_in_reading_order(self):
        ids = [c["id"] for c in self.clauses]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual([c["index"] for c in self.clauses], list(range(len(self.clauses))))
        pages = [c.get("page", 0) for c in self.clauses if c.get("page")]
        self.assertEqual(pages, sorted(pages), "reading order follows page order")

    def test_hard_length_limit_and_splits_are_logged(self):
        for c in self.clauses:
            self.assertLessEqual(len(c["text_display"]), 1000, c["id"])
        n_split = sum(1 for c in self.clauses if re.search(r"-s\d+$", c["id"]))
        if n_split:
            self.assertIn("split ", self.stderr)

    def test_map_and_terms_blocks(self):
        self.assertIsInstance(self.doc["map"], list)
        self.assertTrue(self.doc["map"])
        for m in self.doc["map"]:
            self.assertIn(m["id"], {c["id"] for c in self.clauses})
        self.assertIsInstance(self.doc["terms"], dict)

    def test_report_is_written_next_to_the_fixture_and_the_scan_is_informational(self):
        rep = json.loads((self.out.parent / self.doc["report_file"]).read_text(encoding="utf-8"))
        self.assertEqual(rep["doc_id"], self.doc["doc_id"])
        self.assertEqual(rep["stages"], ["extract", "structure", "segment", "normalize", "pii_scan", "validate", "write"])
        # The named officer's work address is reported with its clause, not refused.
        self.assertTrue(any(h["label"] == "email" and h["clause_id"] for h in rep["pii_scan"]["personal"]))
        self.assertTrue(rep["validate"]["oversized_ok"])
        self.assertTrue(rep["readable"])
        self.assertNotIn("fetched_at", self.doc["source"], "a local file has no fetch time")

    def test_docling_json_is_written_and_named_in_the_fixture(self):
        djson = self.out.parent / self.doc["structure"]["docling_json"]
        self.assertTrue(djson.exists())
        self.assertEqual(self.doc["structure"]["tool"], "docling")

    def test_ingest_is_idempotent(self):
        out2 = Path(self.tmp.name) / "again" / "mitc.json"
        out2.parent.mkdir()
        r = _ingest(SMALLEST, out2, NO_REGISTER)
        self.assertEqual(r.returncode, 0, r.stderr[-1500:])
        self.assertEqual(out2.read_bytes(), self.out.read_bytes(), "second run must be byte-identical")
        d1 = (self.out.parent / self.doc["structure"]["docling_json"]).read_bytes()
        d2 = (out2.parent / self.doc["structure"]["docling_json"]).read_bytes()
        self.assertEqual(d1, d2, "the DoclingDocument JSON must be byte-identical too")


class TestStructureMappingOffline(unittest.TestCase):
    """The parts that need no converter: regex demotion and the block model."""

    def test_regex_pass_demotes_furniture_and_strips_placeholders(self):
        import ingest_structure as istr
        blocks = [
            istr.SBlock("body", "Page 3 of 39", page=3),
            istr.SBlock("body", "UIN: 111N128V01", page=3),
            istr.SBlock("body", "Registered Office: 1st Floor, Somewhere, Mumbai 400001", page=1),
            istr.SBlock("body", "Dear <<Policyholder Name>>, your policy <<Policy No>> starts today.", page=2),
            istr.SBlock("body", "We will pay the sum assured on the death of the life assured during the policy term.", page=2),
            istr.SBlock("heading", "Insurer Name Ltd", page=1),
            istr.SBlock("heading", "Insurer Name Ltd", page=2),
            istr.SBlock("heading", "Insurer Name Ltd", page=3),
        ]
        rep = istr.StructureReport()
        out = istr.regex_boilerplate_pass(blocks, n_pages=6, report=rep)
        by = {b.text: b for b in out}
        self.assertEqual(by["Page 3 of 39"].kind, "boilerplate")
        self.assertEqual(by["UIN: 111N128V01"].kind, "boilerplate")
        self.assertTrue(any(b.demoted_by == "registration_line" for b in out))
        body = next(b for b in out if b.has_placeholder)
        self.assertNotIn("<<", body.text)
        self.assertEqual(body.kind, "body")
        self.assertEqual(body.text, "Dear , your policy starts today.")

        self.assertEqual(rep.placeholders_stripped, 2)
        kept = next(b for b in out if b.text.startswith("We will pay"))
        self.assertEqual(kept.kind, "body")
        # the heading recurring on 3 of 6 pages (50 %) is furniture
        self.assertTrue(all(b.kind == "boilerplate" for b in out if b.text == "Insurer Name Ltd"))


    def test_front_matter_junk_before_the_first_numbered_heading_is_boilerplate(self):
        import ingest_structure as istr
        blocks = [
            istr.SBlock("heading", "Some Policy Wording", level=1),
            istr.SBlock("body", "Source URL: https://irdai.gov.in/documents/37343/policy.pdf"),
            istr.SBlock("body", "Toll free 1800 209 5858 or 022 4890 3009"),
            istr.SBlock("body", "UIN / reference: IRDAN159RP0019V01202021."),
            istr.SBlock("body", "CIN: U66010MH2007PLC177117"),
            istr.SBlock("body", "An ISO 9001:2015 certified company."),
            istr.SBlock("body", "A Certified Company since the nineties."),
            istr.SBlock("body", "This policy covers the insured home building against the insured events listed."),
            istr.SBlock("heading", "1. Preamble", level=1),
            istr.SBlock("body", "Grievances: call 1800 209 5858 or write to the address on www.insurer.in."),
        ]
        out = istr.regex_boilerplate_pass(blocks, n_pages=0)
        kinds = [(b.text[:12], b.kind, b.demoted_by) for b in out]
        for t, k, by in kinds[1:7]:
            self.assertEqual(k, "boilerplate", kinds)
        self.assertEqual({by for _t, _k, by in kinds[1:7]} - {"registration_line"}, {"front_matter"}, kinds)
        self.assertEqual(out[7].kind, "body")
        self.assertEqual(out[9].kind, "body", "after the first numbered heading a phone line is body")

    def test_segment_structured_ids_and_signposts(self):
        import ingest_structure as istr
        import ingest as ing
        blocks = [
            istr.SBlock("heading", "D. Exclusions", level=1, page=1),
            istr.SBlock("body", "(a) War and invasion are not covered under this policy at any time.", page=1),
            istr.SBlock("body", "(b) Nuclear risks are not covered under this policy at any time either.", page=1),
            istr.SBlock("table_stub", "There is a table here: Item, Limit. Ask me for any row.", table_key="t1", page=1),
            istr.SBlock("table_row", "Item: Jewellery; Limit: 1500.", table_key="t1", page=1, extra={"row": 1}),
            istr.SBlock("boilerplate", "Page 1 of 2", page=1, demoted_by="page_number"),
            istr.SBlock("heading", "Definitions", level=1, page=2),
            istr.SBlock("body", "Insured means the person named in the schedule.", page=2),
        ]
        class Opts:
            id_prefix = "sec"; max_clause_chars = 600; min_clause_chars = 40
        raws, notes = ing.segment_structured(blocks, Opts)
        recs = ing.to_records(raws, Opts)
        ids = [r["id"] for r in recs]
        self.assertEqual(ids, ["sec-d", "sec-da", "sec-db", "sec-d-t1", "sec-d-t1-r1", "sec-d-b1",
                               "sec-2", "sec-2-p1"])
        self.assertEqual(recs[0]["text_display"], "Exclusions. 3 items.")   # a, b, and the table stub
        self.assertEqual(recs[0]["kind"], "heading")
        self.assertEqual(recs[1]["text_display"], "(a) War and invasion are not covered under this policy at any time.")
        self.assertEqual(recs[4]["parent"], "sec-d-t1")
        self.assertTrue(recs[4]["spoken_on_request"])
        self.assertEqual(recs[7]["kind"], "definition")
        self.assertEqual(ing.build_terms(recs), {"insured": "sec-2-p1"})
        self.assertEqual([m["title"] for m in ing.build_map(recs)], ["Exclusions", "Definitions"])


if __name__ == "__main__":
    unittest.main()
