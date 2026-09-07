"""end_choice and the recap.

At the end of the document: "That's the end. Want any section again, or a
recap of what we covered?" (8 s; silence stops). "recap" speaks what the
ledger says -- sections heard in full, partly heard, not heard -- with no
model; a section name jumps back in.
"""
import asyncio
import base64
import os
import sys
import tempfile
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
from test_server_section_end import ReadCase                        # noqa: E402


class TestEndChoice(ReadCase):
    async def to_the_end(self):
        """Jump to the last section, read it out; returns the end_choice unit."""
        await self.ws.send_json({"type": "play"})
        first = await self.read_until(lambda u: u["kind"] == "clause")
        await self.ws.send_json({"type": "flush_ack", "context_id": first["context_id"], "rendered_ms": 100})
        await self.ws.send_json({"type": "ask", "question": "go to general conditions"})
        await self.until(lambda m: m.get("type") == "jumped")
        return await self.read_until(lambda u: u["kind"] == "end_choice", limit=120)

    async def test_the_recap_is_built_from_the_ledger(self):
        st = await self.to_the_end()
        self.assertEqual(st["text_display"], "That's the end. Want any section again, or a recap of what we covered?")
        pm = await self.until(lambda m: m.get("type") == "prompt" and m.get("kind") == "end_choice")
        self.assertEqual(pm["options"], ["section", "recap", "stop"])
        await self.ws.send_json({"type": "ask", "question": "recap"})
        recap = await self.until(lambda m: m.get("type") == "unit_started" and m.get("kind") == "recap")
        text = recap["text_display"]
        # General Conditions was read out; Declarations was cut mid-clause before the jump;
        # everything between was skipped by the jump.
        self.assertTrue(text.startswith("You heard 1 section in full: General Conditions."), text)
        self.assertIn("Partly heard: Declarations.", text)
        self.assertIn("Not heard: Coverages and Limits, Definitions", text)
        self.assertIn("and 5 more", text)
        self.assertEqual(self.events("recap")[0]["text"], text)
        await self.until(lambda m: m.get("type") == "document_finished")
        # the recap names only what the ledger holds: General Conditions heard, the rest skipped by the jump
        led = self.s.library.current.session.ledger
        g = self.s.library.current.grounding
        gc = next(x for x in g.sections if x["title"] == "General Conditions")
        self.assertTrue(all(led.get(c["id"]) == "heard" for c in g.clauses[gc["start"]:gc["end"]] if srv.is_readable(c)))

    async def test_silence_ends_the_document(self):
        srv.PROMPT_TIMEOUT_S["end_choice"] = 0.4
        await self.to_the_end()
        fin = await self.until(lambda m: m.get("type") == "document_finished")
        self.assertEqual(fin["name"], "policy")
        self.assertEqual(self.events("prompt_resolved")[-1]["by"], "timeout")
        self.assertFalse(self.s.playing)

    async def test_a_section_named_at_the_end_is_read_again(self):
        await self.to_the_end()
        await self.until(lambda m: m.get("type") == "prompt" and m.get("kind") == "end_choice")
        await self.ws.send_json({"type": "ask", "question": "definitions"})
        jumped = await self.until(lambda m: m.get("type") == "jumped")
        self.assertEqual(jumped["heading"], "Definitions")
        cue = await self.until(lambda m: m.get("type") == "unit_started")
        self.assertEqual(cue["kind"], "cue")

    async def test_recap_before_the_end_by_intent_word(self):
        await self.ws.send_json({"type": "play"})
        first = await self.read_until(lambda u: u["kind"] == "clause")
        await self.ws.send_json({"type": "flush_ack", "context_id": first["context_id"], "rendered_ms": 100})
        await self.ws.send_json({"type": "interrupt"})
        await self.until(lambda m: m.get("type") == "paused")
        await asyncio.sleep(0.2)                     # the acks land after we sent them
        text = self.s.recap_text(self.s.library.current)
        self.assertTrue(text.startswith("Partly heard: Declarations.") or text.startswith("You heard"), text)
        self.assertIn("Not heard:", text)


if __name__ == "__main__":
    unittest.main()
