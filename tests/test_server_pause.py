"""Stopping must not stall the pump or skip a clause.

traces/session_web-29e08c00.jsonl: after the listener paused sec-2-p1 at 2.7 s
of 10.2 s, flow control kept the unplayed 7.5 s in its backlog, above the 4 s
lead, so every later clause stalled after synthesis until pause and play were
pressed again. And pause left the read cursor past the paused clause:
sec-3-p4, paused at 0.9 s, was never read.
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "examples" / "policy-reader"))         # noqa: E402
import server as srv                                                 # noqa: E402

SR = 24000


def ctx(unit_id, turn, audio_ms, rendered_ms, **kw):
    frames = int(audio_ms * SR / 1000)
    return srv.ContextState(context_id=f"{unit_id}#t{turn}", turn_id=turn, unit_id=unit_id,
                            state="playing", bytes=frames * 2, rendered_ms=rendered_ms,
                            audio_ms=audio_ms, synth_done=True, **kw)


class PauseCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.s = srv.ReaderSession(dev=False)
        self.doc = self.s.library.current
        self.clauses = self.doc.grounding.clauses

    async def asyncTearDown(self):
        p = getattr(self.s.events, "path", None)
        if p and Path(p).exists():
            Path(p).unlink()

    async def send(self, m):
        await srv.handle_client_message(self.s, m, set(), None)


class TestBacklog(PauseCase):
    def test_counts_only_the_unplayed_part_of_live_units(self):
        a = ctx("a", 1, 10240.0, 2730.7, abandoned=True)   # paused: never counts
        b = ctx("b", 2, 7120.0, 7120.0, heard=True)         # finished: nothing left
        c = ctx("c", 3, 5000.0, 1000.0)                     # playing: 4 s buffered
        for st in (a, b, c):
            self.s.contexts[st.context_id] = st
        self.assertEqual(self.s._backlog_ms(), 4000.0)

    def test_a_heard_unit_never_contributes_even_with_quantum_slack(self):
        st = ctx("b", 1, 7120.0, 7114.7, heard=True)       # acked within one quantum
        self.s.contexts[st.context_id] = st
        self.assertEqual(self.s._backlog_ms(), 0.0)

    async def test_stop_reading_abandons_everything_not_heard(self):
        live = ctx("a", 1, 10240.0, 2730.7)
        done = ctx("b", 2, 7120.0, 7120.0, heard=True)
        for st in (live, done):
            self.s.contexts[st.context_id] = st
        self.assertGreater(self.s._backlog_ms(), srv.LEAD_MS)
        await self.s.stop_reading()
        self.assertTrue(live.abandoned)
        self.assertFalse(done.abandoned)
        self.assertEqual(self.s._backlog_ms(), 0.0)


class TestPauseAttribution(PauseCase):
    async def test_pause_mid_clause_rewinds_the_cursor_onto_it(self):
        c = self.clauses[1]
        st = ctx(c["id"], 5, 4693.8, 906.7)
        self.s.contexts[st.context_id] = st
        self.doc.session.read_cursor = c["index"] + 1        # read_loop had moved on
        await self.send({"type": "flush_ack", "context_id": st.context_id, "rendered_ms": 906.7})
        await self.send({"type": "pause"})
        sess = self.doc.session
        self.assertFalse(self.s.playing)
        self.assertEqual(sess.read_cursor, c["index"], "the paused clause is read next")
        self.assertTrue(sess.ledger[c["id"]].startswith("truncated@"))
        self.assertTrue(st.abandoned)
        trunc = self.s.events.of_type("unit_truncated")
        self.assertEqual(len(trunc), 1)
        self.assertEqual(trunc[0]["reason"], "pause")
        self.assertEqual(trunc[0]["context_id"], c["id"])
        self.assertEqual(self.s.events.of_type("pause_without_flush_ack"), [])

    async def test_pause_after_a_clause_was_heard_to_the_end_does_not_replay_it(self):
        c = self.clauses[1]
        st = ctx(c["id"], 4, 6720.0, 6720.0, heard=True)
        self.s.contexts[st.context_id] = st
        self.doc.session.ledger[c["id"]] = "heard"
        self.doc.session.read_cursor = c["index"] + 1
        # No word map in this session, so offset_at is unavailable; the helper
        # falls back to char 0, which would look like a cut. Give it one that
        # maps the full audio to the full text.
        class WM:
            spans = []
            def offset_at(self, ms):
                return len(c["text_display"])
        self.s.wordmaps[st.context_id] = WM()
        await self.send({"type": "flush_ack", "context_id": st.context_id, "rendered_ms": 6720.0})
        await self.send({"type": "pause"})
        sess = self.doc.session
        self.assertEqual(sess.read_cursor, c["index"] + 1, "never replay a heard clause")
        self.assertEqual(sess.ledger[c["id"]], "heard")
        self.assertEqual(self.s.events.of_type("unit_truncated"), [])

    async def test_pause_without_flush_ack_is_still_a_stop_and_says_so(self):
        c = self.clauses[1]
        st = ctx(c["id"], 5, 4693.8, 906.7)
        self.s.contexts[st.context_id] = st
        self.doc.session.current_unit_id = c["id"]
        self.s.playing = True
        await self.send({"type": "pause"})
        self.assertFalse(self.s.playing)
        self.assertEqual(len(self.s.events.of_type("pause_without_flush_ack")), 1)
        self.assertTrue(st.abandoned)


if __name__ == "__main__":
    unittest.main()
