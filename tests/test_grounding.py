"""Grounding: deictic -> last heard, spoiler gate, section-ref parsing, no-LLM fallback."""
import asyncio
import re
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

    # ------------------------------------------------------ eligibility refusal
    ELIGIBILITY_PHRASINGS = ["do I qualify for coverage",
                            "can I claim for mold damage",
                            "will they pay for wind damage"]

    def test_eligibility_phrasings_are_tagged(self):
        for q in self.ELIGIBILITY_PHRASINGS:
            with self.subTest(q):
                self.assertTrue(Grounding.is_eligibility_question(q))
                self.assertEqual(self.g.resolve(q, None, read_cursor=212).kind, "eligibility")

    def test_eligibility_answer_refuses_and_never_says_yes_or_no(self):
        from grounding import ELIGIBILITY_REFUSAL
        for q in self.ELIGIBILITY_PHRASINGS:
            with self.subTest(q):
                ans = asyncio.run(self.g.answer(self.g.resolve(q, None, read_cursor=212)))
                self.assertIn(ELIGIBILITY_REFUSAL, ans)
                self.assertIsNone(re.search(r"\byes\b", ans, re.I), ans)
                self.assertIsNone(re.search(r"\bno\b", ans, re.I), ans)

    def test_eligibility_still_cites_the_criteria_clause(self):
        r = self.g.resolve("can I claim for mold damage", None, read_cursor=212)
        self.assertTrue(r.hits)
        self.assertIn(r.hits[0].text_spoken, asyncio.run(self.g.answer(r)))

    def test_cover_question_is_not_eligibility(self):
        q = "what does coverage C cover"
        self.assertFalse(Grounding.is_eligibility_question(q))
        self.assertNotEqual(self.g.resolve(q, None, read_cursor=212).kind, "eligibility")

    def test_spoiler_gate_beats_eligibility_tagging(self):
        """An eligibility ask must not pull an unread clause forward."""
        self.assertEqual(self.g.resolve("am I eligible for this", None, read_cursor=2).kind,
                         "beyond_cursor")

    def test_system_prompt_carries_the_refusal_rule(self):
        from grounding import SYSTEM_PROMPT
        self.assertIn("NEVER decide whether the listener personally qualifies", SYSTEM_PROMPT)
        self.assertIn("Never answer such a question with yes or no.", SYSTEM_PROMPT)

    def test_deictic_prompt_asks_for_a_plain_restatement(self):
        r = self.g.resolve("what does that mean", "sec-5a-v", read_cursor=70)
        self.assertEqual(r.kind, "deictic")
        system = self.g.build_prompt(r)[0]["content"]
        self.assertIn("restate the clause in plain everyday language", system)
        self.assertIn("do not repeat the clause word for word", system)
        self.assertIn("Add nothing that is not in the text", system)
        self.assertIn("Do NOT interpret", system, "every other rule is kept")

    def test_prompt_contains_only_document_text(self):
        r = self.g.resolve("what does that mean", "sec-5a-v", read_cursor=70)
        msgs = self.g.build_prompt(r, heard_text_of_reference="Water damage, meaning flood, surface water")
        self.assertIn("[interrupted]", msgs[1]["content"])
        self.assertIn(self.g.by_id["sec-5a-v"]["text_spoken"], msgs[1]["content"])
        self.assertIn("Do NOT interpret", msgs[0]["content"])




class TestTopicsGuarantee(unittest.TestCase):
    def test_every_fixture_in_the_index_offers_five_to_seven_chips(self):
        import json
        root = Path(__file__).resolve().parents[1] / "examples" / "policy-reader" / "fixtures"
        idx = json.loads((root / "index.json").read_text(encoding="utf-8"))
        for e in idx["documents"]:
            g = Grounding(root / e["path"])
            chips = g.topics
            with self.subTest(document=e["name"]):
                self.assertGreaterEqual(len(chips), 5, [c["topic"] for c in chips])
                self.assertLessEqual(len(chips), 7, [c["topic"] for c in chips])
                self.assertEqual(chips[-1]["topic"], "Read from the start")
                for c in chips[:-1]:
                    self.assertIn(c["section_id"], g.by_id)
                    self.assertLessEqual(len(c["topic"].split()), 8, c["topic"])

    def test_chip_names_are_short_and_clean(self):
        from grounding import chip_name
        self.assertEqual(chip_name("3.DEFINITIONS"), "Definitions")
        self.assertEqual(chip_name("Section 4 Perils insured against"), "Perils insured against")
        self.assertEqual(chip_name("BRIEF PROCEDURE TO BE FOLLOWED FOR RECOVERY OF OVERDUES. 11 items."), "Brief Procedure To Be")
        self.assertEqual(chip_name("Insured Events"), "Insured Events")
        self.assertEqual(chip_name("=== PAGE 3 ==="), "")
        self.assertEqual(chip_name("Level 4"), "Level 4")


if __name__ == "__main__":
    unittest.main()
