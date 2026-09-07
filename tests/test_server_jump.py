"""Jump is the interruption path with a different resume target.

Bug this pins: after an interruption, "Jump there" set the reference position
but not the read cursor and cancelled nothing, so the queued lookahead unit
played and the old position came back. Now: jump while N plays and N+1 is in
flight -> N unit_truncated, N+1 unit_skipped (jump), a `jump` event, the cue
unit, then the target; "go back to where I was" returns to the saved position.
"""
import asyncio
import base64
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ["TTS_PROVIDER"] = "fake"
os.environ.pop("LLM_API_KEY", None)
os.environ.pop("LLM_PROVIDER", None)     # a sourced .env with LLM_PROVIDER=ollama must not reach the tests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "examples" / "policy-reader"))

from aiohttp.test_utils import TestClient, TestServer               # noqa: E402

import server as srv                                                # noqa: E402

HERO = ROOT / "examples" / "policy-reader" / "fixtures" / "policy.json"


def enriched_hero(tmp: Path) -> Path:
    """The hero fixture with a fake enrichment: briefs, tags, topics, questions.
    Written by hand so the test needs no provider."""
    doc = json.loads(HERO.read_text(encoding="utf-8"))
    doc.pop("enrichment", None); doc.pop("overview", None); doc.pop("sections", None); doc.pop("topics", None)
    for c in doc["clauses"]:
        c.pop("tags", None); c.pop("spoken_override", None)
    sections, cur = [], None
    for c in doc["clauses"]:
        if cur is None or cur["title"] != c["section_title"]:
            cur = {"id": c["id"], "title": c["section_title"], "start": c["index"], "body": []}
            sections.append(cur)
        cur["body"].append(c)
    doc["overview"] = {"text": "This is a homeowners policy. It has thirteen sections, from declarations to general conditions. Listening from start to finish takes about 25 minutes.", "generated": True, "provider": "fake"}
    doc["sections"] = [{"id": s["id"], "title": s["title"], "est_minutes": 2,
                        "brief": {"text": f"Sets out {s['title'].lower()}.", "generated": True, "provider": "fake"},
                        "suggested_questions": [{"text": f"What does {s['title'].lower()} say first?",
                                                 "clause_id": s["body"][0]["id"], "generated": True, "provider": "fake"}]}
                       for s in sections]
    excl = next(s for s in sections if s["title"] == "General Exclusions")
    doc["topics"] = [{"topic": "Exclusions", "section_id": excl["id"], "heading": excl["title"], "generated": True, "provider": "fake"},
                     {"topic": "Definitions", "section_id": next(s for s in sections if s["title"] == "Definitions")["id"], "heading": "Definitions", "generated": True, "provider": "fake"},
                     {"topic": "Read from the start", "section_id": None, "heading": None, "generated": False}]
    for c in excl["body"]:
        c["tags"] = {"tags": ["exclusion"], "generated": True, "provider": "fake"}
    doc["enrichment"] = {"provider": "fake", "model": "fake-1", "elapsed_ms": 1, "fields": ["overview", "sections", "topics", "tags", "suggested_questions"], "guard_rejections": []}
    out = tmp / "policy.json"
    out.write_text(json.dumps(doc), encoding="utf-8")
    (tmp / "index.json").write_text(json.dumps({"documents": [{"name": "policy", "doc_id": "policy", "title": doc["title"],
        "path": "policy.json", "clause_count": len(doc["clauses"]), "reviewed": True, "readable": True, "referral": "your insurer"}]}), encoding="utf-8")
    return out


class JumpCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        enriched_hero(Path(self.tmp.name))
        self.app = srv.build_app(dev=False, index_path=Path(self.tmp.name) / "index.json")
        self.s = self.app["session"]
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()
        self.ws = await self.client.ws_connect("/ws/audio")
        self.hello = await self.ws.receive_json(timeout=5)
        self.seen: list = []
        self.audio: dict = {}
        self._lead = srv.LEAD_MS

    async def asyncTearDown(self):
        srv.LEAD_MS = self._lead
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

    def audio_ms(self, ctx):
        return self.audio[ctx] / 2 / 24000 * 1000

    async def ack_all(self, ctx):
        frames = self.audio[ctx] // 2
        ms = self.audio_ms(ctx)
        await self.ws.send_json({"type": "rendered", "context_id": ctx, "rendered_ms": ms, "enqueued_frames": frames})
        await self.ws.send_json({"type": "unit_ended", "context_id": ctx, "enqueued_frames": frames, "rendered_ms": ms})

    async def play_to_first_clause(self):
        """Play, hear the overview (acked), return the first clause unit."""
        await self.ws.send_json({"type": "play"})
        while True:
            st = await self.until(lambda m: m.get("type") == "unit_started")
            await self.until(lambda m: m.get("type") == "unit_done" and m["context_id"] == st["context_id"])
            if st.get("kind", "clause") != "clause":
                await self.ack_all(st["context_id"])
                if st["kind"] in ("pick_topic", "welcome", "start_choice"):        # the opening prompts: read on
                    await self.ws.send_json({"type": "ask", "question": {"pick_topic": "from the top", "welcome": "the first one", "start_choice": "brief"}[st["kind"]]})
                continue
            return st

    def events(self, kind):
        return self.s.events.of_type(kind)


class TestJumpInFlight(JumpCase):
    async def test_jump_while_n_plays_and_n_plus_1_in_flight(self):
        n = await self.play_to_first_clause()
        ctx = n["context_id"]
        # Let N+1 through the lead, then stall.
        srv.LEAD_MS = self.audio_ms(ctx) * 0.6 + 50
        await self.ws.send_json({"type": "rendered", "context_id": ctx, "rendered_ms": self.audio_ms(ctx) * 0.4,
                                 "enqueued_frames": self.audio[ctx] // 2})
        n1 = await self.until(lambda m: m.get("type") == "unit_started" and m["context_id"] != ctx)
        await self.until(lambda m: m.get("type") == "unit_done" and m["context_id"] == n1["context_id"])

        # A jump to General Exclusions (section 5), issued mid-N: ack first, then the jump.
        g = self.s.library.current.grounding
        target = next(s for s in g.sections if s["title"] == "General Exclusions")
        cut = self.audio_ms(ctx) * 0.4
        await self.ws.send_json({"type": "flush_ack", "context_id": ctx, "rendered_ms": cut})
        await self.ws.send_json({"type": "jump", "unit_id": g.clauses[target["start"]]["id"], "reason": "spoiler_offer"})

        jumped = await self.until(lambda m: m.get("type") == "jumped")
        self.assertEqual(jumped["unit_id"], g.clauses[target["start"]]["id"])
        cue = await self.until(lambda m: m.get("type") == "unit_started")
        self.assertEqual(cue["kind"], "cue", "the cue unit comes first")
        self.assertTrue(cue["text_display"].startswith("Okay — going to General Exclusions."))
        self.assertIn("Sets out general exclusions.", cue["text_display"])
        await self.until(lambda m: m.get("type") == "unit_done" and m["context_id"] == cue["context_id"])
        await self.ack_all(cue["context_id"])
        tgt = await self.until(lambda m: m.get("type") == "unit_started" and m.get("kind") == "clause")
        self.assertEqual(tgt["unit_id"], g.clauses[target["start"]]["id"], "then the target")
        self.assertEqual(tgt["char_start"], 0)

        # Ledger: N truncated at the client boundary, N+1 skipped (it was only buffered),
        # everything between skipped, a jump event with turn_id, positions saved/restored.
        trunc = self.events("unit_truncated")
        self.assertEqual(trunc[0]["context_id"], n["unit_id"])
        self.assertTrue(trunc[0]["reason"].startswith("jump:"))
        skipped = {r["unit_id"] for r in self.events("unit_skipped") if r["reason"] == "jump"}
        self.assertIn(n1["unit_id"], skipped)
        self.assertNotIn(tgt["unit_id"], skipped)
        for c in g.clauses[g.by_id[n["unit_id"]]["index"] + 1: target["start"]]:
            self.assertIn(c["id"], skipped)
        j = self.events("jump")
        self.assertEqual(len(j), 1)
        self.assertEqual(j[0]["to_unit"], tgt["unit_id"])
        self.assertEqual(j[0]["from_unit"], n1["unit_id"], "the unit the reader was on")
        self.assertEqual(j[0]["reason"], "spoiler_offer")
        self.assertIsInstance(j[0]["turn_id"], int)
        saved = [r for r in self.events("position_saved") if r.get("label") == "before_jump"]
        self.assertEqual(len(saved), 1)
        restored = [r for r in self.events("position_restored") if r.get("reason") == "jump"]
        self.assertEqual(restored[-1]["unit_id"], tgt["unit_id"])
        self.assertEqual(self.events("flush_ack_timeout"), [])
        sess = self.s.library.current.session
        self.assertTrue(sess.ledger[n["unit_id"]].startswith("truncated@"))
        self.assertEqual(sess.ledger[n1["unit_id"]], "skipped:jump")

        # "go back to where I was": a jump back to the saved position, resumed at its sentence.
        await self.until(lambda m: m.get("type") == "unit_done" and m["context_id"] == tgt["context_id"])
        await self.ws.send_json({"type": "flush_ack", "context_id": tgt["context_id"], "rendered_ms": 500})
        await self.ws.send_json({"type": "ask", "question": "go back to where I was"})
        back = await self.until(lambda m: m.get("type") == "jumped")
        self.assertEqual(back["reason"], "back")
        cue2 = await self.until(lambda m: m.get("type") == "unit_started")
        self.assertEqual(cue2["kind"], "cue")
        self.assertIn("back to where you were", cue2["text_display"])
        await self.until(lambda m: m.get("type") == "unit_done" and m["context_id"] == cue2["context_id"])
        await self.ack_all(cue2["context_id"])
        again = await self.until(lambda m: m.get("type") == "unit_started" and m.get("kind") == "clause")
        self.assertEqual(again["unit_id"], n1["unit_id"], "back to the unit the reader was on")
        self.assertEqual(len(self.events("jump")), 2)


class TestOpeningFlow(JumpCase):
    async def play_past_invitation(self):
        """The first play of a ready document asks start_choice first; "brief"
        gives the overview and the chips, as the opening flow always did."""
        await self.ws.send_json({"type": "play"})
        st = await self.until(lambda m: m.get("type") == "unit_started" and m.get("kind") == "start_choice")
        await self.until(lambda m: m.get("type") == "unit_done" and m["context_id"] == st["context_id"])
        await self.ack_all(st["context_id"])
        await self.until(lambda m: m.get("type") == "prompt" and m.get("kind") == "start_choice")
        await self.ws.send_json({"type": "ask", "question": "brief"})

    async def test_topics_are_offered_with_the_overview_and_a_chip_asks_now_or_overview_first(self):
        await self.play_past_invitation()
        topics = await self.until(lambda m: m.get("type") == "topics")
        names = [t["topic"] for t in topics["topics"]]
        self.assertEqual(names[:2], ["Exclusions", "Definitions"], names)      # the generated topics first
        self.assertEqual(names[-1], "Read from the start")
        self.assertTrue(5 <= len(names) <= 7, names)                          # headings fill the rest
        ov = await self.until(lambda m: m.get("type") == "unit_started")
        self.assertEqual(ov["kind"], "map")
        self.assertIn("homeowners policy", ov["text_display"])
        await self.until(lambda m: m.get("type") == "unit_done" and m["context_id"] == ov["context_id"])
        # Tap a chip mid-overview: ack the playhead, then the topic.
        await self.ws.send_json({"type": "flush_ack", "context_id": ov["context_id"], "rendered_ms": 800})
        await self.ws.send_json({"type": "topic", "section_id": topics["topics"][0]["section_id"]})
        prompt = await self.until(lambda m: m.get("type") == "unit_started" and m.get("kind") == "choice")
        self.assertTrue(prompt["text_display"].startswith("Exclusions is General Exclusions, about 2 minutes. Read it now"))
        await self.until(lambda m: m.get("type") == "unit_done" and m["context_id"] == prompt["context_id"])
        await self.ack_all(prompt["context_id"])
        choice = await self.until(lambda m: m.get("type") == "choice")
        self.assertEqual(choice["options"], ["now", "overview_first"])
        await self.ws.send_json({"type": "choice", "choice": "now"})
        jumped = await self.until(lambda m: m.get("type") == "jumped")
        self.assertEqual(jumped["reason"], "topic")
        cue = await self.until(lambda m: m.get("type") == "unit_started")
        self.assertEqual(cue["kind"], "cue")
        await self.until(lambda m: m.get("type") == "unit_done" and m["context_id"] == cue["context_id"])
        await self.ack_all(cue["context_id"])
        tgt = await self.until(lambda m: m.get("type") == "unit_started" and m.get("kind") == "clause")
        self.assertEqual(tgt["section_title"], "General Exclusions")
        # the transition cue is spoken for the section heading
        self.assertEqual(self.events("jump")[0]["reason"], "topic")

    async def test_overview_first_then_jump(self):
        await self.play_past_invitation()
        topics = await self.until(lambda m: m.get("type") == "topics")
        ov = await self.until(lambda m: m.get("type") == "unit_started")
        await self.until(lambda m: m.get("type") == "unit_done" and m["context_id"] == ov["context_id"])
        await self.ws.send_json({"type": "flush_ack", "context_id": ov["context_id"], "rendered_ms": 300})
        await self.ws.send_json({"type": "topic", "section_id": topics["topics"][1]["section_id"]})
        prompt = await self.until(lambda m: m.get("type") == "unit_started" and m.get("kind") == "choice")
        await self.until(lambda m: m.get("type") == "unit_done" and m["context_id"] == prompt["context_id"])
        await self.ack_all(prompt["context_id"])
        await self.until(lambda m: m.get("type") == "choice")
        await self.ws.send_json({"type": "ask", "question": "overview first"})
        ov2 = await self.until(lambda m: m.get("type") == "unit_started")
        self.assertEqual(ov2["kind"], "map", "the overview again")
        await self.until(lambda m: m.get("type") == "unit_done" and m["context_id"] == ov2["context_id"])
        await self.ack_all(ov2["context_id"])
        jumped = await self.until(lambda m: m.get("type") == "jumped")
        self.assertEqual(jumped["heading"], "Definitions")


class TestExtractive(JumpCase):
    async def test_read_me_every_exclusion_is_cited_and_never_touches_bm25(self):
        g = self.s.library.current.grounding
        calls = []
        orig = g.bm25.rank
        g.bm25.rank = lambda *a, **k: (calls.append(a), orig(*a, **k))[1]
        await self.ws.send_json({"type": "ask", "question": "read me every exclusion"})
        ans = await self.until(lambda m: m.get("type") == "answer")
        self.assertEqual(ans["retrieval_path"], "extractive")
        self.assertTrue(ans["answer"].startswith("There are 18 clauses tagged exclusion."))
        self.assertIn("Section 5(a)(i), General Exclusions:", ans["answer"])
        self.assertEqual(calls, [], "no retrieval for an extractive ask")
        ag = self.events("answer_grounded")[0]
        self.assertEqual(ag["retrieval_path"], "extractive")
        self.assertIn("Section 5(a)(ii), General Exclusions:", ans["answer"])
        self.assertNotIn("important", ans["answer"].lower())

    async def test_suggested_offer_after_a_section_is_answered_from_the_stored_clause(self):
        # Start on section 1's last clause so the boundary comes quickly.
        g = self.s.library.current.grounding
        first = g.sections[0]
        self.s.library.current.session.read_cursor = first["end"] - 1
        self.s._session_started.add(self.s.library.current.name)      # skip the overview here
        await self.ws.send_json({"type": "play"})
        last = await self.until(lambda m: m.get("type") == "unit_started")
        await self.until(lambda m: m.get("type") == "unit_done" and m["context_id"] == last["context_id"])
        await self.ack_all(last["context_id"])
        offer = await self.until(lambda m: m.get("type") == "unit_started" and m.get("kind") == "offer")
        self.assertTrue(offer["text_display"].startswith("People usually ask here whether"))
        await self.until(lambda m: m.get("type") == "unit_done" and m["context_id"] == offer["context_id"])
        await self.ack_all(offer["context_id"])
        o = await self.until(lambda m: m.get("type") == "offer")
        self.assertEqual(o["clause_id"], first["suggested_questions"][0]["clause_id"])
        await self.ws.send_json({"type": "ask", "question": "yes"})
        ans = await self.until(lambda m: m.get("type") == "answer")
        self.assertEqual(ans["retrieval_path"], "suggested")
        self.assertEqual(ans["unit_id"], o["clause_id"])
        self.assertEqual(self.events("answer_grounded")[-1]["retrieval_path"], "suggested")


if __name__ == "__main__":
    unittest.main()
