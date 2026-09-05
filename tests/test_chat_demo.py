"""REPL: read -> stop -> deictic question -> resume, driven programmatically.

The assertion that matters is the acceptance claim in miniature: after an
interruption, "what does that mean" resolves to the clause that was cut, and
the resume point is the sentence containing the cut -- not the next clause,
and not the start of the document.
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "examples" / "policy-reader"))

from chat_demo import ChatSession, match_command  # noqa: E402
from delivery_layer.resume import resume_point    # noqa: E402

FIX = ROOT / "examples" / "policy-reader" / "fixtures" / "policy.json"
LONG_CLAUSE = "sec-4b-vii"          # multi-sentence, 349 display chars


class TestCommandMatching(unittest.TestCase):
    def test_exact_and_unique_prefix(self):
        self.assertEqual(match_command("read"), "read")
        self.assertEqual(match_command("rea"), "read")
        self.assertEqual(match_command("res"), "resume")
        self.assertEqual(match_command("w"), "where")
        self.assertEqual(match_command("L"), "ledger")

    def test_ambiguous_prefix_is_reported(self):
        self.assertTrue(match_command("r").startswith("\0ambiguous:"))

    def test_questions_are_not_commands(self):
        for q in ["what does that mean", "who is an insured", "is my roof covered",
                  "how long do I have to sue you"]:
            self.assertIsNone(match_command(q.split()[0]), q)


class TestReplFlow(unittest.TestCase):
    def setUp(self):
        self.s = ChatSession(FIX)

    def test_read_advances_cursor_and_marks_heard(self):
        self.s.handle("read")
        first = self.s.g.clauses[0]["id"]
        self.assertEqual(self.s.current_id, first)
        self.assertEqual(self.s.cursor, 1)
        self.assertEqual(self.s.status[first], "heard")

    def test_stop_truncates_without_advancing_the_cursor(self):
        self.s.handle(f"read {LONG_CLAUSE}")
        cursor_before = self.s.cursor
        out = self.s.handle("stop 80")
        self.assertIn("[interrupted]", out)
        self.assertEqual(self.s.cursor, cursor_before)
        self.assertEqual(self.s.boundary, 80)
        self.assertEqual(self.s.status[LONG_CLAUSE], "truncated@80")

    def test_deictic_question_resolves_to_the_stopped_clause(self):
        self.s.handle(f"read {LONG_CLAUSE}")
        self.s.handle("stop 80")
        out = self.s.handle("what does that mean")
        self.assertIn("[deictic]", out)
        self.assertIn(f"-> {LONG_CLAUSE}", out)

    def test_truncated_text_is_passed_as_heard_context(self):
        self.s.handle(f"read {LONG_CLAUSE}")
        self.s.handle("stop 80")
        heard = self.s._heard_text()
        self.assertEqual(len(heard), 80)
        self.assertTrue(self.s.g.by_id[LONG_CLAUSE]["text_display"].startswith(heard))

    def test_resume_point_is_sentence_zero_of_the_cut_clause(self):
        self.s.handle(f"read {LONG_CLAUSE}")
        self.s.handle("stop 80")
        c = self.s.g.by_id[LONG_CLAUSE]
        expected = resume_point(c["id"], c["text_display"], c["sentences"], 80, c["section_title"])
        self.assertEqual(expected.sentence_index, 0)
        out = self.s.handle("resume")
        self.assertIn(LONG_CLAUSE, out)
        self.assertIn("sentence 0", out)

    def test_resume_marks_the_clause_heard(self):
        self.s.handle(f"read {LONG_CLAUSE}")
        self.s.handle("stop 80")
        self.s.handle("resume")
        self.assertEqual(self.s.status[LONG_CLAUSE], "heard")
        self.assertEqual(self.s.boundary, len(self.s.g.by_id[LONG_CLAUSE]["text_display"]))

    def test_full_length_stop_is_not_recorded_as_a_truncation(self):
        self.s.handle("read")
        total = len(self.s.g.clauses[0]["text_display"])
        self.s.handle(f"stop {total + 50}")
        self.assertEqual(self.s.status[self.s.g.clauses[0]["id"]], "heard")

    def test_jump_moves_the_cursor(self):
        out = self.s.handle(f"jump {LONG_CLAUSE}")
        self.assertIn(LONG_CLAUSE, out)
        self.assertEqual(self.s.cursor, self.s.g.by_id[LONG_CLAUSE]["index"])

    def test_ledger_reports_heard_and_truncated(self):
        self.s.handle("read")
        self.s.handle(f"read {LONG_CLAUSE}")
        self.s.handle("stop 80")
        led = self.s.handle("ledger")
        self.assertIn("heard", led)
        self.assertIn("truncated@80", led)
        self.assertIn("never_sent", led)

    def test_where_reports_boundary(self):
        self.s.handle(f"read {LONG_CLAUSE}")
        self.s.handle("stop 80")
        self.assertIn("boundary 80 chars", self.s.handle("where"))

    def test_events_are_emitted(self):
        self.s.handle(f"read {LONG_CLAUSE}")
        self.s.handle("stop 80")
        self.s.handle("resume")
        types = [r["type"] for r in self.s.ev.records]
        for t in ("repl_command", "unit_heard", "unit_truncated", "position_restored"):
            self.assertIn(t, types)


class TestReplOnIngestedFixture(unittest.TestCase):
    """The REPL must work on an ingested fixture, not just the hero document."""
    FIX2 = ROOT / "examples" / "policy-reader" / "fixtures" / "carers_allowance.json"

    def setUp(self):
        if not self.FIX2.exists():
            self.skipTest("carers_allowance.json fixture not present")
        self.s = ChatSession(self.FIX2)

    def test_read_and_deictic_resolve(self):
        self.s.handle("read")
        first = self.s.current_id
        out = self.s.handle("what does that mean")
        self.assertIn("[deictic]", out)
        self.assertIn(f"-> {first}", out)

    def test_citation_does_not_speak_a_fake_section_number(self):
        """Unnumbered ids like sec-3-p7 must cite the heading, not "Section 3(p7)"."""
        self.s.handle("read")
        out = self.s.handle("what does that mean")
        self.assertNotIn("p7", out)
        self.assertNotRegex(out, r"Section \d+\(p\d+\)")


if __name__ == "__main__":
    unittest.main()
