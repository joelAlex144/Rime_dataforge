"""Switching documents while something is sounding.

Opening another document -- from the rail, or an upload on /dev -- stopped
synthesis but never flushed the tab with the voice, so the old document's
buffered audio played out and the new one queued behind it: "the audio being
played is not the one from the pdf uploaded".
"""
import asyncio
import base64
import json
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

UNREVIEWED = ROOT / "examples" / "policy-reader" / "fixtures" / "unreviewed"


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

    def audio_ms(self, ctx):
        return self.audio[ctx] / 2 / 24000 * 1000


class OpenCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.app = srv.build_app(dev=False)
        self.s = self.app["session"]
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()
        self.a = Tab(await self.client.ws_connect("/ws/audio"))
        self.hello = await self.a.recv()

    async def asyncTearDown(self):
        if not self.a.ws.closed:
            await self.a.ws.close()
        await self.client.close()
        p = getattr(self.s.events, "path", None)
        if p and Path(p).exists():
            Path(p).unlink()

    async def play_one(self):
        await self.a.ws.send_json({"type": "play"})
        started = await self.a.until(lambda m: m.get("type") == "unit_started")
        await self.a.until(lambda m: m.get("type") == "unit_done" and m["context_id"] == started["context_id"])
        return started

    async def answer_flush(self, ctx, ms):
        await self.a.until(lambda m: m.get("type") == "flush")
        await self.a.ws.send_json({"type": "flush_ack", "context_id": ctx, "rendered_ms": ms})

    async def test_open_from_the_rail_flushes_the_voice_and_starts_the_new_document_clean(self):
        docs = [d["name"] for d in self.hello["documents"]]
        self.assertGreaterEqual(len(docs), 2, "the library needs two documents for this")
        first = self.hello["current"]
        other = next(d for d in docs if d != first)
        started = await self.play_one()
        ctx = started["context_id"]
        cut = self.a.audio_ms(ctx) * 0.3

        await self.a.ws.send_json({"type": "open", "name": other})
        await self.answer_flush(ctx, cut)             # the voice is asked to flush first
        opened = await self.a.until(lambda m: m.get("type") == "document_opened")
        self.assertEqual(opened["name"], other)
        trunc = self.s.events.of_type("unit_truncated")[0]
        self.assertEqual(trunc["reason"], "open")
        self.assertEqual(trunc["context_id"], started["unit_id"])
        # The document being left keeps its position at the playhead.
        left = self.s.library._docs[first].session
        self.assertTrue(left.ledger[started["unit_id"]].startswith("truncated@"))

        nxt = await self.play_one()
        self.assertNotEqual(nxt["unit_id"], started["unit_id"])
        self.assertEqual(nxt["index"], 0, "the new document from its start")
        self.assertIn(nxt["unit_id"], self.s.library.current.grounding.by_id)
        self.assertEqual(self.s.library.current.name, other)

    async def test_uploading_on_dev_opens_the_upload_and_play_reads_it(self):
        started = await self.play_one()
        ctx = started["context_id"]

        sample = ROOT / "traces" / "_ingest_open_test.md"
        sample.write_text("\n\n".join(
            f"# Heading {i}\n\nUploaded paragraph number {i} that is comfortably longer than "
            f"the minimum clause length so that the merge rule leaves it alone." for i in range(1, 26)),
            encoding="utf-8")
        self.addCleanup(lambda: sample.unlink(missing_ok=True))
        r = await self.client.post("/api/dev/ingest", json={"url": str(sample)})
        d = await r.json()
        self.assertEqual(r.status, 200, d)
        out = ROOT / d["path"]
        self.addCleanup(lambda: out.unlink(missing_ok=True))

        # What the /dev page does on success: open it, while the old one sounds.
        opening = asyncio.ensure_future(
            self.client.post("/api/dev/open?unreviewed=1", json={"name": d["name"]}))
        await self.answer_flush(ctx, self.a.audio_ms(ctx) * 0.5)
        r2 = await opening
        self.assertEqual(r2.status, 200, await r2.text())
        opened = await self.a.until(lambda m: m.get("type") == "document_opened")
        self.assertEqual(opened["name"], d["name"])
        self.assertTrue(next(x for x in opened["documents"] if x["name"] == d["name"])["unreviewed"])
        self.assertEqual(self.s.events.of_type("unit_truncated")[0]["reason"], "open")

        nxt = await self.play_one()
        uploaded = json.loads(out.read_text(encoding="utf-8"))
        ids = {c["id"] for c in uploaded["clauses"]}
        self.assertIn(nxt["unit_id"], ids, "play reads the uploaded document")
        self.assertIn("Uploaded paragraph", nxt["text_display"])
        self.assertGreater(self.a.audio.get(nxt["context_id"], 0), 0)


if __name__ == "__main__":
    unittest.main()
