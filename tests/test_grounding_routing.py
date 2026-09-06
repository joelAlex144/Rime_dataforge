"""Resolution order in grounding.py, one case per branch.

deictic -> definition -> section_ref -> bm25 (proximity prior, spoiler gate)
-> none. Every result says which branch answered (`retrieval_path`), and the
two invariants that matter are proved rather than assumed: a deictic question
never touches BM25, and a definition question returns a `definition` clause
even when a `body` clause would out-score it.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "examples" / "policy-reader"))
import grounding as gr                                              # noqa: E402
from grounding import Grounding                                     # noqa: E402

HERO = ROOT / "examples" / "policy-reader" / "fixtures" / "policy.json"


def clause(i, cid, section, title, text, kind=None, **extra):
    c = {"id": cid, "index": i, "section": section, "section_title": title,
         "subsection": None, "item": None, "text_display": text, "text_spoken": text,
         "sentences": [[0, len(text)]], "spoken_map": [[0, len(text), text]]}
    if kind:
        c["kind"] = kind
    c.update(extra)
    return c


class TestBranches(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.g = Grounding(HERO)

    def test_every_path_value_is_one_of_the_declared_set(self):
        self.assertEqual(gr.RETRIEVAL_PATHS, ("deictic", "definition", "section_ref", "bm25", "none"))

    def test_deictic_resolves_to_last_heard_and_never_calls_bm25(self):
        g = Grounding(HERO)
        calls = []
        orig = g.bm25.rank
        g.bm25.rank = lambda *a, **k: (calls.append(a), orig(*a, **k))[1]
        r = g.resolve("what does that mean", "sec-4b-i", read_cursor=g.by_id["sec-4b-iii"]["index"])
        self.assertEqual(r.retrieval_path, "deictic")
        self.assertEqual(r.reference.unit_id, "sec-4b-i")
        self.assertEqual(calls, [], "BM25 must not be consulted for a deictic question")

    def test_definition_lookup_returns_the_definition_clause(self):
        r = self.g.resolve("what does bodily injury mean", "sec-4b-i", read_cursor=self.g.by_id["sec-4b-iii"]["index"])
        self.assertEqual(r.retrieval_path, "definition")
        self.assertEqual(r.reference.unit_id, "sec-3a-iii")
        self.assertEqual(gr.clause_kind(self.g.by_id[r.reference.unit_id]), "definition")

    def test_definition_ignores_the_spoiler_gate(self):
        # Cursor at the very start: Definitions (section 3) is "ahead", and a
        # definition is still returned in_scope because it is reference text.
        r = self.g.resolve("define business", None, read_cursor=0)
        self.assertEqual(r.retrieval_path, "definition")
        self.assertEqual(r.kind, "in_scope")
        self.assertEqual(r.reference.unit_id, "sec-3a-iv")

    def test_definition_fuzzy_match_on_stemmed_overlap(self):
        r = self.g.resolve("what is an insured person", "sec-4b-i", read_cursor=self.g.by_id["sec-4b-iii"]["index"])
        self.assertEqual(r.retrieval_path, "definition")
        self.assertEqual(r.reference.unit_id, "sec-3a-vi")

    def test_section_ref_path(self):
        r = self.g.resolve("what is in section 4 b 2", "sec-4b-iii", read_cursor=self.g.by_id["sec-4b-iii"]["index"])
        self.assertEqual(r.retrieval_path, "section_ref")
        self.assertEqual(r.reference.unit_id, "sec-4b-ii")

    def test_bm25_path_with_spoiler_gate(self):
        cur = self.g.by_id["sec-4b-iii"]["index"]
        r = self.g.resolve("how long do I have to sue you", "sec-4b-i", read_cursor=cur)
        self.assertEqual(r.retrieval_path, "bm25")
        self.assertEqual(r.kind, "beyond_cursor")            # suit clause is in section 9, far ahead
        self.assertTrue(r.beyond)

    def test_none_path(self):
        r = self.g.resolve("zxq quux frobnicate", "sec-4b-i", read_cursor=5)
        self.assertEqual(r.retrieval_path, "none")
        self.assertEqual(r.kind, "not_found")

    def test_proximity_prior_weights_are_exposed_and_ordered(self):
        self.assertEqual(gr.PRIOR_SAME_SECTION, 1.0)
        self.assertGreater(gr.PRIOR_SAME_SECTION, gr.PRIOR_ADJACENT_SECTION)
        self.assertGreater(gr.PRIOR_ADJACENT_SECTION, gr.PRIOR_ELSEWHERE)
        anchor = self.g.by_id["sec-4b-i"]["section"]
        same = self.g.by_id["sec-4b-iii"]["index"]
        adjacent = self.g.by_id["sec-5a-i"]["index"] if "sec-5a-i" in self.g.by_id else next(
            c["index"] for c in self.g.clauses if c["section"] == 5)
        far = next(c["index"] for c in self.g.clauses if c["section"] == 13)
        self.assertEqual(self.g.proximity_prior(same, anchor), 1.0)
        self.assertEqual(self.g.proximity_prior(adjacent, anchor), 0.7)
        self.assertEqual(self.g.proximity_prior(far, anchor), 0.4)


class TestSyntheticFixture(unittest.TestCase):
    """A fixture built here so the two invariants do not depend on the hero
    fixture's wording."""

    def setUp(self):
        body_text = ("Business business business: this clause repeats the word business many times "
                     "so BM25 scores it far above any definition. Business business business business.")
        self.doc = {
            "title": "Synthetic", "synthetic": True, "clause_count": 6,
            "map": [{"title": "Front", "section": 1, "children": 1},
                    {"title": "Definitions", "section": 2, "children": 2},
                    {"title": "Cover", "section": 3, "children": 2}],
            "terms": {"business": "def-2-1"},
            "clauses": [
                clause(0, "hdr-1", 1, "Front", "Front. 1 items.", kind="heading"),
                clause(1, "boil-1", 1, "Front", "Page 1 of 9", kind="boilerplate"),
                clause(2, "def-2-1", 2, "Definitions", "Business means a trade or profession.", kind="definition"),
                clause(3, "row-2-1", 2, "Definitions", "Row: Term: Insured; Definition: you.",
                       kind="table_row", spoken_on_request=True),
                clause(4, "body-3-1", 3, "Cover", body_text, kind="body"),
                clause(5, "body-3-2", 3, "Cover", "We cover sudden water discharge from plumbing.", kind="body"),
            ],
        }
        self.tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
        json.dump(self.doc, self.tmp)
        self.tmp.close()
        self.g = Grounding(self.tmp.name)

    def tearDown(self):
        Path(self.tmp.name).unlink(missing_ok=True)

    def test_definition_beats_a_higher_scoring_body_clause(self):
        # BM25 alone prefers the body clause for "business".
        scores = dict(self.g.bm25.rank(["business"], self.g.retrievable))
        self.assertGreater(scores[4], scores[2])
        r = self.g.resolve("what is business", None, read_cursor=5)
        self.assertEqual(r.retrieval_path, "definition")
        self.assertEqual(r.reference.unit_id, "def-2-1")

    def test_bm25_never_returns_boilerplate_or_a_bare_heading(self):
        r = self.g.resolve("page front items", None, read_cursor=5)
        for h in r.hits + r.beyond:
            self.assertNotIn(h.unit_id, ("hdr-1", "boil-1"))
        self.assertEqual(sorted(self.g.retrievable), [2, 3, 4, 5])

    def test_table_rows_are_retrievable_but_skipped_in_playback(self):
        self.assertIn(3, self.g.retrievable)
        self.assertEqual(self.g.skipped(), [("boil-1", "boilerplate"), ("row-2-1", "table_on_request")])
        self.assertEqual(self.g.next_readable(1), 2)
        self.assertEqual(self.g.next_readable(3), 4)

    def test_map_sentence_is_a_count_only(self):
        self.assertEqual(self.g.map_sentence("document"),
                         "This document has 3 sections. I'll read them in order; interrupt me any time.")


if __name__ == "__main__":
    unittest.main()
