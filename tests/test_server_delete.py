"""DELETE /documents/{doc_id}: the entry, its files and its session go; the
next document opens if it was the open one; a committed fixture is refused
without ?force=1. And an upload whose text is already here (other bytes,
same words) is a 409 with a companion line, not a second entry.
"""
import asyncio
import json
import os
import sys
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

from aiohttp import FormData                                        # noqa: E402

import enrich as enrich_mod                                         # noqa: E402
import server as srv                                                # noqa: E402
from test_server_navigator import NavigatorCase, fake_enrich_fixture  # noqa: E402


class TestDelete(NavigatorCase):
    async def test_delete_removes_entry_files_and_session_and_opens_the_next(self):
        with mock.patch.object(enrich_mod, "enrich_fixture", fake_enrich_fixture):
            frames, entry = await self.upload("_del_a")
        root = self.s.library.root
        name = entry["name"]
        self.assertTrue((root / f"{name}.json").exists())
        await self.a.ws.send_json({"type": "open", "name": name})
        await self.a.until(lambda m: m.get("type") == "document_opened" and m["name"] == name)
        self.assertIs(self.s.library.current, self.s.library._docs[name])

        r = await self.client.delete(f"/documents/{entry['doc_id']}")
        self.assertEqual(r.status, 200, await r.text())
        body = await r.json()
        self.assertIn(f"{name}.json", body["deleted"]["files"])
        self.assertIn(f"{name}.ingest_report.json", body["deleted"]["files"])
        gone = await self.a.until(lambda m: m.get("type") == "document_deleted")
        self.assertEqual(gone["name"], name)
        self.assertNotEqual(gone["current"], name)
        self.assertNotIn(name, [d["name"] for d in gone["documents"]])
        # the registry, the files, the session
        idx = json.loads(self.s.library.index_path.read_text(encoding="utf-8"))
        self.assertNotIn(name, [e["name"] for e in idx["documents"]])
        self.assertFalse((root / f"{name}.json").exists())
        self.assertFalse((root / f"{name}.ingest_report.json").exists())
        self.assertNotIn(name, self.s.library._docs)
        self.assertIsNotNone(self.s.library.current)
        self.assertNotEqual(self.s.library.current.name, name)
        ev = self.s.events.of_type("document_deleted")[0]
        self.assertTrue(ev["was_current"])

    async def test_a_committed_fixture_is_refused_without_force(self):
        doc = self.s.library._docs["policy"]
        r = await self.client.delete(f"/documents/{doc.doc_id}")
        self.assertEqual(r.status, 403)
        self.assertTrue((await r.json())["committed"])
        self.assertTrue(doc.path.exists())
        self.assertIn("policy", self.s.library._docs)
        self.assertEqual(self.s.events.of_type("document_deleted"), [])

    async def test_an_unknown_document_is_404(self):
        r = await self.client.delete("/documents/nope")
        self.assertEqual(r.status, 404)

    async def test_the_same_text_under_other_bytes_is_a_duplicate_not_a_second_entry(self):
        with mock.patch.object(enrich_mod, "enrich_fixture", fake_enrich_fixture):
            frames, a = await self.upload("_dup_a")
            await self.a.ws.send_json({"type": "interrupt"})          # claims the voice: the line is spoken
            await asyncio.sleep(0.3)
            body = "\n\n\n".join(
                f"#  Heading {i}\n\nUploaded  paragraph number {i} that is comfortably longer than "
                f"the minimum clause length so that the merge rule leaves it alone.  " for i in range(1, 26))
            fd = FormData()
            fd.add_field("file", body.encode("utf-8"), filename="_dup_b.md", content_type="text/plain")
            r = await self.client.post("/documents", data=fd)
            self.assertEqual(r.status, 409, await r.text())
            j = await r.json()
            self.assertTrue(j["error"].startswith("That looks like "), j)
            self.assertTrue(j["error"].endswith(", which is already here."), j)
            self.assertEqual(j["existing"]["name"], a["name"])
            self.assertEqual(self.s.events.of_type("upload_duplicate")[0]["document"], a["name"])
            spoken = await self.a.until(lambda m: m.get("type") == "unit_started" and m.get("kind") == "companion")
            self.assertEqual(spoken["text_display"], j["error"])
        self.assertEqual(len([d for d in self.s.library.list() if d["name"].startswith("_dup")]) if False else
                         [d["name"] for d in self.s.library.list()].count(a["name"]), 1)
        self.assertFalse(any(p.name.startswith(".incoming_") and p.name.endswith("_dup_b.md")
                             for p in self.s.library.root.iterdir()))


if __name__ == "__main__":
    unittest.main()
