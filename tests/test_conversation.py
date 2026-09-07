"""The v2 router: the model understands a reply that is not an exact option
word; execution stays deterministic; the v1 rules are the floor.

llm.understand is mocked; the session is given a stand-in answer model so the
understanding step runs at all (with no model configured, the floor is what
every other test exercises)."""
import asyncio
import base64
import json
import os
import sys
import tempfile
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
sys.path.insert(0, str(ROOT / "tests"))

from aiohttp.test_utils import TestClient, TestServer               # noqa: E402

import llm as llm_mod                                               # noqa: E402
import server as srv                                                # noqa: E402
from test_server_start_choice import enriched_hero                  # noqa: E402


async def extractive_llm(messages):
    """A stand-in answer model: the presence of one is what turns understanding on."""
    return "Section says: " + messages[-1]["content"][-60:]


class ConversationCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        enriched_hero(Path(self.tmp.name))
        self.app = srv.build_app(dev=False, index_path=Path(self.tmp.name) / "index.json")
        self.s = self.app["session"]
        self.s.llm = extractive_llm
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()
        self.ws = await self.client.ws_connect("/ws/audio")
        await self.ws.receive_json(timeout=5)
        self.audio: dict = {}
        self.calls: list = []

    async def asyncTearDown(self):
        await self.ws.close()
        await self.client.close()
        p = getattr(self.s.events, "path", None)
        if p and Path(p).exists():
            Path(p).unlink()
        self.tmp.cleanup()

    def understanding(self, result):
        """Patch llm.understand to return `result` (or raise it), recording the ctx."""
        def fake(text, ctx):
            self.calls.append((text, ctx))
            if isinstance(result, Exception):
                raise result
            return dict(result)
        return mock.patch.object(llm_mod, "understand", fake)

    async def recv(self, timeout=6.0):
        m = await self.ws.receive_json(timeout=timeout)
        if m.get("type") == "audio":
            self.audio[m["context_id"]] = self.audio.get(m["context_id"], 0) + len(base64.b64decode(m["b64"]))
        return m

    async def until(self, pred, timeout=10.0):
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

    async def claim(self):
        await self.ws.send_json({"type": "interrupt"})
        await self.until(lambda m: m.get("type") == "sink")

    def understood(self):
        return [(e["intent"], e.get("section_id"), e["via"]) for e in self.s.events.of_type("reply_understood")]


class TestRouter(ConversationCase):
    async def test_a_topic_the_rules_cannot_place_is_understood_and_jumped_to(self):
        await self.claim()
        g = self.s.library.current.grounding
        excl = next(x for x in g.sections if x["title"] == "General Exclusions")
        with self.understanding({"intent": "topic", "section_id": excl["id"], "row": None, "question": None}):
            await self.ws.send_json({"type": "ask", "question": "the part about what they will not pay for"})
            jumped = await self.until(lambda m: m.get("type") == "jumped")
        self.assertEqual(jumped["heading"], "General Exclusions")
        # The understanding is broadcast right after the action, before the read loop's cue.
        heard = await self.until(lambda m: m.get("type") == "reply_understood")
        self.assertEqual((heard["intent"], heard["section_title"], heard["via"]), ("topic", "General Exclusions", "llm"))
        cue = await self.until(lambda m: m.get("type") == "unit_started")
        self.assertEqual(cue["kind"], "cue")
        self.assertEqual(cue["text_display"], "General Exclusions, about 2 minutes. Starting.")
        self.assertEqual(self.understood(), [("topic", excl["id"], "llm")])
        text, ctx = self.calls[0]
        self.assertIn(excl["id"], [x["id"] for x in ctx["sections"]])
        self.assertIsNone(ctx["prompt_kind"])

    async def test_an_exact_option_word_never_reaches_the_model(self):
        await self.claim()
        with self.understanding(AssertionError("the model must not be asked")):
            await self.ws.send_json({"type": "ask", "question": "exclusions"})       # chip text: the rules
            await self.until(lambda m: m.get("type") == "jumped")
        self.assertEqual(self.understood()[0][2], "rules")
        self.assertEqual(self.calls, [])

    async def test_a_question_goes_down_the_ask_path_with_the_cleaned_question(self):
        await self.claim()
        g = self.s.library.current.grounding
        term = next(iter(g.terms))
        with self.understanding({"intent": "question", "section_id": None, "row": None, "question": f"what does {term} mean"}):
            await self.ws.send_json({"type": "ask", "question": f"erm the {term} thing, what was that"})
            ans = await self.until(lambda m: m.get("type") == "answer")
        self.assertEqual(ans["question"], f"what does {term} mean")
        self.assertEqual(ans["kind"], "in_scope")
        self.assertEqual(self.understood(), [("question", None, "llm")])
        self.assertEqual(self.s.events.of_type("reply_routed")[0]["route"], "question")

    async def test_a_question_the_document_does_not_answer_opens_the_not_found_prompt(self):
        await self.claim()
        with self.understanding({"intent": "question", "section_id": None, "row": None,
                                 "question": "jupiter moons please"}):
            await self.ws.send_json({"type": "ask", "question": "so like, jupiter moons please"})
            ans = await self.until(lambda m: m.get("type") == "answer")
        self.assertEqual(ans["kind"], "not_found")
        st = await self.until(lambda m: m.get("type") == "unit_started")
        self.assertEqual(st["kind"], "not_found")
        self.assertTrue(st["text_display"].startswith("I couldn't find that in this document"))
        await self.until(lambda m: m.get("type") == "unit_done" and m["context_id"] == st["context_id"])
        await self.ack_all(st["context_id"])
        pm = await self.until(lambda m: m.get("type") == "prompt")
        self.assertEqual((pm["kind"], pm["options"]), ("not_found", ["carry_on", "question"]))
        await self.ws.send_json({"type": "ask", "question": "carry on"})
        await self.until(lambda m: m.get("type") == "prompt_closed")
        self.assertEqual(self.s.events.of_type("prompt_resolved")[-1]["choice"], "carry_on")

    async def test_unclear_falls_to_the_rules(self):
        await self.claim()
        with self.understanding(dict(llm_mod.UNCLEAR)):
            await self.ws.send_json({"type": "ask", "question": "the part where they talk about paying"})
            ans = await self.until(lambda m: m.get("type") == "answer")
        self.assertIn(ans["kind"], ("in_scope", "beyond_cursor", "not_found"))
        self.assertEqual(self.understood()[0][2], "rules", "the floor took it")
        self.assertEqual(len(self.calls), 1, "the model was asked once")

    async def test_a_model_failure_falls_to_the_rules(self):
        await self.claim()
        with self.understanding(TimeoutError("read timed out")):
            await self.ws.send_json({"type": "ask", "question": "the part where they talk about paying"})
            await self.until(lambda m: m.get("type") == "answer")
        self.assertEqual(self.understood()[0][2], "rules")

    async def test_a_reply_to_an_open_prompt_is_read_against_its_options(self):
        await self.claim()
        await self.ws.send_json({"type": "open", "name": "policy"})
        st = await self.until(lambda m: m.get("type") == "unit_started" and m.get("kind") == "start_choice")
        await self.until(lambda m: m.get("type") == "unit_done" and m["context_id"] == st["context_id"])
        await self.ack_all(st["context_id"])
        await self.until(lambda m: m.get("type") == "prompt" and m.get("kind") == "start_choice")
        with self.understanding({"intent": "brief", "section_id": None, "row": None, "question": None}):
            await self.ws.send_json({"type": "ask", "question": "just tell me roughly what is in it"})
            await self.until(lambda m: m.get("type") == "playing")
        self.assertEqual(self.s.events.of_type("prompt_resolved")[-1]["choice"], "brief")
        self.assertEqual(self.calls[0][1]["prompt_kind"], "start_choice")


if __name__ == "__main__":
    unittest.main()
