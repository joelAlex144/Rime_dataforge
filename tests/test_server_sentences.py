"""Side units are one client unit but one Rime context per sentence, so an
interrupt during the overview fences at most one sentence's bytes and the
rest of the overview is never synthesised. /api/metrics carries the fenced
bytes for the session.
"""
import asyncio
import base64
import os
import sys
import unittest
from pathlib import Path

os.environ["TTS_PROVIDER"] = "fake"
os.environ.pop("LLM_API_KEY", None)
os.environ.pop("LLM_PROVIDER", None)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "examples" / "policy-reader"))
sys.path.insert(0, str(ROOT / "tests"))

from test_server_start_choice import StartChoiceCase                # noqa: E402


class TestSentenceStreaming(StartChoiceCase):
    async def test_the_overview_is_one_rime_context_per_sentence_and_a_cut_fences_at_most_one(self):
        await self.ws.send_json({"type": "play"})
        st, pm = await self.hear_prompt("start_choice")
        await self.ws.send_json({"type": "ask", "question": "brief"})
        ov = await self.until(lambda m: m.get("type") == "unit_started" and m.get("kind") == "map")
        ctx = ov["context_id"]
        first = await self.until(lambda m: m.get("type") == "audio" and m["context_id"] == ctx)
        # Cut it after the first audio of the first sentence.
        await self.ws.send_json({"type": "flush_ack", "context_id": ctx, "rendered_ms": 50})
        await self.ws.send_json({"type": "interrupt"})
        after = 0
        for _ in range(40):
            try:
                m = await self.recv(timeout=0.25)
            except asyncio.TimeoutError:
                break
            if m.get("type") == "audio" and m["context_id"] == ctx:
                after += len(base64.b64decode(m["b64"]))
        sent = [e for e in self.events("sentence_synth") if e["context_id"] == ctx]
        self.assertGreaterEqual(len(sent), 1)
        n_sentences = len([s for s in ov["text_display"].replace("!", ".").replace("?", ".").split(". ") if s.strip()])
        self.assertLess(len(sent), max(2, n_sentences), f"the cut stopped the rest: {len(sent)} of ~{n_sentences} sentences synthesised")
        per_sentence = max(self.audio.get(ctx, 0), 1)
        self.assertLessEqual(after, per_sentence, "at most one sentence's bytes after the cut")
        metrics = await (await self.client.get("/api/metrics")).json()
        self.assertIn("fenced_bytes_session", metrics)
        self.assertIsInstance(metrics["fenced_bytes_session"], int)
        self.assertIn("fenced_unit_bytes", metrics)


if __name__ == "__main__":
    unittest.main()
