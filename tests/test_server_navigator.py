"""The navigator is generated on entry, not by a separate step.

An upload gets an eighth stage, `enrich`, that writes the overview, section
briefs and topic chips before `done`; play then opens with the generated
overview and the chips. When generation fails the document still enters and
play uses the mechanical map. The model is faked: what is tested is the
wiring -- stage, events, reload, spoken unit -- not the prose.
"""
import asyncio
import base64
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

os.environ["TTS_PROVIDER"] = "fake"
os.environ.pop("LLM_API_KEY", None)
os.environ.pop("LLM_PROVIDER", None)
# ENRICH_PROVIDER is set per test (asyncSetUp), never at import: an import-time value
# would leak into every test file collected after this one.
os.environ["NAVIGATOR_WAIT_S"] = "10"

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "examples" / "policy-reader"))
sys.path.insert(0, str(ROOT / "scripts"))

from aiohttp import FormData                                        # noqa: E402
from aiohttp.test_utils import TestClient, TestServer               # noqa: E402

import companion as cp                                              # noqa: E402
import enrich as enrich_mod                                         # noqa: E402
import ingest as ingest_mod                                         # noqa: E402
import server as srv                                                # noqa: E402


def slow_ingest(delay_s: float, stages=("structure", "segment")):
    """The real ingestion, with the named stage events held back by `delay_s`
    each, so the companion has silences to bridge."""
    real = ingest_mod.ingest_document

    def run(source, **kw):
        orig = kw.get("progress")

        def slow_progress(stage, status, ms, detail, extra=None):
            if stage in stages:
                import time
                time.sleep(delay_s)
            if orig:
                orig(stage, status, ms, detail, extra)
        kw["progress"] = slow_progress
        return real(source, **kw)
    return run

OVERVIEW = "This document, Uploaded Wording, covers heading one through heading twenty five in order."


def fake_enrich_fixture(path: Path, provider, force=False, log=None, fields=None):
    """Writes navigator fields the way scripts/enrich.py would, with no model."""
    doc = json.loads(path.read_text(encoding="utf-8"))
    secs = enrich_mod.section_spans(doc)
    fields = tuple(fields or enrich_mod.ALL_FIELDS)
    done = []
    if "overview" in fields:
        doc["overview"] = {"text": OVERVIEW, "generated": True, "provider": provider.name}; done.append("overview")
    if "sections" in fields:
        doc["sections"] = [{"id": s["id"], "title": s["title"], "start": s["start"], "end": s["end"],
                            "est_minutes": 1, "brief": {"text": f"{s['title']} brief.", "generated": True,
                                                        "provider": provider.name}} for s in secs]
        done.append("sections")
    if "topics" in fields:
        doc["topics"] = [{"topic": secs[0]["title"], "section_id": secs[0]["id"], "heading": secs[0]["title"],
                          "generated": True, "provider": provider.name},
                         {"topic": "Read from the start", "section_id": None, "heading": None, "generated": False}]
        done.append("topics")
    e = doc.setdefault("enrichment", {})
    e.update({"provider": provider.name, "model": getattr(provider, "model", None), "elapsed_ms": 1,
              "fields": done, "done": sorted(set(e.get("done", [])) | set(done)), "guard_rejections": []})
    path.write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    return e


def failing_enrich_fixture(path, provider, force=False, log=None, fields=None):
    raise RuntimeError("ollama 500: model not loaded")


class Tab:
    def __init__(self, ws):
        self.ws = ws
        self.audio: dict = {}

    async def recv(self, timeout=5.0):
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
        frames = self.audio.get(ctx, 0) // 2
        ms = frames / 24000 * 1000
        await self.ws.send_json({"type": "rendered", "context_id": ctx, "rendered_ms": ms, "enqueued_frames": frames})
        await self.ws.send_json({"type": "unit_ended", "context_id": ctx, "enqueued_frames": frames, "rendered_ms": ms})


class NavigatorCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Set per test as well as at import: tests/test_enrich.py pops the
        # variable during its own run, which lands before this file in a
        # combined session.
        env = mock.patch.dict(os.environ, {"ENRICH_PROVIDER": "ollama"})
        env.start()
        self.addCleanup(env.stop)
        self.app = srv.build_app(dev=False, warm_converter=False)
        self.s = self.app["session"]
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()
        self.a = Tab(await self.client.ws_connect("/ws/audio"))
        await self.a.recv()                                          # hello
        idx = self.s.library.index_path
        before = idx.read_text(encoding="utf-8")
        self.addCleanup(lambda: idx.write_text(before, encoding="utf-8"))

    async def asyncTearDown(self):
        if not self.a.ws.closed:
            await self.a.ws.close()
        await self.client.close()
        p = getattr(self.s.events, "path", None)
        if p and Path(p).exists():
            Path(p).unlink()

    async def drain(self, until_task, on_message=None, ack=True, grace=1.5):
        """Keep the socket read (a browser always is: the server's heartbeat
        drops a tab that stops answering pings) and ack every unit, until
        `until_task` is done and `grace` seconds have passed. `on_message` sees
        every message and may act (a reply). Returns the units started."""
        units = []
        deadline = None
        while True:
            try:
                m = await self.a.recv(timeout=1.0)
            except asyncio.TimeoutError:
                m = None
            if m is not None:
                if m.get("type") == "unit_started":
                    units.append(m)
                if m.get("type") == "unit_done" and ack and m["context_id"] in self.a.audio:
                    await self.a.ack_all(m["context_id"])
                if on_message:
                    await on_message(m)
            if until_task.done():
                deadline = deadline or (asyncio.get_event_loop().time() + grace)
                if asyncio.get_event_loop().time() > deadline:
                    return units

    async def upload(self, stem: str, body: str = None):
        body = body or "\n\n".join(
            f"# Heading {i}\n\nUploaded paragraph number {i} that is comfortably longer than "
            f"the minimum clause length so that the merge rule leaves it alone." for i in range(1, 26))
        fd = FormData()
        fd.add_field("file", body.encode("utf-8"), filename=f"{stem}.md", content_type="text/plain")
        r = await self.client.post("/documents", data=fd)
        self.assertEqual(r.status, 200)
        frames = [json.loads(l[5:]) for l in (await r.text()).splitlines() if l.startswith("data:")]
        entry = frames[-1]["entry"]
        root = self.s.library.root
        for f in (root / f"{entry['name']}.json", root / f"{entry['name']}.ingest_report.json"):
            self.addCleanup(lambda f=f: f.unlink(missing_ok=True))
        return frames, entry

    async def first_units(self, n=2):
        """Kinds and texts of the first n spoken units after play."""
        await self.a.ws.send_json({"type": "play"})
        out = []
        while len(out) < n:
            st = await self.a.until(lambda m: m.get("type") == "unit_started")
            ctx = st["context_id"]
            await self.a.until(lambda m: m.get("type") == "unit_done" and m["context_id"] == ctx)
            await self.a.ack_all(ctx)
            if st["kind"] in ("pick_topic", "welcome", "start_choice"):            # the opening prompts: read on
                await self.a.ws.send_json({"type": "ask", "question": {"pick_topic": "from the top", "welcome": "the first one", "start_choice": "brief"}[st["kind"]]})
                continue
            out.append(st)
        await self.a.ws.send_json({"type": "pause"})
        return out



class TestNavigator(NavigatorCase):
    async def test_upload_enriches_as_a_stage_and_play_opens_with_the_overview(self):
        with mock.patch.object(enrich_mod, "enrich_fixture", fake_enrich_fixture):
            frames, entry = await self.upload("_nav_ok_test")
            stages = [f["stage"] for f in frames]
            self.assertEqual(stages[-1], "done")
            self.assertIn("enrich", stages, "the eighth stage runs on entry")
            en = next(f for f in frames if f["stage"] == "enrich" and f["status"] != "running")
            self.assertEqual(en["status"], "ok")
            done = self.s.events.of_type("enrich_done")
            self.assertEqual(done[0]["stage"], "navigator")
            self.assertEqual(sorted(done[0]["fields"]), ["overview", "sections", "topics"])
            # The rows say the navigator is ready before anyone presses play.
            row = next(d for d in self.s.listener_library() if d["name"] == entry["name"])
            self.assertEqual(row["navigator"], "ready")
            # No tab has the voice: nothing is said, and the first play is told.
            self.assertEqual(self.s.events.of_type("cue_skipped"), [])
            self.assertEqual([e["document"] for e in self.s.events.of_type("narration_pending")], [entry["name"]])

            await self.a.ws.send_json({"type": "open", "name": entry["name"]})
            await self.a.until(lambda m: m.get("type") == "document_opened")
            units = await self.first_units(3)
            self.assertEqual(units[0]["kind"], "companion", "the backlog first")
            self.assertEqual(units[0]["text_display"], f"While you were away I went through {entry['spoken_title']}. It has 25 sections.")
            self.assertEqual(units[1]["kind"], "map", "then play opens with the overview unit")
            self.assertIn("heading one through heading twenty five", units[1].get("text_display", "").lower())
            self.assertEqual(units[2].get("kind", "clause"), "clause")
            topics = self.s.events.of_type("topics_offered")
            self.assertTrue(topics and topics[-1]["n"] >= 2)

    async def test_with_the_voice_claimed_the_cues_precede_the_invitation(self):
        # The tab with the voice uploads: "Please wait ..." as ingestion starts,
        # "Nearly there ..." as the enrich stage starts, then -- the document
        # opened for it -- the start_choice invitation. In that order.
        await self.a.ws.send_json({"type": "interrupt"})          # claims the voice, nothing sounding
        await self.a.until(lambda m: m.get("type") == "sink")
        with mock.patch.object(enrich_mod, "enrich_fixture", fake_enrich_fixture):
            frames, entry = await self.upload("_nav_cue_test")
            self.assertEqual(frames[-1]["stage"], "done")
            kinds = []
            while len(kinds) < 3:
                st = await self.a.until(lambda m: m.get("type") == "unit_started")
                kinds.append((st["kind"], st["text_display"]))
                await self.a.until(lambda m: m.get("type") == "unit_done" and m["context_id"] == st["context_id"])
                await self.a.ack_all(st["context_id"])
            while not kinds or kinds[-1][0] != "start_choice":
                st = await self.a.until(lambda m: m.get("type") == "unit_started")
                kinds.append((st["kind"], st["text_display"]))
                await self.a.until(lambda m: m.get("type") == "unit_done" and m["context_id"] == st["context_id"])
                await self.a.ack_all(st["context_id"])
            # A fast ingestion: the companion's progress lines, then the invitation. Too
            # quick for the engagement question (asked at ~5 s), so no ingest_wait prompt.
            texts = [t for k, t in kinds if k == "companion"]
            self.assertEqual([k for k, _ in kinds][-1], "start_choice")
            self.assertTrue(all(k == "companion" for k, _ in kinds[:-1]), kinds)
            self.assertEqual(texts[0], "I'll pause here and go through Nav Cue Test.")   # the voice is taken for it
            self.assertEqual(texts[1], "Got the text.")
            self.assertTrue(texts[2].startswith("I can see 25 sections: Heading 1, Heading 2, Heading 3, and more."), texts[2])
            self.assertIn("Checking the wording and any personal details.", texts)
            self.assertIn("Nearly there, putting an overview together.", texts)
            self.assertEqual(texts[-1], "Done.")
            self.assertTrue(kinds[-1][1].startswith("I've gone through"), kinds[-1][1])
            self.assertEqual(self.s.library.current.name, entry["name"], "opened for the voice that uploaded it")
            self.assertEqual(self.s.events.of_type("cue_skipped"), [])
            self.assertEqual([e["kind"] for e in self.s.events.of_type("prompt_opened")], ["start_choice"])
            spoken = self.s.events.of_type("companion_spoken")
            self.assertEqual([e["source"] for e in spoken], ["takeover"] + ["event"] * (len(spoken) - 1))
            self.assertTrue(all(e["origin"] == "template" for e in spoken))
            gap = self.s.events.of_type("narration_gap_ms")[0]
            self.assertEqual(gap["count"], len(spoken))
            found = self.s.events.of_type("sections_found")[0]
            self.assertEqual((found["n"], found["titles"][:2]), (25, ["Heading 1", "Heading 2"]))
            # Spoken order in the trace: the invitation streams after the last companion line.
            self.assertGreater(self.s.events.of_type("start_choice_spoken")[0]["ts_ms"], spoken[-1]["ts_ms"])

    async def test_a_question_asked_while_ingesting_is_parked_and_answered_first(self):
        await self.a.ws.send_json({"type": "interrupt"})
        await self.a.until(lambda m: m.get("type") == "sink")

        def slow_fake(path, provider, force=False, log=None, fields=None):
            import time
            time.sleep(5.0)                          # a real generation takes tens of seconds: keep the prompt open
            return fake_enrich_fixture(path, provider, force, log, fields)   # (the progress lines speak first)

        with mock.patch.object(enrich_mod, "enrich_fixture", slow_fake), \
             mock.patch.object(cp, "COMPANION_QUESTION_AT_S", 0.3):          # the question early, for the test
            up = asyncio.ensure_future(self.upload("_nav_park_test"))
            # The ingest_wait prompt, heard; then the question, parked.
            st = await self.a.until(lambda m: m.get("type") == "unit_started" and m.get("kind") == "ingest_wait")
            await self.a.until(lambda m: m.get("type") == "unit_done" and m["context_id"] == st["context_id"])
            await self.a.ack_all(st["context_id"])
            pm = await self.a.until(lambda m: m.get("type") == "prompt" and m.get("kind") == "ingest_wait")
            self.assertEqual(pm["options"], ["question"])
            await self.a.ws.send_json({"type": "ask", "question": "what does paragraph 7 say"})
            kinds = []
            while not kinds or kinds[-1][0] != "start_choice":
                u = await self.a.until(lambda m: m.get("type") == "unit_started", timeout=30)
                kinds.append((u["kind"], u["text_display"]))
                await self.a.until(lambda m: m.get("type") == "unit_done" and m["context_id"] == u["context_id"])
                await self.a.ack_all(u["context_id"])
            frames, entry = await up
        texts = dict(kinds)
        # The companion acknowledges (a template: no model in tests), the progress lines follow as the
        # stages land, and the tail is fixed: "First, what you asked earlier.", the answer, then the
        # invitation, which names the parked phrase.
        lines = [(e["source"], e["origin"], e["text"]) for e in self.s.events.of_type("companion_spoken")]
        self.assertIn(("ack", "template", "Okay, I'll keep that in mind: what does paragraph 7 say."), lines)
        self.assertIn(("question", "template", cp.ENGAGEMENT_QUESTION), lines)
        self.assertNotIn("plan", [s for s, _, _ in lines], "a reply means no plan line")
        self.assertEqual([k for k, _ in kinds][-3:], ["companion", "answer", "start_choice"])
        self.assertEqual(kinds[-3][1], "First, what you asked earlier.")
        self.assertIn("paragraph number seven", texts["answer"])
        self.assertTrue(kinds[-1][1].endswith(" Or shall I start with what does paragraph 7 say?"), kinds[-1][1])
        self.assertEqual(self.s.events.of_type("question_parked")[0]["question"], "what does paragraph 7 say")
        types = [e["type"] for e in self.s.events.records]
        self.assertLess(types.index("answer_grounded"), types.index("start_choice_spoken"),
                        "the parked question is answered before the invitation")
        self.assertEqual([(e["kind"], e["by"]) for e in self.s.events.of_type("prompt_resolved")][0],
                         ("ingest_wait", "reply"))
        self.assertIsNone(self.s.conv.parked_question)
        # The reader answered it, through grounding.resolve: answer_source is in the trace, and no
        # companion line ever carried the answer's words.
        self.assertTrue(any(e.get("parked") for e in self.s.events.of_type("answer_source")))
        answer = texts["answer"].lower()
        for _, _, line in lines:
            self.assertNotIn(answer[:40], line.lower())
            self.assertNotIn("paragraph number seven", line.lower())

    async def test_slow_stages_are_bridged_without_a_reply(self):
        # Stage events 15 s apart (structure, segment held back): progress lines
        # on the real events, exactly one engagement question at ~5 s, the plan
        # line at 20 s with no reply, one filler after 12 s of silence, and the
        # longest silence under 15 s.
        await self.a.ws.send_json({"type": "interrupt"})
        await self.a.until(lambda m: m.get("type") == "sink")
        with mock.patch.object(enrich_mod, "enrich_fixture", fake_enrich_fixture), \
             mock.patch.object(ingest_mod, "ingest_document", slow_ingest(15.0, ("structure", "segment", "normalize"))):
            up = asyncio.ensure_future(self.upload("_nav_slow_test"))
            await self.drain(up)
            frames, entry = await up
        self.assertEqual(frames[-1]["stage"], "done")
        spoken = [(e["source"], e["text"]) for e in self.s.events.of_type("companion_spoken")]
        sources = [s for s, _ in spoken]
        self.assertEqual(sources.count("question"), 1, spoken)
        self.assertEqual(sources.count("plan"), 1, spoken)
        fillers = [t for s_, t in spoken if s_ == "filler"]
        self.assertGreaterEqual(len(fillers), 1, spoken)
        self.assertLessEqual(len(fillers), cp.COMPANION_MAX_FILLERS)
        self.assertEqual(len(fillers), len(set(fillers)), "a filler is never repeated")
        self.assertEqual(sources.count("ack"), 0)
        self.assertGreaterEqual(sources.count("event"), 4)
        self.assertEqual(spoken[0], ("takeover", "I'll pause here and go through Nav Slow Test."))
        self.assertEqual(spoken[1][1], "Got the text.")
        self.assertIn(("plan", cp.PLAN_LINE), spoken)
        self.assertEqual([(e["kind"], e["by"]) for e in self.s.events.of_type("prompt_resolved")][0],
                         ("ingest_wait", "timeout"), "no reply: the question closes at 20 s")
        gap = self.s.events.of_type("narration_gap_ms")[0]
        self.assertLess(gap["max"], 15000, gap)
        self.assertEqual(gap["count"], len(spoken))
        # Templates carry counts and titles (allowed); what the companion may never do is
        # paraphrase the document: no spoken line contains a six-word span of its clauses.
        fixture = json.loads((self.s.library.root / f"{entry['name']}.json").read_text(encoding="utf-8"))
        self.assertTrue(all(cp.companion_guard(t, fixture) != "fixture span" for _, t in spoken), spoken)
        self.assertEqual(self.s.events.of_type("sections_found")[0]["n"], 25)

    async def test_a_reply_during_slow_stages_is_acknowledged_and_there_is_no_plan_line(self):
        await self.a.ws.send_json({"type": "interrupt"})
        await self.a.until(lambda m: m.get("type") == "sink")
        replied = []

        async def reply_to_the_question(m):
            if m.get("type") == "prompt" and m.get("kind") == "ingest_wait" and not replied:
                replied.append(True)
                await self.a.ws.send_json({"type": "ask", "question": "what does paragraph 7 say"})

        with mock.patch.object(enrich_mod, "enrich_fixture", fake_enrich_fixture), \
             mock.patch.object(ingest_mod, "ingest_document", slow_ingest(15.0, ("structure", "segment", "normalize"))):
            up = asyncio.ensure_future(self.upload("_nav_slow_reply_test"))
            units = await self.drain(up, on_message=reply_to_the_question, grace=4.0)
            frames, entry = await up
        self.assertTrue(replied, "the engagement question was asked and answered")
        kinds = [u["kind"] for u in units]
        self.assertIn("answer", kinds, "the parked question was answered at ready")
        self.assertEqual(kinds[-1], "start_choice", kinds)
        spoken = [(e["source"], e["text"]) for e in self.s.events.of_type("companion_spoken")]
        sources = [s for s, _ in spoken]
        self.assertEqual(sources.count("ack"), 1, spoken)
        self.assertEqual(sources.count("plan"), 0, spoken)
        self.assertEqual(sources.count("question"), 1)
        self.assertIn(("ack", "Okay, I'll keep that in mind: what does paragraph 7 say."), spoken)
        self.assertEqual(self.s.conv.parked_question, None, "answered at ready")
        self.assertTrue(any(e.get("parked") for e in self.s.events.of_type("answer_source")))
        self.assertLess(self.s.events.of_type("narration_gap_ms")[0]["max"], 15000)

    async def test_opening_an_unenriched_document_is_narrated_not_silent(self):
        # A document that entered without its navigator (a broken provider at
        # upload), then opened with the voice claimed and a working, slow (10 s)
        # generation: the companion speaks "Getting to know ...", asks its
        # question at ~5 s, says "Done." -- no silence near the 120 s budget,
        # no enrich_timeout, and the invitation follows.
        with mock.patch.object(enrich_mod, "enrich_fixture", failing_enrich_fixture):
            frames, entry = await self.upload("_nav_open_narrated_test")
        self.s._enriching.clear()
        self.s.events.records.clear() if hasattr(self.s.events, "records") and isinstance(self.s.events.records, list) else None

        def slow_ok(path, provider, force=False, log=None, fields=None):
            import time
            time.sleep(10.0)
            return fake_enrich_fixture(path, provider, force, log, fields)

        await self.a.ws.send_json({"type": "interrupt"})
        await self.a.until(lambda m: m.get("type") == "sink")
        got = asyncio.get_event_loop().create_future()

        async def until_invited(m):
            if m.get("type") == "prompt" and m.get("kind") == "start_choice" and not got.done():
                got.set_result(m)

        with mock.patch.object(enrich_mod, "enrich_fixture", slow_ok):
            await self.a.ws.send_json({"type": "open", "name": entry["name"]})
            units = await self.drain(got, on_message=until_invited, grace=0.5)
            self.assertTrue(got.done(), "the invitation followed the narration")
        kinds = [(u["kind"], u["text_display"]) for u in units]
        self.assertEqual(kinds[0][0], "companion", kinds)
        self.assertEqual(kinds[0][1], f"I'll pause here and go through {entry['spoken_title']}.")
        self.assertTrue(kinds[1][1].startswith("I can see 25 sections: Heading 1, Heading 2, Heading 3, and more."), kinds[1][1])
        self.assertEqual(entry["spoken_title"], "Heading 1", "the first real heading is the spoken title")
        spoken = [(e["source"], e["text"]) for e in self.s.events.of_type("companion_spoken")]
        self.assertIn(("question", cp.ENGAGEMENT_QUESTION), spoken)
        self.assertEqual(spoken[-1], ("event", "Done."))
        self.assertTrue(all(e.get("document") == entry["name"] for e in self.s.events.of_type("companion_spoken")), "bound to the document")
        gap = self.s.events.of_type("narration_gap_ms")[-1]
        self.assertLess(gap["max"], 15000, gap)
        self.assertEqual(self.s.events.of_type("enrich_timeout"), [])
        self.assertEqual(self.s.events.of_type("prompt_opened")[-1]["kind"], "start_choice")

    async def test_failed_generation_still_lets_the_document_in_with_the_mechanical_map(self):
        with mock.patch.object(enrich_mod, "enrich_fixture", failing_enrich_fixture):
            frames, entry = await self.upload("_nav_fail_test")
            en = next(f for f in frames if f["stage"] == "enrich" and f["status"] != "running")
            self.assertEqual(en["status"], "error")
            self.assertEqual(frames[-1]["status"], "ok", "the document enters regardless")
            self.assertTrue(self.s.events.of_type("enrich_failed"))
            row = next(d for d in self.s.listener_library() if d["name"] == entry["name"])
            self.assertEqual(row["navigator"], "mechanical")

            await self.a.ws.send_json({"type": "open", "name": entry["name"]})
            await self.a.until(lambda m: m.get("type") == "document_opened")
            units = await self.first_units(2)
            self.assertEqual(units[0]["kind"], "companion", "uploaded with no voice: the first play is told")
            self.assertTrue(units[0]["text_display"].startswith("While you were away I went through"), units[0]["text_display"])
            self.assertEqual(units[1]["kind"], "map")
            self.assertRegex(units[1].get("text_display", ""), r"has \d+ sections")

    async def test_opening_an_unenriched_document_starts_generation_on_entry(self):
        # Ingest with a broken provider so the fixture enters unenriched; then
        # a later open, with a working one, generates on entry and play waits
        # for it behind a spoken cue instead of reading blind.
        with mock.patch.object(enrich_mod, "enrich_fixture", failing_enrich_fixture):
            frames, entry = await self.upload("_nav_late_test")
        self.s._enriching.clear()

        async def slow_then_ok(path, provider, force=False, log=None, fields=None):
            await asyncio.sleep(0.3)
            return fake_enrich_fixture(path, provider, force, log, fields)

        def sync_slow(path, provider, force=False, log=None, fields=None):
            import time
            time.sleep(0.3)
            return fake_enrich_fixture(path, provider, force, log, fields)

        with mock.patch.object(enrich_mod, "enrich_fixture", sync_slow):
            await self.a.ws.send_json({"type": "open", "name": entry["name"]})
            await self.a.until(lambda m: m.get("type") == "document_opened")
            prep = await self.a.until(lambda m: m.get("type") == "navigator")
            self.assertEqual(prep["state"], "preparing")
            units = await self.first_units(3)
        # Uploaded and opened with no voice: the first play is told first.
        self.assertEqual(units[0]["kind"], "companion")
        self.assertTrue(units[0]["text_display"].startswith("While you were away I"), units[0]["text_display"])
        units = units[1:]
        kinds = [u["kind"] for u in units]
        # Either the cue then the overview (play beat the generation) or the
        # overview straight away (generation beat play). Never a blind clause.
        self.assertIn(kinds, [["cue", "map"], ["map", "clause"]])
        if kinds[0] == "cue":
            self.assertIn("overview", units[0].get("text_display", "").lower())
        # The background "rest" stage can land first with an instant fake, so
        # look for the navigator stage rather than at the last event.
        self.assertIn("navigator", [e["stage"] for e in self.s.events.of_type("enrich_done")])


if __name__ == "__main__":
    unittest.main()
