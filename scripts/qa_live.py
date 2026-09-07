#!/usr/bin/env python3
"""The QA live pass, repeatable: one script against a running policy reader.

    set -a; source .env; set +a
    TTS_PROVIDER=fake python examples/policy-reader/server.py --dev --port 8090 &
    python scripts/qa_live.py --port 8090            # then once with TTS_PROVIDER=rime

Or let it start the server itself: `python scripts/qa_live.py --serve --tts fake`.

What it does, printing every companion and prompt line with a timestamp:
  1. claims the voice and starts reading saral_jeevan_bima
  2. uploads fixtures/source/fixture_arogya_sanjeevani.docx converted to PDF
     while that is being read (the voice is taken over for the upload)
  3. answers the engagement question with "what is the waiting period"
  4. waits for start_choice (the parked question is answered first)
  5. replies "exclusions"
  6. reads into a table and replies "the second one"
  7. deletes the uploaded document
Then it prints narration_gap_ms for the upload, topics_offered.n, the
uploaded fixture's heading list (no PAGE markers, no underscores) and the
delete result, and exits 1 if the longest silence during processing was over
15 s or the headings carried junk.
"""
import argparse
import asyncio
import base64
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import aiohttp

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "examples" / "policy-reader" / "fixtures"
DOCX = FIX / "source" / "fixture_arogya_sanjeevani.docx"
GAP_LIMIT_MS = 15000


# ------------------------------------------------------------------ docx -> pdf
def _pdf_escape(s: str) -> str:
    s = (s.replace("—", "-").replace("–", "-").replace("‘", "'").replace("’", "'")
         .replace("“", '"').replace("”", '"').replace("₹", "Rs ").replace("•", "-"))
    s = s.encode("latin-1", "replace").decode("latin-1")
    return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def docx_to_pdf(src: Path, dst: Path) -> int:
    """A plain PDF of the docx's paragraphs (headings bold and larger, table
    rows as lines), enough for Docling to read text and headings. Returns
    the page count."""
    import docx
    d = docx.Document(str(src))
    lines = []                                        # (text, font, size)
    # A cover line with the time: without it the PDF's text would hash the same
    # as the committed docx and the upload would (rightly) be refused as a
    # duplicate; the pass needs a document that is new to the library.
    lines.append((f"QA live pass copy of {src.name}, converted {time.strftime('%Y-%m-%d %H:%M:%S')}.", "F1", 9))
    lines.append(("", "F1", 10.5))
    for p in d.paragraphs:
        t = " ".join(p.text.split())
        if not t:
            continue
        style = (p.style.name or "").lower() if p.style is not None else ""
        if style == "title":
            lines.append((t, "F2", 16))
        elif style.startswith("heading"):
            lines.append((t, "F2", 13))
        else:
            lines.append((t, "F1", 10.5))
        lines.append(("", "F1", 10.5))
    for tb in d.tables:
        for row in tb.rows:
            lines.append((" | ".join(c.text.strip() for c in row.cells), "F1", 10))
        lines.append(("", "F1", 10.5))
    # wrap
    wrapped = []
    for t, f, sz in lines:
        width = int(95 * 10.5 / sz)
        if not t:
            wrapped.append(("", f, sz))
            continue
        words, cur = t.split(), ""
        for w in words:
            if len(cur) + len(w) + 1 > width and cur:
                wrapped.append((cur, f, sz))
                cur = w
            else:
                cur = f"{cur} {w}".strip()
        if cur:
            wrapped.append((cur, f, sz))
    pages, page, y = [], [], 770
    for t, f, sz in wrapped:
        lead = sz * 1.35
        if y - lead < 50:
            pages.append(page)
            page, y = [], 770
        y -= lead
        page.append((t, f, sz, y))
    if page:
        pages.append(page)
    objs = []                                         # object bodies, 1-based ids by position

    def add(body: str) -> int:
        objs.append(body)
        return len(objs)
    font1 = add("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
    font2 = add("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>")
    pages_id = len(objs) + 1 + 2 * len(pages)         # the Pages object comes after the page/content pairs
    page_ids = []
    for pg in pages:
        content = "\n".join(f"BT /{f} {sz} Tf 50 {y:.1f} Td ({_pdf_escape(t)}) Tj ET" for t, f, sz, y in pg if t)
        cid = add(f"<< /Length {len(content.encode('latin-1'))} >>\nstream\n{content}\nendstream")
        pid = add(f"<< /Type /Page /Parent {pages_id} 0 R /MediaBox [0 0 595 842] "
                  f"/Resources << /Font << /F1 {font1} 0 R /F2 {font2} 0 R >> >> /Contents {cid} 0 R >>")
        page_ids.append(pid)
    assert add(f"<< /Type /Pages /Kids [{' '.join(f'{p} 0 R' for p in page_ids)}] /Count {len(page_ids)} >>") == pages_id
    catalog = add(f"<< /Type /Catalog /Pages {pages_id} 0 R >>")
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{body}\nendobj\n".encode("latin-1")
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    for o in offsets:
        out += f"{o:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root {catalog} 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    dst.write_bytes(bytes(out))
    return len(pages)


# ------------------------------------------------------------------ the pass
class Pass:
    def __init__(self, port: int) -> None:
        self.base = f"http://127.0.0.1:{port}"
        self.port = port
        self.audio: dict = {}
        self.t0 = time.monotonic()
        self.log: list = []
        self.ws = None
        self.sess = None

    def t(self) -> float:
        return round(time.monotonic() - self.t0, 1)

    def say(self, line: str) -> None:
        msg = f"[{self.t():6.1f}s] {line}"
        print(msg, flush=True)
        self.log.append(msg)

    async def recv(self, timeout=240.0):
        m = await asyncio.wait_for(self.ws.receive_json(), timeout=timeout)
        t = m.get("type")
        if t == "audio":
            self.audio[m["context_id"]] = self.audio.get(m["context_id"], 0) + len(base64.b64decode(m["b64"]))
        elif t == "flush":
            # the sink flushes and reports its playhead: 40 % of what it holds
            ctx = getattr(self, "sounding", None)
            frames = self.audio.get(ctx, 0) // 2 if ctx else 0
            await self.ws.send_json({"type": "flush_ack", "context_id": ctx or "", "rendered_ms": frames / 24000 * 1000 * 0.4})
        elif t == "unit_started":
            self.sounding = m["context_id"]
            if m.get("kind") in ("companion", "cue", "answer", "map", "recap", "row") or m.get("kind") in PROMPT_KINDS:
                self.say(f"  {m['kind']:13} {m['text_display'][:110]}")
        elif t == "unit_done":
            await self.ack(m["context_id"])
        elif t == "prompt":
            self.say(f"  prompt/{m.get('kind')}  options={m.get('options')}")
        elif t == "document_deleted":
            self.say(f"  document_deleted name={m.get('name')} current={m.get('current')}")
        return m

    async def ack(self, ctx: str) -> None:
        frames = self.audio.get(ctx, 0) // 2
        ms = frames / 24000 * 1000
        await self.ws.send_json({"type": "rendered", "context_id": ctx, "rendered_ms": ms, "enqueued_frames": frames})
        await self.ws.send_json({"type": "unit_ended", "context_id": ctx, "enqueued_frames": frames, "rendered_ms": ms})

    async def until(self, pred, timeout=240.0):
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise asyncio.TimeoutError()
            m = await self.recv(min(left, 240.0))
            if pred(m):
                return m

    async def events(self, kind: str) -> list:
        async with self.sess.get(f"{self.base}/api/events") as r:
            data = await r.json()
        return [e for e in data["records"] if e.get("type") == kind]

    async def run(self) -> int:
        failures = []
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=1800)) as sess:
            self.sess = sess
            async with sess.get(f"{self.base}/api/status") as r:
                st = await r.json()
            self.say(f"server {st.get('session_id')} tts={((st.get('provider') or {}).get('provider'))} "
                     f"enrichment={st.get('enrichment')}")
            async with sess.ws_connect(f"ws://127.0.0.1:{self.port}/ws/audio") as ws:
                self.ws = ws
                hello = await ws.receive_json()
                names = [d["name"] for d in hello.get("documents", [])]
                # ---- 1. claim, open, read ------------------------------------
                await ws.send_json({"type": "interrupt"})
                await self.until(lambda m: m.get("type") == "sink")
                await ws.send_json({"type": "open", "name": "saral_jeevan_bima"})
                await self.until(lambda m: m.get("type") == "document_opened" and m["name"] == "saral_jeevan_bima")
                await ws.send_json({"type": "play"})
                self.say("1. voice claimed; play on saral_jeevan_bima")
                pm = await self.until(lambda m: m.get("type") == "prompt" and m.get("kind") in ("start_choice", "welcome"), 60)
                if pm["kind"] == "welcome":
                    await ws.send_json({"type": "ask", "question": "saral jeevan bima"})
                    await self.until(lambda m: m.get("type") == "prompt" and m.get("kind") == "start_choice", 60)
                await ws.send_json({"type": "ask", "question": "from the start"})
                clause = await self.until(lambda m: m.get("type") == "unit_started" and m.get("kind") == "clause", 60)
                self.say(f"   reading: {clause['unit_id']} {clause['text_display'][:60]!r}")
                await asyncio.sleep(1.0)
                # ---- 2. the upload while reading -------------------------------
                pdf = Path(os.environ.get("QA_TMP", "/tmp")) / "fixture_arogya_sanjeevani_qa.pdf"
                pages = docx_to_pdf(DOCX, pdf)
                self.say(f"2. {DOCX.name} -> {pdf.name} ({pages} pages); uploading while reading")
                # the earlier upload of the same bytes, if any, first
                fd = aiohttp.FormData()
                fd.add_field("file", pdf.read_bytes(), filename=pdf.name, content_type="application/pdf")
                t_up = time.monotonic()

                async def upload():
                    async with sess.post(f"{self.base}/documents", data=fd) as r:
                        txt = await r.text()
                        return r.status, [json.loads(l[5:]) for l in txt.splitlines() if l.startswith("data:")]
                up = asyncio.ensure_future(upload())
                # ---- 3. the engagement question --------------------------------
                replied = False
                invited = None
                while not up.done() or invited is None:
                    try:
                        m = await self.recv(timeout=2.0)
                    except asyncio.TimeoutError:
                        if up.done() and invited is None and time.monotonic() - t_up > 600:
                            break
                        continue
                    if m.get("type") == "prompt" and m.get("kind") == "ingest_wait" and not replied:
                        replied = True
                        await ws.send_json({"type": "ask", "question": "what is the waiting period"})
                        self.say("3. replied to the engagement question: what is the waiting period")
                    if m.get("type") == "prompt" and m.get("kind") == "start_choice":
                        invited = m
                status, frames = await up
                entry = frames[-1].get("entry") if frames and frames[-1].get("stage") == "done" else None
                self.say(f"   upload HTTP {status} in {round(time.monotonic() - t_up, 1)} s; entry={entry and entry['name']} "
                         f"spoken_title={entry and entry.get('spoken_title')!r}; "
                         f"stages={[(f['stage'], f['status']) for f in frames if f['stage'] != 'done']}")
                if status == 409:
                    self.say("   (the same text was already here: 409; the pass needs a fresh upload -- delete it first)")
                    return 1
                trunc = await self.events("unit_truncated")
                self.say(f"   unit_truncated: {[(e.get('reason'), e.get('unit_id')) for e in trunc][-2:]}")
                if not replied:
                    failures.append("the engagement question was never asked")
                if invited is None:
                    invited = await self.until(lambda m: m.get("type") == "prompt" and m.get("kind") == "start_choice", 300)
                self.say("4. start_choice heard")
                gaps = [g for g in await self.events("narration_gap_ms") if g.get("document") == (entry or {}).get("name")]
                gap = gaps[-1] if gaps else None
                self.say(f"   narration_gap_ms: max={gap and gap['max']} count={gap and gap['count']} gaps={gap and gap['gaps']}")
                if gap is None or gap["max"] > GAP_LIMIT_MS:
                    failures.append(f"narration gap {gap and gap['max']} ms > {GAP_LIMIT_MS}")
                # ---- 5. exclusions -------------------------------------------
                await ws.send_json({"type": "ask", "question": "exclusions"})
                self.say("5. replied: exclusions")
                m = await self.until(lambda m: m.get("type") in ("jumped", "prompt", "answer") and
                                     (m.get("type") != "prompt" or m.get("kind") in ("confirm_topic", "not_found")), 60)
                if m.get("type") == "prompt" and m.get("kind") == "confirm_topic":
                    await ws.send_json({"type": "ask", "question": "yes"})
                    m = await self.until(lambda m: m.get("type") == "jumped", 60)
                if m.get("type") == "jumped":
                    self.say(f"   jumped -> {m.get('heading')} ({m.get('reason')})")
                else:
                    self.say(f"   no jump: {m.get('type')} {m.get('kind') or m.get('answer', '')[:80]}")
                topics = await self.events("topics_offered")
                self.say(f"   topics_offered.n: {[e.get('n') for e in topics]}")
                # ---- 6. into a table ------------------------------------------
                table = None
                await ws.send_json({"type": "pause"})
                await asyncio.sleep(0.5)
                for name in [entry["name"] if entry else None, "arogya_sanjeevani"]:
                    if not name:
                        continue
                    await ws.send_json({"type": "open", "name": name})
                    await self.until(lambda m: m.get("type") == "document_opened" and m["name"] == name, 30)
                    await asyncio.sleep(2.0)
                    await ws.send_json({"type": "topic", "section_id": None})       # from the start
                    try:
                        table = await self.until(lambda m: m.get("type") == "prompt" and m.get("kind") == "table_choice", 90)
                        self.say(f"6. table_choice in {name}: labels={table.get('labels')}")
                        break
                    except asyncio.TimeoutError:
                        self.say(f"   no table reached in {name} within 90 s")
                        await ws.send_json({"type": "pause"})
                        await asyncio.sleep(0.5)
                if table is not None:
                    await ws.send_json({"type": "ask", "question": "the second one"})
                    row = await self.until(lambda m: m.get("type") == "unit_started" and m.get("kind") == "row", 60)
                    self.say(f"   row spoken: {row['unit_id']} {row['text_display'][:90]!r}")
                    await self.until(lambda m: m.get("type") == "prompt" and m.get("kind") == "table_choice", 60)
                    await ws.send_json({"type": "ask", "question": "carry on"})
                    await asyncio.sleep(1.0)
                    await ws.send_json({"type": "pause"})
                else:
                    failures.append("no table_choice reached")
                # ---- the uploaded fixture's headings --------------------------
                if entry:
                    fx = json.loads((FIX / f"{entry['name']}.json").read_text(encoding="utf-8"))
                    heads = [c["text_display"] for c in fx["clauses"] if c.get("kind") == "heading"]
                    junk = [h for h in heads if "===" in h or "PAGE" in h.split(".")[0].upper() or "___" in h]
                    self.say(f"   headings ({len(heads)}): junk={len(junk)}")
                    for h in heads[:40]:
                        self.say(f"      - {h[:90]}")
                    if junk:
                        failures.append(f"heading junk: {junk[:3]}")
                # ---- 7. delete ---------------------------------------------------
                if entry:
                    async with sess.delete(f"{self.base}/documents/{entry['doc_id']}") as r:
                        j = await r.json()
                    self.say(f"7. DELETE {entry['name']} -> HTTP {r.status} files={j.get('deleted', {}).get('files')} current={j.get('current')}")
                    idx = json.loads((FIX / "index.json").read_text(encoding="utf-8"))
                    left = [p.name for p in FIX.glob(f"{entry['name']}*")]
                    self.say(f"   still in index.json: {entry['name'] in [e['name'] for e in idx['documents']]}; files left: {left}")
                    if r.status != 200 or left:
                        failures.append("delete did not remove the document")
            async with sess.get(f"{self.base}/api/metrics") as r:
                mt = await r.json()
            self.say(f"metrics: fenced_bytes_session={mt.get('fenced_bytes_session')} fenced_unit_bytes={mt.get('fenced_unit_bytes')} "
                     f"ttfb_p50={mt.get('ttfb_p50')}")
        for f in failures:
            self.say(f"FAIL: {f}")
        self.say("PASS" if not failures else f"FAILED ({len(failures)})")
        return 1 if failures else 0


async def wait_up(base: str, seconds: float = 60) -> bool:
    async with aiohttp.ClientSession() as sess:
        for _ in range(int(seconds * 2)):
            try:
                async with sess.get(f"{base}/api/status", timeout=aiohttp.ClientTimeout(total=2)) as r:
                    if r.status == 200:
                        return True
            except Exception:
                pass
            await asyncio.sleep(0.5)
    return False


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8090)
    ap.add_argument("--serve", action="store_true", help="start the server on --port for the pass, then stop it")
    ap.add_argument("--tts", default=os.environ.get("TTS_PROVIDER", "fake"), help="TTS_PROVIDER for --serve (fake | rime)")
    args = ap.parse_args()
    proc = None
    if args.serve:
        env = dict(os.environ, TTS_PROVIDER=args.tts)
        proc = subprocess.Popen([sys.executable, "-u", str(ROOT / "examples" / "policy-reader" / "server.py"),
                                 "--dev", "--port", str(args.port)], env=env,
                                stdout=open(Path(os.environ.get("QA_TMP", "/tmp")) / f"qa_live_{args.port}.log", "w"),
                                stderr=subprocess.STDOUT)
        if not asyncio.run(wait_up(f"http://127.0.0.1:{args.port}")):
            print("server did not come up", file=sys.stderr)
            proc.terminate()
            return 2
    try:
        return asyncio.run(Pass(args.port).run())
    finally:
        if proc is not None:
            proc.terminate()


PROMPT_KINDS = ("choice", "offer", "start_choice", "confirm_topic", "table_choice", "welcome", "ingest_wait",
                "pick_topic", "section_end", "not_found", "end_choice")

if __name__ == "__main__":
    sys.exit(main())
