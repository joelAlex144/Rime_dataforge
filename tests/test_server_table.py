"""table_choice: a table reached while reading is a prompt, not a stub sentence.

Uses the arogya_sanjeevani fixture, whose first table (sec-1-t1) has 8 rows
that are spoken only on request. A row label speaks that row as a `row` unit
(acked, in the ledger as heard) and asks "Another, or carry on?"; "all" reads
every row in order; silence carries on after the table with the rows still
skipped:table_on_request.
"""
import asyncio
import base64
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

os.environ["TTS_PROVIDER"] = "fake"
os.environ.pop("LLM_API_KEY", None)
os.environ.pop("LLM_PROVIDER", None)
os.environ.pop("ENRICH_PROVIDER", None)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "examples" / "policy-reader"))

from aiohttp.test_utils import TestClient, TestServer               # noqa: E402

import llm as llm_mod                                               # noqa: E402
import server as srv                                                # noqa: E402

STUB = "sec-1-t1"
ROWS = [f"{STUB}-r{i}" for i in range(1, 9)]


class TableCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.env = mock.patch.dict(os.environ, {"ENRICH_PROVIDER": ""})
        self.env.start()
        self.app = srv.build_app(dev=False, warm_converter=False)
        self.s = self.app["session"]
        self.s.library.open("arogya_sanjeevani")
        self.s._opened_by_listener.add("arogya_sanjeevani")          # chosen: no welcome re-routes it
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()
        self.ws = await self.client.ws_connect("/ws/audio")
        await self.ws.receive_json(timeout=5)
        self.audio: dict = {}
        self.classify = mock.patch.object(llm_mod, "classify", side_effect=AssertionError("classify called"))
        self.classify.start()
        self._timeouts = dict(srv.PROMPT_TIMEOUT_S)

    async def asyncTearDown(self):
        srv.PROMPT_TIMEOUT_S.update(self._timeouts)
        self.classify.stop()
        self.env.stop()
        await self.ws.close()
        await self.client.close()
        p = getattr(self.s.events, "path", None)
        if p and Path(p).exists():
            Path(p).unlink()

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

    async def hear(self, pred=None):
        """The next unit, acked once done; returns its unit_started."""
        st = await self.until(lambda m: m.get("type") == "unit_started" and (pred is None or pred(m)))
        await self.until(lambda m: m.get("type") == "unit_done" and m["context_id"] == st["context_id"])
        await self.ack_all(st["context_id"])
        return st

    async def play_to_table(self):
        """Play; ack the map and the clauses before the table; return the
        table_choice unit and the prompt message."""
        await self.ws.send_json({"type": "play"})
        while True:
            st = await self.hear()
            if st["kind"] in ("pick_topic", "welcome", "start_choice"):            # the opening prompts: read on
                await self.ws.send_json({"type": "ask", "question": {"pick_topic": "from the top", "welcome": "the first one", "start_choice": "brief"}[st["kind"]]})
                continue
            if st["kind"] == "table_choice":
                pm = await self.until(lambda m: m.get("type") == "prompt" and m.get("kind") == "table_choice")
                return st, pm

    def ledger(self):
        return self.s.library.current.session.ledger

    async def ledger_says(self, unit_id, value, timeout=2.0):
        """The ack is processed by the server after we sent it; give it a moment."""
        t0 = asyncio.get_event_loop().time()
        while self.ledger().get(unit_id) != value and asyncio.get_event_loop().time() - t0 < timeout:
            await asyncio.sleep(0.05)
        return self.ledger().get(unit_id)


class TestTableChoice(TableCase):
    async def test_the_prompt_names_the_table_and_its_first_six_rows(self):
        st, pm = await self.play_to_table()
        self.assertEqual(st["unit_id"], STUB, "the prompt stands for the stub")
        self.assertTrue(st["text_display"].startswith("Here there's a table of Document type to Source URL, 8 rows: "
                                                      "Document type, Issuer / source, UIN / reference, Source pages, "
                                                      "Word count (body), Section headings."), st["text_display"])
        self.assertTrue(st["text_display"].endswith("Want one of them, all of them, or shall I carry on?"))
        self.assertEqual(pm["options"], ["row", "all", "carry_on"])
        self.assertEqual(pm["labels"][:2], ["Document type", "Issuer / source"])
        self.assertEqual(await self.ledger_says(STUB, "heard"), "heard", "the stub is heard as its prompt")
        self.assertTrue(all(self.ledger()[r] == "skipped:table_on_request" for r in ROWS))

    async def test_a_row_by_label_is_spoken_then_another_or_carry_on(self):
        await self.play_to_table()
        await self.ws.send_json({"type": "ask", "question": "issuer"})
        row = await self.hear(lambda m: m.get("kind") == "row")
        self.assertEqual(row["unit_id"], f"{STUB}-r2")
        self.assertTrue(row["text_display"].startswith("Issuer / source: Reliance General"))
        self.assertEqual(await self.ledger_says(f"{STUB}-r2", "heard"), "heard")
        again = await self.hear(lambda m: m.get("kind") == "table_choice")
        self.assertEqual(again["text_display"], "Another, or carry on?")
        await self.until(lambda m: m.get("type") == "prompt" and m.get("kind") == "table_choice")
        await self.ws.send_json({"type": "ask", "question": "the fourth one"})
        row4 = await self.hear(lambda m: m.get("kind") == "row")
        self.assertEqual(row4["unit_id"], f"{STUB}-r4")
        await self.hear(lambda m: m.get("kind") == "table_choice")
        await self.until(lambda m: m.get("type") == "prompt" and m.get("kind") == "table_choice")
        await self.ws.send_json({"type": "ask", "question": "carry on"})
        nxt = await self.until(lambda m: m.get("type") == "unit_started")
        self.assertEqual(nxt["kind"], "clause")
        self.assertGreater(nxt["index"], 10, "reading continues after the table")
        self.assertEqual(self.ledger()[f"{STUB}-r1"], "skipped:table_on_request")
        # (the opening pick_topic reply comes first; the table's three replies follow)
        routed = [(e["route"], e["choice"]) for e in self.s.events.of_type("reply_routed")]
        self.assertEqual(routed[-3:], [("pending", "row"), ("pending", "row"), ("pending", "carry_on")])
        self.assertEqual([(e["by"], e["choice"]) for e in self.s.events.of_type("prompt_resolved")][-3:],
                         [("reply", "row"), ("reply", "row"), ("reply", "carry_on")])

    async def test_all_reads_every_row_in_order_then_continues(self):
        await self.play_to_table()
        await self.ws.send_json({"type": "ask", "question": "all of them"})
        ids = []
        for _ in ROWS:
            row = await self.hear(lambda m: m.get("kind") == "row")
            ids.append(row["unit_id"])
        self.assertEqual(ids, ROWS)
        nxt = await self.until(lambda m: m.get("type") == "unit_started")
        self.assertEqual(nxt["kind"], "clause")
        self.assertGreater(nxt["index"], 10)
        await self.ledger_says(ROWS[-1], "heard")
        self.assertTrue(all(self.ledger()[r] == "heard" for r in ROWS))

    async def test_silence_carries_on_after_the_table(self):
        srv.PROMPT_TIMEOUT_S["table_choice"] = 0.4
        await self.play_to_table()
        nxt = await self.until(lambda m: m.get("type") == "unit_started")
        self.assertEqual(nxt["kind"], "clause")
        self.assertGreater(nxt["index"], 10)
        self.assertTrue(all(self.ledger()[r] == "skipped:table_on_request" for r in ROWS))
        self.assertEqual(self.s.events.of_type("prompt_resolved")[-1]["by"], "timeout")


if __name__ == "__main__":
    unittest.main()
