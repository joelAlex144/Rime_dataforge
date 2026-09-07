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
os.environ.pop("LLM_PROVIDER", None)     # a sourced .env with LLM_PROVIDER=ollama must not reach the tests

import sys                                                            # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "examples" / "policy-reader"))

from aiohttp.test_utils import TestClient, TestServer               # noqa: E402

import server as srv                                                # noqa: E402

TRACES = ROOT / "traces"
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
    UPLOAD = True          # the demo default; the gate tests turn it off

    async def asyncSetUp(self):
        self.app = srv.build_app(dev=self.DEV, allow_upload=self.UPLOAD)
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

    async def test_llm_is_off_and_stt_describes_the_external_bridge(self):
        d = await (await self.client.get("/api/status")).json()
        self.assertEqual(d["llm"], {"state": "off", "detail": "extractive"})
        self.assertEqual(d["stt"]["state"], "warn")
        self.assertIn("voice bridge", d["stt"]["detail"])

    async def test_ingest_cell_reports_the_structure_pass_and_upload_is_always_on(self):
        d = await (await self.client.get("/api/status")).json()
        self.assertIn(d["ingest"]["state"], ("ok", "warn"))
        self.assertTrue(d["upload_enabled"])
        self.assertFalse(d["dev"], "upload on does not mean dev on")

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

    async def test_dev_provider_is_404_without_dev(self):
        r = await self.client.post("/api/dev/provider", json={"name": "fake"})
        self.assertEqual(r.status, 404)

    async def test_hello_carries_upload_enabled(self):
        ws = await self.client.ws_connect("/ws/audio")
        try:
            hello = await ws.receive_json(timeout=5)
        finally:
            await ws.close()
        self.assertTrue(hello["upload_enabled"])
        self.assertFalse(hello["dev"])


class TestDevEnabled(ServerCase):
    DEV = True

    async def test_status_reports_dev(self):
        d = await (await self.client.get("/api/status")).json()
        self.assertTrue(d["dev"])
        self.assertIn(d["ingest"]["state"], ("ok", "warn"))

    async def test_provider_swap_to_fake_is_allowed(self):
        r = await self.client.post("/api/dev/provider", json={"name": "fake"})
        self.assertEqual(r.status, 200)
        self.assertEqual((await r.json())["provider"]["provider"], "fake")

    async def test_provider_swap_rejects_unknown_name(self):
        r = await self.client.post("/api/dev/provider", json={"name": "elevenlabs"})
        self.assertEqual(r.status, 400)


class TempLibraryCase(unittest.IsolatedAsyncioTestCase):
    """A server over an empty temporary library, so uploads never touch the
    real fixtures/ or its index.json."""
    async def asyncSetUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.index = Path(self.tmp.name) / "index.json"
        self.index.write_text('{"documents": []}', encoding="utf-8")
        self.app = srv.build_app(dev=False, index_path=self.index)
        self.session = self.app["session"]
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        p = getattr(self.session.events, "path", None)
        if p and Path(p).exists():
            Path(p).unlink()
        self.tmp.cleanup()

    @staticmethod
    def frames(text: str) -> list:
        out = []
        for block in text.split("\n\n"):
            for line in block.splitlines():
                if line.startswith("data:"):
                    out.append(json.loads(line[5:].strip()))
        return out

    async def upload(self, name: str, body: str):
        from aiohttp import FormData
        fd = FormData()
        fd.add_field("file", body.encode("utf-8"), filename=name, content_type="text/plain")
        r = await self.client.post("/documents", data=fd)
        return r, (self.frames(await r.text()) if r.status == 200 else await r.json())


def _sample_md(extra: str = "") -> str:
    return ("# Northlake General Insurance Company Limited\n\nPolicy wording, UIN 111N128V01. "
            "For any complaint write to grievance@insurer.co.in or escalate to the regulator at "
            "complaints@irdai.gov.in. Toll free helpline 1800 209 5858 (Mon-Sat). " + extra + "\n\n"
            + "\n\n".join(
                f"# Section {i}\n\nThis paragraph of the sample policy is comfortably longer than "
                f"the minimum clause length so that it survives the merge rule intact, number {i}."
                for i in range(1, 26)))


PERSON = "Policyholder Ramesh Kumar, ramesh.k@gmail.com, 9876543210, is insured."


class TestDocumentsUpload(TempLibraryCase):
    async def test_streams_every_stage_then_the_library_entry(self):
        r, frames = await self.upload("sample.md", _sample_md())
        self.assertEqual(r.status, 200)
        self.assertEqual(r.headers["Content-Type"].split(";")[0], "text/event-stream")
        stages = [f["stage"] for f in frames]
        self.assertEqual(stages[:7], list(srv.INGEST_STAGES))
        self.assertEqual(stages[-1], "done")
        for f in frames[:7]:
            self.assertEqual(f["status"], "ok")
            self.assertIn("elapsed_ms", f)
        entry = frames[-1]["entry"]
        for k in ("doc_id", "name", "title", "reviewed", "readable", "clause_count"):
            self.assertIn(k, entry)
        self.assertFalse(entry["reviewed"])
        self.assertTrue(entry["readable"])
        self.assertGreater(entry["clause_count"], 0)
        self.assertEqual(len(entry["doc_id"]), 16)
        docs = json.loads(self.index.read_text(encoding="utf-8"))["documents"]
        self.assertEqual([d["doc_id"] for d in docs], [entry["doc_id"]])
        self.assertFalse(docs[0]["reviewed"])
        self.assertTrue((self.index.parent / f"{entry['name']}.json").exists())
        self.assertTrue((self.index.parent / f"{entry['name']}.ingest_report.json").exists())
        lib = self.session.listener_library()
        self.assertEqual([d["doc_id"] for d in lib], [entry["doc_id"]])
        self.assertTrue(lib[0]["unreviewed"])

    async def test_same_bytes_twice_returns_the_existing_entry(self):
        _, first = await self.upload("a.md", _sample_md())
        r, second = await self.upload("renamed.md", _sample_md())
        self.assertEqual(r.status, 200)
        self.assertEqual(len(second), 1)
        self.assertTrue(second[0]["existing"])
        self.assertEqual(second[0]["entry"]["doc_id"], first[-1]["entry"]["doc_id"])
        self.assertEqual(len(json.loads(self.index.read_text(encoding="utf-8"))["documents"]), 1)

    async def test_personal_data_never_blocks_but_is_in_the_report(self):
        r, frames = await self.upload("pers.md", _sample_md(PERSON))
        self.assertEqual(r.status, 200)
        entry = frames[-1]["entry"]
        rep = await (await self.client.get(f"/documents/{entry['doc_id']}/report")).json()
        self.assertEqual(sorted(h["label"] for h in rep["pii_scan"]["personal"]), ["email", "phone"])
        self.assertTrue(all(h["clause_id"] for h in rep["pii_scan"]["personal"]))
        self.assertEqual(len(rep["pii_scan"]["institutional"]), 3)
        self.assertIn("by_kind", rep["validate"])
        self.assertIn("boilerplate_first_15", rep["validate"])
        self.assertTrue(rep["validate"]["oversized_ok"])

    async def test_accept_sets_reviewed(self):
        _, frames = await self.upload("acc.md", _sample_md())
        doc_id = frames[-1]["entry"]["doc_id"]
        r = await self.client.post(f"/documents/{doc_id}/accept")
        self.assertEqual(r.status, 200)
        self.assertTrue((await r.json())["reviewed"])
        self.assertTrue(json.loads(self.index.read_text(encoding="utf-8"))["documents"][0]["reviewed"])
        self.assertFalse(self.session.listener_library()[0]["unreviewed"])
        self.assertEqual(len(self.session.events.of_type("document_accepted")), 1)

    async def test_no_body_text_is_readable_false_not_an_error(self):
        r, frames = await self.upload("scan.md", "Page 1 of 3\n\n2\n\nPage 3 of 3\n")
        self.assertEqual(r.status, 200, frames)
        self.assertFalse(frames[-1]["entry"]["readable"])
        self.assertEqual([d["readable"] for d in self.session.listener_library()], [False])

    async def test_the_two_http_errors(self):
        from aiohttp import FormData
        fd = FormData()
        fd.add_field("file", b"MZ...", filename="setup.exe", content_type="application/octet-stream")
        self.assertEqual((await self.client.post("/documents", data=fd)).status, 415)
        cap = srv.MAX_UPLOAD_BYTES
        srv.MAX_UPLOAD_BYTES = 1000
        try:
            fd = FormData()
            fd.add_field("file", b"x" * 2000, filename="big.txt", content_type="text/plain")
            self.assertEqual((await self.client.post("/documents", data=fd)).status, 413)
        finally:
            srv.MAX_UPLOAD_BYTES = cap

    async def test_report_404_for_unknown_document(self):
        self.assertEqual((await self.client.get("/documents/nope/report")).status, 404)


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
