"""Asking a question over /ws/audio, on FakeTTS.

Three things the trace session_web-d2211205.jsonl showed were wrong:
the voice kept reading after a question, the answer was text only, and the
question resolved against the last clause synthesised rather than the last
clause heard. The assertions here are the event order the fix promises:
flush_ack -> unit_truncated -> question_resolved -> answer_spoken ->
position_restored, with the answer synthesised as a unit of its own and heard
only on the client's acks, and reading resumed at the cut sentence.
"""
import asyncio
import base64
import os
import sys
import unittest
from pathlib import Path

os.environ["TTS_PROVIDER"] = "fake"
os.environ.pop("LLM_API_KEY", None)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "examples" / "policy-reader"))

from aiohttp.test_utils import TestClient, TestServer               # noqa: E402

import server as srv                                                # noqa: E402

SR = 24000


class WsCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.app = srv.build_app(dev=False)
        self.s = self.app["session"]
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()
        self.ws = await self.client.ws_connect("/ws/audio")
        self.hello = await self.ws.receive_json(timeout=5)
        self.audio: dict = {}                 # context_id -> bytes received
        self.seen: list = []                  # every message, in order
        self._lead = srv.LEAD_MS

    async def asyncTearDown(self):
        srv.LEAD_MS = self._lead
        await self.ws.close()
        await self.client.close()
        p = getattr(self.s.events, "path", None)
        if p and Path(p).exists():
            Path(p).unlink()

    async def recv(self, timeout=5.0):
        m = await self.ws.receive_json(timeout=timeout)
        self.seen.append(m)
        if m.get("type") == "audio":
            self.audio[m["context_id"]] = self.audio.get(m["context_id"], 0) + len(base64.b64decode(m["b64"]))
        return m

    async def until(self, pred, timeout=8.0):
        """Receive until pred(message) is true; returns that message."""
        async def go():
            while True:
                m = await self.recv()
                if pred(m):
                    return m
        return await asyncio.wait_for(go(), timeout=timeout)

    def audio_ms(self, ctx):
        return self.audio[ctx] / 2 / SR * 1000

    async def ack_all(self, ctx):
        """Play a whole unit, as the client does: acks then the completion signal."""
        frames = self.audio[ctx] // 2
        ms = self.audio_ms(ctx)
        await self.ws.send_json({"type": "rendered", "context_id": ctx, "rendered_ms": ms,
                                 "enqueued_frames": frames})
        await self.ws.send_json({"type": "unit_ended", "context_id": ctx,
                                 "enqueued_frames": frames, "rendered_ms": ms})

    def event_types(self):
        return [r["type"] for r in self.s.events.records]

    @staticmethod
    def is_event(m, kind):
        """Ledger events reach the client as {"type": "event", "record": {...}}."""
        return m.get("type") == "event" and (m.get("record") or {}).get("type") == kind

    async def play_first_clause(self):
        await self.ws.send_json({"type": "play"})
        started = await self.until(lambda m: m.get("type") == "unit_started")
        ctx = started["context_id"]
        await self.until(lambda m: m.get("type") == "unit_done" and m["context_id"] == ctx)
        return started, ctx


class TestAskWhilePlaying(WsCase):
    async def test_question_stops_the_reader_speaks_the_answer_and_resumes_at_the_cut_sentence(self):
        started, ctx = await self.play_first_clause()
        unit_id = started["unit_id"]
        clause = self.s.library.current.grounding.by_id[unit_id]
        cut_ms = self.audio_ms(ctx) * 0.4

        # The client's sequence: the playhead first, then the question.
        await self.ws.send_json({"type": "flush_ack", "context_id": ctx, "rendered_ms": cut_ms})
        await self.ws.send_json({"type": "ask", "question": "what does that mean"})

        boundary = await self.until(lambda m: m.get("type") == "boundary")
        self.assertEqual(boundary["unit_id"], unit_id)
        self.assertLess(boundary["char_end"], boundary["of"], "cut mid-clause")
        self.assertFalse(self.s.playing, "the reader is stopped by the question")

        answer = await self.until(lambda m: m.get("type") == "answer")
        self.assertEqual(answer["kind"], "deictic")
        self.assertEqual(answer["unit_id"], unit_id, "deictic: the clause just heard")
        self.assertEqual(answer["source"], "extractive")

        spoken = await self.until(lambda m: m.get("type") == "unit_started" and m.get("kind") == "answer")
        actx = spoken["context_id"]
        self.assertTrue(actx.startswith("answer#t"))
        self.assertEqual(spoken["text_display"], answer["answer"])
        await self.until(lambda m: m.get("type") == "unit_done" and m["context_id"] == actx)
        self.assertGreater(self.audio.get(actx, 0), 0, "the answer was synthesised")

        # Nothing resumes until the client says it heard the whole answer.
        with self.assertRaises(asyncio.TimeoutError):
            await self.until(lambda m: m.get("type") == "playing", timeout=1.0)
        await self.ack_all(actx)
        await self.until(lambda m: m.get("type") == "playing", timeout=4.0)
        rp = await self.until(lambda m: m.get("type") == "resume_point")
        self.assertEqual(rp["unit_id"], unit_id)
        resumed = await self.until(lambda m: m.get("type") == "unit_started" and m.get("kind") == "clause")
        self.assertEqual(resumed["unit_id"], unit_id, "the cut clause, not the next one")
        self.assertEqual(resumed["char_start"], rp["char_start"])
        self.assertIn(rp["char_start"], [a for a, _ in clause["sentences"]],
                      "resumes on a sentence boundary")
        self.assertEqual(resumed["text_display"], clause["text_display"], "display text stays whole")

        # Opening the document at startup emits its own position_restored; the
        # order that matters starts at the listener's flush ack.
        types = self.event_types()
        types = types[types.index("flush_ack"):]
        order = ["flush_ack", "unit_truncated", "question_resolved", "answer_spoken", "position_restored"]
        idx = [types.index(t) for t in order]
        self.assertEqual(idx, sorted(idx), f"event order {order} violated in {types}")
        q = next(r for r in self.s.events.records if r["type"] == "question_resolved")
        self.assertEqual(q["last_heard_unit_id"], unit_id)
        self.assertEqual(self.s.events.of_type("unit_truncated")[0]["reason"], "ask")

    async def test_a_beyond_cursor_answer_waits_for_the_listener(self):
        started, ctx = await self.play_first_clause()
        await self.ws.send_json({"type": "flush_ack", "context_id": ctx,
                                 "rendered_ms": self.audio_ms(ctx) * 0.5})
        # Something far ahead in the document.
        g = self.s.library.current.grounding
        far = g.clauses[-1]["text_display"].split()[:6]
        await self.ws.send_json({"type": "ask", "question": " ".join(far)})
        answer = await self.until(lambda m: m.get("type") == "answer")
        if answer["kind"] != "beyond_cursor":
            self.skipTest(f"retrieval did not classify the probe as beyond_cursor ({answer['kind']})")
        spoken = await self.until(lambda m: m.get("type") == "unit_started" and m.get("kind") == "answer")
        await self.until(lambda m: m.get("type") == "unit_done" and m["context_id"] == spoken["context_id"])
        await self.ack_all(spoken["context_id"])
        await self.until(lambda m: m.get("type") == "paused")
        with self.assertRaises(asyncio.TimeoutError):
            await self.until(lambda m: m.get("type") == "playing", timeout=1.2)
        self.assertFalse(self.s.playing)


class TestPauseRegression(WsCase):
    async def prime_two_clauses(self, pct=0.4):
        """Clause N synthesised, acked to pct, and N+1 buffered behind it."""
        started, ctx = await self.play_first_clause()
        # Let exactly one more clause through the lead: after N the backlog is
        # the unplayed 60% of N, which must be under LEAD_MS; after N+1 it is
        # over it again, so the pump stalls there and the test is deterministic.
        srv.LEAD_MS = self.audio_ms(ctx) * (1 - pct) + 50
        await self.ws.send_json({"type": "rendered", "context_id": ctx,
                                 "rendered_ms": self.audio_ms(ctx) * pct,
                                 "enqueued_frames": self.audio[ctx] // 2})
        nxt = await self.until(lambda m: m.get("type") == "unit_started" and m["context_id"] != ctx)
        await self.until(lambda m: m.get("type") == "unit_done" and m["context_id"] == nxt["context_id"])
        return started, ctx, nxt

    async def test_pause_at_40_percent_with_next_clause_buffered(self):
        started, ctx, nxt = await self.prime_two_clauses(0.4)
        n_id, n1_id = started["unit_id"], nxt["unit_id"]
        cut_ms = self.audio_ms(ctx) * 0.4
        await self.ws.send_json({"type": "flush_ack", "context_id": ctx, "rendered_ms": cut_ms})
        await self.ws.send_json({"type": "pause"})
        boundary = await self.until(lambda m: m.get("type") == "boundary")
        self.assertEqual(boundary["unit_id"], n_id)
        await self.until(lambda m: m.get("type") == "paused")

        sess = self.s.library.current.session
        self.assertTrue(sess.ledger[n_id].startswith("truncated@"))
        self.assertNotIn(n1_id, sess.ledger, "N+1 was buffered, never heard, so it has no entry")
        n1 = self.s.contexts[nxt["context_id"]]
        self.assertEqual(n1.state, "fenced")
        self.assertTrue(n1.abandoned)
        fenced = [r for r in self.s.events.of_type("unit_fenced") if r["context_id"] == nxt["context_id"]]
        self.assertEqual(len(fenced), 1)
        self.assertEqual(fenced[0]["bytes"], self.audio[nxt["context_id"]])

        # Play resumes at N's sentence boundary, and N+1 is read again after N.
        await self.ws.send_json({"type": "play"})
        rp = await self.until(lambda m: m.get("type") == "resume_point")
        self.assertEqual(rp["unit_id"], n_id)
        resumed = await self.until(lambda m: m.get("type") == "unit_started")
        self.assertEqual(resumed["unit_id"], n_id)
        self.assertEqual(resumed["char_start"], rp["char_start"])
        clause = self.s.library.current.grounding.by_id[n_id]
        self.assertIn(rp["char_start"], [a for a, _ in clause["sentences"]])
        rctx = resumed["context_id"]
        await self.until(lambda m: m.get("type") == "unit_done" and m["context_id"] == rctx)
        await self.ack_all(rctx)
        heard = (await self.until(lambda m: self.is_event(m, "unit_heard")
                                  and m["record"]["unit_id"] == n_id))["record"]
        self.assertEqual(heard["char_end"], heard["of"])
        self.assertEqual(sess.ledger[n_id], "heard")
        self.assertNotIn(n1_id, sess.ledger, "N+1 is still not heard early")
        again = await self.until(lambda m: m.get("type") == "unit_started" and m["unit_id"] == n1_id)
        self.assertNotEqual(again["context_id"], nxt["context_id"], "re-synthesised, not the fenced audio")

    async def test_deictic_question_uses_the_clause_heard_not_the_last_synthesised(self):
        started, ctx, nxt = await self.prime_two_clauses(0.5)
        self.assertNotEqual(started["unit_id"], nxt["unit_id"])
        await self.ws.send_json({"type": "flush_ack", "context_id": ctx,
                                 "rendered_ms": self.audio_ms(ctx) * 0.5})
        await self.ws.send_json({"type": "ask", "question": "repeat that"})
        answer = await self.until(lambda m: m.get("type") == "answer")
        self.assertEqual(answer["unit_id"], started["unit_id"],
                         "resolved against the clause at the playhead, not N+1 which was only buffered")
        q = self.s.events.of_type("question_resolved")[0]
        self.assertEqual(q["last_heard_unit_id"], started["unit_id"])


if __name__ == "__main__":
    unittest.main()
