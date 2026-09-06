#!/usr/bin/env python3
"""HTTP + /ws/audio for the policy reader web demo.

One process, one port, one event stream. The listener route and the developer
diagnostics route are two views of the same session: /dev shows the event log
that / is driven by, so nothing on the diagnostics page is a separate
measurement path that could disagree with what the listener heard.

Two rules shape the whole file:

1. **Heard is never inferred from sent.** A unit is marked heard only when the
   client's `rendered` acks -- produced from the AudioWorklet's own clock --
   reach the unit's audio duration. The delivery boundary on an interrupt comes
   from the `flush_ack` the client sends *before* the interrupt, mapped through
   WordMap.offset_at(). The server's own "I sent N bytes" number is reported
   separately, in the contexts table, and is never used for position.

2. **Runtime ingestion is dev-only and quarantined.** `--dev` enables
   /api/dev/ingest, which writes to fixtures/unreviewed/ and nothing else. The
   listener's library is built from index.json; an unreviewed file is invisible
   there until a human moves it in. There is no code path from an upload to the
   judged flow.

  python examples/policy-reader/server.py                 # judged flow
  python examples/policy-reader/server.py --dev           # + upload and provider swap
  TTS_PROVIDER=fake python examples/policy-reader/server.py    # no key needed
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import statistics
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from aiohttp import WSMsgType, web

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

from delivery_layer.events import EventLog                     # noqa: E402
from delivery_layer.normalize import Segment                   # noqa: E402
from delivery_layer.position import resume_point                 # noqa: E402
from delivery_layer.tts import make_provider                   # noqa: E402
from delivery_layer.tts.base import AudioChunk, Done, Timestamps, TTSError  # noqa: E402
from delivery_layer.tts.fake import FakeTTS                    # noqa: E402
from delivery_layer.wordmap import build_word_map              # noqa: E402
from library import Library, LibraryError                      # noqa: E402

FIXTURES = HERE / "fixtures"
INDEX = FIXTURES / "index.json"
UNREVIEWED = FIXTURES / "unreviewed"
TRACES = ROOT / "traces"

CHARS_PER_SECOND = 14.0          # for the listener's "minutes left" estimate
# How far synthesis may run ahead of the playhead. One clause of buffer keeps
# playback gapless; a bounded amount is wasted (fenced) on an interruption.
LEAD_MS = 4000.0
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
DEFAULT_REFERRAL = "the team that publishes this document"
# Audible tone for the disclosed fallback voice. Set FAKE_TONE_HZ=0 for silence.
FAKE_TONE_HZ = float(os.environ.get("FAKE_TONE_HZ", "196")) or None


# ==========================================================================
# event log that also fans out to connected browsers
# ==========================================================================

class BroadcastEventLog(EventLog):
    """Every emit is mirrored to /dev in real time.

    The diagnostics page is not given a parallel telemetry channel; it is given
    this log. If a number appears on /dev it is because an event carried it.
    """

    def __init__(self, path=None, session_id="web") -> None:
        super().__init__(path, session_id)
        self.sockets: set = set()
        self._seq = 0
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def bind_loop(self, loop) -> None:
        self._loop = loop

    def emit(self, type: str, **fields: Any) -> dict:
        rec = super().emit(type, **fields)
        self._seq += 1
        rec["seq"] = self._seq
        if self._loop is not None and self.sockets:
            self._loop.call_soon_threadsafe(self._fanout, dict(rec))
        return rec

    def _fanout(self, rec: dict) -> None:
        asyncio.ensure_future(broadcast({"type": "event", "record": rec}, self.sockets))

    def tail(self, after: int = 0, type_filter: str = "", unit_filter: str = "",
             limit: int = 500) -> list:
        out = []
        for i, r in enumerate(self.records, start=1):
            if i <= after:
                continue
            if type_filter and type_filter not in r.get("type", ""):
                continue
            if unit_filter:
                hay = f"{r.get('context_id','')}{r.get('unit_id','')}{r.get('document','')}"
                if unit_filter not in hay:
                    continue
            r = dict(r)
            r["seq"] = i
            out.append(r)
        return out[-limit:]


async def broadcast(msg: dict, sockets) -> None:
    dead = []
    payload = json.dumps(msg)
    for ws in list(sockets):
        try:
            await ws.send_str(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        sockets.discard(ws)


# ==========================================================================
# per-context bookkeeping
# ==========================================================================

@dataclass
class ContextState:
    context_id: str
    turn_id: int
    unit_id: str
    state: str = "queued"        # queued|streaming|playing|done|fenced|error
    bytes: int = 0               # bytes the SERVER sent -- not evidence of hearing
    rendered_ms: float = 0.0     # bytes the CLIENT says it played -- this is evidence
    fenced_bytes: int = 0
    audio_ms: float = 0.0          # derived from BYTES, never from timestamps
    predicted_end_ms: Optional[float] = None   # what Rime's timestamps claimed
    ttfb_ms: Optional[float] = None
    synth_done: bool = False       # provider finished sending this unit
    heard: bool = False            # client acked playing it to the end

    def as_dict(self) -> dict:
        return {
            "context_id": self.context_id, "turn_id": self.turn_id, "unit_id": self.unit_id,
            "state": self.state, "bytes": self.bytes,
            "rendered_ms": round(self.rendered_ms, 1), "fenced_bytes": self.fenced_bytes,
            "audio_ms": round(self.audio_ms, 1), "ttfb_ms": self.ttfb_ms,
            "predicted_end_ms": (round(self.predicted_end_ms, 1)
                                 if self.predicted_end_ms is not None else None),
        }


# ==========================================================================
# the session
# ==========================================================================

class ReaderSession:
    def __init__(self, dev: bool = False, index_path: Path = INDEX) -> None:
        self.id = f"web-{uuid.uuid4().hex[:8]}"
        self.dev = dev
        self.started = time.monotonic()
        TRACES.mkdir(exist_ok=True)
        self.events = BroadcastEventLog(TRACES / f"session_{self.id}.jsonl", session_id=self.id)
        self.library = Library(index_path, self.events)
        self.index_path = index_path
        self.provider = None
        self.provider_connected_at: Optional[float] = None
        self.contexts: dict[str, ContextState] = {}
        self.wordmaps: dict[str, Any] = {}
        self.turn = 0
        self.playing = False
        self.replaying: Optional[str] = None
        self._reader: Optional[asyncio.Task] = None
        self._flush_acks: list[tuple[float, float]] = []   # (ack_ms, interrupt_ms)
        self._pending_flush: Optional[float] = None
        self._flush_ctx: Optional[str] = None
        # Flow control: how much audio has been synthesised vs actually played,
        # both in ms. The reader pumps synthesis ahead of the playhead by at
        # most LEAD_MS, so audio is always buffered (no inter-clause gap) but a
        # bounded amount is wasted on an interruption.
        self._synth_ms: float = 0.0
        self._unreviewed: Optional[str] = None
        rows = self.library.list()
        if rows:
            self.library.open(rows[0]["name"])

    # ------------------------------------------------------------ provider
    async def ensure_provider(self, name: Optional[str] = None):
        if self.provider is not None and name is None:
            return self.provider
        if self.provider is not None:
            try:
                await self.provider.close()
            except Exception:
                pass
        if name == "fake":
            # The fake provider emits digital silence by default, which makes an
            # offline demo look broken. A tone is not speech and can never be
            # mistaken for Rime, but it proves the audio path end to end.
            self.provider = FakeTTS(self.events, realtime=True, tone_hz=FAKE_TONE_HZ)
        elif name == "rime":
            os.environ["TTS_PROVIDER"] = "rime"
            self.provider = make_provider(self.events)
        else:
            self.provider = make_provider(self.events)
        await self.provider.connect()
        self.provider_connected_at = time.monotonic()
        return self.provider

    @property
    def descriptor(self) -> dict:
        return self.provider.descriptor if self.provider else {"provider": "none"}

    # ------------------------------------------------------------- library
    def index_entries(self) -> list:
        try:
            raw = json.loads(self.index_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return raw.get("documents", [])

    def referral_for(self, name: str) -> str:
        for e in self.index_entries():
            if e.get("name") == name:
                return e.get("referral") or DEFAULT_REFERRAL
        return DEFAULT_REFERRAL

    def listener_library(self) -> list:
        """What the listener rail shows. No ids, no milliseconds, no provider."""
        out = []
        for row in self.library.list():
            doc = self.library._docs[row["name"]]
            g = doc.grounding
            sections = []
            for c in g.clauses:
                if not sections or sections[-1] != c["section_title"]:
                    sections.append(c["section_title"])
            total_chars = sum(len(c["text_display"]) for c in g.clauses)
            est_min = max(1, round(total_chars / CHARS_PER_SECOND / 60))
            s = doc.session
            done_chars = sum(len(c["text_display"]) for c in g.clauses
                             if s.ledger.get(c["id"]) == "heard")
            left_min = max(0, round((total_chars - done_chars) / CHARS_PER_SECOND / 60))
            idx = 0
            if s.read_index >= 0:
                title = g.clauses[min(s.read_index, len(g.clauses) - 1)]["section_title"]
                idx = sections.index(title) + 1 if title in sections else 0
            finished = s.read_cursor >= len(g.clauses) and bool(s.ledger)
            out.append({
                "name": doc.name, "title": doc.title,
                "section_count": len(sections),
                "estimated_minutes": est_min,
                "progress": {
                    "started": bool(s.ledger),
                    "finished": finished,
                    "current_section_index": idx,
                    "minutes_left": left_min,
                },
                "referral": self.referral_for(doc.name),
                "unreviewed": doc.name == self._unreviewed,
            })
        return out

    # ------------------------------------------------------------- metrics
    def metrics(self) -> dict:
        ttfbs = [r["ttfb_ms"] for r in self.events.records
                 if r.get("type") in ("synth_first_byte", "synth_done") and r.get("ttfb_ms")]
        fenced = 0
        seen_cancel = False
        for r in self.events.records:
            if r.get("type") == "cancel_issued":
                seen_cancel = True
                fenced = 0
            elif seen_cancel and r.get("type") == "result_fenced":
                fenced += int(r.get("bytes") or 0)
        interp = sum(1 for wm in self.wordmaps.values() for s in wm.spans if s.estimated)
        total = sum(len(wm.spans) for wm in self.wordmaps.values())
        gaps = [b - a for a, b in self._flush_acks if b >= a]

        def pct(vals, p):
            if not vals:
                return None
            vals = sorted(vals)
            k = max(0, min(len(vals) - 1, int(round((p / 100) * (len(vals) - 1)))))
            return round(vals[k], 1)

        return {
            "ttfb_p50": pct(ttfbs, 50), "ttfb_p95": pct(ttfbs, 95),
            "fenced_bytes_after_clear": fenced if seen_cancel else None,
            "interpolated_spans": interp if total else None,
            "spans_total": total or None,
            "flush_ack_p50": round(statistics.median(gaps), 1) if gaps else None,
        }

    def status(self) -> dict:
        def cell(state, detail):
            return {"state": state, "detail": detail}

        ing = cell("ok", "dev upload enabled") if self.dev else cell("off", "build time only")
        norm = cell("ok", f"{len(self.wordmaps)} word maps built") if self.wordmaps \
            else cell("ok", "ready")
        if self.provider is None:
            rime = cell("down", "not connected")
        elif self.provider.name == "fake":
            rime = cell("warn", "fake provider, no Rime connection")
        else:
            age = int(time.monotonic() - (self.provider_connected_at or time.monotonic()))
            rime = cell("ok", f"open {age} s")
        clients = len(self.events.sockets)
        cws = cell("ok", f"{clients} connected") if clients else cell("down", "no client")
        llm = cell("off", "extractive") if not os.environ.get("LLM_API_KEY") else \
            cell("ok", os.environ.get("LLM_PROVIDER", "anthropic"))
        return {
            "session_id": self.id,
            "dev": self.dev,
            "replaying": self.replaying,
            "provider": self.descriptor,
            "ingest": ing,
            "normalize": norm,
            "rime_ws": rime,
            "client_ws": cws,
            "stt": cell("warn", "button only"),
            "llm": llm,
        }

    # -------------------------------------------------------------- reading
    def _played_ms(self) -> float:
        """Total audio actually played across all units, from client acks."""
        return sum(min(st.rendered_ms, st.audio_ms) for st in self.contexts.values()
                   if st.audio_ms > 0)

    async def _mark_heard(self, st: "ContextState", sockets) -> None:
        """A unit is heard once, when the client says it finished playing it.

        Called from the ack path and the unit_ended path, never from the synth
        pump: heard is client-acked delivery, not something the server infers
        from having sent the audio.
        """
        if st.heard:
            return
        st.heard = True
        st.state = "done"
        doc = self.library.current
        if doc is None:
            return
        c = doc.grounding.by_id.get(st.unit_id)
        if c is None:
            return
        doc.session.ledger[st.unit_id] = "heard"
        doc.session.last_heard_unit_id = st.unit_id
        doc.session.boundary_char = len(c["text_display"])
        self.events.emit("unit_heard", document=doc.name, context_id=st.context_id,
                         unit_id=st.unit_id, char_end=len(c["text_display"]),
                         of=len(c["text_display"]), rendered_ms=round(st.rendered_ms, 1))

    async def read_loop(self, sockets) -> None:
        """Read clause after clause until paused, interrupted, or out of document."""
        doc = self.library.current
        g, s = doc.grounding, doc.session
        provider = await self.ensure_provider()
        try:
            while self.playing and s.read_cursor < len(g.clauses):
                c = g.clauses[s.read_cursor]
                self.turn += 1
                ctx_id = f"{c['id']}#t{self.turn}"
                st = ContextState(ctx_id, self.turn, c["id"], state="streaming")
                self.contexts[ctx_id] = st
                s.current_unit_id = c["id"]
                s.read_index = c["index"]
                s.read_cursor += 1

                await broadcast({
                    "type": "unit_started", "context_id": ctx_id, "turn_id": self.turn,
                    "unit_id": c["id"], "index": c["index"],
                    "section_title": c["section_title"], "path": c.get("path"),
                    "text_display": c["text_display"], "sentences": c["sentences"],
                }, sockets)

                segs = [Segment(a, b, sp, replaced=c["text_display"][a:b] != sp)
                        for a, b, sp in c["spoken_map"]]
                ts = None
                async for item in provider.synth(c["text_spoken"], ctx_id):
                    if not self.playing:
                        break
                    if isinstance(item, Timestamps):
                        ts = item
                        # NOT audio_ms. These are a prediction that undershoots
                        # the delivered audio; audio_ms comes from bytes only.
                        st.predicted_end_ms = item.end_ms[-1] if item.end_ms else None
                        try:
                            wm = build_word_map(c["id"], c["text_display"], segs,
                                                item.words, item.start_ms, item.end_ms)
                            self.wordmaps[ctx_id] = wm
                        except Exception as e:                       # alignment is best effort
                            self.events.emit("wordmap_failed", context_id=ctx_id, error=str(e))
                        # Spans carry char offsets into text_display, so the
                        # client can advance a read-along highlight from its own
                        # audio clock instead of waiting for a boundary event.
                        wm_now = self.wordmaps.get(ctx_id)
                        spans = ([{"char_start": sp.char_start, "char_end": sp.char_end,
                                   "t_start_ms": sp.t_start_ms, "t_end_ms": sp.t_end_ms}
                                  for sp in wm_now.spans] if wm_now else [])
                        await broadcast({"type": "timestamps", "context_id": ctx_id,
                                         "words": item.words, "start_ms": item.start_ms,
                                         "end_ms": item.end_ms, "spans": spans}, sockets)
                    elif isinstance(item, AudioChunk):
                        st.bytes += len(item.pcm)
                        # The only honest length: bytes actually produced.
                        st.audio_ms = st.bytes / 2 / provider.sample_rate * 1000
                        st.state = "playing"
                        await broadcast({"type": "audio", "context_id": ctx_id, "seq": item.seq,
                                         "sample_rate": provider.sample_rate,
                                         "b64": base64.b64encode(item.pcm).decode()}, sockets)
                    elif isinstance(item, Done):
                        st.ttfb_ms = item.ttfb_ms
                        st.state = "done"
                        if item.total_bytes:
                            st.audio_ms = item.total_bytes / 2 / provider.sample_rate * 1000
                        self.events.emit(
                            "timestamp_drift", context_id=ctx_id, unit_id=c["id"],
                            predicted_end_ms=(round(st.predicted_end_ms, 1)
                                              if st.predicted_end_ms else None),
                            audio_ms=round(st.audio_ms, 1),
                            drift_ms=(round(st.audio_ms - st.predicted_end_ms, 1)
                                      if st.predicted_end_ms else None),
                            ratio=(round(st.predicted_end_ms / st.audio_ms, 3)
                                   if st.predicted_end_ms and st.audio_ms else None))
                        # Stretch the map onto the real audio and re-broadcast,
                        # so the client's read-along tracks the voice instead of
                        # the prediction.
                        wm_done = self.wordmaps.get(ctx_id)
                        if wm_done is not None and wm_done.spans:
                            wm_done.stretch_to(st.audio_ms)
                            await broadcast({
                                "type": "timestamps", "context_id": ctx_id,
                                "words": [sp.word for sp in wm_done.spans],
                                "start_ms": [sp.t_start_ms for sp in wm_done.spans],
                                "end_ms": [sp.t_end_ms for sp in wm_done.spans],
                                "spans": [{"char_start": sp.char_start, "char_end": sp.char_end,
                                           "t_start_ms": sp.t_start_ms, "t_end_ms": sp.t_end_ms}
                                          for sp in wm_done.spans],
                                "corrected": True,
                            }, sockets)
                        st.synth_done = True
                        self._synth_ms += st.audio_ms
                        await broadcast({"type": "unit_done", "context_id": ctx_id,
                                         "unit_id": c["id"], "bytes": item.total_bytes}, sockets)
                    elif isinstance(item, TTSError):
                        st.state = "error"
                        await broadcast({"type": "provider_error", "context_id": ctx_id,
                                         "message": item.message}, sockets)

                if not self.playing:
                    return
                if st.bytes == 0:
                    self.events.emit("no_audio", context_id=ctx_id)

                # Flow control, NOT a wait for this clause to finish. Synthesis
                # runs ahead of the playhead by at most LEAD_MS so the next
                # clause's audio is already buffered when this one ends -- that
                # is what removes the inter-clause gap. "Heard" is marked
                # separately, from client acks (see _mark_heard). The old code
                # blocked here for the whole real-time playback, which made every
                # clause boundary a pause and, when the last ack plateaued just
                # short of the end, a 12-second one.
                while self.playing and (self._synth_ms - self._played_ms()) > LEAD_MS:
                    await asyncio.sleep(0.05)

            # Synthesis is done; wait for playback to drain before declaring the
            # document finished, so a listener still hearing the last clause is
            # not told it is over.
            drain_deadline = time.monotonic() + (self._synth_ms / 1000.0) + 15.0
            while self.playing and self._played_ms() + 250 < self._synth_ms:
                if time.monotonic() > drain_deadline:
                    self.events.emit("drain_timeout",
                                     played_ms=round(self._played_ms(), 1),
                                     synth_ms=round(self._synth_ms, 1))
                    break
                await asyncio.sleep(0.1)
            if self.playing and s.read_cursor >= len(g.clauses):
                self.playing = False
                await broadcast({"type": "document_finished", "name": doc.name}, sockets)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.events.emit("reader_error", error=str(e))
            await broadcast({"type": "provider_error", "message": str(e)}, sockets)

    async def stop_reading(self) -> None:
        self.playing = False
        if self._reader and not self._reader.done():
            self._reader.cancel()
            try:
                await self._reader
            except (asyncio.CancelledError, Exception):
                pass
        self._reader = None


# ==========================================================================
# HTTP handlers
# ==========================================================================

def _json(data, status=200):
    return web.json_response(data, status=status,
                             dumps=lambda o: json.dumps(o, ensure_ascii=False))


async def api_status(request):
    return _json(request.app["session"].status())


async def api_contexts(request):
    s = request.app["session"]
    return _json({"contexts": [c.as_dict() for c in s.contexts.values()]})


async def api_metrics(request):
    return _json(request.app["session"].metrics())


async def api_events(request):
    s = request.app["session"]
    after = int(request.query.get("after", 0) or 0)
    return _json({"records": s.events.tail(after,
                                           request.query.get("type", ""),
                                           request.query.get("unit", ""))})


async def api_library(request):
    return _json({"documents": request.app["session"].listener_library()})


async def api_traces(request):
    out = []
    for p in sorted(TRACES.glob("*.jsonl")):
        try:
            out.append({"name": p.name, "bytes": p.stat().st_size})
        except OSError:
            continue
    return _json({"traces": out})


async def api_replay(request):
    """Replay a committed trace at 20x. No audio, no Rime, no key needed.

    This is how a judge sees the evidence without credentials: the diagnostics
    page is driven by the same event shapes whether they arrive live or from a
    file on disk.
    """
    s = request.app["session"]
    body = await request.json()
    name = Path(str(body.get("trace", ""))).name
    path = TRACES / name
    if not name or not path.exists():
        return _json({"error": f"no trace {name!r}"}, status=404)
    if s.replaying:
        return _json({"error": f"already replaying {s.replaying}"}, status=409)

    async def run():
        s.replaying = name
        socks = s.events.sockets
        await broadcast({"type": "replay_start", "trace": name}, socks)
        try:
            prev = None
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                ts = rec.get("ts_ms")
                if prev is not None and isinstance(ts, (int, float)):
                    await asyncio.sleep(min(0.25, max(0.0, (ts - prev) / 1000.0 / 20.0)))
                if isinstance(ts, (int, float)):
                    prev = ts
                await broadcast({"type": "event", "record": rec, "replay": True}, socks)
        finally:
            s.replaying = None
            await broadcast({"type": "replay_end", "trace": name}, socks)

    asyncio.ensure_future(run())
    return _json({"ok": True, "trace": name})


async def api_dev_provider(request):
    s = request.app["session"]
    if not s.dev:
        return _json({"error": "not available outside --dev"}, status=404)
    body = await request.json()
    name = str(body.get("name", "")).lower()
    if name not in ("rime", "fake"):
        return _json({"error": "name must be rime or fake"}, status=400)
    await s.stop_reading()
    try:
        await s.ensure_provider(name)
    except Exception as e:
        return _json({"error": str(e)}, status=502)
    await broadcast({"type": "provider_active", **s.descriptor}, s.events.sockets)
    return _json({"ok": True, "provider": s.descriptor})


INGEST_STAGES = ["extract", "structure", "segment", "normalize", "pii scan", "validate"]


async def api_dev_ingest(request):
    """Run scripts/ingest.py in a subprocess, into fixtures/unreviewed/ only.

    Dry run first so a document that fails validation or the PII scan never
    reaches disk. Progress is streamed as ingest_progress over /ws/audio so the
    stages are visible while they happen rather than as one lump at the end.
    """
    s = request.app["session"]
    if not s.dev:
        return _json({"error": "not available outside --dev"}, status=404)

    source, stem, upload = None, None, None
    if request.content_type and request.content_type.startswith("multipart/"):
        reader = await request.multipart()
        field = await reader.next()
        while field is not None:
            if field.name == "file":
                filename = Path(field.filename or "upload.txt").name
                stem = Path(filename).stem
                UNREVIEWED.mkdir(parents=True, exist_ok=True)
                upload = UNREVIEWED / f".incoming_{filename}"
                size = 0
                with upload.open("wb") as fh:
                    while True:
                        chunk = await field.read_chunk()
                        if not chunk:
                            break
                        size += len(chunk)
                        if size > MAX_UPLOAD_BYTES:
                            fh.close()
                            upload.unlink(missing_ok=True)
                            return _json({"error": "file larger than 20 MB"}, status=413)
                        fh.write(chunk)
                source = str(upload)
            elif field.name == "url":
                source = (await field.text()).strip()
            field = await reader.next()
    else:
        body = await request.json()
        source = str(body.get("url", "")).strip()

    if not source:
        return _json({"error": "provide a file or a url"}, status=400)
    if stem is None:
        stem = "".join(ch if ch.isalnum() or ch in "-_" else "-"
                       for ch in source.rsplit("/", 1)[-1] or "upload")[:60] or "upload"

    UNREVIEWED.mkdir(parents=True, exist_ok=True)
    out = UNREVIEWED / f"{stem}.json"
    socks = s.events.sockets

    async def stage(name, state, detail):
        await broadcast({"type": "ingest_progress", "stage": name,
                         "state": state, "detail": detail}, socks)

    async def run_ingest(extra):
        cmd = [sys.executable, str(ROOT / "scripts" / "ingest.py"), source,
               "--out", str(out), "--name", stem] + extra
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            cwd=str(ROOT))
        so, se = await proc.communicate()
        return proc.returncode, so.decode("utf-8", "replace"), se.decode("utf-8", "replace")

    await stage("extract", "ok", f"reading {source[:70]}")
    code, so, se = await run_ingest(["--dry-run"])
    report = [ln for ln in (so + se).splitlines() if ln.strip()]

    if code == 2:
        hits = [ln.strip() for ln in se.splitlines() if ":" in ln and "PII" not in ln][:10]
        await stage("pii scan", "fail", "refused: looks like real personal data")
        if upload:
            upload.unlink(missing_ok=True)
        return _json({"ok": False, "stage": "pii scan", "code": 2,
                      "report": report, "redacted_hits": hits,
                      "detail": "Refused. Use a synthetic or public document."}, status=422)
    if code != 0:
        await stage("validate", "fail", (se.strip().splitlines() or ["failed"])[-1][:200])
        if upload:
            upload.unlink(missing_ok=True)
        return _json({"ok": False, "stage": "validate", "code": code,
                      "report": report}, status=422)

    for st_name in ("structure", "segment", "normalize", "pii scan"):
        await stage(st_name, "ok", "passed")
    warn = [ln for ln in report if ln.lower().startswith("warning")]
    await stage("validate", "warn" if warn else "ok",
                warn[0][:200] if warn else "schema and span coverage ok")

    code, so2, se2 = await run_ingest([])
    if code != 0:
        await stage("validate", "fail", "write failed")
        if upload:
            upload.unlink(missing_ok=True)
        return _json({"ok": False, "stage": "write", "code": code,
                      "report": report + (so2 + se2).splitlines()}, status=422)
    if upload:
        upload.unlink(missing_ok=True)

    doc = json.loads(out.read_text(encoding="utf-8"))
    preview = [{"id": c["id"], "section_title": c["section_title"],
                "text_display": c["text_display"], "text_spoken": c["text_spoken"],
                "spoken_map": c["spoken_map"]} for c in doc["clauses"][:20]]
    s.events.emit("dev_ingest_written", name=stem, path=str(out.relative_to(ROOT)),
                  clause_count=doc["clause_count"])
    return _json({
        "ok": True, "name": stem, "clause_count": doc["clause_count"],
        "path": str(out.relative_to(ROOT)),
        "report": report + [ln for ln in (so2 + se2).splitlines() if ln.strip()],
        "warnings": warn,
        "clauses": preview[:5], "preview": preview,
        "note": "Written to fixtures/unreviewed/. It is not in the listener library "
                "until a human reviews it and moves it into index.json.",
    })


async def api_dev_open_unreviewed(request):
    """Load an unreviewed fixture into THIS session only. --dev, and never index.json."""
    s = request.app["session"]
    if not s.dev:
        return _json({"error": "not available outside --dev"}, status=404)
    if request.query.get("unreviewed") != "1":
        return _json({"error": "pass ?unreviewed=1 to acknowledge"}, status=400)
    body = await request.json()
    name = Path(str(body.get("name", ""))).stem
    path = UNREVIEWED / f"{name}.json"
    if not path.exists():
        return _json({"error": f"no unreviewed fixture {name!r}"}, status=404)
    from library import Document
    doc = Document(name, json.loads(path.read_text(encoding="utf-8")).get("title", name), path)
    s.library._docs[name] = doc
    s._unreviewed = name
    s.library.open(name)
    s.events.emit("unreviewed_opened", name=name)
    return _json({"ok": True, "name": name, "unreviewed": True})


# ==========================================================================
# /ws/audio
# ==========================================================================

async def ws_audio(request):
    s = request.app["session"]
    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(request)
    s.events.sockets.add(ws)
    socks = s.events.sockets
    try:
        await s.ensure_provider()
    except Exception as e:
        await ws.send_str(json.dumps({"type": "provider_error", "message": str(e)}))
    await ws.send_str(json.dumps({"type": "hello", "session_id": s.id, "dev": s.dev,
                                  "provider": s.descriptor,
                                  "documents": s.listener_library(),
                                  "current": s.library.current.name if s.library.current else None}))
    try:
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            try:
                m = json.loads(msg.data)
            except ValueError:
                continue
            await handle_client_message(s, m, socks, ws)
    finally:
        socks.discard(ws)
        if not socks:
            await s.stop_reading()
    return ws


async def handle_client_message(s: ReaderSession, m: dict, socks, ws) -> None:
    t = m.get("type")

    if t == "rendered":
        # The audio clock, every 100 ms. This is the only evidence of hearing.
        st = s.contexts.get(m.get("context_id", ""))
        if st:
            st.rendered_ms = max(st.rendered_ms, float(m.get("rendered_ms") or 0.0))
            if st.state != "fenced":
                st.state = "playing" if not st.heard else st.state
            s.events.emit("frames_played", context_id=st.context_id,
                          rendered_ms=round(st.rendered_ms, 1))
            # With intact chunks rendered_ms reaches audio_ms exactly; the only
            # legitimate slack is one render quantum (128 frames). There is no
            # plateau tolerance: a unit that stops short was not fully heard,
            # and the frame count says whether that is a dropped chunk.
            if st.synth_done and not st.heard and st.audio_ms > 0:
                slack = 128.0 / s.provider.sample_rate * 1000.0 if s.provider else 6.0
                if st.rendered_ms + slack >= st.audio_ms:
                    enq = m.get("enqueued_frames")
                    if enq is not None and int(enq) * 2 != st.bytes:
                        s.events.emit("frame_count_mismatch", context_id=st.context_id,
                                      enqueued_frames=int(enq), expected_frames=st.bytes // 2,
                                      server_bytes=st.bytes, via="rendered")
                    else:
                        await s._mark_heard(st, socks)
        return

    if t == "unit_ended":
        # The client's definitive "I have emitted the last sample of this unit".
        #
        # It is accepted only if the client's frame count matches the bytes the
        # server sent. A shortfall means chunks were dropped in transit -- the
        # unit was NOT heard in full, whatever the client believes it drained.
        # rendered_ms only ever holds a value the client sent -- the periodic
        # ack, or the exact drained count carried on this message. The server
        # never stamps a delivery number the client did not report.
        st = s.contexts.get(m.get("context_id", ""))
        if not st or not st.synth_done:
            return
        enq = m.get("enqueued_frames")
        if enq is None or int(enq) * 2 != st.bytes:
            s.events.emit("frame_count_mismatch", context_id=st.context_id,
                          enqueued_frames=(int(enq) if enq is not None else None),
                          expected_frames=st.bytes // 2, server_bytes=st.bytes,
                          via="unit_ended")
            return
        # The client's own drained count for this unit. This is a value the
        # client measured and sent, so recording it keeps "heard" client-
        # acknowledged; the periodic ack alone lands up to 100 ms short.
        if m.get("rendered_ms") is not None:
            st.rendered_ms = max(st.rendered_ms, float(m["rendered_ms"]))
            s.events.emit("frames_played", context_id=st.context_id,
                          rendered_ms=round(st.rendered_ms, 1), final=True)
        await s._mark_heard(st, socks)
        return

    if t == "flush_ack":
        # Must arrive BEFORE interrupt. Records the boundary the client actually
        # reached, so the interrupt is measured against the audio clock rather
        # than against whatever the server had finished sending. Its context_id
        # is the PLAYHEAD unit, which under lookahead is not the last one
        # synthesised -- the interrupt uses this, not current_unit_id.
        fctx = m.get("context_id", "")
        st = s.contexts.get(fctx)
        if st:
            st.rendered_ms = float(m.get("rendered_ms") or st.rendered_ms)
        s._pending_flush = time.monotonic() * 1000.0
        s._flush_ctx = fctx or None
        s.events.emit("flush_ack", context_id=fctx,
                      rendered_ms=round(float(m.get("rendered_ms") or 0.0), 1))
        return

    if t == "interrupt":
        now = time.monotonic() * 1000.0
        if s._pending_flush is not None:
            s._flush_acks.append((s._pending_flush, now))
            s._pending_flush = None
        else:
            s.events.emit("interrupt_without_flush_ack")
        await s.stop_reading()
        if s.provider:
            await s.provider.cancel()
        doc = s.library.current
        sess = doc.session
        # The clause the listener was actually hearing is the flush_ack's
        # context (the playhead), not the last clause synthesised, which under
        # lookahead may be several clauses further on and already buffered.
        ctx_id = getattr(s, "_flush_ctx", None)
        if ctx_id not in s.contexts:
            ctx_id = sess.current_unit_id and next(
                (c for c in reversed(list(s.contexts))
                 if s.contexts[c].unit_id == sess.current_unit_id), None)
        s._flush_ctx = None
        st = s.contexts.get(ctx_id) if ctx_id else None
        if st:
            st.state = "fenced"
            wm = s.wordmaps.get(ctx_id)
            c = doc.grounding.by_id[st.unit_id]
            char_end = wm.offset_at(st.rendered_ms) if wm else 0
            word_i, word = -1, ""
            if wm:
                for i, sp in enumerate(wm.spans):
                    if sp.t_end_ms <= st.rendered_ms:
                        word_i, word = i, sp.text if hasattr(sp, "text") else ""
            sess.last_heard_unit_id = st.unit_id
            sess.boundary_char = char_end
            sess.ledger[st.unit_id] = (f"truncated@{char_end}"
                                       if char_end < len(c["text_display"]) else "heard")
            sess.read_cursor = c["index"]          # do not skip the interrupted clause
            s.events.emit("unit_truncated", document=doc.name, context_id=st.unit_id,
                          char_end=char_end, of=len(c["text_display"]),
                          rendered_ms=round(st.rendered_ms, 1))
            await broadcast({"type": "boundary", "unit_id": st.unit_id,
                             "context_id": ctx_id, "rendered_ms": round(st.rendered_ms, 1),
                             "char_end": char_end, "word_index": word_i, "word": word,
                             "of": len(c["text_display"])}, socks)
        await broadcast({"type": "paused"}, socks)
        return

    if t == "play":
        if s.replaying:
            return
        s.playing = True
        await broadcast({"type": "playing"}, socks)
        s._reader = asyncio.ensure_future(s.read_loop(socks))
        return

    if t == "pause":
        await s.stop_reading()
        if s.provider:
            await s.provider.cancel()
        await broadcast({"type": "paused"}, socks)
        return

    if t == "open":
        await s.stop_reading()
        try:
            doc = s.library.open(str(m.get("name", "")))
        except LibraryError as e:
            await ws.send_str(json.dumps({"type": "error", "message": str(e)}))
            return
        await broadcast({"type": "document_opened", "name": doc.name,
                         "documents": s.listener_library()}, socks)
        return

    if t == "jump":
        doc = s.library.current
        c = doc.grounding.by_id.get(str(m.get("unit_id", "")))
        if c:
            s.events.emit("position_saved", document=doc.name,
                          unit_id=doc.session.current_unit_id, cursor=doc.session.read_cursor)
            doc.session.read_cursor = c["index"]
            s.events.emit("position_restored", document=doc.name, unit_id=c["id"],
                          sentence_index=0, char_start=0)
            await broadcast({"type": "jumped", "unit_id": c["id"]}, socks)
        return

    if t == "resume":
        doc = s.library.current
        sess = doc.session
        if not sess.last_heard_unit_id:
            return
        c = doc.grounding.by_id[sess.last_heard_unit_id]
        rp = resume_point(c["id"], c["text_display"], c["sentences"], sess.boundary_char,
                          c["section_title"])
        s.events.emit("position_restored", document=doc.name, unit_id=rp.unit_id,
                      sentence_index=rp.sentence_index, char_start=rp.char_start)
        await broadcast({"type": "resume_point", "unit_id": rp.unit_id,
                         "sentence_index": rp.sentence_index, "char_start": rp.char_start,
                         "cue": rp.cue, "text": rp.text}, socks)
        return

    if t == "ask":
        doc = s.library.current
        sess = doc.session
        q = str(m.get("question", "")).strip()
        if not q:
            return
        g = doc.grounding
        r = g.resolve(q, sess.last_heard_unit_id, read_cursor=max(sess.read_index, 0))
        target = r.hits[0].unit_id if r.hits else (r.beyond[0].unit_id if r.beyond else None)
        sess.history.append((q, r.kind, target))
        s.events.emit("question_resolved", document=doc.name, kind=r.kind,
                      unit_id=target, question=q)
        heard = None
        if sess.last_heard_unit_id:
            c = g.by_id[sess.last_heard_unit_id]
            if sess.boundary_char < len(c["text_display"]):
                heard = c["text_display"][:sess.boundary_char]
        answer = await g.answer(r, heard_text_of_reference=heard)
        await broadcast({
            "type": "answer", "question": q, "kind": r.kind, "unit_id": target,
            "answer": answer,
            "referral": s.referral_for(doc.name),
            "offer": r.kind == "beyond_cursor",
        }, socks)
        return


# ==========================================================================
# app
# ==========================================================================

def _file_route(path: Path):
    async def handler(request):
        return web.FileResponse(path)
    return handler


def build_app(dev: bool = False, index_path: Path = INDEX) -> web.Application:
    app = web.Application(client_max_size=MAX_UPLOAD_BYTES + 1024 * 1024)
    app["session"] = ReaderSession(dev=dev, index_path=index_path)
    app.router.add_get("/api/status", api_status)
    app.router.add_get("/api/contexts", api_contexts)
    app.router.add_get("/api/metrics", api_metrics)
    app.router.add_get("/api/events", api_events)
    app.router.add_get("/api/library", api_library)
    app.router.add_get("/api/traces", api_traces)
    app.router.add_post("/api/replay", api_replay)
    app.router.add_post("/api/dev/ingest", api_dev_ingest)
    app.router.add_post("/api/dev/provider", api_dev_provider)
    app.router.add_post("/api/dev/open", api_dev_open_unreviewed)
    app.router.add_get("/ws/audio", ws_audio)

    dist = HERE / "web" / "dist"
    if dist.exists():
        async def index_html(request):
            return web.FileResponse(dist / "index.html")
        app.router.add_static("/assets", dist / "assets")
        # Files Vite copies to the dist ROOT -- notably player-worklet.js, which
        # the client loads with audioWorklet.addModule("/player-worklet.js").
        # Without these routes it 404s, the worklet never registers, and there
        # is no audio and no rendered acks: the failure is completely silent.
        for f in sorted(dist.iterdir()):
            if f.is_file() and f.name != "index.html":
                app.router.add_get(f"/{f.name}", _file_route(f))
        app.router.add_get("/", index_html)
        app.router.add_get("/dev", index_html)

    async def on_start(a):
        a["session"].events.bind_loop(asyncio.get_running_loop())
    app.on_startup.append(on_start)
    return app


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--dev", action="store_true",
                    help="enable /api/dev/* : runtime ingestion into fixtures/unreviewed/ "
                         "and provider swapping. Never enable this for the judged flow.")
    args = ap.parse_args()
    app = build_app(dev=args.dev)
    s = app["session"]
    print(f"session {s.id}  dev={args.dev}  provider={os.environ.get('TTS_PROVIDER', 'rime')}")
    print(f"api + websocket  http://{args.host}:{args.port}  ws://{args.host}:{args.port}/ws/audio")
    if (HERE / "web" / "dist").exists():
        print(f"ui               http://{args.host}:{args.port}/  and  /dev  (served from web/dist)")
    else:
        # Without a build there is no / route here at all, and opening this port
        # in a browser returns 404. Say so rather than printing a dead link.
        print("ui               not built. Either:")
        print("                   cd examples/policy-reader/web && npm run dev   "
              "-> open http://localhost:5173")
        print("                   cd examples/policy-reader/web && npm run build "
              "-> reload this port")
    if args.dev:
        print("dev mode: /api/dev/ingest writes to fixtures/unreviewed/ only; "
              "nothing reaches the listener library without a human moving it into index.json")
    web.run_app(app, host=args.host, port=args.port, print=None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
