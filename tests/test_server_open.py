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

    async def ack_all(self, ctx):
        frames = self.audio[ctx] // 2
        ms = self.audio_ms(ctx)
        await self.ws.send_json({"type": "rendered", "context_id": ctx, "rendered_ms": ms,
                                 "enqueued_frames": frames})
        await self.ws.send_json({"type": "unit_ended", "context_id": ctx,
                                 "enqueued_frames": frames, "rendered_ms": ms})


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
        while True:
            started = await self.a.until(lambda m: m.get("type") == "unit_started")
            ctx = started["context_id"]
            await self.a.until(lambda m: m.get("type") == "unit_done" and m["context_id"] == ctx)
            if started.get("kind", "clause") != "clause":
                await self.a.ack_all(ctx)        # a document map plays before the first clause
                continue
            return started

    async def answer_flush(self, ctx, ms):
        await self.a.until(lambda m: m.get("type") == "flush")
        await self.a.ws.send_json({"type": "flush_ack", "context_id": ctx, "rendered_ms": ms})

    async def test_open_from_the_rail_flushes_the_voice_and_starts_the_new_document_clean(self):
        docs = [d["name"] for d in self.hello["documents"]]
        self.assertGreaterEqual(len(docs), 2, "the library needs two documents for this")
        # Start on the hero fixture (short clauses) so the flush is answered
        # long before the server stops waiting for it, then leave it.
        first = "policy"
        await self.a.ws.send_json({"type": "open", "name": first})
        await self.a.until(lambda m: m.get("type") == "document_opened")
        other = next(d for d in docs if d != first)
        started = await self.play_one()
        ctx = started["context_id"]
        cut = self.a.audio_ms(ctx) * 0.3

        # What the client does: flush locally, ack the playhead, then open.
        await self.a.ws.send_json({"type": "flush_ack", "context_id": ctx, "rendered_ms": cut})
        await self.a.ws.send_json({"type": "open", "name": other})
        opened = await self.a.until(lambda m: m.get("type") == "document_opened")
        self.assertEqual(opened["name"], other)
        self.assertEqual(self.s.events.of_type("flush_ack_timeout"), [], "the flush ack must be waited for")
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

    async def test_an_uploaded_document_is_in_the_library_at_once_and_play_reads_it(self):
        started = await self.play_one()
        ctx = started["context_id"]
        from aiohttp import FormData
        body = "\n\n".join(
            f"# Heading {i}\n\nUploaded paragraph number {i} that is comfortably longer than "
            f"the minimum clause length so that the merge rule leaves it alone." for i in range(1, 26))
        fd = FormData()
        fd.add_field("file", body.encode("utf-8"), filename="_ingest_open_test.md", content_type="text/plain")
        idx = self.s.library.index_path
        before = idx.read_text(encoding="utf-8")
        self.addCleanup(lambda: idx.write_text(before, encoding="utf-8"))     # the real registry is restored
        r = await self.client.post("/documents", data=fd)
        self.assertEqual(r.status, 200)
        frames = [json.loads(l[5:]) for l in (await r.text()).splitlines() if l.startswith("data:")]
        entry = frames[-1]["entry"]
        root = self.s.library.root
        for f in (root / f"{entry['name']}.json", root / f"{entry['name']}.ingest_report.json"):
            self.addCleanup(lambda f=f: f.unlink(missing_ok=True))
        self.assertFalse(entry["reviewed"])
        changed = await self.a.until(lambda m: m.get("type") == "library_changed")
        self.assertIn(entry["name"], [d["name"] for d in changed["documents"]])

        # Open it from the rail while the old one sounds: the client acks the
        # playhead first, then opens.
        await self.a.ws.send_json({"type": "flush_ack", "context_id": ctx,
                                   "rendered_ms": self.a.audio_ms(ctx) * 0.5})
        await self.a.ws.send_json({"type": "open", "name": entry["name"]})
        opened = await self.a.until(lambda m: m.get("type") == "document_opened")
        self.assertEqual(opened["name"], entry["name"])
        self.assertTrue(next(x for x in opened["documents"] if x["name"] == entry["name"])["unreviewed"])
        self.assertEqual(self.s.events.of_type("unit_truncated")[0]["reason"], "open")

        nxt = await self.play_one()
        # The first spoken unit of the upload is its first heading's signpost;
        # the body paragraph follows it.
        uploaded = json.loads((root / f"{entry['name']}.json").read_text(encoding="utf-8"))
        self.assertIn(nxt["unit_id"], {c["id"] for c in uploaded["clauses"]}, "play reads the upload")
        self.assertRegex(nxt["text_display"], r"^Heading 1\. \d+ items?\.$")
        self.assertGreater(self.a.audio.get(nxt["context_id"], 0), 0)


class OpenFromOtherTab(OpenCase):
    async def test_open_from_a_tab_without_the_voice_asks_the_sink_to_flush(self):
        b = Tab(await self.client.ws_connect("/ws/audio"))
        await b.recv()                                       # hello
        await self.a.ws.send_json({"type": "open", "name": "policy"})
        await self.a.until(lambda m: m.get("type") == "document_opened")
        started = await self.play_one()
        ctx = started["context_id"]
        other = next(d["name"] for d in self.hello["documents"] if d["name"] != "policy")
        await b.ws.send_json({"type": "open", "name": other})  # no ack: B has no audio
        await self.a.until(lambda m: m.get("type") == "flush")
        await self.a.ws.send_json({"type": "flush_ack", "context_id": ctx,
                                   "rendered_ms": self.a.audio_ms(ctx) * 0.4})
        opened = await self.a.until(lambda m: m.get("type") == "document_opened")
        self.assertEqual(opened["name"], other)
        self.assertEqual(self.s.events.of_type("flush_ack_timeout"), [])
        self.assertEqual(self.s.events.of_type("unit_truncated")[0]["context_id"], started["unit_id"])
        await b.ws.close()


if __name__ == "__main__":
    unittest.main()
