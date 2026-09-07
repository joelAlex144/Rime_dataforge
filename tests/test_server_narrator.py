"""The interactive voice runs whenever a document is processed, on both
paths, taking the voice over if it has to.

(1) An upload while a document is being read: the reading is cut at the
    playhead (unit_truncated reason=upload), the companion narrates the
    stages, at least five lines, no cue_skipped, no silence over 15 s.
(2) Opening a document with no navigator, a 20 s generation: the same.
(3) No tab holds the voice: nothing is spoken; the first play is told what
    was processed meanwhile.
"""
import asyncio
import json
import os
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

os.environ["TTS_PROVIDER"] = "fake"
os.environ.pop("LLM_API_KEY", None)
os.environ.pop("LLM_PROVIDER", None)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "examples" / "policy-reader"))
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import companion as cp                                              # noqa: E402
import enrich as enrich_mod                                         # noqa: E402
import ingest as ingest_mod                                         # noqa: E402
import server as srv                                                # noqa: E402
from test_server_navigator import NavigatorCase, fake_enrich_fixture, failing_enrich_fixture, slow_ingest  # noqa: E402


def slow_fake(delay_s: float):
    def run(path, provider, force=False, log=None, fields=None):
        time.sleep(delay_s)
        return fake_enrich_fixture(path, provider, force=force, log=log, fields=fields)
    return run


class NarratorCase(NavigatorCase):
    READING = "arogya_sanjeevani"          # a committed fixture with its navigator: the first play invites, "from the start" reads

    async def start_reading_policy(self):
        """Claim the voice, open a ready document, answer its invitation with
        "from the start" and leave the first clause sounding, unacked."""
        await self.a.ws.send_json({"type": "interrupt"})
        await self.a.until(lambda m: m.get("type") == "sink")
        await self.a.ws.send_json({"type": "open", "name": self.READING})
        await self.a.until(lambda m: m.get("type") == "document_opened" and m["name"] == self.READING)
        await self.a.ws.send_json({"type": "play"})
        st = await self.a.until(lambda m: m.get("type") == "unit_started" and m.get("kind") == "start_choice", timeout=20)
        await self.a.until(lambda m: m.get("type") == "unit_done" and m["context_id"] == st["context_id"], timeout=20)
        await self.a.ack_all(st["context_id"])
        await self.a.until(lambda m: m.get("type") == "prompt" and m.get("kind") == "start_choice", timeout=20)
        await self.a.ws.send_json({"type": "ask", "question": "from the start"})
        clause = await self.a.until(lambda m: m.get("type") == "unit_started" and m.get("kind") == "clause", timeout=20)
        await self.a.until(lambda m: m.get("type") == "unit_done" and m["context_id"] == clause["context_id"], timeout=20)
        return clause

    async def follow(self, until_task, sounding_ctx, timeout=90.0):
        """Read the socket until `until_task` is done and start_choice has been
        heard: answer a flush request with the playhead, ack every unit, and
        collect what was spoken."""
        spoken = []
        deadline = time.monotonic() + timeout
        invited = False
        while time.monotonic() < deadline and not (until_task.done() and invited):
            try:
                m = await self.a.recv(timeout=1.0)
            except asyncio.TimeoutError:
                continue
            if m.get("type") == "flush":
                frames = self.a.audio.get(sounding_ctx, 0) // 2
                await self.a.ws.send_json({"type": "flush_ack", "context_id": sounding_ctx,
                                           "rendered_ms": frames / 24000 * 1000 * 0.4})
            elif m.get("type") == "unit_started":
                spoken.append((round(time.monotonic(), 2), m["kind"], m["text_display"]))
            elif m.get("type") == "unit_done":
                await self.a.ack_all(m["context_id"])
            elif m.get("type") == "prompt" and m.get("kind") == "start_choice":
                invited = True
        return spoken

    def companion_lines(self):
        return [e for e in self.s.events.of_type("companion_spoken")]


class TestVoiceTakeover(NarratorCase):
    async def test_an_upload_while_reading_takes_the_voice_and_narrates(self):
        clause = await self.start_reading_policy()
        with mock.patch.object(enrich_mod, "enrich_fixture", fake_enrich_fixture), \
             mock.patch.object(ingest_mod, "ingest_document", slow_ingest(4.0, ("structure", "segment"))):
            up = asyncio.ensure_future(self.upload("_narr_upload_test"))
            spoken = await self.follow(up, clause["context_id"])
            frames, entry = await up
        trunc = self.s.events.of_type("unit_truncated")
        self.assertTrue(trunc and trunc[0]["reason"] == "upload", trunc)
        self.assertEqual(self.s.events.of_type("cue_skipped"), [])
        lines = self.companion_lines()
        self.assertGreaterEqual(len(lines), 5, [(l["source"], l["text"]) for l in lines])
        self.assertEqual(lines[0]["source"], "takeover")
        self.assertTrue(lines[0]["text"].startswith("I'll pause here and go through "), lines[0]["text"])
        self.assertTrue(all(l["document"] in ("_narr_upload_test", entry["name"]) for l in lines), lines)
        gap = [g for g in self.s.events.of_type("narration_gap_ms") if g["document"] == entry["name"]][-1]
        self.assertLess(gap["max"], 15000, gap)
        self.assertEqual([k for _, k, _ in spoken if k == "start_choice"], ["start_choice"])
        # the document being read kept its position at the playhead
        self.assertTrue(str(self.s.library._docs[self.READING].session.ledger.get(clause["unit_id"], "")).startswith("truncated@"))

    async def test_opening_an_unenriched_document_takes_the_voice_and_narrates(self):
        with mock.patch.object(enrich_mod, "enrich_fixture", failing_enrich_fixture):
            frames, entry = await self.upload("_narr_open_test")
        self.s._enriching.clear()
        self.s.conv.narrators.clear()
        clause = await self.start_reading_policy()
        with mock.patch.object(enrich_mod, "enrich_fixture", slow_fake(20.0)):
            # the sink's own client: the playhead first, then the open
            frames_n = self.a.audio.get(clause["context_id"], 0) // 2
            await self.a.ws.send_json({"type": "flush_ack", "context_id": clause["context_id"],
                                       "rendered_ms": frames_n / 24000 * 1000 * 0.4})
            await self.a.ws.send_json({"type": "open", "name": entry["name"]})
            await self.a.until(lambda m: m.get("type") == "document_opened" and m["name"] == entry["name"])
            t = self.s._enriching[entry["name"]]
            spoken = await self.follow(t, clause["context_id"], timeout=60)
        trunc = self.s.events.of_type("unit_truncated")
        self.assertTrue(trunc and trunc[0]["reason"] == "open", trunc)
        self.assertEqual(self.s.events.of_type("cue_skipped"), [])
        self.assertEqual(self.s.events.of_type("enrich_timeout"), [])
        # (the upload with no voice was told at the first play: a backlog line, not this narration)
        lines = [l for l in self.companion_lines() if l["document"] == entry["name"] and l["source"] != "backlog"]
        self.assertGreaterEqual(len(lines), 5, [(l["source"], l["text"]) for l in lines])
        self.assertEqual([l["source"] for l in lines[:2]], ["takeover", "event"])
        self.assertTrue(lines[1]["text"].startswith("I can see 25 sections: Heading 1, Heading 2, Heading 3, and more."), lines[1]["text"])
        self.assertTrue(any(l["source"] == "filler" for l in lines), "a filler bridged the generation")
        self.assertTrue(self.s.events.of_type("sections_found"), "the headings went to the client as sections_found")
        gap = [g for g in self.s.events.of_type("narration_gap_ms") if g["document"] == entry["name"]][-1]
        self.assertLess(gap["max"], 15000, gap)
        self.assertEqual([k for _, k, _ in spoken if k == "start_choice"], ["start_choice"])

    async def test_with_no_voice_nothing_is_said_and_the_first_play_tells_the_backlog(self):
        with mock.patch.object(enrich_mod, "enrich_fixture", fake_enrich_fixture):
            frames, entry = await self.upload("_narr_quiet_test")
        self.assertEqual(self.s.events.of_type("companion_spoken"), [])
        self.assertEqual(self.s.events.of_type("unit_started"), [] if self.s.events.of_type("unit_started") else [])
        self.assertEqual(self.s.events.of_type("narration_pending")[0]["document"], entry["name"])
        self.assertIn(entry["name"], self.s.conv.pending_narration)
        # a play: the voice is claimed and the backlog comes first
        await self.a.ws.send_json({"type": "play"})
        first = await self.a.until(lambda m: m.get("type") == "unit_started", timeout=15)
        self.assertEqual(first["kind"], "companion")
        self.assertEqual(first["text_display"], f"While you were away I went through {entry['spoken_title']}. It has 25 sections.")
        self.assertEqual(self.s.events.of_type("backlog_spoken")[0]["document"], entry["name"])
        self.assertEqual(self.s.conv.pending_narration, {})


if __name__ == "__main__":
    unittest.main()
