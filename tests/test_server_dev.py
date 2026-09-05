"""Server: status/metrics shape, the dev-mode boundary, and trace replay.

The dev-mode assertions are the important ones. Runtime ingestion is the single
place where the demo could grow a path from an upload into the judged flow, so
these tests pin both halves: it is absent without --dev, and with --dev it
writes under fixtures/unreviewed/ and leaves index.json untouched.

Everything here runs on TTS_PROVIDER=fake: no key, no network.
"""
import json
import os
import unittest
from pathlib import Path

os.environ["TTS_PROVIDER"] = "fake"
os.environ.pop("LLM_API_KEY", None)

import sys                                                            # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "examples" / "policy-reader"))

from aiohttp.test_utils import TestClient, TestServer               # noqa: E402

import server as srv                                                # noqa: E402

TRACES = ROOT / "traces"
UNREVIEWED = ROOT / "examples" / "policy-reader" / "fixtures" / "unreviewed"
INDEX = ROOT / "examples" / "policy-reader" / "fixtures" / "index.json"

SAMPLE_TRACE = "\n".join(json.dumps(r) for r in [
    {"ts_ms": 0.0, "type": "provider_active", "provider": "fake", "modelId": "fake"},
    {"ts_ms": 10.0, "type": "synth_requested", "context_id": "sec-1-i#t1", "chars": 40},
    {"ts_ms": 30.0, "type": "synth_first_byte", "context_id": "sec-1-i#t1", "ttfb_ms": 20.0},
    {"ts_ms": 60.0, "type": "frames_played", "context_id": "sec-1-i#t1", "rendered_ms": 500.0},
    {"ts_ms": 80.0, "type": "cancel_issued", "generation": 1},
    {"ts_ms": 85.0, "type": "result_fenced", "context_id": "sec-1-i#t1", "bytes": 4096},
    {"ts_ms": 90.0, "type": "unit_truncated", "context_id": "sec-1-i", "char_end": 30, "of": 136},
])


class ServerCase(unittest.IsolatedAsyncioTestCase):
    DEV = False

    async def asyncSetUp(self):
        self.app = srv.build_app(dev=self.DEV)
        self.session = self.app["session"]
        self.server = TestServer(self.app)
        self.client = TestClient(self.server)
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        p = getattr(self.session.events, "path", None)
        if p and Path(p).exists():
            Path(p).unlink()


class TestStatusAndMetrics(ServerCase):
    async def test_status_has_all_six_cells_with_state_and_detail(self):
        r = await self.client.get("/api/status")
        self.assertEqual(r.status, 200)
        d = await r.json()
        for key in ("ingest", "normalize", "rime_ws", "client_ws", "stt", "llm"):
            with self.subTest(key):
                self.assertIn(key, d)
                self.assertIn(d[key]["state"], ("ok", "warn", "down", "off"))
                self.assertIsInstance(d[key]["detail"], str)
        self.assertTrue(d["session_id"].startswith("web-"))
        self.assertIn("provider", d)
        self.assertFalse(d["dev"])

    async def test_llm_is_off_and_stt_is_button_only_without_keys(self):
        d = await (await self.client.get("/api/status")).json()
        self.assertEqual(d["llm"], {"state": "off", "detail": "extractive"})
        self.assertEqual(d["stt"], {"state": "warn", "detail": "button only"})

    async def test_ingest_cell_is_off_outside_dev(self):
        d = await (await self.client.get("/api/status")).json()
        self.assertEqual(d["ingest"]["state"], "off")

    async def test_metrics_are_null_on_an_empty_session(self):
        d = await (await self.client.get("/api/metrics")).json()
        for k in ("ttfb_p50", "ttfb_p95", "fenced_bytes_after_clear",
                  "interpolated_spans", "spans_total", "flush_ack_p50"):
            with self.subTest(k):
                self.assertIn(k, d)
                self.assertIsNone(d[k], f"{k} should be null before anything happened")

    async def test_contexts_and_events_are_empty_but_well_formed(self):
        self.assertEqual((await (await self.client.get("/api/contexts")).json())["contexts"], [])
        recs = (await (await self.client.get("/api/events")).json())["records"]
        self.assertIsInstance(recs, list)

    async def test_library_entries_are_listener_safe(self):
        d = await (await self.client.get("/api/library")).json()
        self.assertTrue(d["documents"])
        for doc in d["documents"]:
            for k in ("name", "title", "section_count", "estimated_minutes",
                      "progress", "referral"):
                self.assertIn(k, doc)
            self.assertGreater(doc["estimated_minutes"], 0)
            self.assertTrue(doc["referral"])

    async def test_traces_listing_reports_sizes(self):
        d = await (await self.client.get("/api/traces")).json()
        self.assertIsInstance(d["traces"], list)
        for t in d["traces"]:
            self.assertIn("name", t)
            self.assertIsInstance(t["bytes"], int)


class TestDevDisabled(ServerCase):
    DEV = False

    async def test_dev_ingest_is_404_without_dev(self):
        r = await self.client.post("/api/dev/ingest", json={"url": "https://example.com"})
        self.assertEqual(r.status, 404)

    async def test_dev_provider_is_404_without_dev(self):
        r = await self.client.post("/api/dev/provider", json={"name": "fake"})
        self.assertEqual(r.status, 404)

    async def test_dev_open_unreviewed_is_404_without_dev(self):
        r = await self.client.post("/api/dev/open?unreviewed=1", json={"name": "anything"})
        self.assertEqual(r.status, 404)


class TestDevEnabled(ServerCase):
    DEV = True

    async def test_status_reports_dev(self):
        d = await (await self.client.get("/api/status")).json()
        self.assertTrue(d["dev"])
        self.assertEqual(d["ingest"]["state"], "ok")

    async def test_provider_swap_to_fake_is_allowed(self):
        r = await self.client.post("/api/dev/provider", json={"name": "fake"})
        self.assertEqual(r.status, 200)
        self.assertEqual((await r.json())["provider"]["provider"], "fake")

    async def test_provider_swap_rejects_unknown_name(self):
        r = await self.client.post("/api/dev/provider", json={"name": "elevenlabs"})
        self.assertEqual(r.status, 400)

    async def test_ingest_writes_only_under_unreviewed_and_leaves_index_alone(self):
        index_before = INDEX.read_text(encoding="utf-8")
        before = set(p.name for p in UNREVIEWED.glob("*.json")) if UNREVIEWED.exists() else set()

        sample = ROOT / "traces" / "_ingest_sample.md"
        body = "\n\n".join(
            f"# Section {i}\n\nThis paragraph of the sample document is comfortably longer "
            f"than the minimum clause length so that it survives the merge rule intact, "
            f"number {i}." for i in range(1, 26))
        sample.write_text(body, encoding="utf-8")
        self.addCleanup(lambda: sample.unlink(missing_ok=True))

        r = await self.client.post("/api/dev/ingest", json={"url": str(sample)})
        # a local path is not a url; the endpoint takes it as a source string
        payload = await r.json()

        if r.status == 200:
            out = ROOT / payload["path"]
            self.addCleanup(lambda: out.unlink(missing_ok=True))
            self.assertIn("fixtures/unreviewed/", payload["path"].replace(os.sep, "/"))
            self.assertTrue(out.exists())
            self.assertLessEqual(len(payload["clauses"]), 5)
            self.assertIn("unreviewed", payload["note"])
        # whatever happened, the registry must be untouched
        self.assertEqual(INDEX.read_text(encoding="utf-8"), index_before,
                         "dev ingest must never write to index.json")
        after = set(p.name for p in UNREVIEWED.glob("*.json")) if UNREVIEWED.exists() else set()
        self.assertTrue(after >= before)

    async def test_ingest_never_registers_the_document_in_the_listener_library(self):
        before = {d["name"] for d in (await (await self.client.get("/api/library")).json())["documents"]}
        sample = ROOT / "traces" / "_ingest_sample2.md"
        sample.write_text("\n\n".join(
            f"# H{i}\n\nA sufficiently long paragraph of sample text for clause {i} that "
            f"clears the minimum length threshold comfortably." for i in range(1, 26)),
            encoding="utf-8")
        self.addCleanup(lambda: sample.unlink(missing_ok=True))
        r = await self.client.post("/api/dev/ingest", json={"url": str(sample)})
        if r.status == 200:
            written = ROOT / (await r.json())["path"]
            self.addCleanup(written.unlink, True)
        after = {d["name"] for d in (await (await self.client.get("/api/library")).json())["documents"]}
        self.assertEqual(before, after,
                         "an ingested document must not appear in the listener library")


class TestReplay(ServerCase):
    DEV = False

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.trace = TRACES / "_test_replay.jsonl"
        self.trace.write_text(SAMPLE_TRACE, encoding="utf-8")
        self.addCleanup(lambda: self.trace.unlink(missing_ok=True))

    async def test_trace_appears_in_the_listing(self):
        names = [t["name"] for t in (await (await self.client.get("/api/traces")).json())["traces"]]
        self.assertIn("_test_replay.jsonl", names)

    async def test_replay_emits_start_records_and_end(self):
        ws = await self.client.ws_connect("/ws/audio")
        try:
            await ws.receive_json(timeout=5)          # hello
            r = await self.client.post("/api/replay", json={"trace": "_test_replay.jsonl"})
            self.assertEqual(r.status, 200)

            kinds, records, deadline = [], [], 12
            import asyncio
            async def drain():
                while True:
                    m = await ws.receive_json()
                    kinds.append(m.get("type"))
                    if m.get("type") == "event":
                        records.append(m["record"])
                    if m.get("type") == "replay_end":
                        return
            await asyncio.wait_for(drain(), timeout=deadline)
        finally:
            await ws.close()

        self.assertIn("replay_start", kinds)
        self.assertIn("replay_end", kinds)
        self.assertLess(kinds.index("replay_start"), kinds.index("replay_end"))
        types = [r.get("type") for r in records]
        self.assertIn("unit_truncated", types)
        self.assertIn("result_fenced", types)

    async def test_replay_of_a_missing_trace_is_404(self):
        r = await self.client.post("/api/replay", json={"trace": "nope.jsonl"})
        self.assertEqual(r.status, 404)

    async def test_replay_cannot_escape_the_traces_directory(self):
        r = await self.client.post("/api/replay", json={"trace": "../.env"})
        self.assertEqual(r.status, 404)


if __name__ == "__main__":
    unittest.main()
