"""Enrichment scheduling: the navigator pass has its own lock and priority;
the rest pass runs one model call at a time and yields.

Fake model: the navigator fields are written at once; the rest pass is six
steps of 0.8 s each. Document B's navigator lands while document A's rest
pass is mid-way, and a question asked during that pass is answered within one
step -- the next step waits until the answer has been heard.
"""
import asyncio
import base64
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

from aiohttp.test_utils import TestClient, TestServer               # noqa: E402

import enrich as enrich_mod                                         # noqa: E402
import server as srv                                                # noqa: E402
from test_server_navigator import NavigatorCase, fake_enrich_fixture  # noqa: E402


class SlowSteps:
    """Stands in for Enricher: six rest steps of 0.8 s, recorded."""
    calls: list = []

    def __init__(self, provider, cps, src, log=None):
        self.p = provider
        self.rejections = []

    def steps(self, doc, fields=None, force=False):
        def step(k):
            def run():
                time.sleep(0.8)
                SlowSteps.calls.append((doc.get("title"), k, time.monotonic()))
            return run
        return [(f"fake:{k}", step(k)) for k in range(6)]

    def finish(self, doc, done, t0, fields=None, force=False):
        e = doc.setdefault("enrichment", {})
        e.update({"provider": "fake", "fields": list(done), "done": sorted(set(e.get("done", [])) | set(done)),
                  "guard_rejections": []})
        return e


class TestScheduling(NavigatorCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        SlowSteps.calls = []
        self._delay = srv.ENRICH_REST_DELAY_S
        srv.ENRICH_REST_DELAY_S = 0

    async def asyncTearDown(self):
        srv.ENRICH_REST_DELAY_S = self._delay
        await super().asyncTearDown()

    async def test_b_navigator_lands_during_a_rest_and_an_ask_is_answered_within_one_step(self):
        with mock.patch.object(enrich_mod, "enrich_fixture", fake_enrich_fixture), \
             mock.patch.object(enrich_mod, "Enricher", SlowSteps), \
             mock.patch.object(enrich_mod, "measured_chars_per_second", lambda: (14.0, "fake")):
            frames_a, a = await self.upload("_sched_a")
            await asyncio.sleep(1.0)                              # A's rest pass is under way
            body_b = "\n\n".join(
                f"# Part {i}\n\nA second document, paragraph {i}, with a different text so that the "
                f"content hash differs and the upload is not a duplicate of the first." for i in range(1, 21))
            frames_b, b = await self.upload("_sched_b", body=body_b)
            t_b = time.monotonic()
            # B's navigator is done (the upload returned with enrich ok) while A's rest is mid-way.
            self.assertEqual([f["status"] for f in frames_b if f["stage"] == "enrich"][-1], "ok")
            rest_done = [e for e in self.s.events.of_type("enrich_done") if e["stage"] == "rest" and e["document"] == a["name"]]
            self.assertEqual(rest_done, [], "A's rest pass is still running when B's navigator lands")
            self.assertGreaterEqual(len(SlowSteps.calls), 1)
            self.assertLess(len(SlowSteps.calls), 6)

            # A question during the rest pass: answered at once; the next step waits for the answer.
            await self.a.ws.send_json({"type": "interrupt"})      # claims the voice, nothing sounding
            await asyncio.sleep(0.3)
            t0 = time.monotonic()
            await self.a.ws.send_json({"type": "ask", "question": "which paragraph mentions the content hash"})
            ans = await self.a.until(lambda m: m.get("type") == "answer", timeout=6)
            self.assertLess(time.monotonic() - t0, 2.0, "answered within one step")
            spoken = await self.a.until(lambda m: m.get("type") == "unit_started" and m.get("kind") == "answer")
            await self.a.until(lambda m: m.get("type") == "unit_done" and m["context_id"] == spoken["context_id"])
            # The step that was in flight when the question came finishes (0.8 s
            # at most); no further step starts while the answer is unheard.
            await asyncio.sleep(1.0)
            steps_before = len(self.s.events.of_type("enrich_step"))
            await asyncio.sleep(1.5)                              # the answer is sounding, not yet acked
            self.assertEqual(len(self.s.events.of_type("enrich_step")), steps_before,
                             "no rest step while an answer is in flight")
            await self.a.ack_all(spoken["context_id"])
            # and the pass resumes once the answer is heard
            for _ in range(60):
                if len(self.s.events.of_type("enrich_step")) > steps_before:
                    break
                await asyncio.sleep(0.2)
            self.assertGreater(len(self.s.events.of_type("enrich_step")), steps_before)
            for _ in range(100):
                if any(e["stage"] == "rest" for e in self.s.events.of_type("enrich_done")):
                    break
                await asyncio.sleep(0.2)
        # the fixture was written step by step and the pass finished for both documents
        done = [e["document"] for e in self.s.events.of_type("enrich_done") if e["stage"] == "rest"]
        self.assertIn(a["name"], done)

    async def test_the_rest_pass_can_be_switched_off(self):
        with mock.patch.object(enrich_mod, "enrich_fixture", fake_enrich_fixture), \
             mock.patch.object(enrich_mod, "Enricher", SlowSteps), \
             mock.patch.object(enrich_mod, "measured_chars_per_second", lambda: (14.0, "fake")):
            r = await self.client.post("/api/dev/enrich_rest", json={"enabled": False})
            self.assertEqual((await r.json())["enabled"], False)
            frames, a = await self.upload("_sched_off")
            await asyncio.sleep(1.0)
        self.assertEqual(SlowSteps.calls, [])
        self.assertTrue(any(e["reason"] == "disabled" for e in self.s.events.of_type("enrich_rest_skipped")))
        self.assertEqual((await (await self.client.get("/api/status")).json())["enrich_rest"]["enabled"], False)


if __name__ == "__main__":
    unittest.main()
