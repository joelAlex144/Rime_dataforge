"""One audio sink per session.

Two tabs on one session were two voices: every socket got the audio, and a
pause on one tab left the other draining its buffer. Now the tab that pressed
play is the only socket that receives audio, a stop from any other tab asks
that sink to flush and waits for its playhead, and play from another tab hands
the voice over at the sentence being heard.
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


class Tab:
    def __init__(self, ws):
        self.ws = ws
        self.seen: list = []
        self.audio: dict = {}

    async def recv(self, timeout=5.0):
        m = await self.ws.receive_json(timeout=timeout)
        self.seen.append(m)
        if m.get("type") == "audio":
            self.audio[m["context_id"]] = self.audio.get(m["context_id"], 0) + len(base64.b64decode(m["b64"]))
        return m

    async def until(self, pred, timeout=8.0):
        async def go():
            while True:
                m = await self.recv()
                if pred(m):
                    return m
        return await asyncio.wait_for(go(), timeout=timeout)

    async def drain(self, seconds=0.8):
        """Read whatever arrives for a while; returns the types."""
        end = asyncio.get_running_loop().time() + seconds
        while True:
            left = end - asyncio.get_running_loop().time()
            if left <= 0:
                break
            try:
                await asyncio.wait_for(self.recv(timeout=left), timeout=left)
            except asyncio.TimeoutError:
                break
        return [m.get("type") for m in self.seen]

    def audio_ms(self, ctx):
        return self.audio[ctx] / 2 / 24000 * 1000

    async def ack_all(self, ctx):
        frames = self.audio[ctx] // 2
        ms = self.audio_ms(ctx)
        await self.ws.send_json({"type": "rendered", "context_id": ctx, "rendered_ms": ms,
                                 "enqueued_frames": frames})
        await self.ws.send_json({"type": "unit_ended", "context_id": ctx,
                                 "enqueued_frames": frames, "rendered_ms": ms})


class TwoTabs(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.app = srv.build_app(dev=False)
        self.s = self.app["session"]
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()
        self.a = Tab(await self.client.ws_connect("/ws/audio"))
        self.b = Tab(await self.client.ws_connect("/ws/audio"))
        self.hello_a = await self.a.recv()
        self.hello_b = await self.b.recv()

    async def asyncTearDown(self):
        for t in (self.a, self.b):
            if not t.ws.closed:
                await t.ws.close()
        await self.client.close()
        p = getattr(self.s.events, "path", None)
        if p and Path(p).exists():
            Path(p).unlink()

    async def a_plays_one_clause(self):
        await self.a.ws.send_json({"type": "play"})
        while True:
            started = await self.a.until(lambda m: m.get("type") == "unit_started")
            ctx = started["context_id"]
            await self.a.until(lambda m: m.get("type") == "unit_done" and m["context_id"] == ctx)
            if started.get("kind", "clause") != "clause":
                await self.a.ack_all(ctx)        # the document map, heard first
                continue
            return started, ctx

    async def test_audio_goes_only_to_the_tab_that_pressed_play(self):
        self.assertFalse(self.hello_a["sink"])
        started, ctx = await self.a_plays_one_clause()
        self.assertGreater(self.a.audio.get(ctx, 0), 0)
        sink_a = next(m for m in self.a.seen if m.get("type") == "sink")
        self.assertTrue(sink_a["you"])
        types_b = await self.b.drain(0.8)
        self.assertIn("unit_started", types_b, "the other tab still sees the state")
        self.assertIn("playing", types_b)
        self.assertNotIn("audio", types_b, "and hears nothing")
        sink_b = next(m for m in self.b.seen if m.get("type") == "sink")
        self.assertFalse(sink_b["you"])
        self.assertTrue(sink_b["any"])

    async def test_pause_from_the_other_tab_flushes_the_sink_and_uses_its_playhead(self):
        started, ctx = await self.a_plays_one_clause()
        cut = self.a.audio_ms(ctx) * 0.4
        await self.b.ws.send_json({"type": "pause"})              # no flush_ack: B has no audio
        flush = await self.a.until(lambda m: m.get("type") == "flush")
        self.assertEqual(flush, {"type": "flush"})
        await self.a.ws.send_json({"type": "flush_ack", "context_id": ctx, "rendered_ms": cut})
        boundary = await self.b.until(lambda m: m.get("type") == "boundary")
        self.assertEqual(boundary["unit_id"], started["unit_id"])
        self.assertEqual(boundary["context_id"], ctx)
        self.assertLess(boundary["char_end"], boundary["of"])
        await self.a.until(lambda m: m.get("type") == "paused")
        self.assertFalse(self.s.playing)
        self.assertEqual(self.s.events.of_type("pause_without_flush_ack"), [])
        self.assertEqual(self.s.events.of_type("flush_ack_timeout"), [])
        trunc = self.s.events.of_type("unit_truncated")[0]
        self.assertEqual(trunc["reason"], "pause")
        self.assertAlmostEqual(trunc["rendered_ms"], round(cut, 1), places=0)

    async def test_play_from_the_other_tab_hands_the_voice_over_at_the_sentence(self):
        started, ctx = await self.a_plays_one_clause()
        cut = self.a.audio_ms(ctx) * 0.5
        await self.b.ws.send_json({"type": "play"})
        await self.a.until(lambda m: m.get("type") == "flush")
        await self.a.ws.send_json({"type": "flush_ack", "context_id": ctx, "rendered_ms": cut})
        sink_b = await self.b.until(lambda m: m.get("type") == "sink" and m.get("you"))
        self.assertTrue(sink_b["you"])
        resumed = await self.b.until(lambda m: m.get("type") == "unit_started")
        self.assertEqual(resumed["unit_id"], started["unit_id"], "picked up on the new tab")
        rp = next(m for m in self.b.seen if m.get("type") == "resume_point")
        self.assertEqual(resumed["char_start"], rp["char_start"])
        await self.b.until(lambda m: m.get("type") == "audio")
        # The old tab: told it is no longer the sink, and no audio after the flush.
        await self.a.drain(0.6)
        flush_at = next(i for i, m in enumerate(self.a.seen) if m.get("type") == "flush")
        after = [m.get("type") for m in self.a.seen[flush_at + 1:]]
        self.assertNotIn("audio", after)
        self.assertIn("sink", after)
        self.assertFalse([m for m in self.a.seen if m.get("type") == "sink"][-1]["you"])
        self.assertEqual(self.s.events.of_type("unit_truncated")[0]["reason"], "handover")

    async def test_acks_and_flush_acks_from_a_non_sink_are_ignored(self):
        started, ctx = await self.a_plays_one_clause()
        st = self.s.contexts[ctx]
        await self.b.ws.send_json({"type": "flush_ack", "context_id": ctx, "rendered_ms": 999.0})
        await self.b.ws.send_json({"type": "rendered", "context_id": ctx, "rendered_ms": 999.0,
                                   "enqueued_frames": 1})
        await self.b.drain(0.4)
        self.assertEqual(self.s.events.of_type("flush_ack"), [])
        self.assertEqual(st.rendered_ms, 0.0)

    async def test_sink_disconnect_stops_reading_and_tells_the_other_tab(self):
        started, ctx = await self.a_plays_one_clause()
        await self.a.ws.close()
        await self.b.until(lambda m: m.get("type") == "paused")
        sink_b = await self.b.until(lambda m: m.get("type") == "sink")
        self.assertFalse(sink_b["any"])
        self.assertFalse(self.s.playing)
        self.assertIsNone(self.s.sink)
        self.assertEqual(self.s.events.of_type("unit_truncated")[0]["reason"], "sink_left")


if __name__ == "__main__":
    unittest.main()
