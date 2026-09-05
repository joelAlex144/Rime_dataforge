"""Grounding: deictic -> last heard, spoiler gate, section-ref parsing, no-LLM fallback."""
import asyncio
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "examples" / "policy-reader"))
from grounding import Grounding  # noqa: E402

FIX = ROOT / "examples" / "policy-reader" / "fixtures" / "policy.json"


class TestGrounding(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.g = Grounding(FIX)

    def test_fixture_shape(self):
        self.assertTrue(150 <= len(self.g.clauses) <= 250)
        self.assertIn("sec-4b-ii", self.g.by_id)
        c = self.g.by_id["sec-4b-ii"]
        for k in ("text_display", "text_spoken", "sentences", "spoken_map", "index", "section_title"):
            self.assertIn(k, c)

    def test_deictic_uses_last_heard_not_last_sent(self):
        last_heard = "sec-4b-i"
        r = self.g.resolve("what does that mean", last_heard, read_cursor=self.g.by_id["sec-4b-iii"]["index"])
        self.assertEqual(r.kind, "deictic")
        self.assertEqual(r.hits[0].unit_id, last_heard)

    def test_content_question_is_not_deictic(self):
        r = self.g.resolve("does this cover flooding", "sec-4b-i", read_cursor=40)
        self.assertNotEqual(r.kind, "deictic")

    def test_spoiler_gate_withholds_clauses_ahead(self):
        r = self.g.resolve("how long do I have to sue you", None, read_cursor=40)
        self.assertEqual(r.kind, "beyond_cursor")
        self.assertGreater(r.beyond[0].index, 40)
        ans = asyncio.run(self.g.answer(r))
        self.assertIn("further down", ans)
        self.assertNotIn("2 years", ans)
        self.assertNotIn("two years", ans)

    def test_in_scope_answer_cites_section(self):
        r = self.g.resolve("what is the deductible for wind", None, read_cursor=40)
        self.assertEqual(r.kind, "in_scope")
        self.assertEqual(r.hits[0].unit_id, "sec-1-vii")
        ans = asyncio.run(self.g.answer(r))
        self.assertTrue(ans.startswith("Section 1(vii)"))

    def test_explicit_section_ref(self):
        self.assertEqual(self.g.resolve_section_ref("read me section 4 b 7"), "sec-4b-vii")
        self.assertEqual(self.g.resolve_section_ref("what is Section 9(b)(i)"), "sec-9b-i")
        r = self.g.resolve("what is in section 4 b 7", None, read_cursor=10)
        self.assertEqual(r.kind, "beyond_cursor")

    def test_prompt_contains_only_document_text(self):
        r = self.g.resolve("what does that mean", "sec-5a-v", read_cursor=70)
        msgs = self.g.build_prompt(r, heard_text_of_reference="Water damage, meaning flood, surface water")
        self.assertIn("[interrupted]", msgs[1]["content"])
        self.assertIn(self.g.by_id["sec-5a-v"]["text_spoken"], msgs[1]["content"])
        self.assertIn("Do NOT interpret", msgs[0]["content"])


if __name__ == "__main__":
    unittest.main()
