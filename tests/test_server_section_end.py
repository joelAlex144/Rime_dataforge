"""section_end and the resume cue.

At a top-level section boundary the reader may ask "That's {heading}. Next is
{next}. Carry on, or something else?" (5 s; silence carries on) -- never
within SECTION_END_MIN_GAP_S of the last prompt, never right after a jump the
listener asked for, and never again once the listener has let it time out
twice (prompt_muted). Play after a pause longer than RESUME_CUE_AFTER_S says
"We were in {heading}. Carrying on." before the clause.
"""
import asyncio
import base64
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

os.environ["TTS_PROVIDER"] = "fake"
os.environ.pop("LLM_API_KEY", None)
os.environ.pop("LLM_PROVIDER", None)
os.environ.pop("ENRICH_PROVIDER", None)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "examples" / "policy-reader"))
sys.path.insert(0, str(ROOT / "tests"))

from aiohttp.test_utils import TestClient, TestServer               # noqa: E402

import server as srv                                                # noqa: E402
from test_server_start_choice import enriched_hero                  # noqa: E402


class ReadCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        enriched_hero(Path(self.tmp.name))
        self.app = srv.build_app(dev=False, index_path=Path(self.tmp.name) / "index.json")
        self.s = self.app["session"]
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()
        self.ws = await self.client.ws_connect("/ws/audio")
        await self.ws.receive_json(timeout=5)
        self.audio: dict = {}
        self._saved = (dict(srv.PROMPT_TIMEOUT_S), srv.SECTION_END_MIN_GAP_S, srv.RESUME_CUE_AFTER_S)

    async def asyncTearDown(self):
        srv.PROMPT_TIMEOUT_S.update(self._saved[0])
        srv.SECTION_END_MIN_GAP_S, srv.RESUME_CUE_AFTER_S = self._saved[1], self._saved[2]
        await self.ws.close()
        await self.client.close()
        p = getattr(self.s.events, "path", None)
        if p and Path(p).exists():
            Path(p).unlink()
        self.tmp.cleanup()

    async def recv(self, timeout=6.0):
        m = await self.ws.receive_json(timeout=timeout)
        if m.get("type") == "audio":
            self.audio[m["context_id"]] = self.audio.get(m["context_id"], 0) + len(base64.b64decode(m["b64"]))
        return m

    async def until(self, pred, timeout=12.0):
        async def go():
            while True:
                m = await self.recv()
                if pred(m):
                    return m
        return await asyncio.wait_for(go(), timeout=timeout)

    async def ack_all(self, ctx):
        frames = self.audio[ctx] // 2
        ms = frames / 24000 * 1000
        await self.ws.send_json({"type": "rendered", "context_id": ctx, "rendered_ms": ms, "enqueued_frames": frames})
        await self.ws.send_json({"type": "unit_ended", "context_id": ctx, "enqueued_frames": frames, "rendered_ms": ms})

    async def hear(self, timeout=12.0):
        """The next unit, acked; the opening prompts are answered 'from the top'."""
        st = await self.until(lambda m: m.get("type") == "unit_started", timeout)
        await self.until(lambda m: m.get("type") == "unit_done" and m["context_id"] == st["context_id"], timeout)
        await self.ack_all(st["context_id"])
        if st["kind"] in ("pick_topic", "welcome"):
            await self.ws.send_json({"type": "ask", "question": "from the top"})
        elif st["kind"] == "start_choice":
            await self.ws.send_json({"type": "ask", "question": "brief"})     # the overview and the chips, as before
        return st

    async def read_until(self, pred, limit=80):
        """Play (or keep going) and ack units until `pred(unit)`; returns that unit."""
        for _ in range(limit):
            st = await self.hear()
            if pred(st):
                return st
        self.fail("unit not reached")

    def events(self, kind):
        return self.s.events.of_type(kind)


class TestSectionEnd(ReadCase):
    async def test_at_a_boundary_the_reader_asks_and_carry_on_reads_the_next_section(self):
        srv.SECTION_END_MIN_GAP_S = 0
        await self.ws.send_json({"type": "play"})
        st = await self.read_until(lambda u: u["kind"] == "section_end")
        self.assertEqual(st["text_display"], "That's Declarations. Next is Coverages and Limits. Carry on, or something else?")
        pm = await self.until(lambda m: m.get("type") == "prompt" and m.get("kind") == "section_end")
        self.assertEqual(pm["options"], ["carry_on", "topic", "question"])
        await self.ws.send_json({"type": "ask", "question": "carry on"})
        nxt = await self.until(lambda m: m.get("type") == "unit_started")
        self.assertEqual((nxt["kind"], nxt["section_title"]), ("clause", "Coverages and Limits"))
        self.assertEqual(self.events("prompt_resolved")[-1]["choice"], "carry_on")

    async def test_silence_carries_on_and_a_topic_jumps(self):
        srv.SECTION_END_MIN_GAP_S = 0
        srv.PROMPT_TIMEOUT_S["section_end"] = 0.4
        await self.ws.send_json({"type": "play"})
        await self.read_until(lambda u: u["kind"] == "section_end")
        nxt = await self.until(lambda m: m.get("type") == "unit_started")
        self.assertEqual((nxt["kind"], nxt["section_title"]), ("clause", "Coverages and Limits"))
        self.assertEqual(self.events("prompt_resolved")[-1]["by"], "timeout")
        # the next boundary: a topic named in reply jumps there
        srv.PROMPT_TIMEOUT_S["section_end"] = 5.0
        await self.until(lambda m: m.get("type") == "unit_done" and m["context_id"] == nxt["context_id"])
        await self.ack_all(nxt["context_id"])
        await self.read_until(lambda u: u["kind"] == "section_end")
        await self.until(lambda m: m.get("type") == "prompt" and m.get("kind") == "section_end")
        await self.ws.send_json({"type": "ask", "question": "exclusions"})
        jumped = await self.until(lambda m: m.get("type") == "jumped")
        self.assertEqual(jumped["heading"], "General Exclusions")

    async def test_never_within_the_gap_of_the_last_prompt(self):
        # The opening pick_topic prompt was just spoken: the first boundary stays quiet.
        await self.ws.send_json({"type": "play"})
        nxt = await self.read_until(lambda u: u["kind"] == "clause" and u["section_title"] == "Coverages and Limits")
        self.assertEqual([u["kind"] for u in [nxt]], ["clause"])
        # the first play's invitation, then the chips; no section_end within the gap
        self.assertEqual([e["kind"] for e in self.events("prompt_opened")], ["start_choice", "pick_topic"])

    async def test_never_right_after_a_jump_the_listener_asked_for(self):
        srv.SECTION_END_MIN_GAP_S = 0
        await self.ws.send_json({"type": "play"})
        first = await self.read_until(lambda u: u["kind"] == "clause")
        await self.ws.send_json({"type": "flush_ack", "context_id": first["context_id"], "rendered_ms": 100})
        await self.ws.send_json({"type": "ask", "question": "go to definitions"})
        await self.until(lambda m: m.get("type") == "jumped")
        # Definitions is read to its end, then the next section starts without a prompt.
        nxt = await self.read_until(lambda u: u["kind"] in ("section_end",) or (u["kind"] == "clause" and u["section_title"] == "Perils Insured Against"))
        self.assertEqual(nxt["kind"], "clause", "no section_end after the jump the listener asked for")
        self.assertNotIn("section_end", [e["kind"] for e in self.events("prompt_opened")])

    async def test_muted_after_two_timeouts(self):
        srv.SECTION_END_MIN_GAP_S = 0
        srv.PROMPT_TIMEOUT_S["section_end"] = 0.3
        await self.ws.send_json({"type": "play"})
        titles, ends = [], 0
        # Read through four sections: the first two boundaries ask (and time
        # out), the third asks nothing -- the kind is muted.
        while len(titles) < 4:
            u = await self.hear()
            if u["kind"] == "section_end":
                ends += 1
            elif u["kind"] == "clause" and (not titles or titles[-1] != u["section_title"]):
                titles.append(u["section_title"])
        self.assertEqual(ends, 2)
        self.assertEqual([e["kind"] for e in self.events("prompt_muted")], ["section_end"])
        self.assertIn("section_end", self.s.conv.muted)
        self.assertEqual([e["kind"] for e in self.events("prompt_opened")].count("section_end"), 2)


class TestResumeCue(ReadCase):
    async def test_play_after_a_long_pause_says_where_we_were(self):
        srv.RESUME_CUE_AFTER_S = 0.2
        await self.ws.send_json({"type": "play"})
        first = await self.read_until(lambda u: u["kind"] == "clause")
        nxt = await self.until(lambda m: m.get("type") == "unit_started" and m.get("kind") == "clause")
        await self.until(lambda m: m.get("type") == "unit_done" and m["context_id"] == nxt["context_id"])
        await self.ws.send_json({"type": "flush_ack", "context_id": nxt["context_id"], "rendered_ms": 200})
        await self.ws.send_json({"type": "pause"})
        await self.until(lambda m: m.get("type") == "paused")
        await asyncio.sleep(0.4)
        await self.ws.send_json({"type": "play"})
        cue = await self.until(lambda m: m.get("type") == "unit_started")
        self.assertEqual((cue["kind"], cue["text_display"]), ("cue", "We were in Declarations. Carrying on."))
        await self.until(lambda m: m.get("type") == "unit_done" and m["context_id"] == cue["context_id"])
        await self.ack_all(cue["context_id"])
        res = await self.until(lambda m: m.get("type") == "unit_started" and m.get("kind") == "clause")
        self.assertEqual(res["unit_id"], nxt["unit_id"], "the cut clause, after the cue")
        self.assertEqual(self.events("resume_cue")[0]["section"], self.s.library.current.grounding.sections[0]["id"])

    async def test_a_short_pause_has_no_cue(self):
        await self.ws.send_json({"type": "play"})
        first = await self.read_until(lambda u: u["kind"] == "clause")
        nxt = await self.until(lambda m: m.get("type") == "unit_started" and m.get("kind") == "clause")
        await self.until(lambda m: m.get("type") == "unit_done" and m["context_id"] == nxt["context_id"])
        await self.ws.send_json({"type": "flush_ack", "context_id": nxt["context_id"], "rendered_ms": 200})
        await self.ws.send_json({"type": "pause"})
        await self.until(lambda m: m.get("type") == "paused")
        await self.ws.send_json({"type": "play"})
        res = await self.until(lambda m: m.get("type") == "unit_started")
        self.assertEqual(res["kind"], "clause")
        self.assertEqual(self.events("resume_cue"), [])


if __name__ == "__main__":
    unittest.main()
