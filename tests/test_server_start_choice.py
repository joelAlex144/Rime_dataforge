"""start_choice: when a ready document is opened with the voice claimed, the
reader invites a topic instead of waiting for play.

  "I've gone through {title}, about {N} minutes to read. Is there a topic you
  have in mind? If not, I'll give you a brief and you can pick from there."

A topic name confirms ("{heading}, about m minutes. Read it now?") and jumps;
silence plays the overview and the chips; a question is answered and the
invitation is spoken once more, then never again. The model is faked: the
rules route every reply here, so llm.classify / llm.map_topic must not be
reached at all.
"""
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

from aiohttp.test_utils import TestClient, TestServer               # noqa: E402

import llm as llm_mod                                               # noqa: E402
import server as srv                                                # noqa: E402

HERO = ROOT / "examples" / "policy-reader" / "fixtures" / "policy.json"


def enriched_hero(tmp: Path) -> Path:
    """The hero fixture with a hand-written navigator (same as test_server_jump)."""
    doc = json.loads(HERO.read_text(encoding="utf-8"))
    for k in ("enrichment", "overview", "sections", "topics"):
        doc.pop(k, None)
    for c in doc["clauses"]:
        c.pop("tags", None); c.pop("spoken_override", None)
    sections, cur = [], None
    for c in doc["clauses"]:
        if cur is None or cur["title"] != c["section_title"]:
            cur = {"id": c["id"], "title": c["section_title"], "start": c["index"], "body": []}
            sections.append(cur)
        cur["body"].append(c)
    doc["overview"] = {"text": "This is a homeowners policy. It has thirteen sections, from declarations to general "
                               "conditions.", "generated": True, "provider": "fake"}
    doc["sections"] = [{"id": s["id"], "title": s["title"], "est_minutes": 2,
                        "brief": {"text": f"Sets out {s['title'].lower()}.", "generated": True, "provider": "fake"},
                        "suggested_questions": []} for s in sections]
    excl = next(s for s in sections if s["title"] == "General Exclusions")
    doc["topics"] = [{"topic": "Exclusions", "section_id": excl["id"], "heading": excl["title"], "generated": True,
                      "provider": "fake"},
                     {"topic": "Read from the start", "section_id": None, "heading": None, "generated": False}]
    doc["enrichment"] = {"provider": "fake", "model": "fake-1", "elapsed_ms": 1,
                         "fields": ["overview", "sections", "topics"], "done": ["overview", "sections", "topics"]}
    out = tmp / "policy.json"
    out.write_text(json.dumps(doc), encoding="utf-8")
    (tmp / "index.json").write_text(json.dumps({"documents": [{
        "name": "policy", "doc_id": "policy", "title": doc["title"], "path": "policy.json",
        "clause_count": len(doc["clauses"]), "reviewed": True, "readable": True, "referral": "your insurer"}]}),
        encoding="utf-8")
    return out


class StartChoiceCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        enriched_hero(Path(self.tmp.name))
        self.app = srv.build_app(dev=False, index_path=Path(self.tmp.name) / "index.json")
        self.s = self.app["session"]
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()
        self.ws = await self.client.ws_connect("/ws/audio")
        await self.ws.receive_json(timeout=5)                           # hello
        self.audio: dict = {}
        self.seen: list = []
        # The model must not be reached: every reply here is read by the rules.
        self.classify = mock.patch.object(llm_mod, "classify", side_effect=AssertionError("classify called"))
        self.map_topic = mock.patch.object(llm_mod, "map_topic", side_effect=AssertionError("map_topic called"))
        self.classify.start(); self.map_topic.start()
        self._timeouts = dict(srv.PROMPT_TIMEOUT_S)

    async def asyncTearDown(self):
        srv.PROMPT_TIMEOUT_S.update(self._timeouts)
        self.classify.stop(); self.map_topic.stop()
        await self.ws.close()
        await self.client.close()
        p = getattr(self.s.events, "path", None)
        if p and Path(p).exists():
            Path(p).unlink()
        self.tmp.cleanup()

    async def recv(self, timeout=6.0):
        m = await self.ws.receive_json(timeout=timeout)
        self.seen.append(m)
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

    async def hear_prompt(self, kind):
        """The prompt's unit, acked; returns (unit_started, prompt message)."""
        st = await self.until(lambda m: m.get("type") == "unit_started" and m.get("kind") == kind)
        await self.until(lambda m: m.get("type") == "unit_done" and m["context_id"] == st["context_id"])
        await self.ack_all(st["context_id"])
        pm = await self.until(lambda m: m.get("type") == "prompt" and m.get("kind") == kind)
        return st, pm

    async def claim_and_open(self):
        # The voice is claimed by an interrupt (nothing sounding), then the
        # ready document is opened: that is the moment of the invitation.
        await self.ws.send_json({"type": "interrupt"})
        await self.until(lambda m: m.get("type") == "sink")
        await self.ws.send_json({"type": "open", "name": "policy"})
        await self.until(lambda m: m.get("type") == "document_opened")

    def events(self, kind):
        return self.s.events.of_type(kind)


class TestStartChoice(StartChoiceCase):
    async def test_a_topic_reply_confirms_then_jumps(self):
        await self.claim_and_open()
        st, pm = await self.hear_prompt("start_choice")
        self.assertTrue(st["text_display"].startswith("I've gone through Northlake Mutual"), st["text_display"])
        self.assertRegex(st["text_display"], r"^I've gone through .+: \d+ sections\. Is there a topic you have in mind\?")
        self.assertEqual(pm["options"], ["topic", "brief", "start"])

        await self.ws.send_json({"type": "ask", "question": "exclusions"})
        st2, pm2 = await self.hear_prompt("confirm_topic")
        self.assertEqual(st2["text_display"], "General Exclusions, about 2 minutes. Read it now?")
        self.assertEqual(pm2["options"], ["yes", "no"])

        await self.ws.send_json({"type": "ask", "question": "yes"})
        jumped = await self.until(lambda m: m.get("type") == "jumped")
        self.assertEqual((jumped["reason"], jumped["heading"]), ("topic", "General Exclusions"))
        cue = await self.until(lambda m: m.get("type") == "unit_started")
        self.assertEqual(cue["kind"], "cue")
        await self.until(lambda m: m.get("type") == "unit_done" and m["context_id"] == cue["context_id"])
        await self.ack_all(cue["context_id"])
        tgt = await self.until(lambda m: m.get("type") == "unit_started" and m.get("kind") == "clause")
        self.assertEqual(tgt["section_title"], "General Exclusions")

        self.assertEqual([e["kind"] for e in self.events("prompt_opened")], ["start_choice", "confirm_topic"])
        self.assertEqual([(e["kind"], e["by"], e["choice"]) for e in self.events("prompt_resolved")],
                         [("start_choice", "reply", "topic"), ("confirm_topic", "reply", "yes")])
        self.assertEqual([e["route"] for e in self.events("reply_routed")], ["pending", "pending"])

    async def test_silence_plays_the_overview_and_the_chips(self):
        srv.PROMPT_TIMEOUT_S["start_choice"] = 0.4
        await self.claim_and_open()
        await self.hear_prompt("start_choice")
        await self.until(lambda m: m.get("type") == "playing")
        topics = await self.until(lambda m: m.get("type") == "topics")
        names = [t["topic"] for t in topics["topics"]]
        self.assertEqual((names[0], names[-1]), ("Exclusions", "Read from the start"), names)
        self.assertTrue(5 <= len(names) <= 7, names)
        ov = await self.until(lambda m: m.get("type") == "unit_started")
        self.assertEqual(ov["kind"], "map")
        self.assertIn("homeowners policy", ov["text_display"])
        self.assertEqual([(e["kind"], e["by"]) for e in self.events("prompt_resolved")], [("start_choice", "timeout")])

    async def test_after_the_brief_pick_topic_offers_the_chips_and_a_topic_jumps(self):
        srv.PROMPT_TIMEOUT_S["start_choice"] = 0.4
        await self.claim_and_open()
        await self.hear_prompt("start_choice")                    # silence: the brief
        await self.until(lambda m: m.get("type") == "playing")
        ov = await self.until(lambda m: m.get("type") == "unit_started")
        self.assertEqual(ov["kind"], "map")
        await self.until(lambda m: m.get("type") == "unit_done" and m["context_id"] == ov["context_id"])
        await self.ack_all(ov["context_id"])
        st, pm = await self.hear_prompt("pick_topic")
        self.assertTrue(st["text_display"].startswith("Where shall we start: Exclusions, "), st["text_display"])
        self.assertTrue(st["text_display"].endswith(", or from the top?"), st["text_display"])
        self.assertEqual((pm["options"], pm["topics"][0], len(pm["topics"])), (["topic", "top"], "Exclusions", 3))
        await self.ws.send_json({"type": "ask", "question": "exclusions"})
        jumped = await self.until(lambda m: m.get("type") == "jumped")
        self.assertEqual((jumped["reason"], jumped["heading"]), ("topic", "General Exclusions"))
        self.assertEqual([(e["kind"], e["by"], e["choice"]) for e in self.events("prompt_resolved")][-1],
                         ("pick_topic", "reply", "topic"))

    async def test_pick_topic_silence_reads_from_the_top(self):
        srv.PROMPT_TIMEOUT_S["start_choice"] = 0.4
        srv.PROMPT_TIMEOUT_S["pick_topic"] = 0.4
        await self.claim_and_open()
        await self.hear_prompt("start_choice")
        await self.until(lambda m: m.get("type") == "playing")
        ov = await self.until(lambda m: m.get("type") == "unit_started")
        await self.until(lambda m: m.get("type") == "unit_done" and m["context_id"] == ov["context_id"])
        await self.ack_all(ov["context_id"])
        await self.hear_prompt("pick_topic")
        nxt = await self.until(lambda m: m.get("type") == "unit_started")
        self.assertEqual((nxt["kind"], nxt["index"]), ("clause", 0))
        self.assertEqual(self.events("prompt_resolved")[-1]["by"], "timeout")

    async def test_from_the_start_reads_without_the_overview(self):
        await self.claim_and_open()
        await self.hear_prompt("start_choice")
        await self.ws.send_json({"type": "ask", "question": "from the start"})
        await self.until(lambda m: m.get("type") == "playing")
        first = await self.until(lambda m: m.get("type") == "unit_started")
        self.assertEqual(first["kind"], "clause", "no overview first")
        self.assertEqual(first["index"], 0)

    async def test_a_question_is_answered_then_the_invitation_is_spoken_once_more(self):
        await self.claim_and_open()
        await self.hear_prompt("start_choice")
        g = self.s.library.current.grounding
        term = next(iter(g.terms))
        await self.ws.send_json({"type": "ask", "question": f"what does {term} mean"})
        ans = await self.until(lambda m: m.get("type") == "answer")
        self.assertEqual(ans["kind"], "in_scope")
        spoken = await self.until(lambda m: m.get("type") == "unit_started" and m.get("kind") == "answer")
        await self.until(lambda m: m.get("type") == "unit_done" and m["context_id"] == spoken["context_id"])
        await self.ack_all(spoken["context_id"])
        # The invitation again -- once.
        st2, _ = await self.hear_prompt("start_choice")
        self.assertTrue(st2["text_display"].startswith("I've gone through"))
        self.assertEqual(self.events("prompt_resolved")[0]["choice"], "question")

        term2 = list(g.terms)[1] if len(g.terms) > 1 else term
        await self.ws.send_json({"type": "ask", "question": f"what does {term2} mean"})
        spoken2 = await self.until(lambda m: m.get("type") == "unit_started" and m.get("kind") == "answer")
        await self.until(lambda m: m.get("type") == "unit_done" and m["context_id"] == spoken2["context_id"])
        await self.ack_all(spoken2["context_id"])
        # This time reading resumes and no third invitation is spoken.
        await self.until(lambda m: m.get("type") == "playing", timeout=6.0)
        nxt = await self.until(lambda m: m.get("type") == "unit_started")
        self.assertNotEqual(nxt["kind"], "start_choice")
        self.assertEqual([e["kind"] for e in self.events("prompt_opened")].count("start_choice"), 2)
        self.assertEqual([e["route"] for e in self.events("reply_routed")], ["question", "question"])



class TestWhenToAsk(StartChoiceCase):
    """start_choice is asked once per document: after a run of rail opens has
    settled (1.5 s), never for the page's own re-open at load, else on the
    first play."""

    def add_copies(self, *names):
        tmp = Path(self.tmp.name)
        doc = json.loads((tmp / "policy.json").read_text(encoding="utf-8"))
        idx = json.loads((tmp / "index.json").read_text(encoding="utf-8"))
        for n in names:
            d = dict(doc, title=f"{n.title()} Wording")
            (tmp / f"{n}.json").write_text(json.dumps(d), encoding="utf-8")
            idx["documents"].append({"name": n, "doc_id": n, "title": d["title"], "path": f"{n}.json",
                                     "clause_count": len(doc["clauses"]), "reviewed": True, "readable": True,
                                     "referral": "your insurer"})
        (tmp / "index.json").write_text(json.dumps(idx), encoding="utf-8")
        self.s.library.reload()

    async def test_three_quick_opens_ask_once_for_the_document_that_stays(self):
        self.add_copies("second", "third")
        await self.ws.send_json({"type": "interrupt"})
        await self.until(lambda m: m.get("type") == "sink")
        for name in ("policy", "second", "third"):
            await self.ws.send_json({"type": "open", "name": name})
            await self.until(lambda m: m.get("type") == "document_opened" and m["name"] == name)
            await asyncio.sleep(0.3)
        st, pm = await self.hear_prompt("start_choice")
        self.assertTrue(st["text_display"].startswith("I've gone through Third Wording"), st["text_display"])
        await asyncio.sleep(2.0)                                     # long enough for any other timer
        asked = [e for e in self.events("prompt_opened") if e["kind"] == "start_choice"]
        self.assertEqual(len(asked), 1, asked)
        due = self.events("start_choice_due")
        self.assertEqual([d["document"] for d in due], ["third"])

    async def test_the_page_reopening_its_document_at_load_is_not_asked_and_the_first_play_is(self):
        # No voice yet; the page re-opens the document it shows: nothing is said.
        await self.ws.send_json({"type": "open", "name": "policy"})
        await self.until(lambda m: m.get("type") == "document_opened")
        await asyncio.sleep(2.0)
        self.assertEqual([e for e in self.events("prompt_opened") if e["kind"] == "start_choice"], [])
        # The first play asks, before anything is read.
        await self.ws.send_json({"type": "play"})
        st, pm = await self.hear_prompt("start_choice")
        self.assertTrue(st["text_display"].startswith("I've gone through"), st["text_display"])
        self.assertEqual(self.events("unit_started")[0]["kind"] if self.events("unit_started") else "start_choice", "start_choice")


class WelcomeCase(StartChoiceCase):
    """Two documents in the library: the first play asks which one."""
    async def asyncSetUp(self):
        await super().asyncSetUp()
        tmp = Path(self.tmp.name)
        doc = json.loads((tmp / "policy.json").read_text(encoding="utf-8"))
        doc["title"] = "Second Wording"
        (tmp / "second.json").write_text(json.dumps(doc), encoding="utf-8")
        idx = json.loads((tmp / "index.json").read_text(encoding="utf-8"))
        idx["documents"].append({"name": "second", "doc_id": "second", "title": "Second Wording", "path": "second.json",
                                 "clause_count": len(doc["clauses"]), "reviewed": True, "readable": True,
                                 "referral": "your insurer"})
        (tmp / "index.json").write_text(json.dumps(idx), encoding="utf-8")
        self.s.library.reload()

    async def play_and_hear_welcome(self):
        await self.ws.send_json({"type": "play"})
        return await self.hear_prompt("welcome")


class TestWelcome(WelcomeCase):
    async def test_the_first_play_asks_which_document_and_a_title_opens_it(self):
        st, pm = await self.play_and_hear_welcome()
        self.assertTrue(st["text_display"].startswith("I can read a policy or agreement to you and answer questions as we go. "
                                                      "You have 2 here: "), st["text_display"])
        self.assertIn("Second Wording", st["text_display"])
        self.assertTrue(st["text_display"].endswith("Which one, or upload a new one?"))
        self.assertEqual((pm["options"], pm["titles"][1]), (["title", "upload", "first"], "Second Wording"))
        await self.ws.send_json({"type": "ask", "question": "second wording"})
        opened = await self.until(lambda m: m.get("type") == "document_opened")
        self.assertEqual(opened["name"], "second")
        st2, _ = await self.hear_prompt("start_choice")
        self.assertTrue(st2["text_display"].startswith("I've gone through Second Wording"), st2["text_display"])
        self.assertEqual([(e["kind"], e["by"], e["choice"]) for e in self.events("prompt_resolved")][0],
                         ("welcome", "reply", "title"))

    async def test_upload_focuses_the_upload_box(self):
        await self.play_and_hear_welcome()
        await self.ws.send_json({"type": "ask", "question": "upload"})
        await self.until(lambda m: m.get("type") == "focus_upload")
        self.assertEqual(self.events("prompt_resolved")[-1]["choice"], "upload")

    async def test_silence_reads_the_first_document(self):
        srv.PROMPT_TIMEOUT_S["welcome"] = 0.4
        await self.play_and_hear_welcome()
        opened = await self.until(lambda m: m.get("type") == "document_opened")
        self.assertEqual(opened["name"], "policy")
        await self.until(lambda m: m.get("type") == "playing")
        ov = await self.until(lambda m: m.get("type") == "unit_started")
        self.assertEqual(ov["kind"], "map")
        self.assertEqual(self.events("prompt_resolved")[-1]["by"], "timeout")

    async def test_a_title_the_rules_cannot_match_goes_through_the_model(self):
        async def stand_in(messages):
            return "x"
        self.s.llm = stand_in
        await self.play_and_hear_welcome()
        with mock.patch.object(llm_mod, "understand",
                               lambda text, ctx: {"intent": "topic", "section_id": "second", "row": None, "question": None}):
            await self.ws.send_json({"type": "ask", "question": "the other one"})
            opened = await self.until(lambda m: m.get("type") == "document_opened")
        self.assertEqual(opened["name"], "second")
        self.assertEqual(self.events("reply_understood")[-1]["via"], "llm")

    async def test_a_document_the_listener_opened_is_not_asked_about(self):
        await self.ws.send_json({"type": "interrupt"})
        await self.until(lambda m: m.get("type") == "sink")
        await self.ws.send_json({"type": "open", "name": "second"})
        await self.until(lambda m: m.get("type") == "document_opened")
        st, _ = await self.hear_prompt("start_choice")               # the voice was claimed: the invitation
        await self.ws.send_json({"type": "play"})                     # play over it reads; no welcome
        await self.until(lambda m: m.get("type") == "playing")
        self.assertNotIn("welcome", [e["kind"] for e in self.events("prompt_opened")])


if __name__ == "__main__":
    unittest.main()
