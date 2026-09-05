"""Library: per-document sessions survive switching, and nothing leaks across.

The claim under test is that a document's position is *its own*. Reading in B
must not move A's cursor, touch A's ledger, or change what a deictic question in
A would resolve to. That is the same invariant the delivery ledger relies on,
one level up.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "examples" / "policy-reader"))

from chat_demo import ChatSession                                    # noqa: E402
from delivery_layer.events import EventLog                           # noqa: E402
from library import (AmbiguousDocument, Library, Session,            # noqa: E402
                     UnknownDocument)

FIXTURES = ROOT / "examples" / "policy-reader" / "fixtures"
POLICY = FIXTURES / "policy.json"
CARERS = FIXTURES / "carers_allowance.json"
REAL_INDEX = FIXTURES / "index.json"

LONG_CLAUSE = "sec-4b-vii"      # in policy.json, 349 display chars


def make_index(tmp: Path) -> Path:
    """Two documents whose titles deliberately overlap, so `open` can be ambiguous."""
    index = tmp / "index.json"
    index.write_text(json.dumps({"documents": [
        {"name": "alpha", "title": "Alpha Homeowners Policy", "path": str(POLICY),
         "clause_count": 213, "source": {"type": "synthetic"}},
        {"name": "beta", "title": "Beta Allowance Policy", "path": str(CARERS),
         "clause_count": 45, "source": {"type": "url"}},
    ]}, indent=1))
    return index


class LibraryTestBase(unittest.TestCase):
    def setUp(self):
        if not CARERS.exists():
            self.skipTest("carers_allowance.json not present")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.index = make_index(Path(self.tmp.name))
        self.ev = EventLog(None, session_id="test-library")
        self.lib = Library(self.index, self.ev)

    def types(self):
        return [r["type"] for r in self.ev.records]


class TestRegistry(LibraryTestBase):
    def test_list_reports_registered_documents(self):
        rows = {r["name"]: r for r in self.lib.list()}
        self.assertEqual(set(rows), {"alpha", "beta"})
        self.assertEqual(rows["beta"]["clause_count"], 45)
        self.assertEqual(rows["alpha"]["title"], "Alpha Homeowners Policy")

    def test_current_is_none_before_open(self):
        self.assertIsNone(self.lib.current)

    def test_open_by_exact_name(self):
        self.assertEqual(self.lib.open("alpha").name, "alpha")
        self.assertEqual(self.lib.current.name, "alpha")

    def test_open_by_title_substring(self):
        self.assertEqual(self.lib.open("Homeowners").name, "alpha")

    def test_ambiguous_open_raises(self):
        with self.assertRaises(AmbiguousDocument) as cm:
            self.lib.open("Policy")          # matches both titles
        self.assertIn("alpha", str(cm.exception))
        self.assertIn("beta", str(cm.exception))

    def test_unknown_open_raises(self):
        with self.assertRaises(UnknownDocument):
            self.lib.open("nothing-like-this")

    def test_exact_name_beats_an_otherwise_ambiguous_title(self):
        """'beta' is a name; it must not trip the title-ambiguity path."""
        self.assertEqual(self.lib.open("beta").name, "beta")

    def test_grounding_is_lazy_and_cached(self):
        doc = self.lib._docs["alpha"]
        self.assertFalse(doc.loaded)
        first = doc.grounding
        self.assertTrue(doc.loaded)
        self.assertIs(first, doc.grounding)

    def test_opening_does_not_load_the_other_document(self):
        self.lib.open("alpha")
        self.lib.current.grounding
        self.assertFalse(self.lib._docs["beta"].loaded)

    def test_real_index_lists_both_shipped_fixtures(self):
        lib = Library(REAL_INDEX, EventLog(None))
        names = {r["name"] for r in lib.list()}
        self.assertIn("policy", names)
        self.assertIn("carers_allowance", names)


class TestSwitchingPreservesPosition(LibraryTestBase):
    def setUp(self):
        super().setUp()
        self.s = ChatSession(self.lib, self.ev)
        self.lib.open("alpha")

    def test_position_is_exactly_as_left(self):
        # --- read and interrupt in A
        self.s.handle(f"read {LONG_CLAUSE}")
        self.s.handle("stop 80")
        a_cursor, a_heard, a_boundary = self.s.cursor, self.s.last_heard_id, self.s.boundary
        a_current, a_index = self.s.current_id, self.s.read_index
        a_ledger = dict(self.s.status)
        self.assertEqual(a_heard, LONG_CLAUSE)
        self.assertEqual(a_boundary, 80)

        # --- go and read in B
        self.s.handle("open beta")
        self.assertEqual(self.s.doc.name, "beta")
        self.s.handle("read")
        self.s.handle("read")
        self.assertNotEqual(self.s.cursor, a_cursor)

        # --- come back
        self.s.handle("open alpha")
        self.assertEqual(self.s.doc.name, "alpha")
        self.assertEqual(self.s.cursor, a_cursor)
        self.assertEqual(self.s.last_heard_id, a_heard)
        self.assertEqual(self.s.boundary, a_boundary)
        self.assertEqual(self.s.current_id, a_current)
        self.assertEqual(self.s.read_index, a_index)
        self.assertEqual(dict(self.s.status), a_ledger)

    def test_ledger_for_a_is_unchanged_by_activity_in_b(self):
        self.s.handle(f"read {LONG_CLAUSE}")
        self.s.handle("stop 80")
        before = dict(self.s.status)
        self.s.handle("open beta")
        for _ in range(5):
            self.s.handle("read")
        b_ledger = dict(self.s.status)
        self.s.handle("open alpha")
        self.assertEqual(dict(self.s.status), before)
        self.assertEqual(before[LONG_CLAUSE], "truncated@80")
        # and the two ledgers share no unit ids
        self.assertEqual(set(before) & set(b_ledger), set())

    def test_deictic_still_resolves_to_the_cut_clause_after_a_round_trip(self):
        self.s.handle(f"read {LONG_CLAUSE}")
        self.s.handle("stop 80")
        self.s.handle("open beta")
        self.s.handle("read")
        self.s.handle("open alpha")
        out = self.s.handle("what does that mean")
        self.assertIn("[deictic]", out)
        self.assertIn(f"-> {LONG_CLAUSE}", out)

    def test_resume_after_a_round_trip_is_still_sentence_zero(self):
        self.s.handle(f"read {LONG_CLAUSE}")
        self.s.handle("stop 80")
        self.s.handle("open beta")
        self.s.handle("read")
        self.s.handle("open alpha")
        self.assertIn("sentence 0", self.s.handle("resume"))

    def test_events_show_the_save_restore_pair(self):
        self.s.handle(f"read {LONG_CLAUSE}")
        self.s.handle("stop 80")
        n = len(self.ev.records)
        self.s.handle("open beta")
        new = self.ev.records[n:]
        kinds = [r["type"] for r in new]
        self.assertIn("document_opened", kinds)
        self.assertIn("position_saved", kinds)
        self.assertIn("position_restored", kinds)

        opened = [r for r in new if r["type"] == "document_opened"][0]
        saved = [r for r in new if r["type"] == "position_saved"][0]
        restored = [r for r in new if r["type"] == "position_restored"][0]
        self.assertEqual(opened["name"], "beta")
        self.assertEqual(saved["document"], "alpha")          # the one being left
        self.assertEqual(saved["boundary_char"], 80)
        self.assertEqual(saved["last_heard_unit_id"], LONG_CLAUSE)
        self.assertEqual(restored["document"], "beta")        # the one being entered

    def test_no_save_event_when_reopening_the_same_document(self):
        n = len(self.ev.records)
        self.s.handle("open alpha")
        kinds = [r["type"] for r in self.ev.records[n:]]
        self.assertIn("document_opened", kinds)
        self.assertNotIn("position_saved", kinds)


class TestCrossDocumentQuestionsAreNotAnswered(LibraryTestBase):
    """No cross-document retrieval, by design. Asking B about A is not_found."""

    def setUp(self):
        super().setUp()
        self.s = ChatSession(self.lib, self.ev)

    def test_question_is_answered_from_the_current_document_only(self):
        self.s.handle("open beta")
        for _ in range(20):
            self.s.handle("read")
        out = self.s.handle("what is the windstorm and hail deductible")
        self.assertNotIn("sec-1-vii", out)      # that clause lives in alpha

    def test_same_question_resolves_once_the_right_document_is_open(self):
        self.s.handle("open alpha")
        self.s.handle("read sec-1-vii")
        out = self.s.handle("what is the deductible for wind")
        self.assertIn("sec-1-vii", out)


class TestSessionPersistence(LibraryTestBase):
    def test_save_and_load_round_trips_sessions_only(self):
        s = ChatSession(self.lib, self.ev)
        s.handle("open alpha")
        s.handle(f"read {LONG_CLAUSE}")
        s.handle("stop 80")
        s.handle("open beta")
        s.handle("read")

        out = Path(self.tmp.name) / "sessions.json"
        self.lib.save(out)
        blob = json.loads(out.read_text())
        self.assertEqual(blob["current"], "beta")
        self.assertEqual(set(blob["sessions"]), {"alpha", "beta"})
        self.assertEqual(blob["sessions"]["alpha"]["boundary_char"], 80)
        # fixtures must not be embedded
        self.assertNotIn("clauses", json.dumps(blob))

        fresh = Library(self.index, EventLog(None))
        fresh.load(out)
        self.assertEqual(fresh.current.name, "beta")
        a = fresh._docs["alpha"].session
        self.assertEqual(a.boundary_char, 80)
        self.assertEqual(a.last_heard_unit_id, LONG_CLAUSE)
        self.assertEqual(a.ledger[LONG_CLAUSE], "truncated@80")

    def test_session_round_trips_through_dict(self):
        s = Session(read_cursor=7, last_heard_unit_id="x", boundary_char=12,
                    ledger={"x": "truncated@12"}, history=[("q", "deictic", "x")])
        again = Session.from_dict(s.to_dict())
        self.assertEqual(again.read_cursor, 7)
        self.assertEqual(again.ledger, {"x": "truncated@12"})
        self.assertEqual(again.history, [("q", "deictic", "x")])

    def test_history_records_question_kind_and_unit(self):
        s = ChatSession(self.lib, self.ev)
        s.handle("open alpha")
        s.handle(f"read {LONG_CLAUSE}")
        s.handle("what does that mean")
        q, kind, unit = s.sess.history[-1]
        self.assertEqual(kind, "deictic")
        self.assertEqual(unit, LONG_CLAUSE)


class TestReplLibraryCommands(LibraryTestBase):
    def setUp(self):
        super().setUp()
        self.s = ChatSession(self.lib, self.ev)

    def test_docs_lists_and_marks_current(self):
        self.s.handle("open beta")
        out = self.s.handle("docs")
        self.assertIn("alpha", out)
        self.assertIn("beta", out)
        self.assertIn("* beta", out)

    def test_open_reports_ambiguity_instead_of_raising(self):
        out = self.s.handle("open Policy")
        self.assertIn("matches 2 documents", out)

    def test_do_i_qualify_is_a_question_not_the_docs_command(self):
        """`docs` takes no argument, so a sentence starting with "do" is a question."""
        self.s.handle("open alpha")
        self.s.handle("read sec-3a-v")
        out = self.s.handle("do I qualify for coverage")
        self.assertNotIn("index.json", out)
        self.assertIn("A (spoken):", out)

    def test_bare_where_still_works_as_a_command(self):
        self.s.handle("open alpha")
        self.assertIn("cursor", self.s.handle("w"))

    def test_where_with_trailing_words_is_a_question(self):
        self.s.handle("open alpha")
        self.s.handle("read")
        out = self.s.handle("where does the policy period begin")
        self.assertIn("A (spoken):", out)

    def test_single_fixture_library_still_works(self):
        s = ChatSession(POLICY, EventLog(None))
        self.assertEqual(len(s.lib.list()), 1)
        self.assertEqual(s.doc.name, "policy")
        self.assertIn("sec-1-i", s.handle("read"))


if __name__ == "__main__":
    unittest.main()
