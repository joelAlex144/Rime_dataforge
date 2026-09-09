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

2. **Upload runs the same ingestion as build time.** POST /documents calls
   scripts/ingest.py's ingest_document() in a worker thread, streams the eight
   stages as server-sent events, writes the fixture, the DoclingDocument, the
   table CSVs and an ingest report into fixtures/, and appends the library
   entry with `reviewed: false`. Nothing in the report blocks the document;
   the developer page shows it and its Accept button sets `reviewed: true`.

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
import re
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
from segment import sentence_spans                             # noqa: E402
from library import Library, LibraryError, clean_title         # noqa: E402
from grounding import spoken_text, is_readable                 # noqa: E402
import grounding as gr                                         # noqa: E402
from conversation import Conversation                          # noqa: E402
import companion as cp                                         # noqa: E402
from llm import check_llm, describe_llm, make_llm, warm_llm     # noqa: E402

FIXTURES = HERE / "fixtures"
INDEX = FIXTURES / "index.json"
sys.path.insert(0, str(ROOT / "scripts"))
TRACES = ROOT / "traces"

CHARS_PER_SECOND = 14.0          # for the listener's "minutes left" estimate
# How far synthesis may run ahead of the playhead. One clause of buffer keeps
# playback gapless; a bounded amount is wasted (fenced) on an interruption.
LEAD_MS = 4000.0
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
# How long play at the top of a document waits for its overview to be generated
# before reading with the mechanical map. Granite 3B on a 6 GB GPU needs ~30-60 s
# for a 300-clause wording; the cue is spoken first so the line is not silent.
NAVIGATOR_WAIT_S = float(os.environ.get("NAVIGATOR_WAIT_S", "120"))
# The conversation layer: one prompt open at a time, each spoken as a unit of
# its own kind and, once heard, given this many seconds of silence before the
# default. choice: now / overview first after a chip. offer: a suggested
# question after a section. start_choice: the invitation when a document is
# ready. confirm_topic: "{heading}, about m minutes. Read it now?".
# table_choice: a table reached while reading.
PROMPT_TIMEOUT_S = {"choice": 8.0, "offer": 6.0, "start_choice": 8.0, "confirm_topic": 6.0, "table_choice": 6.0,
                    # v2 moments: welcome on the first play, ingest_wait as a prompt (closes at ready, no
                    # timer), pick_topic after the brief, section_end at a boundary, not_found after a
                    # question the document does not answer, end_choice at the end.
                    "welcome": 8.0, "ingest_wait": None, "pick_topic": 8.0, "section_end": 5.0,
                    "not_found": 6.0, "end_choice": 8.0}
PROMPT_KINDS = tuple(PROMPT_TIMEOUT_S)
LOOP_PROMPTS = ("offer", "table_choice", "pick_topic", "section_end", "end_choice")   # opened inside read_loop
# Engagement: section_end keeps this many seconds from the last prompt, and a
# play after a pause longer than this speaks "We were in {heading}. Carrying on."
SECTION_END_MIN_GAP_S = float(os.environ.get("SECTION_END_MIN_GAP_S", "180"))
RESUME_CUE_AFTER_S = float(os.environ.get("RESUME_CUE_AFTER_S", "120"))
# The background enrichment (tags, suggested questions, table shapes) shares the
# one local model with understanding and answers. It waits this long after the
# navigator lands, and for a moment with no prompt open and no answer in
# flight, so the first minute after an upload -- the most talked-through one --
# has the model free. Pre-generation (scripts/enrich.py --all) avoids it entirely.
ENRICH_REST_DELAY_S = float(os.environ.get("ENRICH_REST_DELAY_S", "60"))
# A document opened from the rail is invited to a topic 1.5 s after the last
# open: a run of clicks asks once, for the document that stays.
START_CHOICE_DEBOUNCE_S = 1.5
# The rest pass can be switched off (env, and the /dev toggle): the reader
# then works from the navigator fields alone and the model stays free.
ENRICH_REST_ENABLED = os.environ.get("ENRICH_REST_ENABLED", "true").strip().lower() not in ("0", "false", "no", "off")
NON_CLAUSE_KINDS = ("answer", "map", "cue", "recap", "companion") + PROMPT_KINDS
_ORDINALS = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7,
             "eighth": 8, "ninth": 9, "tenth": 10, "1st": 1, "2nd": 2, "3rd": 3, "4th": 4, "5th": 5,
             "6th": 6, "7th": 7, "8th": 8, "9th": 9, "10th": 10, "one": 1, "two": 2, "three": 3,
             "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}


class _Pending:
    """A document being ingested, before the write stage names it: what the
    companion's lines and prompt are bound to until then."""
    def __init__(self, name: str, title: str) -> None:
        self.name, self.title, self.doc_id = name, title, name
        self.spoken_title = clean_title(title)


def _row_index(q: str, labels: list) -> Optional[int]:
    """Zero-based row for a reply to a table prompt: a row label (exact, prefix,
    substring, then token overlap), or an ordinal ("the second one", "row 3",
    "number two", "3", "last"). None when it is neither."""
    if not labels:
        return None
    ql = gr.normalise_term(q)
    if not ql:
        return None
    norm = [gr.normalise_term(x) for x in labels]
    for i, lab in enumerate(norm):
        if lab and ql == lab:
            return i
    for i, lab in enumerate(norm):
        if lab and (lab.startswith(ql) or ql.startswith(lab) or ql in lab or lab in ql):
            return i
    qs = {gr.stem(t) for t in gr.tokenize(ql)}
    best, score = None, 0.0
    for i, lab in enumerate(norm):
        ls = {gr.stem(t) for t in gr.tokenize(lab)}
        if qs and ls:
            sc = len(qs & ls) / len(qs)
            if sc > score:
                best, score = i, sc
    if best is not None and score >= 0.6:
        return best
    if re.fullmatch(r"(?:the )?last(?: one| row)?", ql):
        return len(labels) - 1
    m = re.search(r"\b(?:row|number|no|item|entry|the)?\s*(\d+|" + "|".join(_ORDINALS) + r")\b", ql)
    if m:
        tok = m.group(1)
        n = int(tok) if tok.isdigit() else _ORDINALS[tok]
        if 1 <= n <= len(labels):
            return n - 1
    return None


def _row_label(r: dict) -> str:
    """A row's label. A row sentence built from a header row ("Excluded item:
    Cosmetic surgery; Note: ...", with or without a leading "Row:") is labelled
    by its first cell's value, since the first key is the column header every
    row shares; a key: value row from the structure pass ("Document type:
    Insurance policy ...") by its key; anything else by its first few words."""
    t = re.sub(r"^Row:\s*", "", r["text_display"].strip())
    m = re.match(r"^[^:;]{1,60}:\s*([^;]+);", t)
    if m:
        return m.group(1).strip().rstrip(".")
    if ":" in t and 0 < t.index(":") <= 60:
        return t[:t.index(":")].strip()
    return " ".join(t.split()[:5])


def _table_rows(g, stub: dict) -> list:
    """The rows of a table stub: the clauses that name it as parent, else the
    run of table_row clauses right after it (fixtures without parent ids)."""
    rows = [r for r in g.clauses[stub["index"] + 1:] if r.get("parent") == stub["id"] and gr.clause_kind(r) == "table_row"]
    if rows:
        return rows
    j = stub["index"] + 1
    while j < len(g.clauses) and gr.clause_kind(g.clauses[j]) == "table_row":
        rows.append(g.clauses[j])
        j += 1
    return rows


def _table_description(stub: dict, labels: list) -> str:
    """What the table is of: enrichment's spoken description when there is one
    (minus its closing question), else "{first label} to {last label}"."""
    ov = stub.get("spoken_override")
    if isinstance(ov, dict) and ov.get("text"):
        return re.sub(r"\s*Which one do you want\??\s*$", "", ov["text"].strip(), flags=re.I).rstrip(".")
    if labels:
        return f"{labels[0]} to {labels[-1]}" if len(labels) > 1 else labels[0]
    return "entries"
UPLOAD_SUFFIXES = (".pdf", ".docx", ".txt", ".md", ".html", ".htm")
# Side units are synthesised one sentence per Rime context: the client sees
# one unit, but a cancel fences at most one sentence's bytes.
SENTENCE_STREAMED_KINDS = ("map", "companion", "start_choice", "welcome", "recap", "ingest_wait")
# The fixtures in git: DELETE refuses them unless ?force=1.
COMMITTED_FIXTURES = frozenset({"policy", "carers_allowance", "arogya_sanjeevani", "bharat_griha_raksha",
                                "home_loan_mitc", "saral_jeevan_bima", "two_wheeler_loan_agreement"})
INGEST_STAGES = ("extract", "structure", "segment", "normalize", "pii_scan", "validate", "write")
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
    abandoned: bool = False        # reading stopped before it was heard; excluded from flow control
    kind: str = "clause"           # clause | answer
    char_start: int = 0            # a resumed clause is synthesised from here; 0 otherwise

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
    def __init__(self, dev: bool = False, index_path: Path = INDEX,
                 allow_upload: bool = True) -> None:
        self.id = f"web-{uuid.uuid4().hex[:8]}"
        self.dev = dev
        self.started = time.monotonic()
        TRACES.mkdir(exist_ok=True)
        self.events = BroadcastEventLog(TRACES / f"session_{self.id}.jsonl", session_id=self.id)
        self.library = Library(index_path, self.events)
        self.index_path = index_path
        # Upload is a first-class path on both routes (POST /documents); the
        # 25 MB cap and the type check are the only refusals.
        self.allow_upload = True
        self._ingest_lock = asyncio.Lock()          # one conversion at a time
        # On-entry enrichment: the navigator fields are generated when a
        # document enters (upload) or is first opened without them, never by
        # a separate step. One model call at a time; a task per document.
        # Navigator enrichment has its own lock and priority: the rest pass
        # (tags, questions, tables) runs one model call at a time and yields
        # while a navigator waits, an answer is in flight or a prompt is open.
        self._navigator_lock = asyncio.Lock()
        self._open_task: Optional[asyncio.Task] = None
        self._open_seq = 0
        self._navigator_waiting = 0
        self._enriching: dict[str, asyncio.Task] = {}
        self.enrich_rest_enabled = ENRICH_REST_ENABLED
        # Optional LLM behind grounded answers; None means extractive. Key never
        # leaves this process.
        self.llm = make_llm()
        # Filled by on_start after check_llm()/warm_llm(); read by /api/status.
        self.llm_state: Optional[dict] = None
        self._resume_from: Optional[tuple] = None      # (unit_id, char_start) after a cut
        self._answer: Optional[dict] = None            # the spoken answer in flight
        self._answer_task: Optional[asyncio.Task] = None
        self._stop_seq = 0                             # bumps on every stop_reading
        # Exactly one socket receives audio: the tab that pressed play. Every
        # other tab sees the same state and events but hears nothing, so two
        # open tabs cannot become two voices, and "heard" has one witness.
        self.sink = None
        self._flush_waiter: Optional[asyncio.Future] = None
        # Documents whose session-start bookkeeping has been done: the map
        # spoken once and one unit_skipped per clause linear playback never
        # sends, so the session record has no silent gaps.
        self._session_started: set = set()
        # Conversation layer: the one open prompt (kind, options, opened_at,
        # payload -- see open_prompt), the cue the next read_loop speaks first,
        # the position a jump left ("go back to where I was"), the sections
        # already offered on, and the start_choice bookkeeping: how often the
        # invitation was spoken per document (at most twice: once, and once
        # more after a question) and the document to re-open it for.
        self.conv = Conversation(self, PROMPT_TIMEOUT_S)   # the one open prompt lives here (see _prompt)
        self._pending_cue: Optional[str] = None
        self._jump_back: Optional[dict] = None
        self._offered: set = set()
        self._timer: Optional[asyncio.Task] = None
        self._start_asked: dict = {}
        self._start_reopen: Optional[str] = None
        self._map_skipped: set = set()                 # "from the start": no overview first
        self._table_decision: Optional[dict] = None    # what the listener chose at a table prompt
        self._speak_lock: Optional[asyncio.Lock] = None   # created on the running loop, first use
        self._paused_at: Optional[float] = None        # when the listener last paused or interrupted
        self._opened_by_listener: set = set()          # documents the listener chose (rail, welcome, title)
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
        rows = self.library.list()
        if rows:
            self.library.open(rows[0]["name"])

    # ------------------------------------------------------------ provider
    async def ensure_provider(self, name: Optional[str] = None):
        if self.provider is not None and name is None:
            if getattr(self.provider, "connected", True):
                return self.provider
            # The socket died under us (Rime drops an idle connection; seen
            # as "keepalive ping timeout" after a four-hour pause). Every play
            # failed with reader_error until the process was restarted.
            # Reconnect the same kind of provider instead.
            name = self.provider.name
            self.events.emit("provider_reconnect", provider=name)
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
            self.provider = make_provider(self.events, name="rime")
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

    def topics_for(self, doc) -> list:
        """The chips for a document that has its navigator; [] before that
        (the client shows the sections found until the chips land)."""
        if doc is None or not self.has_navigator(doc):
            return []
        try:
            return [{"topic": t["topic"], "section_id": t.get("section_id"), "heading": t.get("heading")}
                    for t in doc.grounding.topics]
        except Exception:
            return []

    def sections_for(self, doc) -> list:
        """The real section outline -- title and the same est_minutes already
        spoken at a section transition (`sec.get("est_minutes")` in
        read_loop) -- for a client-side "up next" list. [] before the
        navigator has run; nothing here is estimated client-side."""
        if doc is None or not self.has_navigator(doc):
            return []
        try:
            return [{"id": s["id"], "title": s["title"], "est_minutes": s.get("est_minutes")}
                    for s in doc.grounding.sections]
        except Exception:
            return []

    def opened_message(self, doc) -> dict:
        """document_opened, carrying the chips so no later frame has to and
        the client never clears them for the document it shows."""
        return {"type": "document_opened", "name": doc.name, "documents": self.listener_library(),
                "topics": self.topics_for(doc), "sections": self.sections_for(doc)}

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
                "name": doc.name, "title": doc.title, "spoken_title": doc.spoken_title,
                "section_count": len(sections),
                "estimated_minutes": est_min,
                "progress": {
                    "started": bool(s.ledger),
                    "finished": finished,
                    "current_section_index": idx,
                    "minutes_left": left_min,
                },
                "referral": self.referral_for(doc.name),
                "doc_id": doc.doc_id,
                "reviewed": doc.reviewed,
                "readable": doc.readable,
                "unreviewed": not doc.reviewed,
                # ready | preparing | mechanical: whether play opens with the
                # generated overview and chips, is still generating them, or
                # will use the mechanical map.
                "navigator": self.navigator_state(doc),
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

        fenced_session = sum(int(r.get("bytes") or 0) for r in self.events.records if r.get("type") == "result_fenced")
        fenced_units = sum(c.bytes for c in self.contexts.values() if c.state == "fenced")
        return {
            "ttfb_p50": pct(ttfbs, 50), "ttfb_p95": pct(ttfbs, 95),
            "fenced_bytes_after_clear": fenced if seen_cancel else None,
            "fenced_bytes_session": fenced_session,
            "fenced_unit_bytes": fenced_units,
            "interpolated_spans": interp if total else None,
            "spans_total": total or None,
            "flush_ack_p50": round(statistics.median(gaps), 1) if gaps else None,
        }

    def status(self) -> dict:
        def cell(state, detail):
            return {"state": state, "detail": detail}

        try:
            import ingest_structure as _istr
            ing = cell("ok", "docling warm" if _istr._CONVERTERS else "docling ready (cold)") \
                if _istr.docling_available() else cell("warn", "docling not installed: text/docx only")
        except Exception as e:
            ing = cell("warn", f"structure pass unavailable: {e}"[:80])
        norm = cell("ok", f"{len(self.wordmaps)} word maps built") if self.wordmaps \
            else cell("ok", "ready")
        if self.provider is None:
            rime = cell("down", "not connected")
        elif self.provider.name == "fake":
            rime = cell("warn", "fake provider, no Rime connection")
        elif not getattr(self.provider, "connected", True):
            rime = cell("down", "socket dropped; reconnects on the next play")
        else:
            age = int(time.monotonic() - (self.provider_connected_at or time.monotonic()))
            rime = cell("ok", f"open {age} s")
        clients = len(self.events.sockets)
        cws = cell("ok", f"{clients} connected") if clients else cell("down", "no client")
        ld = describe_llm()
        if ld["active"] == "llm" and self.llm:
            st = self.llm_state or {}
            if st.get("ok") is False:
                # Configured but not answering: every question falls back to
                # the extractive answer and the trace says llm_failed.
                llm = cell("warn", f"{ld['provider']} {ld['model']}: {st.get('detail', '')}"[:120])
            else:
                llm = cell("ok", f"{ld['provider']} {ld['model']}")
        else:
            llm = cell("off", "extractive")
        return {
            "session_id": self.id,
            "dev": self.dev,
            "upload_enabled": self.allow_upload,
            "enrich_rest": {"enabled": self.enrich_rest_enabled, "navigator_waiting": self._navigator_waiting},
            "enrichment": {"configured": self.enrichment_configured(),
                           "provider": os.environ.get("ENRICH_PROVIDER", "none").strip().lower() or "none",
                           "model": os.environ.get("OLLAMA_MODEL", "granite4.2:3b")
                           if os.environ.get("ENRICH_PROVIDER", "").strip().lower() == "ollama" else None,
                           "current": self.navigator_state(self.library.current) if self.library.current else None},
            "replaying": self.replaying,
            "provider": self.descriptor,
            "ingest": ing,
            "normalize": norm,
            "rime_ws": rime,
            "client_ws": cws,
            # No direct visibility into the bridge process from here (it's
            # just another /ws/audio client, same as a browser tab) -- this
            # describes the mechanism honestly without claiming a live
            # status this endpoint can't actually verify.
            "stt": cell("warn", "external voice bridge (examples/policy-reader/voice/bridge.py)"),
            "llm": llm,
        }

    # ---------------------------------------------------------- audio sink
    def audio_targets(self) -> set:
        return {self.sink} if self.sink is not None else set()

    async def announce_sink(self, sockets) -> None:
        for ws in list(sockets):
            try:
                await ws.send_str(json.dumps({"type": "sink", "you": ws is self.sink,
                                              "any": self.sink is not None}))
            except Exception:
                pass

    async def request_flush(self, timeout: float = 3.0) -> bool:
        """A stop from a tab that is not the audio sink: ask the sink to flush
        and report its playhead, and wait for that flush_ack. The boundary is
        still the audio clock's, just measured on the tab that has the audio."""
        if self.sink is None:
            return False
        self._flush_waiter = asyncio.get_running_loop().create_future()
        try:
            await self.sink.send_str(json.dumps({"type": "flush"}))
            await asyncio.wait_for(self._flush_waiter, timeout)
            return True
        except (asyncio.TimeoutError, Exception):
            # Three seconds: the tab must drain the audio frames already queued
            # on its socket (a 600-char clause is ~1,000 of them) before it
            # sees the flush frame.
            self.events.emit("flush_ack_timeout", timeout_ms=int(timeout * 1000))
            return False
        finally:
            self._flush_waiter = None

    async def claim_sink(self, ws, sockets) -> None:
        if ws is self.sink:
            return
        self.sink = ws
        await self.announce_sink(sockets)

    # -------------------------------------------------------------- reading
    def _backlog_ms(self) -> float:
        """Audio sent but not yet acked as played, over units still deliverable.

        Abandoned units are excluded. When reading stops, the client flushes
        its queue and a fresh read_loop re-synthesises from the cursor, so the
        unplayed remainder of a stopped unit will never be acked. Counting it
        held the 4 s lead for the rest of session_web-29e08c00 (7.5 s of a
        clause paused at 2.7 s): every later clause stalled after synthesis
        until the listener pressed pause and play again.
        """
        return sum(max(0.0, st.audio_ms - st.rendered_ms)
                   for st in self.contexts.values()
                   if st.audio_ms > 0 and not st.abandoned and not st.heard)

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
        if st.kind == "answer":
            # Heard means the listener heard the whole answer. Only now does
            # reading pick up again, and only for answers that do not wait on a
            # choice (Jump there / Keep going).
            asyncio.ensure_future(self._after_answer(st, sockets))
            return
        if st.kind in PROMPT_KINDS:
            # The prompt was heard: now the listener has the floor for a while.
            asyncio.ensure_future(self._after_prompt(st, sockets))
            doc = self.library.current
            if doc is None or st.unit_id not in doc.grounding.by_id:
                return                                   # a prompt with no clause behind it
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

    async def _stream_sentences(self, provider, st: "ContextState", unit_id: str, text: str,
                                sockets, keep_going) -> None:
        """A side unit (the overview, an invitation, a companion line) as one
        client context but one Rime context per sentence: each sentence is
        its own synth call, so a cancel mid-unit fences at most that
        sentence's bytes -- not the whole overview streaming on for 38 s.
        The unit's spans carry the sentence boundaries; the word map is
        built per sentence and shifted onto the full text."""
        ctx_id = st.context_id
        spans = [(a, b) for a, b in (sentence_spans(text) or [(0, len(text))])]
        total_bytes = 0
        for i, (a, b) in enumerate(spans):
            if not keep_going():
                break
            sent = text[a:b].strip()
            if not sent:
                continue
            part = ContextState(f"{ctx_id}.s{i}", st.turn_id, unit_id, state="streaming", kind=st.kind)
            await self._stream_sentence_part(provider, st, part, unit_id, sent, a, sockets, keep_going,
                                             last=(i == len(spans) - 1))
            total_bytes += part.bytes
            if part.state == "error":
                st.state = "error"
                break
        if st.state != "error":
            st.state = "done"
            st.synth_done = True
            await broadcast({"type": "unit_done", "context_id": ctx_id, "unit_id": unit_id,
                             "bytes": total_bytes, "sentences": len(spans)}, sockets)
        if st.bytes == 0:
            self.events.emit("no_audio", context_id=ctx_id)

    async def _stream_sentence_part(self, provider, st: "ContextState", part: "ContextState", unit_id: str,
                                    sent: str, offset: int, sockets, keep_going, last: bool) -> None:
        """One sentence of a side unit through Rime under its own context id;
        audio and word spans are relayed on the unit's client context."""
        ctx_id = st.context_id
        self.events.emit("sentence_synth", context_id=ctx_id, rime_context=part.context_id,
                         unit_id=unit_id, chars=len(sent), offset=offset)
        async for item in provider.synth(sent, part.context_id):
            if not keep_going():
                break
            if isinstance(item, Timestamps):
                try:
                    wm = build_word_map(unit_id, sent, [Segment(0, len(sent), sent)],
                                        item.words, item.start_ms, item.end_ms)
                    base = st.audio_ms
                    spans = [{"char_start": sp.char_start + offset, "char_end": sp.char_end + offset,
                              "t_start_ms": sp.t_start_ms + base, "t_end_ms": sp.t_end_ms + base} for sp in wm.spans]
                except Exception as e:                       # alignment is best effort
                    self.events.emit("wordmap_failed", context_id=ctx_id, error=str(e))
                    spans = []
                await broadcast({"type": "timestamps", "context_id": ctx_id, "words": item.words,
                                 "start_ms": [t + st.audio_ms for t in item.start_ms],
                                 "end_ms": [t + st.audio_ms for t in item.end_ms],
                                 "spans": spans, "sentence": part.context_id}, sockets)
            elif isinstance(item, AudioChunk):
                part.bytes += len(item.pcm)
                st.bytes += len(item.pcm)
                st.audio_ms = st.bytes / 2 / provider.sample_rate * 1000
                st.state = part.state = "playing"
                await broadcast({"type": "audio", "context_id": ctx_id, "seq": item.seq,
                                 "sample_rate": provider.sample_rate,
                                 "b64": base64.b64encode(item.pcm).decode()}, self.audio_targets())
            elif isinstance(item, Done):
                if st.ttfb_ms is None:
                    st.ttfb_ms = item.ttfb_ms
                part.state = "done"
                part.synth_done = True
            elif isinstance(item, TTSError):
                part.state = "error"
                await broadcast({"type": "provider_error", "context_id": ctx_id,
                                 "message": item.message}, sockets)
                return

    async def _stream_unit(self, provider, st: "ContextState", unit_id: str,
                           text_display: str, text_spoken: str, segs: list,
                           sockets, keep_going, char_start: int = 0) -> None:
        """Synthesise one unit and stream it to the client.

        Shared by clauses and spoken answers, so an answer is delivered exactly
        like a clause: word map, byte-derived audio_ms, unit_done, and heard
        only on the client's acks. `char_start` shifts the word map's char
        offsets back onto the full display text when a clause is resumed from
        a sentence boundary rather than its start.
        """
        ctx_id = st.context_id

        def span_dicts(wm):
            return [{"char_start": sp.char_start + char_start, "char_end": sp.char_end + char_start,
                     "t_start_ms": sp.t_start_ms, "t_end_ms": sp.t_end_ms} for sp in wm.spans]

        async for item in provider.synth(text_spoken, ctx_id):
            if not keep_going():
                break
            if isinstance(item, Timestamps):
                # NOT audio_ms. These are a prediction that undershoots
                # the delivered audio; audio_ms comes from bytes only.
                st.predicted_end_ms = item.end_ms[-1] if item.end_ms else None
                try:
                    wm = build_word_map(unit_id, text_display, segs,
                                        item.words, item.start_ms, item.end_ms)
                    self.wordmaps[ctx_id] = wm
                except Exception as e:                       # alignment is best effort
                    self.events.emit("wordmap_failed", context_id=ctx_id, error=str(e))
                # Spans carry char offsets into text_display, so the
                # client can advance a read-along highlight from its own
                # audio clock instead of waiting for a boundary event.
                wm_now = self.wordmaps.get(ctx_id)
                await broadcast({"type": "timestamps", "context_id": ctx_id,
                                 "words": item.words, "start_ms": item.start_ms,
                                 "end_ms": item.end_ms,
                                 "spans": span_dicts(wm_now) if wm_now else []}, sockets)
            elif isinstance(item, AudioChunk):
                st.bytes += len(item.pcm)
                # The only honest length: bytes actually produced.
                st.audio_ms = st.bytes / 2 / provider.sample_rate * 1000
                st.state = "playing"
                await broadcast({"type": "audio", "context_id": ctx_id, "seq": item.seq,
                                 "sample_rate": provider.sample_rate,
                                 "b64": base64.b64encode(item.pcm).decode()},
                                self.audio_targets())
            elif isinstance(item, Done):
                st.ttfb_ms = item.ttfb_ms
                st.state = "done"
                if item.total_bytes:
                    st.audio_ms = item.total_bytes / 2 / provider.sample_rate * 1000
                self.events.emit(
                    "timestamp_drift", context_id=ctx_id, unit_id=unit_id,
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
                        "spans": span_dicts(wm_done),
                        "corrected": True,
                    }, sockets)
                st.synth_done = True
                await broadcast({"type": "unit_done", "context_id": ctx_id,
                                 "unit_id": unit_id, "bytes": item.total_bytes}, sockets)
            elif isinstance(item, TTSError):
                st.state = "error"
                await broadcast({"type": "provider_error", "context_id": ctx_id,
                                 "message": item.message}, sockets)
        if st.bytes == 0:
            self.events.emit("no_audio", context_id=ctx_id)

    # ------------------------------------------------------------ enrichment
    @staticmethod
    def enrichment_configured() -> bool:
        return os.environ.get("ENRICH_PROVIDER", "none").strip().lower() not in ("", "none", "off")

    @staticmethod
    def has_navigator(doc) -> bool:
        fx = doc.fixture
        return bool(fx.get("overview")) and bool(fx.get("topics"))

    def navigator_state(self, doc) -> str:
        """ready | preparing | mechanical -- what the listener will get at play."""
        if self.has_navigator(doc):
            return "ready"
        t = self._enriching.get(doc.name)
        if t is not None and not t.done():
            return "preparing"
        return "mechanical"

    def ensure_navigator(self, doc, sockets=None, retry: bool = False, narrate: bool = True) -> asyncio.Task:
        """Generate the overview, section briefs and topic chips for `doc` if
        it has none, in a worker thread, once. Returns the task (done at once
        when nothing needs doing). On success the document's fixture and
        grounding are reloaded so the next play speaks the generated overview,
        and the remaining fields follow in the background.

        Once per document per process: a failed attempt is not repeated at
        open or play -- the rows already say `mechanical`, and play must not
        speak a "one moment" cue for a wait that will fail again. An upload
        of the document asks for a fresh attempt with retry=True."""
        t = self._enriching.get(doc.name)
        if t is not None and (not t.done() or not retry):
            return t
        t = asyncio.ensure_future(self._enrich(doc, sockets, narrate=narrate))
        self._enriching[doc.name] = t
        return t

    async def take_voice_for(self, doc, reason: str, socks, ws=None) -> bool:
        """The voice for processing `doc` (an upload, or the enrichment of a
        document just opened). Whatever is sounding is stopped exactly like an
        interrupt -- boundary at the playhead, position saved, unit_truncated
        with this `reason` -- any open prompt is closed as superseded, and the
        companion says "I'll pause here and go through {title}." Returns True
        when the companion may speak now. With no tab holding the voice nothing
        is said: the document is remembered and told at the first play."""
        if self.sink is None:
            if doc.name not in self.conv.pending_narration:
                self.conv.pending_narration[doc.name] = {"reason": reason, "at": time.monotonic()}
                self.events.emit("narration_pending", document=doc.name, reason=reason)
            return False
        if self.playing or (self._reader and not self._reader.done()) or self._answer is not None:
            await self.stop_reading()
            if ws is not self.sink and self._pending_flush is None:
                await self.request_flush()
            await _stop_and_attribute(self, socks, reason)
            self.events.emit("voice_taken", document=doc.name, reason=reason)
        if self._prompt is not None and not (self.prompt_kind() == "ingest_wait" and self.prompt_document() == doc.name):
            await self.resolve_prompt("superseded", None)
        await self.speak_companion(cp.TAKEOVER_LINE.format(title=doc.spoken_title), "takeover", "template",
                                   socks, doc=doc)
        return True

    async def speak_backlog(self, socks) -> None:
        """The first play after documents were processed with no voice: one
        line per document, bound to it."""
        for name in list(self.conv.pending_narration):
            self.conv.pending_narration.pop(name, None)
            doc = self.library._docs.get(name)
            if doc is None:
                continue
            still = (self._enriching.get(name) is not None and not self._enriching[name].done())
            if still:
                line = cp.BACKLOG_PENDING_LINE.format(title=doc.spoken_title)
            else:
                k = len(doc.grounding.sections)
                line = cp.BACKLOG_LINE.format(title=doc.spoken_title, n=k, s="" if k == 1 else "s")
            self.events.emit("backlog_spoken", document=doc.name, text=line)
            await self.speak_companion(line, "backlog", "template", socks, doc=doc)

    async def narrate_enrichment(self, doc, socks, owner: str = "open"):
        """The companion for one document's ingest or enrichment, after
        take_voice_for: bound to that document, so every line and the
        engagement question name it whatever is open. On the open path the
        first line after the takeover is the structure line ("I can see {S}
        sections: ...") and the headings go to the client as sections_found.
        One per document."""
        n = self.conv.narrators.get(doc.name)
        if n is not None and not n.finished:
            return n
        n = cp.Narrator(self, socks, fillers_used=self.conv.fillers_used, doc=doc, owner=owner)
        self.conv.narrators[doc.name] = n
        opening = None
        if owner == "open":
            try:
                titles = [s["title"] for s in doc.grounding.sections]
            except Exception:
                titles = []
            await self.sections_found(titles[:40], socks)
            opening = cp.progress_line("structure", "ok", {"headings": titles, "n_sections": len(titles)})
        n.start(opening_line=opening)
        return n

    def schedule_start_choice(self, doc, socks) -> None:
        """The invitation for a document opened from the rail, once the opens
        have stopped for START_CHOICE_DEBOUNCE_S: a later open cancels it, and
        it is skipped if by then the document has started, was asked, or the
        voice has gone. Otherwise the first play asks."""
        if self._open_task is not None and not self._open_task.done():
            self._open_task.cancel()
        self._open_seq += 1
        seq = self._open_seq

        async def later():
            await asyncio.sleep(START_CHOICE_DEBOUNCE_S)
            if seq != self._open_seq or self.library.current is not doc or self.sink is None:
                return
            if doc.name in self._session_started or doc.name in self._start_asked:
                return
            self.events.emit("start_choice_due", document=doc.name, after_ms=round(START_CHOICE_DEBOUNCE_S * 1000))
            await self._start_choice_if_due(doc, socks)
        self._open_task = asyncio.ensure_future(later())

    async def _start_choice_if_due(self, doc, socks) -> None:
        """start_choice for a document that is ready: now if it is the open
        document, the voice is claimed and idle and no other prompt is open;
        otherwise deferred until it is opened. The parked question first."""
        if self.library.current is not doc or self.sink is None:
            self.conv.deferred_start.add(doc.name)
            return
        if self.playing or (self._reader and not self._reader.done()) or self._answer is not None:
            return
        if self._prompt is not None and not (self.prompt_kind() == "ingest_wait" and self.prompt_document() == doc.name):
            self.conv.deferred_start.add(doc.name)      # another document's prompt has the floor
            return
        if self.prompt_kind() == "ingest_wait" and self.prompt_document() == doc.name:
            await self.resolve_prompt("cancelled", None)  # closes at ready
        if self.conv.parked_for(doc.name):
            await self.answer_parked(doc, socks)
        elif self.has_navigator(doc) and doc.name not in self._session_started and doc.name not in self._start_asked:
            await self.open_start_choice(doc, socks)
        else:
            self.conv.deferred_start.discard(doc.name)

    async def _enrich(self, doc, sockets, narrate: bool = True) -> str:
        if self.has_navigator(doc):
            return "ready"
        if not self.enrichment_configured():
            return "mechanical"
        import enrich as enrich_mod
        from delivery_layer.enrich.provider import make_enrich_provider
        loop = asyncio.get_running_loop()
        socks = sockets if sockets is not None else self.events.sockets
        t0 = time.monotonic()
        narrator = None
        if narrate and doc.name not in self.conv.narrators:
            # The interactive voice runs whenever a document is processed:
            # take it over if something is sounding, else note it for later.
            if await self.take_voice_for(doc, "open", socks):
                narrator = await self.narrate_enrichment(doc, socks, owner="open")
                narrator.prime("takeover", cp.TAKEOVER_LINE.format(title=doc.spoken_title))
        try:
            provider = make_enrich_provider()
            self.events.emit("enrich_started", document=doc.name, doc_id=doc.doc_id,
                             provider=provider.name, model=getattr(provider, "model", None),
                             fields=list(enrich_mod.NAVIGATOR_FIELDS))
            await broadcast({"type": "navigator", "name": doc.name, "state": "preparing"}, socks)
            self._navigator_waiting += 1
            try:
                async with self._navigator_lock:
                    info = await loop.run_in_executor(
                        None, lambda: enrich_mod.enrich_fixture(doc.path, provider, fields=enrich_mod.NAVIGATOR_FIELDS))
            finally:
                self._navigator_waiting -= 1
            # Reload so play speaks the generated overview -- unless a read of
            # this document is already past the top (the wait timed out): then
            # the index is not swapped under it; the next open picks it up.
            if not (self.playing and self.library.current is doc and doc.session.read_cursor > 0):
                doc._fixture = None
                doc._grounding = None
            ms = round((time.monotonic() - t0) * 1000)
            self.events.emit("enrich_done", document=doc.name, doc_id=doc.doc_id, stage="navigator",
                             provider=provider.name, model=getattr(provider, "model", None),
                             fields=info.get("fields", []), elapsed_ms=ms,
                             guard_rejections=len(info.get("guard_rejections") or []))
            await broadcast({"type": "navigator", "name": doc.name, "state": "ready",
                             "documents": self.listener_library(), "topics": self.topics_for(doc),
                             "sections": self.sections_for(doc)}, socks)
            if narrator is not None:
                await narrator.finish()                  # "Done.", and narration_gap_ms
            if self.sink is not None:
                await self._start_choice_if_due(doc, socks)
            # Tags, suggested questions and table shapes: not needed before the
            # first word, so they follow without holding anything.
            asyncio.ensure_future(self._enrich_rest(doc, provider, enrich_mod))
            return "ready"
        except Exception as e:
            self.events.emit("enrich_failed", document=doc.name, doc_id=doc.doc_id,
                             stage="navigator", error=str(e)[:200],
                             elapsed_ms=round((time.monotonic() - t0) * 1000))
            await broadcast({"type": "navigator", "name": doc.name, "state": "mechanical",
                             "error": str(e)[:120]}, socks)
            if narrator is not None:
                await narrator.finish(ok=False)
            if self.prompt_kind() == "ingest_wait" and self.prompt_document() == doc.name:
                await self.resolve_prompt("cancelled", None)
            return "mechanical"

    def _rest_may_run(self) -> bool:
        """The rest pass steps only when the model is nobody else's: no
        navigator waiting, no answer in flight, no prompt open, and the pass
        is enabled."""
        return (self.enrich_rest_enabled and self._navigator_waiting == 0
                and self._answer is None and self._prompt is None)

    async def _enrich_rest(self, doc, provider, enrich_mod) -> None:
        rest = tuple(f for f in enrich_mod.ALL_FIELDS if f not in enrich_mod.NAVIGATOR_FIELDS)
        loop = asyncio.get_running_loop()
        if not self.enrich_rest_enabled:
            self.events.emit("enrich_rest_skipped", document=doc.name, doc_id=doc.doc_id, reason="disabled")
            return
        # Not now: the listener is being asked and answered; the model is theirs.
        deadline = time.monotonic() + ENRICH_REST_DELAY_S
        while time.monotonic() < deadline:
            await asyncio.sleep(0.5)
        while not self._rest_may_run():
            if not self.enrich_rest_enabled:
                self.events.emit("enrich_rest_skipped", document=doc.name, doc_id=doc.doc_id, reason="disabled")
                return
            await asyncio.sleep(0.3)
        self.events.emit("enrich_rest_started", document=doc.name, doc_id=doc.doc_id,
                         waited_s=round(ENRICH_REST_DELAY_S))
        t0 = time.monotonic()
        try:
            import json as _json
            fixture = _json.loads(doc.path.read_text(encoding="utf-8"))
            cps, src = enrich_mod.measured_chars_per_second()
            enricher = enrich_mod.Enricher(provider, cps, src, log=open(os.devnull, "w"))
            steps = enricher.steps(fixture, fields=rest)
            done_names = []
            for i, (name, fn) in enumerate(steps):
                # One model call, then the floor is checked again: a navigator,
                # an answer or a prompt takes precedence over the next step.
                while not self._rest_may_run():
                    if not self.enrich_rest_enabled:
                        self.events.emit("enrich_rest_skipped", document=doc.name, doc_id=doc.doc_id,
                                         reason="disabled", after_steps=i)
                        return
                    await asyncio.sleep(0.3)
                t_s = time.monotonic()
                await loop.run_in_executor(None, fn)
                self.events.emit("enrich_step", document=doc.name, doc_id=doc.doc_id, step=name,
                                 index=i, of=len(steps), ms=round((time.monotonic() - t_s) * 1000))
                done_names.append(name)
                enrich_mod.write_fixture(doc.path, fixture)          # progress persists step by step
            info = enricher.finish(fixture, list(rest), t0, fields=rest)
            enrich_mod.write_fixture(doc.path, fixture, info)
            # Pick the new fields up at the next open or play; never swap the
            # index under a read in progress.
            if not (self.playing and self.library.current is doc):
                doc._fixture = None
                doc._grounding = None
            self.events.emit("enrich_done", document=doc.name, doc_id=doc.doc_id, stage="rest",
                             provider=provider.name, model=getattr(provider, "model", None),
                             fields=info.get("fields", []), steps=len(steps),
                             elapsed_ms=round((time.monotonic() - t0) * 1000))
        except Exception as e:
            self.events.emit("enrich_failed", document=doc.name, doc_id=doc.doc_id,
                             stage="rest", error=str(e)[:200])

    async def read_loop(self, sockets) -> None:
        """Read clause after clause until paused, interrupted, or out of document."""
        doc = self.library.current
        g, s = doc.grounding, doc.session
        provider = await self.ensure_provider()
        try:
            if doc.name not in self._session_started:
                self._session_started.add(doc.name)
                for unit_id, reason in g.skipped():
                    self.events.emit("unit_skipped", document=doc.name, unit_id=unit_id, reason=reason)
                    s.ledger[unit_id] = f"skipped:{reason}"
                if s.read_cursor == 0 and not self.has_navigator(doc) and self.enrichment_configured():
                    # The document entered without its navigator (an older
                    # fixture, or an upload whose enrichment is still running).
                    # Say so, then wait for it rather than reading blind; the
                    # cue keeps the line alive. Past the budget, or on failure,
                    # the mechanical map is spoken instead.
                    t = self.ensure_navigator(doc, sockets, narrate=False)   # the read loop speaks its own cue
                    if not t.done():
                        n = self.conv.narrators.get(doc.name)
                        if n is None or n.finished:
                            await self._speak_unit(provider, "cue",
                                                   "One moment while I get an overview of this document ready.",
                                                   doc, sockets, extra={"reason": "navigator_preparing"})
                        try:
                            await asyncio.wait_for(asyncio.shield(t), timeout=NAVIGATOR_WAIT_S)
                        except asyncio.TimeoutError:
                            self.events.emit("enrich_timeout", document=doc.name, waited_s=NAVIGATOR_WAIT_S)
                        if not self.playing:
                            return
                    g = doc.grounding                     # reloaded if the navigator landed
                if s.read_cursor == 0 and (g.map or g.overview_text) and doc.name not in self._map_skipped:
                    # The overview (generated on entry) or the mechanical
                    # map, spoken once; the topic chips show while it plays.
                    await broadcast({"type": "topics", "document": doc.name,
                                     "topics": [{"topic": t["topic"], "section_id": t.get("section_id"),
                                                 "heading": t.get("heading")} for t in g.topics]}, sockets)
                    self.events.emit("topics_offered", document=doc.name, doc_id=doc.doc_id,
                                     n=len(g.topics))
                    await self._speak_map(provider, g, doc, sockets)
                    if not self.playing:
                        return
            if self._pending_cue:
                # A jump's cue: "Okay -- going to {heading}." Its own unit.
                cue, self._pending_cue = self._pending_cue, None
                await self._speak_unit(provider, "cue", cue, doc, sockets)
                if not self.playing:
                    return
            while self.playing and s.read_cursor < len(g.clauses):
                # Boilerplate and on-request table rows keep their place in
                # reading order but are never sent; they were logged above.
                s.read_cursor = g.next_readable(s.read_cursor)
                if s.read_cursor >= len(g.clauses):
                    break
                c = g.clauses[s.read_cursor]
                # A section boundary: first the suggested question for the
                # section just finished (once it has been heard), then the
                # coming-up cue for the new one.
                sec = g.section_at_start(c["index"])
                if sec is not None and s.read_cursor > 0:
                    offered = await self._offer_after_section(provider, g, doc, sockets, c["index"])
                    if not self.playing:
                        return
                    if not offered:
                        await self.maybe_section_end(provider, g, doc, sockets, c["index"], sec)
                        if not self.playing:
                            return
                if gr.clause_kind(c) == "table_stub" and not c.get("read_inline"):
                    rows = _table_rows(g, c)
                    if rows:
                        # Not the stub sentence and on: ask which row, all of
                        # them, or carry on. Rows spoken on request are heard
                        # like clauses; the rest stay skipped:table_on_request.
                        s.current_unit_id = c["id"]
                        s.read_index = c["index"]
                        s.read_cursor += 1
                        await self._table_choice(provider, g, doc, sockets, c, rows)
                        if not self.playing:
                            return
                        continue
                self.turn += 1
                ctx_id = f"{c['id']}#t{self.turn}"
                st = ContextState(ctx_id, self.turn, c["id"], state="streaming")
                self.contexts[ctx_id] = st
                s.current_unit_id = c["id"]
                s.read_index = c["index"]
                s.read_cursor += 1

                # A clause cut by an interrupt or a pause is picked up at the
                # sentence containing the cut, not re-read from the top. The
                # display text stays whole; only the synthesised text is the
                # remainder, and its word map is shifted back onto the whole.
                char_start = 0
                if self._resume_from and self._resume_from[0] == c["id"]:
                    rp = resume_point(c["id"], c["text_display"], c["sentences"],
                                      self._resume_from[1], c["section_title"])
                    self._resume_from = None
                    char_start = max(0, min(rp.char_start, len(c["text_display"]) - 1))
                    st.char_start = char_start
                    self.events.emit("position_restored", document=doc.name, unit_id=rp.unit_id,
                                     sentence_index=rp.sentence_index, char_start=char_start)
                    await broadcast({"type": "resume_point", "unit_id": rp.unit_id,
                                     "sentence_index": rp.sentence_index,
                                     "char_start": char_start, "cue": rp.cue,
                                     "text": rp.text}, sockets)
                text_spoken, pieces = _spoken_from(c, char_start)
                display = c["text_display"][char_start:]
                if char_start == 0:
                    if sec is not None and sec.get("brief"):
                        # Section transition, spoken as the coming-up cue.
                        est = sec.get("est_minutes")
                        text_spoken = (f"Next is {sec['title']}. {sec['brief']}"
                                       + (f" About {est} minute{'s' if est != 1 else ''}." if est else ""))
                        pieces = list(c["spoken_map"])
                    elif c.get("spoken_override"):
                        text_spoken = spoken_text(c)          # generated table description / row sentence

                await broadcast({
                    "type": "unit_started", "context_id": ctx_id, "turn_id": self.turn,
                    "unit_id": c["id"], "index": c["index"], "kind": "clause",
                    "section_title": c["section_title"], "path": c.get("path"),
                    "text_display": c["text_display"], "sentences": c["sentences"],
                    "char_start": char_start,
                }, sockets)

                segs = [Segment(a - char_start, b - char_start, sp,
                                replaced=c["text_display"][a:b] != sp)
                        for a, b, sp in pieces]
                try:
                    await self._stream_unit(provider, st, c["id"], display, text_spoken, segs,
                                            sockets, lambda: self.playing, char_start)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    # A dead socket before a single byte went out: reconnect
                    # and say the same unit again on the same context. Once
                    # audio has been sent the client holds part of it, so that
                    # case still surfaces as reader_error.
                    if st.bytes or getattr(provider, "connected", True):
                        raise
                    self.events.emit("provider_reconnect", provider=provider.name,
                                     context_id=ctx_id, error=str(e)[:160])
                    provider = await self.ensure_provider()
                    await self._stream_unit(provider, st, c["id"], display, text_spoken, segs,
                                            sockets, lambda: self.playing, char_start)

                if not self.playing:
                    return

                # Flow control, NOT a wait for this clause to finish. Synthesis
                # runs ahead of the playhead by at most LEAD_MS so the next
                # clause's audio is already buffered when this one ends -- that
                # is what removes the inter-clause gap. "Heard" is marked
                # separately, from client acks (see _mark_heard). The old code
                # blocked here for the whole real-time playback, which made every
                # clause boundary a pause and, when the last ack plateaued just
                # short of the end, a 12-second one.
                while self.playing and self._backlog_ms() > LEAD_MS:
                    await asyncio.sleep(0.05)

            # Synthesis is done; wait for playback to drain before declaring the
            # document finished, so a listener still hearing the last clause is
            # not told it is over.
            drain_deadline = time.monotonic() + (self._backlog_ms() / 1000.0) + 15.0
            while self.playing and self._backlog_ms() > 250:
                if time.monotonic() > drain_deadline:
                    self.events.emit("drain_timeout", backlog_ms=round(self._backlog_ms(), 1))
                    break
                await asyncio.sleep(0.1)
            if self.playing and s.read_cursor >= len(g.clauses):
                await self.open_end_choice(provider, g, doc, sockets)
                if self.playing:                         # no section asked for again: it is over
                    self.playing = False
                    await broadcast({"type": "document_finished", "name": doc.name}, sockets)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.events.emit("reader_error", error=str(e))
            await broadcast({"type": "provider_error", "message": str(e)}, sockets)

    async def _speak_unit(self, provider, kind: str, text: str, doc, sockets,
                          keep_going=None, extra: Optional[dict] = None,
                          wait_lead: bool = True, unit_id: Optional[str] = None,
                          display: Optional[str] = None, index: int = -1,
                          section_title: Optional[str] = None) -> "ContextState":
        """A non-clause unit -- map/overview, cue, a prompt, a table row spoken
        on request -- streamed like a clause so it is acked and interruptible,
        with its own context. `unit_id` names a clause of the document when the
        unit stands for one (a table row, the table prompt for its stub) so the
        ack lands in the ledger; otherwise the unit is its kind and has no
        ledger entry."""
        self.turn += 1
        ctx_id = f"{kind}#t{self.turn}"
        uid = unit_id or kind
        display = text if display is None else display
        st = ContextState(ctx_id, self.turn, uid, state="streaming", kind=kind)
        self.contexts[ctx_id] = st
        self.events.emit(f"{kind}_spoken", document=doc.name, doc_id=doc.doc_id, context_id=ctx_id,
                         unit_id=uid, text=text, **(extra or {}))
        await broadcast({
            "type": "unit_started", "context_id": ctx_id, "turn_id": self.turn,
            "unit_id": uid, "index": index, "kind": kind,
            "section_title": section_title or doc.title, "path": None,
            "text_display": display, "sentences": [[0, len(display)]], "char_start": 0,
        }, sockets)
        if kind in SENTENCE_STREAMED_KINDS and display == text:
            await self._stream_sentences(provider, st, uid, text, sockets, keep_going or (lambda: self.playing))
        else:
            await self._stream_unit(provider, st, uid, display, text, [Segment(0, len(text), text)],
                                    sockets, keep_going or (lambda: self.playing))
        # Flow control only when more units follow. A prompt is the last thing
        # said, and its caller must not block a socket's handler waiting for
        # acks that arrive on that same socket.
        while wait_lead and (keep_going or (lambda: self.playing))() and self._backlog_ms() > LEAD_MS:
            await asyncio.sleep(0.05)
        return st

    async def speak_companion(self, text: str, source: str, origin: str, socks, cancelled=None, doc=None) -> Optional["ContextState"]:
        """One companion line: a `companion` unit under the speak lock, acked
        and interruptible, never over a prompt that is waiting for the listener
        (a heard prompt with its timer running). `cancelled()` true stops the
        line in flight -- any listener reply does that -- and the client gets
        the terminal unit_done marked cut, as for a prompt. Trace:
        companion_spoken{source, origin, text}."""
        doc = doc if doc is not None else self.library.current
        if self.sink is None or doc is None:
            self.events.emit("companion_skipped", reason="no_sink", source=source, text=text)
            return None
        cancelled = cancelled or (lambda: False)
        provider = await self.ensure_provider()
        async with self.speak_lock:
            while self._timer and not self._timer.done() and not cancelled():
                await asyncio.sleep(0.2)                 # the listener has the floor
            if cancelled():
                self.events.emit("companion_cancelled", source=source, text=text)
                return None
            st = await self._speak_unit(provider, "companion", text, doc, socks,
                                        keep_going=lambda: not cancelled(), wait_lead=False,
                                        extra={"source": source, "origin": origin})
        if not st.synth_done and st.state != "error":
            st.abandoned = True
            st.state = "fenced"
            self.events.emit("unit_fenced", context_id=st.context_id, unit_id=st.unit_id,
                             bytes=st.bytes, rendered_ms=round(st.rendered_ms, 1), reason="companion_cancelled")
            await broadcast({"type": "unit_done", "context_id": st.context_id, "unit_id": st.unit_id,
                             "bytes": st.bytes, "cut": True}, socks)
        return st

    async def sections_found(self, titles: list, socks) -> None:
        """The structure pass has the headings: the client gets the section
        list now, before enrichment, and the trace records it."""
        self.events.emit("sections_found", n=len(titles), titles=titles)
        await broadcast({"type": "sections_found", "titles": titles}, socks)

    async def speak_cue(self, text: str, reason: str, sockets=None) -> Optional[asyncio.Task]:
        """A cue over the claimed voice, in a task: it never holds its caller
        (an upload's stages run on). Skipped, and said so in the trace, when no
        tab has the voice (cue_skipped{reason: no_sink}) or the voice is busy
        reading (reason: reading): two voices at once is worse than silence."""
        socks = sockets if sockets is not None else self.events.sockets
        doc = self.library.current
        if self.sink is None or doc is None:
            self.events.emit("cue_skipped", reason="no_sink", cue=reason)
            return None
        if self.playing or (self._reader and not self._reader.done()) or self._answer is not None:
            self.events.emit("cue_skipped", reason="reading", cue=reason)
            return None
        provider = await self.ensure_provider()

        async def say():
            async with self.speak_lock:
                return await self._speak_unit(provider, "cue", text, doc, socks, keep_going=lambda: True,
                                              extra={"reason": reason}, wait_lead=False)
        return asyncio.ensure_future(say())

    async def _speak_map(self, provider, g, doc, sockets) -> None:
        """The overview generated at build time, or the mechanical map, once at
        session start. Not a clause; no ledger entry. Once it is heard, the
        pick_topic prompt: "Where shall we start: {top three chips}, or from
        the top?" (8 s; silence reads from the top)."""
        text = g.overview_text or g.map_sentence("policy" if "policy" in doc.spoken_title.lower() else "document")
        st = await self._speak_unit(provider, "map", text, doc, sockets,
                                    extra={"sections": len(g.map), "generated": bool(g.overview_text)})
        chips = [t for t in g.topics if t.get("section_id")][:3]
        if not chips or not self.playing:
            return
        await self._wait_heard(st)
        if not self.playing:
            return
        names = [t["topic"] for t in chips]
        prompt = "Where shall we start: " + ", ".join(names) + ", or from the top?"
        task = await self.open_prompt("pick_topic", prompt, ["topic", "top"], sockets=sockets,
                                      payload={"topics": names, "sections": [t["section_id"] for t in chips]},
                                      classify_options={**{n: f"topic:{t['section_id']}" for n, t in zip(names, chips)},
                                                        "from the top": "top"})
        st2 = await task
        await self._wait_heard(st2, until=lambda: self.prompt_kind() != "pick_topic")
        await self._wait_prompt("pick_topic")       # a topic jumps (cancelling this loop); the top reads on

    async def _on_pick_topic(self, pend, by, choice, socks, ws, data) -> None:
        if choice == "topic" and data.get("section"):
            await self.jump_to(socks, data["section"]["start"], "topic", ws)
        # top, silence, a question: the read loop continues from the top

    async def _wait_heard(self, st: "ContextState", timeout: float = 30.0, until=None) -> bool:
        """Block until the client acks the whole unit (or reading stops, or
        `until()` says the unit no longer matters -- a superseded prompt)."""
        t0 = time.monotonic()
        while self.playing and not st.heard and time.monotonic() - t0 < timeout and not (until and until()):
            await asyncio.sleep(0.05)
        return st.heard

    async def _offer_after_section(self, provider, g, doc, sockets, next_index: int) -> bool:
        """At a section boundary, one suggested question for the section just
        finished: "People usually ask here whether ... Want that?" Yes answers
        it from the stored clause with no retrieval; no or silence continues.
        Returns True when it prompted (section_end then stays quiet: never two
        prompts back to back)."""
        prev = g.section_of(next_index - 1)
        if prev is None or prev["id"] in self._offered or not prev.get("suggested_questions"):
            return False
        # Only after a section the listener actually heard to its end. After a
        # jump the clauses passed over are skipped, not heard, and there is no
        # offer for them.
        sess = doc.session
        last_readable = next((c for c in reversed(g.clauses[prev["start"]:prev["end"]]) if is_readable(c)), None)
        if last_readable is None or sess.ledger.get(last_readable["id"]) != "heard":
            return False
        self._offered.add(prev["id"])
        # The offer belongs after the section is HEARD, not merely sent.
        while self.playing and self._backlog_ms() > 0:
            await asyncio.sleep(0.05)
        if not self.playing:
            return False
        q = prev["suggested_questions"][0]
        text = f"People usually ask here whether {q['text'].rstrip('?').lstrip()}. Want that?"
        task = await self.open_prompt("offer", text, ["yes", "go_on"], sockets=sockets,
                                      payload={"question": q["text"], "clause_id": q["clause_id"], "section": prev["id"]},
                                      extra={"clause_id": q["clause_id"], "section": prev["id"]})
        st = await task
        await self._wait_heard(st, until=lambda: self.prompt_kind() != "offer")
        # _after_prompt (on heard) runs the timer; here we simply wait it out.
        await self._wait_prompt("offer")
        return True

    async def maybe_section_end(self, provider, g, doc, sockets, next_index: int, nxt: dict) -> None:
        """At a top-level section boundary: "That's {heading}. Next is {next}.
        Carry on, or something else?" (5 s; silence carries on). Rate-limited:
        never within SECTION_END_MIN_GAP_S of the last prompt, never right after
        a jump the listener asked for, never once the listener has let it time
        out twice (muted), and only once the section was heard to its end."""
        prev = g.section_of(next_index - 1)
        if prev is None or prev["id"] == nxt["id"]:
            return
        # Only the end of a section the listener heard to its end counts as a
        # boundary here -- the target of a jump is not one, and does not use
        # up the "just jumped" rule.
        last = next((c for c in reversed(g.clauses[prev["start"]:prev["end"]]) if is_readable(c)), None)
        if last is None or doc.session.ledger.get(last["id"]) != "heard":
            return
        jumped = self.conv.jump_since_boundary
        self.conv.jump_since_boundary = False
        if jumped or "section_end" in self.conv.muted:
            return
        if time.monotonic() - self.conv.last_prompt_at < SECTION_END_MIN_GAP_S:
            return
        while self.playing and self._backlog_ms() > 0:
            await asyncio.sleep(0.05)
        if not self.playing:
            return
        text = f"That's {prev['title']}. Next is {nxt['title']}. Carry on, or something else?"
        task = await self.open_prompt("section_end", text, ["carry_on", "topic", "question"], sockets=sockets,
                                      payload={"prev": prev["id"], "next": nxt["title"]},
                                      classify_options={"carry on": "carry_on", "a topic name": "topic"})
        st = await task
        await self._wait_heard(st, until=lambda: self.prompt_kind() != "section_end")
        await self._wait_prompt("section_end")      # a topic jumps (cancelling this loop); carry on reads on

    async def _on_section_end(self, pend, by, choice, socks, ws, data) -> None:
        if choice == "topic" and data.get("section"):
            await self.jump_to(socks, data["section"]["start"], "topic", ws)
        # carry on, silence, a question: nothing to do here

    async def open_end_choice(self, provider, g, doc, sockets) -> None:
        """The end of the document: "That's the end. Want any section again, or
        a recap of what we covered?" (8 s; silence stops). A section jumps
        back in; "recap" speaks what the ledger says was heard."""
        text = "That's the end. Want any section again, or a recap of what we covered?"
        task = await self.open_prompt("end_choice", text, ["section", "recap", "stop"], sockets=sockets,
                                      classify_options={"a section name": "topic", "a recap": "recap", "stop": "stop"})
        st = await task
        await self._wait_heard(st, until=lambda: self.prompt_kind() != "end_choice")
        await self._wait_prompt("end_choice")

    async def _on_end_choice(self, pend, by, choice, socks, ws, data) -> None:
        if choice in ("section", "topic") and data.get("section"):
            await self.jump_to(socks, data["section"]["start"], "topic", ws)
            return
        if choice == "recap":
            await self.speak_recap(socks)
        # stop, silence, a question: the read loop finishes the document

    # ------------------------------------------------------------- prompts
    #
    # One prompt open at a time. Each is spoken as a unit of its own kind
    # (choice, offer, start_choice, confirm_topic, table_choice), heard on the
    # client's acks, and only then gives the listener the floor for
    # PROMPT_TIMEOUT_S seconds. Opening a prompt while one is open resolves the
    # old one as `superseded`. Every open and every resolution is in the trace:
    # prompt_opened{kind, options}, prompt_resolved{kind, by, choice}.

    @property
    def _prompt(self) -> Optional[dict]:
        return self.conv.prompt

    @_prompt.setter
    def _prompt(self, value: Optional[dict]) -> None:
        self.conv.prompt = value

    def prompt_kind(self) -> Optional[str]:
        return self._prompt["kind"] if self._prompt else None

    @property
    def speak_lock(self) -> asyncio.Lock:
        """Side units -- an ingest cue, a prompt -- are spoken one after another
        in the order they were asked for, never interleaved: two cues and an
        invitation fired within a second would otherwise stream their chunks
        together. Clauses are serialised by the read loop and never take this."""
        if self._speak_lock is None:
            self._speak_lock = asyncio.Lock()
        return self._speak_lock

    async def open_prompt(self, kind: str, text: str, options: list, timeout_s: Optional[float] = None,
                          payload: Optional[dict] = None, sockets=None, extra: Optional[dict] = None,
                          classify_options: Optional[dict] = None, unit_id: Optional[str] = None,
                          index: int = -1, doc=None) -> asyncio.Task:
        """Make `kind` the open prompt and speak `text` as its unit, in a task
        (a websocket handler that opened it must return so that socket's acks
        for the prompt can be handled). `classify_options` maps the labels the
        closed-set classifier is shown to the option each stands for. Returns
        the speaking task."""
        socks = sockets if sockets is not None else self.events.sockets
        if self._prompt is not None:
            await self.resolve_prompt("superseded", None)
        self._cancel_timer()
        doc = doc if doc is not None else self.library.current
        pend = {"kind": kind, "options": list(options), "opened_at": time.monotonic(),
                "payload": payload or {}, "text": text, "sockets": socks,
                "timeout_s": PROMPT_TIMEOUT_S[kind] if timeout_s is None else timeout_s,
                "classify_options": classify_options or {}, "document": doc.name if doc else None}
        if kind in self.conv.muted:
            # The listener let this kind time out twice: not asked again this
            # session; its default happens at once, silently.
            self.events.emit("prompt_skipped", document=doc.name if doc else None, kind=kind, reason="muted")
            await getattr(self, f"_on_{kind}")(pend, "timeout", None, socks, None, {})
            fut = asyncio.get_running_loop().create_future()
            fut.set_result(ContextState(f"{kind}#muted", self.turn, kind, state="fenced", kind=kind, abandoned=True))
            return fut
        self._prompt = pend
        self.conv.note_opened(kind)
        self.events.emit("prompt_opened", document=doc.name if doc else None, kind=kind,
                         options=list(options), text=text)
        provider = await self.ensure_provider()
        return asyncio.ensure_future(self._speak_prompt(provider, pend, doc, socks, extra, unit_id, index))

    def prompt_document(self) -> Optional[str]:
        return self._prompt.get("document") if self._prompt else None

    async def _speak_prompt(self, provider, pend: dict, doc, socks, extra, unit_id, index) -> "ContextState":
        """The prompt's unit. A prompt closed while its audio is still streaming
        (a reply, a button, a newer prompt) stops streaming; the client then
        gets a terminal unit_done marked `cut` and the context is fenced, so
        every unit_started has its end and flow control forgets it. Prompts
        are the only units that end this way; a clause cut by an interrupt
        goes through the interruption path unchanged."""
        async with self.speak_lock:
            if self._prompt is not pend:
                return ContextState(f"{pend['kind']}#closed", self.turn, pend["kind"], state="fenced",
                                    kind=pend["kind"], abandoned=True)
            st = await self._speak_unit(provider, pend["kind"], pend["text"], doc, socks,
                                        keep_going=lambda: self._prompt is pend, extra=extra,
                                        wait_lead=False, unit_id=unit_id, index=index)
        if not st.synth_done and st.state != "error":
            st.abandoned = True
            st.state = "fenced"
            self.events.emit("unit_fenced", context_id=st.context_id, unit_id=st.unit_id,
                             bytes=st.bytes, rendered_ms=round(st.rendered_ms, 1), reason="prompt_closed")
            await broadcast({"type": "unit_done", "context_id": st.context_id, "unit_id": st.unit_id,
                             "bytes": st.bytes, "cut": True}, socks)
        return st

    async def resolve_prompt(self, by: str, choice: Optional[str], ws=None, data: Optional[dict] = None) -> None:
        """Close the open prompt. `by` is reply | timeout | chip | superseded |
        cancelled; `choice` one of its options, or "question" for a reply that
        was a question. The kind's handler runs the consequence for a reply or
        a timeout; a prompt closed by a chip, a jump, a stop or a newer prompt
        has no consequence of its own."""
        pend = self._prompt
        if pend is None:
            return
        self._prompt = None
        self._cancel_timer()
        kind, socks = pend["kind"], pend["sockets"]
        self.events.emit("prompt_resolved", kind=kind, by=by, choice=choice, document=pend.get("document"),
                         open_ms=round((time.monotonic() - pend["opened_at"]) * 1000))
        if by == "timeout" and self.conv.note_timeout(kind):
            self.events.emit("prompt_muted", kind=kind, timeouts=self.conv.timeout_counts[kind])
        await broadcast({"type": "prompt_closed", "kind": kind}, socks)
        if kind == "choice":
            await broadcast({"type": "choice_closed"}, socks)
        elif kind == "offer":
            await broadcast({"type": "offer_closed"}, socks)
        if by in ("superseded", "cancelled", "chip", "navigated"):
            return
        await getattr(self, f"_on_{kind}")(pend, by, choice, socks, ws, data or {})

    async def _wait_prompt(self, kind: str) -> None:
        while self.playing and self._prompt is not None and self._prompt["kind"] == kind:
            await asyncio.sleep(0.05)

    async def _after_prompt(self, st: "ContextState", sockets) -> None:
        """The prompt was heard: show its options and give the listener the
        floor for the kind's timeout, then take the default."""
        pend = self._prompt
        if pend is None or pend["kind"] != st.kind:
            return
        pl = pend["payload"]
        public = {k: v for k, v in pl.items() if k in ("labels", "heading", "question", "clause_id", "topics", "titles", "next")}
        await broadcast({"type": "prompt", "kind": pend["kind"], "options": pend["options"],
                         "text": pend["text"], **public}, sockets)
        if pend["kind"] == "choice":
            await broadcast({"type": "choice", "options": ["now", "overview_first"],
                             "section_id": (pl.get("section") or {}).get("id")}, sockets)
        elif pend["kind"] == "offer":
            await broadcast({"type": "offer", "question": pl.get("question"), "clause_id": pl.get("clause_id")}, sockets)

        if pend["timeout_s"] is None:
            return                                   # closes when its moment passes, not by silence

        async def timeout(_socks):
            if self._prompt is pend:
                await self.resolve_prompt("timeout", None)
        await self._arm_timer(pend["timeout_s"], timeout, sockets)

    async def _arm_timer(self, seconds: float, fn, sockets) -> None:
        if self._timer and not self._timer.done():
            self._timer.cancel()

        async def go():
            await asyncio.sleep(seconds)
            await fn(sockets)
        self._timer = asyncio.ensure_future(go())

    def _cancel_timer(self) -> None:
        if self._timer and not self._timer.done():
            self._timer.cancel()
        self._timer = None

    def _prompt_grammar(self, pend: dict, q: str, g) -> Optional[tuple]:
        """Deterministic reading of a reply against the open prompt's options:
        (choice, data) or None when the reply is none of them."""
        kind = pend["kind"]
        shape = gr.reply_shape(q)
        if kind == "choice":
            if shape in ("yes", "now"):
                return "now", None
            if shape == "overview_first":
                return "overview_first", None
        elif kind == "offer":
            if shape == "yes":
                return "yes", None
            if shape in ("no", "carry_on"):
                return "go_on", None
        elif kind == "confirm_topic":
            if shape in ("yes", "now", "start"):
                return "yes", None
            if shape in ("no", "carry_on", "brief", "overview_first"):
                return "no", None
        elif kind == "start_choice":
            if shape in ("brief", "no", "overview_first", "carry_on"):
                return "brief", None
            if shape in ("start", "now"):
                return "start", None
            if not gr.is_question_like(q):
                sec = g.find_section(q)              # exact chip text or heading; a phrase around it is the model's
                if sec:
                    return "topic", {"section": sec, "text": q}
        elif kind == "table_choice":
            if shape == "all":
                return "all", None
            if shape in ("no", "carry_on"):
                return "carry_on", None
            i = _row_index(q, pend["payload"].get("labels") or [])
            if i is not None:
                return "row", {"row": i, "text": q}
        elif kind == "ingest_wait":
            return "question", {"text": q}           # everything said now is a question to park
        elif kind == "pick_topic":
            if shape in ("start", "carry_on", "no") or re.fullmatch(r"(?i)\s*(?:the |from the )?top\s*[.!]*", q):
                return "top", None
            if not gr.is_question_like(q):
                sec = g.find_section(q)
                if sec:
                    return "topic", {"section": sec}
        elif kind == "section_end":
            if shape in ("carry_on", "yes", "no"):
                return "carry_on", None
            if not gr.is_question_like(q):
                sec = g.find_section(q)
                if sec:
                    return "topic", {"section": sec}
        elif kind == "not_found":
            if shape in ("carry_on", "yes", "no"):
                return "carry_on", None
        elif kind == "end_choice":
            if re.fullmatch(r"(?i)\s*(?:a |the )?recap(?: please)?\s*[.!]*", q):
                return "recap", None
            if shape in ("no", "carry_on", "yes"):
                return "stop", None
            if not gr.is_question_like(q):
                sec = g.find_section(q)
                if sec:
                    return "section", {"section": sec}
        elif kind == "welcome":
            if re.fullmatch(r"(?i)\s*(?:upload|add|a new one|new document|upload a new one)\s*[.!]*", q):
                return "upload", None
            name = self.find_document_by_title(q)
            if name:
                return "title", {"name": name}
            if shape in ("start", "yes", "carry_on") or re.fullmatch(r"(?i)\s*(?:the )?first(?: one| document)?\s*[.!]*", q):
                return "first", None
        return None

    def find_document_by_title(self, text: str) -> Optional[str]:
        """A library entry named by its title or name: exact, then substring,
        then token overlap (>= 0.6). None when nothing matches."""
        q = gr.normalise_term(text)
        if not q:
            return None
        rows = self.library.list()
        for d in rows:
            if q in (gr.normalise_term(d.get("title") or ""), gr.normalise_term(d["name"])):
                return d["name"]
        for d in rows:
            t = gr.normalise_term(d.get("title") or "")
            if t and (q in t or t in q):
                return d["name"]
        qs = {gr.stem(x) for x in gr.tokenize(q)}
        best, score = None, 0.0
        for d in rows:
            ts = {gr.stem(x) for x in gr.tokenize(gr.normalise_term(d.get("title") or ""))}
            if qs and ts:
                sc = len(qs & ts) / len(qs)
                if sc > score:
                    best, score = d["name"], sc
        return best if score >= 0.6 else None

    @staticmethod
    def row_index(text: str, labels: list) -> Optional[int]:
        return _row_index(text, labels)

    async def route_reply(self, text: str, socks=None, ws=None) -> dict:
        """Every listener utterance goes through the Conversation's four steps
        (conversation.py): the rules for an exact option word or chip text,
        the model's understanding, its execution, and the v1 rules as the
        floor. `route_reply_rules` below is that floor, v1 exactly as built."""
        return await self.conv.route(text, socks, ws)

    async def route_reply_rules(self, text: str, classify: bool = True) -> dict:
        """v1, the floor: 1. the open prompt's grammar; 2. the navigation
        regexes; 3. a question; 4. when a prompt is open and 1-2 did not match
        (and `classify`), the model classifies the reply into the prompt's
        options plus "question" (closed set, 5 s; any failure is "question").
        Returns {route: pending|nav|question|llm_classify, ...}."""
        doc = self.library.current
        g, sess = doc.grounding, doc.session
        q = (text or "").strip()
        pend = self._prompt
        if pend is not None:
            hit = self._prompt_grammar(pend, q, g)
            if hit is not None:
                return {"route": "pending", "kind": pend["kind"], "choice": hit[0], "data": hit[1] or {"text": q}}
        nav = g.navigation_intent(q, at_index=max(sess.read_index, 0))
        if nav:
            return {"route": "nav", "intent": nav["intent"], "nav": nav}
        if classify and pend is not None and self.llm is not None:
            import llm as llm_mod
            labels = pend["classify_options"] or {o: o for o in pend["options"]}
            t0 = time.monotonic()
            picked = await asyncio.get_running_loop().run_in_executor(None, llm_mod.classify, q, list(labels))
            choice = labels.get(picked, "question")
            self.events.emit("llm_classify", kind=pend["kind"], options=list(labels), picked=picked,
                             choice=choice, ms=round((time.monotonic() - t0) * 1000))
            if choice != "question" and self._prompt is pend:
                data = {"text": q}
                if isinstance(choice, str) and choice.startswith("row:"):
                    data["row"] = int(choice[4:])
                    choice = "row"
                elif isinstance(choice, str) and choice.startswith("topic:"):
                    data["section"] = next((x for x in g.sections if x["id"] == choice[6:]), None)
                    choice = "topic"
                elif isinstance(choice, str) and choice.startswith("title:"):
                    data["name"] = choice[6:]
                    choice = "title"
                return {"route": "llm_classify", "kind": pend["kind"], "choice": choice, "data": data}
        return {"route": "question"}

    async def _start_reading(self, socks) -> None:
        if self.playing or (self._reader and not self._reader.done()):
            return
        self._start_reopen = None                    # reading has begun: the invitation is over
        self.playing = True
        await broadcast({"type": "playing"}, socks)
        self._reader = asyncio.ensure_future(self.read_loop(socks))

    def _est_minutes(self, g, clauses=None) -> int:
        chars = sum(len(c["text_display"]) for c in (g.clauses if clauses is None else clauses) if is_readable(c))
        return max(1, round(chars / CHARS_PER_SECOND / 60))

    # -- the kinds' consequences: (pend, by, choice, sockets, ws, data)

    async def _on_choice(self, pend, by, choice, socks, ws, data) -> None:
        sec = pend["payload"]["section"]
        if by == "timeout":
            # No answer after the overview: "I'll read from the start. Interrupt me any time."
            self._pending_cue = "I'll read from the start. Interrupt me any time."
            await self._start_reading(socks)
        elif choice == "now":
            await self.jump_to(socks, sec["start"], "topic", ws)
        elif choice == "overview_first":
            # the overview again as a unit, then the jump. In a task, for the
            # same reason as the prompt.
            asyncio.ensure_future(self._overview_then_jump(socks, sec, ws))

    async def _on_offer(self, pend, by, choice, socks, ws, data) -> None:
        pl = pend["payload"]
        if choice == "yes":
            await self.answer_suggested(socks, pl)
            return
        self.events.emit("offer_declined", reason="silence" if by == "timeout" else (choice or "declined"),
                         clause_id=pl.get("clause_id"), section=pl.get("section"))
        # reading continues: _offer_after_section sees the prompt closed

    async def open_start_choice(self, doc, socks) -> None:
        """The document is ready and the voice is claimed: invite a topic."""
        g = doc.grounding
        n = len(g.sections)
        text = (f"I've gone through {doc.spoken_title}: {n} section{'s' if n != 1 else ''}. "
                "Is there a topic you have in mind? If not, I'll give you a brief and you can pick from there.")
        phrase = getattr(self.conv, "parked_phrase_pending", None) or (self.conv.parked_for(doc.name) or {}).get("phrase")
        if phrase:
            text += cp.START_WITH.format(phrase=phrase)   # "...or shall I start with {phrase}?"
            self.conv.parked_phrase_pending = None
        self._start_asked[doc.name] = self._start_asked.get(doc.name, 0) + 1
        self.conv.deferred_start.discard(doc.name)
        await self.open_prompt("start_choice", text, ["topic", "brief", "start"], sockets=socks, doc=doc,
                               payload={"document": doc.name},
                               classify_options={"a topic name": "topic", "the brief": "brief",
                                                 "from the start": "start"})

    async def _on_start_choice(self, pend, by, choice, socks, ws, data) -> None:
        doc = self.library.current
        g = doc.grounding
        if choice == "question":
            # Answered by the ask path; the invitation once more after the answer.
            if self._start_asked.get(doc.name, 0) < 2:
                self._start_reopen = doc.name
            return
        if choice == "topic":
            sec = data.get("section")
            if sec is None and self.llm is not None:
                import llm as llm_mod
                t0 = time.monotonic()
                sid = await asyncio.get_running_loop().run_in_executor(
                    None, llm_mod.map_topic, data.get("text", ""),
                    [{"id": s["id"], "title": s["title"]} for s in g.sections])
                self.events.emit("llm_map_topic", text=data.get("text", "")[:120], section_id=sid,
                                 ms=round((time.monotonic() - t0) * 1000))
                sec = g.section_for_id(sid)
            if sec is None:
                self._pending_cue = "I couldn't find that one, so here's the brief."
                await self._start_reading(socks)
                return
            m = sec.get("est_minutes") or self._est_minutes(g, g.clauses[sec["start"]:sec["end"]])
            text = f"{sec['title']}, about {m} minute{'s' if m != 1 else ''}. Read it now?"
            await self.open_prompt("confirm_topic", text, ["yes", "no"], sockets=socks,
                                   payload={"section": sec, "heading": sec["title"]}, extra={"section": sec["id"]})
            return
        if choice == "start":
            self._map_skipped.add(doc.name)
        # brief, no, silence: the overview and the chips (the map unit), then reading
        await self._start_reading(socks)

    async def open_welcome(self, socks) -> None:
        """The first play in a session, nothing started, more than one document
        in the library: which one, or upload a new one? (8 s; silence reads the
        first document.)"""
        rows = self.library.list()
        titles = [d.get("spoken_title") or d.get("title") or d["name"] for d in rows]
        listed = ", ".join(titles[:5]) + (f", and {len(titles) - 5} more" if len(titles) > 5 else "")
        text = ("I can read a policy or agreement to you and answer questions as we go. "
                f"You have {len(rows)} here: {listed}. Which one, or upload a new one?")
        self.conv.welcomed = True
        await self.open_prompt("welcome", text, ["title", "upload", "first"], sockets=socks,
                               payload={"titles": titles, "names": [d["name"] for d in rows]},
                               classify_options={**{t: f"title:{d['name']}" for t, d in zip(titles, rows)},
                                                 "upload a new one": "upload", "the first one": "first"})

    async def _on_welcome(self, pend, by, choice, socks, ws, data) -> None:
        if choice == "question":
            return
        if choice == "upload":
            self.events.emit("focus_upload")
            await broadcast({"type": "focus_upload"}, socks)
            return
        rows = self.library.list()
        if choice == "title" and data.get("name"):
            # A document the listener named: open it; the invitation follows
            # when its navigator is ready, else reading starts.
            self._opened_by_listener.add(data["name"])
            await _switch_document(self, socks, data["name"], ws, invite="now")
            if self.prompt_kind() is None and self.voice_free() is None:
                await self._start_reading(socks)
            return
        # "the first one", or silence: read the first document.
        if rows:
            doc = self.library.open(rows[0]["name"])
            self._opened_by_listener.add(doc.name)
            await broadcast(self.opened_message(doc), socks)
        await self._start_reading(socks)

    def voice_free(self) -> Optional[str]:
        """None when a tab has the voice and nothing is sounding; else why not."""
        if self.sink is None or self.library.current is None:
            return "no_sink"
        if self.playing or (self._reader and not self._reader.done()) or self._answer is not None:
            return "reading"
        return None

    async def open_ingest_wait(self, socks, text: Optional[str] = None, doc=None) -> None:
        """Upload or enrichment starts, voice claimed: a prompt with one option
        -- a question to park for THAT document and answer first when it is
        ready. No timer; it closes at ready. Opening another document does not
        close it. Without the voice: cue_skipped, as for a cue."""
        busy = self.voice_free()
        if busy:
            self.events.emit("cue_skipped", reason=busy, cue="ingest_wait")
            return
        await self.open_prompt("ingest_wait", text or ("I'm going through the document now, about a minute. While I do: "
                               "is there something you want to know from it? I'll look for it first."),
                               ["question"], sockets=socks, doc=doc)

    async def _on_ingest_wait(self, pend, by, choice, socks, ws, data) -> None:
        q = (data or {}).get("text", "").strip()
        name = pend.get("document") or (self.library.current.name if self.library.current else None)
        if not q or not name:
            if by == "timeout" and name in self.conv.deferred_start and self.library.current \
                    and self.library.current.name == name:
                await self._start_choice_if_due(self.library.current, socks)
            return
        self.events.emit("question_parked", question=q[:200], document=name)
        narrator = self.conv.narrators.get(name)
        if narrator is not None and not narrator.finished:
            # The companion acknowledges (template, or its model behind the guard)
            # and names the topic phrase start_choice will repeat.
            phrase = await narrator.on_reply(q, (data or {}).get("slot"))
        else:
            phrase = cp.topic_phrase(q)
            await self.speak_cue("Got it, I'll look for that.", "parked")
        self.conv.park(name, q, phrase)

    async def answer_parked(self, doc, socks) -> None:
        """The document is ready: the question parked during ingestion is
        answered first, over the whole document (no spoiler gate: nothing has
        been read yet), and the invitation follows once the answer is heard."""
        parked = self.conv.parked.pop(doc.name, None)
        q = parked["question"] if parked else None
        if not q:
            return
        self.conv.parked_phrase_pending = parked["phrase"]     # start_choice names it once
        g, sess = doc.grounding, doc.session
        await self.speak_companion(cp.READY_LINE, "ready", "template", socks, doc=doc)
        r = g.resolve(q, None, read_cursor=len(g.clauses))
        target = r.hits[0].unit_id if r.hits else None
        self.events.emit("question_resolved", document=doc.name, kind=r.kind, unit_id=target, question=q,
                         retrieval_path=r.retrieval_path, parked=True)
        source = "llm" if (self.llm is not None and r.kind in ("in_scope", "deictic")) else "extractive"
        try:
            answer = await g.answer(r, llm=self.llm)
        except Exception as e:
            self.events.emit("llm_failed", error=str(e)[:200])
            source = "extractive"
            answer = await g.answer(r, llm=None)
        self.events.emit("answer_source", source=source, kind=r.kind, unit_id=target, parked=True)
        self.events.emit("answer_grounded", document=doc.name, question=q, kind=r.kind, unit_id=target,
                         retrieval_path=r.retrieval_path, source=source, parked=True)
        await broadcast({"type": "answer", "question": q, "kind": r.kind, "unit_id": target, "answer": answer,
                         "source": source, "retrieval_path": r.retrieval_path,
                         "referral": self.referral_for(doc.name), "offer": False}, socks)
        self._start_reopen = doc.name                # after the answer is heard: the invitation
        self.playing = False
        if self._answer_task and not self._answer_task.done():
            self._answer_task.cancel()
        self._answer_task = asyncio.ensure_future(self.speak_answer(answer, r.kind, target, socks))

    async def _on_not_found(self, pend, by, choice, socks, ws, data) -> None:
        if choice == "question":
            return                                   # the new question is on its way
        if pend["payload"].get("was_reading"):
            await self._start_reading(socks)         # carry on, or silence: back to the cut sentence

    async def open_not_found(self, socks, was_reading: bool) -> None:
        await self.open_prompt("not_found", "I couldn't find that in this document; the insurer or lender can "
                               "tell you. Carry on, or try another word?", ["carry_on", "question"],
                               sockets=socks, payload={"was_reading": was_reading})

    def recap_text(self, doc) -> str:
        """What was covered, from the ledger alone: sections heard in full,
        partly heard (a truncation or some clauses missing), and skipped or not
        reached. No model; this is the observability claim spoken aloud."""
        g, led = doc.grounding, doc.session.ledger
        full, part, skipped = [], [], []
        for sec in g.sections:
            readable = [c for c in g.clauses[sec["start"]:sec["end"]] if is_readable(c)]
            if not readable:
                continue
            states = [led.get(c["id"]) for c in readable]
            heard = sum(1 for x in states if x == "heard")
            touched = sum(1 for x in states if x == "heard" or (isinstance(x, str) and x.startswith("truncated@")))
            (full if heard == len(readable) else part if touched else skipped).append(sec["title"])

        def names(xs):
            return ", ".join(xs[:6]) + (f", and {len(xs) - 6} more" if len(xs) > 6 else "")
        parts = []
        if full:
            parts.append(f"You heard {len(full)} section{'s' if len(full) != 1 else ''} in full: {names(full)}.")
        if part:
            parts.append(f"Partly heard: {names(part)}.")
        if skipped:
            parts.append(f"Not heard: {names(skipped)}.")
        return " ".join(parts) or "Nothing has been read yet."

    async def speak_recap(self, socks) -> None:
        doc = self.library.current
        text = self.recap_text(doc)
        self.events.emit("recap", document=doc.name, text=text)
        provider = await self.ensure_provider()
        async with self.speak_lock:
            await self._speak_unit(provider, "recap", text, doc, socks, keep_going=lambda: True, wait_lead=False)

    async def _on_confirm_topic(self, pend, by, choice, socks, ws, data) -> None:
        if choice == "question":
            return
        sec = pend["payload"]["section"]
        if choice == "yes":
            await self.jump_to(socks, sec["start"], "topic", ws)
            return
        await self._start_reading(socks)      # no, or silence: the brief

    async def _on_table_choice(self, pend, by, choice, socks, ws, data) -> None:
        # The read loop owns the table: it is waiting on this prompt and acts
        # on the decision recorded here (row i / all / carry on).
        if by == "timeout" or choice in ("question", None):
            self._table_decision = {"choice": "carry_on"}
        else:
            self._table_decision = {"choice": choice, "row": data.get("row")}

    async def _table_choice(self, provider, g, doc, sockets, stub: dict, rows: list) -> None:
        """A table reached while reading: "Here there's a table of {description},
        {n} rows: {first six labels}. Want one of them, all of them, or shall I
        carry on?" A row label or ordinal speaks that row (a `row` unit, acked
        like a clause) and asks "Another, or carry on?"; "all" reads the rows in
        order; "carry on", "no" or silence continues after the table."""
        labels = [_row_label(r) for r in rows]
        n = len(rows)
        ov = stub.get("spoken_override")
        if isinstance(ov, dict) and ov.get("text"):
            # The generated description is a sentence already ("The table is ...");
            # spoken as-is, then the question.
            desc = _table_description(stub, labels)
            text = f"{desc}. Want one of them, all of them, or shall I carry on?"
        else:
            text = (f"Here there's a table of {_table_description(stub, labels)}, {n} row{'s' if n != 1 else ''}: "
                    f"{', '.join(labels[:6])}. Want one of them, all of them, or shall I carry on?")
        classify = {lab: f"row:{i}" for i, lab in enumerate(labels)}
        classify.update({"all of them": "all", "carry on": "carry_on"})
        payload = {"stub": stub["id"], "rows": [r["id"] for r in rows], "labels": labels}
        spoken_rows: set = set()
        while self.playing:
            self._table_decision = None
            task = await self.open_prompt("table_choice", text, ["row", "all", "carry_on"], sockets=sockets,
                                          payload=payload, classify_options=classify,
                                          extra={"stub": stub["id"], "rows": n}, unit_id=stub["id"],
                                          index=stub["index"])
            st = await task
            await self._wait_heard(st, until=lambda: self.prompt_kind() != "table_choice")
            await self._wait_prompt("table_choice")
            if not self.playing:
                return
            decision = self._table_decision or {"choice": "carry_on"}
            if decision["choice"] == "row" and decision.get("row") is not None:
                r = rows[decision["row"]]
                await self._speak_row(provider, doc, sockets, r)
                spoken_rows.add(r["id"])
                if not self.playing:
                    return
                text = "Another, or carry on?"
                continue
            if decision["choice"] == "all":
                for r in rows:
                    if r["id"] in spoken_rows:
                        continue
                    await self._speak_row(provider, doc, sockets, r)
                    if not self.playing:
                        return
            self.events.emit("table_done", document=doc.name, stub=stub["id"], rows=n,
                             spoken=len(spoken_rows) if decision["choice"] != "all" else n)
            return

    async def _speak_row(self, provider, doc, sockets, r: dict) -> None:
        """One table row on request: its own unit, kind `row`, the row's id, so
        the client's ack marks it heard in the ledger like a clause."""
        await self._speak_unit(provider, "row", spoken_text(r), doc, sockets, unit_id=r["id"],
                               display=r["text_display"], index=r["index"], section_title=r["section_title"],
                               extra={"parent": r.get("parent")})

    # ------------------------------------------------------------- jumping
    async def jump_to(self, socks, target_index: int, reason: str, ws=None, char_start: int = 0,
                      label: Optional[str] = None, cue: Optional[str] = None) -> None:
        """Every jump is the interruption path with a different resume target.

        1. turn_id++            2. cancel fan-out: clear, scheduler stop, flush
        3. ledger: unit_truncated at the client boundary; unit_skipped (jump)
           for every readable clause between the cut and the target
        4. position_saved (label before_jump) so "go back" returns; read cursor
           := sentence start of the target; position_restored (reason jump)
        5. cue unit "Okay -- going to {heading}." + brief, its own unit
        6. resume at the target.
        """
        doc = self.library.current
        g, sess = doc.grounding, doc.session
        target_index = max(0, min(target_index, len(g.clauses) - 1))
        if self._prompt is not None:
            await self.resolve_prompt("navigated" if self.prompt_kind() in ("table_choice", "offer") else "chip", None)
        self._cancel_timer()
        self.turn += 1
        turn_id = self.turn
        from_unit = sess.current_unit_id
        sounding = self.playing or (self._reader and not self._reader.done()) or self._answer is not None \
            or any(not st.heard and not st.abandoned and st.bytes for st in self.contexts.values())
        if sounding:
            await self.stop_reading()
            if self.sink is not None and ws is not self.sink and self._pending_flush is None:
                await self.request_flush()
            await _stop_and_attribute(self, socks, f"jump:{reason}")
        else:
            await self.stop_reading()
        # 3. everything between the cut and the target is skipped, not silently lost
        cut_index = sess.read_cursor
        if target_index > cut_index:
            for c in g.clauses[cut_index:target_index]:
                # heard stays heard; the truncated unit keeps its truncation;
                # only what was never reached is skipped.
                if is_readable(c) and c["id"] not in sess.ledger:
                    sess.ledger[c["id"]] = "skipped:jump"
                    self.events.emit("unit_skipped", document=doc.name, doc_id=doc.doc_id,
                                     unit_id=c["id"], reason="jump")
        # 4. save where we were, move the cursor to the target's sentence start
        back = {"unit_id": from_unit or (g.clauses[cut_index]["id"] if cut_index < len(g.clauses) else None),
                "char": sess.boundary_char if sess.last_heard_unit_id == from_unit else 0,
                "index": cut_index}
        self.events.emit("position_saved", document=doc.name, doc_id=doc.doc_id, label="before_jump",
                         unit_id=back["unit_id"], cursor=cut_index, char=back["char"])
        if reason != "back":
            self._jump_back = back
        target = g.clauses[target_index]
        sess.read_cursor = g.next_readable(target_index)
        self._resume_from = (target["id"], char_start) if char_start else None
        self.events.emit("position_restored", document=doc.name, doc_id=doc.doc_id, unit_id=target["id"],
                         sentence_index=0, char_start=char_start, reason="jump")
        self.events.emit("jump", document=doc.name, doc_id=doc.doc_id, from_unit=from_unit,
                         to_unit=target["id"], reason=reason, turn_id=turn_id)
        # 5. cue, then 6. resume
        sec = g.section_at_start(target_index) or g.section_of(target_index)
        heading = label or (sec["title"] if sec else target["section_title"])
        if cue is None:
            cue = f"Okay — going to {heading}."
            if sec and sec.get("brief") and reason != "back":
                cue += f" {sec['brief']}"
            if reason == "back":
                cue = "Okay — back to where you were."
        self._pending_cue = cue
        self._start_reopen = None                    # the listener moved on: no invitation after this
        if reason in ("topic", "skip", "chip", "back", "repeat", "spoiler_offer"):
            self.conv.note_listener_jump()
        await broadcast({"type": "jumped", "unit_id": target["id"], "reason": reason,
                         "heading": heading}, socks)
        self.playing = True
        await broadcast({"type": "playing"}, socks)
        self._reader = asyncio.ensure_future(self.read_loop(socks))

    async def offer_section(self, socks, sec: dict, ws=None) -> None:
        """A topic chip or spoken topic: name it and ask now or overview first."""
        doc = self.library.current
        self._cancel_timer()
        sounding = self.playing or (self._reader and not self._reader.done()) or self._answer is not None
        if sounding:
            await self.stop_reading()
            if self.sink is not None and ws is not self.sink and self._pending_flush is None:
                await self.request_flush()
            await _stop_and_attribute(self, socks, "topic")
        est = sec.get("est_minutes")
        topic = next((t["topic"] for t in doc.grounding.topics if t.get("section_id") == sec["id"]), sec["title"])
        text = (f"{topic} is {sec['title']}" + (f", about {est} minute{'s' if est != 1 else ''}" if est else "")
                + ". Read it now, or hear the rest of the overview first?")
        await self.open_prompt("choice", text, ["now", "overview_first"], sockets=socks,
                               payload={"section": sec, "was_overview": sounding}, extra={"section": sec["id"]})

    async def resolve_choice(self, socks, choice: str, ws=None) -> None:
        if self.prompt_kind() == "choice":
            await self.resolve_prompt("reply", choice, ws)

    async def _overview_then_jump(self, socks, sec: dict, ws=None) -> None:
        doc = self.library.current
        provider = await self.ensure_provider()
        self.playing = True
        await broadcast({"type": "playing"}, socks)
        g = doc.grounding
        text = g.overview_text or g.map_sentence("policy" if "policy" in doc.spoken_title.lower() else "document")
        st = await self._speak_unit(provider, "map", text, doc, socks, wait_lead=False)
        heard = await self._wait_heard(st)
        if heard and self.playing:
            self.playing = False
            await self.jump_to(socks, sec["start"], "topic", ws)

    async def answer_suggested(self, socks, pend: dict) -> None:
        """Yes to the offer: answer from the stored clause, no retrieval."""
        doc = self.library.current
        g, sess = doc.grounding, doc.session
        c = g.by_id[pend["clause_id"]]
        from grounding import GroundingResult
        r = GroundingResult("in_scope", pend["question"], [g._hit(c["index"], 1.0)], [], c["id"],
                            sess.read_cursor, "suggested")
        self.events.emit("question_resolved", document=doc.name, kind=r.kind, unit_id=c["id"],
                         question=pend["question"], retrieval_path="suggested")
        answer = await g.answer(r, llm=None)
        self.events.emit("answer_grounded", document=doc.name, question=pend["question"], kind=r.kind,
                         unit_id=c["id"], retrieval_path="suggested", source="extractive")
        await broadcast({"type": "answer", "question": pend["question"], "kind": r.kind, "unit_id": c["id"],
                         "answer": answer, "source": "extractive", "retrieval_path": "suggested",
                         "referral": self.referral_for(doc.name), "offer": False}, socks)
        self.playing = False
        self._answer_task = asyncio.ensure_future(self.speak_answer(answer, r.kind, c["id"], socks))

    # ------------------------------------------------------------- answers
    async def speak_answer(self, text: str, kind: str, unit_id: Optional[str], sockets) -> None:
        """Say the answer through the same provider, as a unit of its own.

        Its context lives under the generation fence like any clause, so an
        interrupt cuts it and its stragglers are fenced; and it is heard only
        on the client's acks, so reading resumes when the listener has actually
        heard the answer, not when the server finished sending it.
        """
        try:
            provider = await self.ensure_provider()
        except Exception as e:
            await broadcast({"type": "provider_error", "message": str(e)}, sockets)
            await broadcast({"type": "paused"}, sockets)
            return
        self.turn += 1
        ctx_id = f"answer#t{self.turn}"
        st = ContextState(ctx_id, self.turn, "answer", state="streaming", kind="answer")
        self.contexts[ctx_id] = st
        self._answer = {"context_id": ctx_id, "kind": kind, "unit_id": unit_id}
        self.events.emit("answer_spoken", context_id=ctx_id, unit_id=unit_id, kind=kind)
        await broadcast({
            "type": "unit_started", "context_id": ctx_id, "turn_id": self.turn,
            "unit_id": "answer", "index": -1, "kind": "answer",
            "section_title": "Answer", "path": None,
            "text_display": text, "sentences": [[0, len(text)]], "char_start": 0,
        }, sockets)
        segs = [Segment(0, len(text), text)]
        await self._stream_unit(provider, st, "answer", text, text, segs, sockets,
                                lambda: self._answer is not None
                                and self._answer["context_id"] == ctx_id)
        if st.bytes == 0 or st.state == "error":
            # Nothing will ever be acked for it. Do not leave the listener on
            # "Answering..." forever.
            await self._after_answer(st, sockets, failed=True)

    async def _after_answer(self, st: "ContextState", sockets, failed: bool = False) -> None:
        info = self._answer if self._answer and self._answer["context_id"] == st.context_id else None
        self._answer = None
        kind = info["kind"] if info else "unknown"
        doc = self.library.current
        if (not failed and doc is not None and self._start_reopen == doc.name and kind != "beyond_cursor"
                and not self.playing):
            # A question during start_choice (once more) or the question parked
            # during ingestion (first time), answered: the invitation.
            self._start_reopen = None
            await self.open_start_choice(doc, sockets)
            return
        if failed or kind in ("beyond_cursor", "not_found") or self.playing:
            # These wait for the listener: Jump there, Keep going, or play.
            self._start_reopen = None
            await broadcast({"type": "paused"}, sockets)
            return
        seq = self._stop_seq
        await asyncio.sleep(0.6)
        if self.playing or self._stop_seq != seq or self._answer is not None:
            return                                   # something else happened meanwhile
        self.playing = True
        await broadcast({"type": "playing"}, sockets)
        self._reader = asyncio.ensure_future(self.read_loop(sockets))

    async def stop_reading(self) -> None:
        self.playing = False
        if self._reader and not self._reader.done():
            self._reader.cancel()
            try:
                await self._reader
            except (asyncio.CancelledError, Exception):
                pass
        self._reader = None
        self._stop_seq += 1
        self._answer = None            # a spoken answer in flight is cut too
        self._cancel_timer()
        if self._prompt is not None and self._prompt["kind"] in LOOP_PROMPTS:
            await self.resolve_prompt("cancelled", None)
        # Whatever was not heard when reading stopped will not be: the client
        # flushes its queue and the next read_loop re-synthesises from the
        # cursor. Take those units out of flow control (see _backlog_ms), and
        # say so in the trace: audio that was sent and never heard is fenced.
        for st in self.contexts.values():
            if not st.heard and not st.abandoned:
                st.abandoned = True
                if st.bytes and st.state != "fenced":
                    st.state = "fenced"
                    self.events.emit("unit_fenced", context_id=st.context_id, unit_id=st.unit_id,
                                     bytes=st.bytes, rendered_ms=round(st.rendered_ms, 1))


async def _switch_document(s: "ReaderSession", socks, name: str, ws=None, invite: str = "debounce"):
    """Open another document. If anything is sounding, the tab with the voice is
    flushed first and the cut attributed on the document being left, so its
    buffered audio does not play out under the new document's first clause and
    its position is saved at the playhead, not at what was synthesised.

    The sink's own client sends its flush_ack before `open` (buildOpen), like
    pause and interrupt. A flush is requested only from a *different* socket:
    a socket's messages are handled one at a time, so waiting here for an ack
    from the same socket can never succeed.
    """
    if s.playing or (s._reader and not s._reader.done()) or s._answer is not None:
        await s.stop_reading()
        if s.sink is not None and ws is not s.sink and s._pending_flush is None:
            await s.request_flush()
        await _stop_and_attribute(s, socks, "open")
    else:
        await s.stop_reading()
    if s._prompt is not None and s.prompt_kind() != "ingest_wait":
        await s.resolve_prompt("navigated", None)   # a table or an offer left behind; ingest_wait stays
    s._resume_from = None
    doc = s.library.open(name)
    await broadcast(s.opened_message(doc), socks)
    s._start_reopen = None
    if not s.has_navigator(doc) and s.enrichment_configured():
        # Entering a document without its overview starts generating one now,
        # so by the time the listener presses play it is usually there.
        s.ensure_navigator(doc, socks)
    elif invite != "none" and s.sink is not None and s.has_navigator(doc) \
            and doc.name not in s._session_started and doc.name not in s._start_asked:
        # The voice is claimed and the document is ready: invite a topic
        # instead of waiting for play -- at once for a document the listener
        # named, after the rail has gone quiet for one clicked (deferred
        # while another document's ingest_wait has the floor).
        if invite == "now":
            await s._start_choice_if_due(doc, socks)
        else:
            s.schedule_start_choice(doc, socks)
    return doc


def _spoken_from(c: dict, char_start: int) -> tuple:
    """The spoken text for text_display[char_start:], rebuilt from spoken_map.

    text_spoken is the display text with each mapped span replaced by its
    spoken form, so the remainder is the same construction from `char_start`.
    Returns (text_spoken, pieces) where pieces are the spoken_map rows kept.
    """
    disp = c["text_display"]
    pieces = [(a, b, sp) for a, b, sp in c["spoken_map"] if a >= char_start]
    if char_start <= 0:
        return c["text_spoken"], list(c["spoken_map"])
    out, prev = [], char_start
    for a, b, sp in pieces:
        out.append(disp[prev:a])
        out.append(sp)
        prev = b
    out.append(disp[prev:])
    return "".join(out).strip(), pieces


# ==========================================================================
# HTTP handlers
# ==========================================================================

def _json(data, status=200):
    return web.json_response(data, status=status,
                             dumps=lambda o: json.dumps(o, ensure_ascii=False))


async def api_status(request):
    return _json(request.app["session"].status())


async def api_livekit_token(request):
    """Mint a LiveKit token for the browser tab to publish its mic into this
    session's voice room. Purely additive: it hands out a token and nothing
    else, and only exists so voice/bridge.py has audio to subscribe to. No
    interrupt/ask/delivery logic lives here -- see voice/bridge.py and
    handle_client_message's existing "interrupt"/"ask" handling for that."""
    s = request.app["session"]
    try:
        from voice.livekit_token import mint_token, room_name_for_session
        token = mint_token(s.id, "listener", can_publish=True, can_subscribe=False)
    except Exception as e:
        return _json({"error": str(e)}, status=503)
    return _json({
        "url": os.environ.get("LIVEKIT_URL", ""),
        "token": token,
        "room": room_name_for_session(s.id),
    })


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


async def api_documents_delete(request):
    """DELETE /documents/{doc_id}[?force=1]: the entry out of index.json, its
    fixture, Docling JSON, ingest report and table CSVs off disk, its session
    closed. A committed fixture is refused without force. If it was the open
    document the next one opens. Broadcast: document_deleted."""
    s = request.app["session"]
    doc = s.library.by_doc_id(request.match_info["doc_id"])
    if doc is None:
        return _json({"error": "no such document"}, status=404)
    if doc.name in COMMITTED_FIXTURES and request.query.get("force") != "1":
        return _json({"error": f"{doc.name} is a committed fixture; pass ?force=1 to delete it anyway",
                      "committed": True}, status=403)
    socks = s.events.sockets
    was_current = s.library.current is doc
    if was_current and (s.playing or (s._reader and not s._reader.done()) or s._answer is not None):
        await s.stop_reading()
        await _stop_and_attribute(s, socks, "delete")
    t = s._enriching.pop(doc.name, None)
    if t is not None and not t.done():
        t.cancel()
    if s.prompt_document() == doc.name and s._prompt is not None:
        await s.resolve_prompt("cancelled", None)
    narr = s.conv.narrators.pop(doc.name, None)
    if narr is not None and not narr.finished:
        narr.cancel_line()
    s.conv.parked.pop(doc.name, None)
    s.conv.deferred_start.discard(doc.name)
    entry = s.library.remove(doc.name)
    root = s.library.root
    removed = []
    for p in [root / f"{doc.name}.json", root / f"{doc.name}.docling.json",
              root / f"{doc.name}.ingest_report.json", *root.glob(f"{doc.name}.t*.csv")]:
        if p.exists():
            p.unlink()
            removed.append(p.name)
    src = str((entry.get("source") or {}).get("path_or_url") or "")
    if src.startswith(".incoming_") and (root / src).exists():
        (root / src).unlink()
        removed.append(src)
    s.events.emit("document_deleted", document=doc.name, doc_id=doc.doc_id, files=removed,
                  was_current=was_current, forced=request.query.get("force") == "1")
    nxt = None
    if was_current:
        rows = s.library.list()
        if rows:
            nxt = rows[0]["name"]
            await _switch_document(s, socks, nxt, invite="none")
    await broadcast({"type": "document_deleted", "name": doc.name, "doc_id": doc.doc_id,
                     "current": nxt if was_current else (s.library.current.name if s.library.current else None),
                     "documents": s.listener_library()}, socks)
    await broadcast({"type": "library_changed", "documents": s.listener_library()}, socks)
    return _json({"deleted": {"name": doc.name, "doc_id": doc.doc_id, "files": removed}, "current": nxt})


async def api_dev_spoken_title(request):
    """POST {"name", "spoken_title"}: what the voice calls a document."""
    s = request.app["session"]
    body = await request.json()
    name = str(body.get("name", ""))
    if name not in s.library._docs:
        return _json({"error": "no such document"}, status=404)
    entry = s.library.set_spoken_title(name, str(body.get("spoken_title", "")))
    await broadcast({"type": "library_changed", "documents": s.listener_library()}, s.events.sockets)
    return _json(entry)


async def api_dev_enrich_rest(request):
    """POST {"enabled": bool}: the rest pass on or off for this session."""
    s = request.app["session"]
    body = await request.json()
    s.enrich_rest_enabled = bool(body.get("enabled", True))
    s.events.emit("enrich_rest_toggled", enabled=s.enrich_rest_enabled)
    return _json({"enabled": s.enrich_rest_enabled})


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




async def api_documents_post(request):
    """POST /documents: one PDF (or .docx/.txt/.md/.html, or {"url": ...}).

    Runs ingest_document() -- the same function as scripts/ingest.py -- in a
    worker thread and streams each stage as a server-sent event
    `{stage, status, elapsed_ms}`; the last event is `{stage: "done",
    entry: {...}}` with the library entry. The 25 MB cap and an unsupported
    type are the only HTTP errors. Uploading the same bytes twice returns the
    existing entry.
    """
    s = request.app["session"]
    source, upload, filename = None, None, None
    if request.content_type and request.content_type.startswith("multipart/"):
        reader = await request.multipart()
        field = await reader.next()
        while field is not None:
            if field.name == "file":
                filename = Path(field.filename or "upload.pdf").name
                if Path(filename).suffix.lower() not in UPLOAD_SUFFIXES:
                    return _json({"error": f"unsupported type {Path(filename).suffix!r}; "
                                           f"send {', '.join(UPLOAD_SUFFIXES)}"}, status=415)
                s.library.root.mkdir(parents=True, exist_ok=True)
                upload = s.library.root / f".incoming_{uuid.uuid4().hex[:8]}_{filename}"
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
                            return _json({"error": "file larger than 25 MB"}, status=413)
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

    import ingest as ingest_mod
    # Idempotent: the doc_id is the content hash, so the same PDF is the same document.
    if upload is not None:
        doc_id = ingest_mod._doc_id_for(upload.read_bytes())
        existing = s.library.by_doc_id(doc_id)
        if existing is not None:
            upload.unlink(missing_ok=True)
            resp = web.StreamResponse(headers={"Content-Type": "text/event-stream",
                                               "Cache-Control": "no-cache"})
            await resp.prepare(request)
            await resp.write(("data: " + json.dumps({"stage": "done", "status": "ok", "elapsed_ms": 0,
                                                     "existing": True,
                                                     "entry": s.library.entry(existing.name)}) + "\n\n").encode())
            await resp.write_eof()
            return resp
        dup = s.library.by_text_hash(ingest_mod.text_fingerprint(upload))
        if dup is not None:
            upload.unlink(missing_ok=True)
            line = cp.DUPLICATE_LINE.format(title=dup.spoken_title)
            s.events.emit("upload_duplicate", document=dup.name, doc_id=dup.doc_id, filename=filename)
            if s.voice_free() is None:
                asyncio.ensure_future(s.speak_companion(line, "template", "duplicate", s.events.sockets, doc=dup))
            return _json({"error": line, "existing": s.library.entry(dup.name)}, status=409)
        name = doc_id
    else:
        name = None

    resp = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache",
                                       "X-Accel-Buffering": "no"})
    await resp.prepare(request)
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()

    def progress(stage, status, ms, detail, extra=None):
        loop.call_soon_threadsafe(queue.put_nowait, {"stage": stage, "status": status,
                                                     "elapsed_ms": ms, "detail": detail, "extra": extra or {}})

    async def send(ev: dict):
        await resp.write(("data: " + json.dumps(ev) + "\n\n").encode())

    async def run():
        async with s._ingest_lock:
            return await loop.run_in_executor(
                None, lambda: ingest_mod.ingest_document(
                    source, out_dir=s.library.root, name=name,
                    title=Path(filename).stem if filename else None,
                    progress=progress, register=True, index_path=s.library.index_path))

    # The voice, if a tab has it, is the companion's while the stages run: real
    # progress, one question, an acknowledgement or a plan, fillers against
    # silence. Without the voice, nothing is said and the trace says why.
    narrator = None
    # Bound to the document being ingested (a placeholder until the write
    # stage names it), never to whatever the listener has open. The voice is
    # taken over for it: whatever was being read is cut at the playhead and
    # its position saved; with no tab holding the voice it is told at play.
    pending_doc = _Pending(name or Path(filename or "upload").stem, Path(filename or "upload").stem)
    if await s.take_voice_for(pending_doc, "upload", s.events.sockets):
        narrator = cp.Narrator(s, s.events.sockets, fillers_used=s.conv.fillers_used, doc=pending_doc, owner="upload")
        s.conv.narrators[narrator.doc.name] = narrator
        narrator.prime("takeover", cp.TAKEOVER_LINE.format(title=pending_doc.spoken_title))
        narrator.start()
    task = asyncio.ensure_future(run())
    try:
        while not task.done():
            try:
                ev = await asyncio.wait_for(queue.get(), timeout=0.25)
            except asyncio.TimeoutError:
                continue
            await send({k: v for k, v in ev.items() if k != "extra"})
            await broadcast({"type": "ingest_progress", "stage": ev["stage"], "state": ev["status"],
                             "detail": ev["detail"]}, s.events.sockets)
            if narrator is not None:
                await narrator.on_event(ev["stage"], ev["status"], ev["detail"], ev.get("extra"))
        while not queue.empty():
            ev = queue.get_nowait()
            await send({k: v for k, v in ev.items() if k != "extra"})
            if narrator is not None:
                await narrator.on_event(ev["stage"], ev["status"], ev["detail"], ev.get("extra"))
        try:
            res = task.result()
        except Exception as e:
            s.events.emit("ingest_failed", error=str(e)[:200], source=Path(source).name)
            await send({"stage": "done", "status": "error", "elapsed_ms": 0, "error": str(e)[:300]})
            if narrator is not None:
                await narrator.finish(ok=False)
                s.conv.narrators.pop(narrator.doc.name, None)
            return resp
        s.library.reload()
        entry = s.library.entry(res.name) or res.entry
        s.events.emit("document_ingested", document=res.name, doc_id=res.doc_id,
                      clause_count=res.clause_count, readable=res.readable,
                      elapsed_ms=res.report["elapsed_ms"].get("total"))
        await broadcast({"type": "library_changed", "documents": s.listener_library()}, s.events.sockets)
        # Eighth stage, on entry: the navigator (overview, section briefs,
        # topic chips) for a readable document, when a provider is configured.
        # The listener sees one more slice of the progress bar; /dev sees the
        # stage and its time. Skipped, not failed, when there is no provider.
        d = s.library.by_doc_id(res.doc_id) or (s.library._docs.get(res.name) if hasattr(s.library, "_docs") else None)
        if d is not None and pending_doc.name in s.conv.pending_narration:
            s.conv.pending_narration[d.name] = s.conv.pending_narration.pop(pending_doc.name)
        if narrator is not None and d is not None:
            # The ingest is written: the narrator, its prompt and its parked
            # question now belong to the real document.
            old = narrator.doc.name
            narrator.doc = d
            s.conv.narrators.pop(old, None)
            s.conv.narrators[d.name] = narrator
            if old in s.conv.parked:
                s.conv.parked[d.name] = s.conv.parked.pop(old)
            if s.prompt_kind() == "ingest_wait" and s.prompt_document() == old:
                s._prompt["document"] = d.name
        if d is not None and res.readable and s.enrichment_configured():
            t_e = time.monotonic()
            await send({"stage": "enrich", "status": "running", "elapsed_ms": 0, "detail": "overview and topics"})
            if narrator is not None:
                await narrator.on_event("enrich", "running")
            state = await s.ensure_navigator(d, s.events.sockets, retry=True)
            ms_e = round((time.monotonic() - t_e) * 1000)
            await send({"stage": "enrich", "status": "ok" if state == "ready" else "error", "elapsed_ms": ms_e,
                        "detail": "overview and topics" if state == "ready" else "generation failed; mechanical map"})
            await broadcast({"type": "ingest_progress", "stage": "enrich",
                             "state": "ok" if state == "ready" else "error", "detail": None}, s.events.sockets)
        else:
            await send({"stage": "enrich", "status": "skipped", "elapsed_ms": 0,
                        "detail": "no ENRICH_PROVIDER" if not s.enrichment_configured() else "not readable"})
        await send({"stage": "done", "status": "ok", "elapsed_ms": res.report["elapsed_ms"].get("total", 0),
                    "entry": entry})
        if narrator is not None:
            await narrator.finish()                          # "Done.", and narration_gap_ms in the trace
        if s.prompt_kind() == "ingest_wait" and d is not None and s.prompt_document() == d.name:
            await s.resolve_prompt("cancelled", None)       # closes at ready, with or without a reply
        if (s.sink is not None and not s.playing and not (s._reader and not s._reader.done())
                and s._answer is None and d is not None and s.library.current is not d
                and s.prompt_kind() is None):
            # The tab with the voice just heard the companion: open what it
            # uploaded. The parked question is answered first, then the
            # invitation. Nothing is sounding that a switch would have to cut.
            doc = s.library.open(d.name)
            s._start_reopen = None
            await broadcast(s.opened_message(doc), s.events.sockets)
        if d is not None and s.sink is not None:
            await s._start_choice_if_due(d, s.events.sockets)
    finally:
        if upload is not None:
            upload.unlink(missing_ok=True)
        try:
            await resp.write_eof()
        except Exception:
            pass
    return resp


def _doc_for(s, doc_id: str):
    d = s.library.by_doc_id(doc_id)
    if d is None and doc_id in s.library._docs:
        d = s.library._docs[doc_id]
    return d


async def api_document_report(request):
    """GET /documents/<doc_id>/report: the ingest report, for the developer page."""
    s = request.app["session"]
    d = _doc_for(s, request.match_info["doc_id"])
    if d is None:
        return _json({"error": "no such document"}, status=404)
    path = d.path.parent / (d.report or f"{d.name}.ingest_report.json")
    if not path.exists():
        return _json({"error": "no report for this document (ingested before reports existed)"}, status=404)
    return web.Response(text=path.read_text(encoding="utf-8"), content_type="application/json")


async def api_document_enrichment(request):
    """GET /documents/<doc_id>/enrichment: the generated fields for the
    developer page -- provider, model, elapsed, guard rejections, overview,
    briefs, topics, questions."""
    s = request.app["session"]
    d = _doc_for(s, request.match_info["doc_id"])
    if d is None:
        return _json({"error": "no such document"}, status=404)
    fx = d.fixture
    return _json({
        "doc_id": d.doc_id, "name": d.name,
        "enrichment": fx.get("enrichment") or {"provider": "none", "fields": [], "guard_rejections": []},
        "overview": fx.get("overview"), "sections": fx.get("sections") or [], "topics": fx.get("topics") or [],
        "tagged_clauses": sum(1 for c in fx["clauses"] if c.get("tags")),
        "table_overrides": sum(1 for c in fx["clauses"] if c.get("spoken_override")),
    })


async def api_document_accept(request):
    """POST /documents/<doc_id>/accept: the developer is the reviewer."""
    s = request.app["session"]
    d = _doc_for(s, request.match_info["doc_id"])
    if d is None:
        return _json({"error": "no such document"}, status=404)
    entry = s.library.set_reviewed(d.name, True)
    await broadcast({"type": "library_changed", "documents": s.listener_library()}, s.events.sockets)
    return _json(entry)


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
        try:
            await s.ensure_provider()
        except Exception as e:
            await ws.send_str(json.dumps({"type": "provider_error", "message": str(e)}))
        await ws.send_str(json.dumps({"type": "hello", "session_id": s.id, "dev": s.dev,
                                      "upload_enabled": s.allow_upload,
                                      "sink": ws is s.sink, "sink_any": s.sink is not None,
                                      "provider": s.descriptor,
                                      "documents": s.listener_library(),
                                      "current": s.library.current.name if s.library.current else None,
                                      "topics": s.topics_for(s.library.current),
                                      "sections": s.sections_for(s.library.current)}))
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            try:
                m = json.loads(msg.data)
            except ValueError:
                continue
            await handle_client_message(s, m, socks, ws)
    except ConnectionResetError:
        # The client (browser tab or voice bridge) vanished mid-handshake or
        # mid-session -- nothing to do but clean up below like any other
        # disconnect. Before this try/except covered ensure_provider()/hello
        # too, a client that disappeared in that window skipped the
        # socks.discard() below entirely, leaving a dead socket in the
        # broadcast set that every later interrupt/ask/progress message would
        # then also try (and fail) to write to.
        pass
    finally:
        socks.discard(ws)
        if ws is s.sink:
            # The tab with the audio went away. Nothing can be heard until
            # another tab presses play, so stop now and attribute the boundary
            # to the last ack rather than let synthesis run unheard.
            s.sink = None
            if s.playing or (s._reader and not s._reader.done()) or s._answer is not None:
                await _stop_and_attribute(s, socks, "sink_left")
            await s.announce_sink(socks)
        if not socks:
            await s.stop_reading()
    return ws


async def _stop_and_attribute(s: ReaderSession, socks, reason: str) -> None:
    """Stop reading, cancel synthesis, attribute the boundary to the clause the
    listener was hearing. Shared by interrupt and pause: both cut the audio at
    the playhead, and both must leave the read cursor on the cut clause."""
    now = time.monotonic() * 1000.0
    if s._pending_flush is not None:
        s._flush_acks.append((s._pending_flush, now))
        s._pending_flush = None
    else:
        s.events.emit(f"{reason}_without_flush_ack")
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
    if st and st.kind in NON_CLAUSE_KINDS:
        # The listener cut the spoken answer, or the document map. Neither is
        # a clause: the answer's position is the clause it was about, already
        # attributed when the question stopped the reader, and the map has no
        # position at all. Fence it and record nothing else.
        st.state = "fenced"
        st = None
    if st:
        st.state = "fenced"
        wm = s.wordmaps.get(ctx_id)
        c = doc.grounding.by_id[st.unit_id]
        # A resumed clause's word map starts at its char_start.
        char_end = (wm.offset_at(st.rendered_ms) + st.char_start) if wm else st.char_start
        char_end = min(char_end, len(c["text_display"]))
        word_i, word = -1, ""
        if wm:
            for i, sp in enumerate(wm.spans):
                if sp.t_end_ms <= st.rendered_ms:
                    word_i, word = i, sp.text if hasattr(sp, "text") else ""
        sess.last_heard_unit_id = st.unit_id
        sess.boundary_char = char_end
        cut = char_end < len(c["text_display"])
        sess.ledger[st.unit_id] = f"truncated@{char_end}" if cut else "heard"
        # Re-read a cut clause from the cursor; never skip it (pause used
        # to leave the cursor past it, so sec-3-p4 vanished after a pause
        # at 0.9 s) and never replay one that was heard to the end. The
        # re-read starts at the sentence containing the cut (read_loop).
        sess.read_cursor = c["index"] if cut else c["index"] + 1
        s._resume_from = (c["id"], char_end) if cut else None
        if cut:
            s.events.emit("unit_truncated", document=doc.name, context_id=st.unit_id,
                          char_end=char_end, of=len(c["text_display"]),
                          rendered_ms=round(st.rendered_ms, 1), reason=reason)
        await broadcast({"type": "boundary", "unit_id": st.unit_id,
                         "context_id": ctx_id, "rendered_ms": round(st.rendered_ms, 1),
                         "char_end": char_end, "word_index": word_i, "word": word,
                         "of": len(c["text_display"])}, socks)
    await broadcast({"type": "paused"}, socks)
    return


async def handle_client_message(s: ReaderSession, m: dict, socks, ws) -> None:
    t = m.get("type")

    if s.sink is not None and ws is not s.sink and t in ("rendered", "unit_ended", "flush_ack"):
        # Only the tab with the audio has a clock. Anything else is a tab that
        # flushed an empty player; recording it would move the boundary.
        return

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
        audible_stop_ts = m.get("audible_stop_ts")
        if audible_stop_ts is not None:
            # The client's audio-hardware clock value at the moment playback
            # actually stopped -- what A1 (audible-stop latency) is measured
            # against. Mirrors agent.py's Ledger.log_audible_stop() for this
            # server's own lightweight event mechanism.
            s.events.emit("audible_stop", context_id=fctx,
                          turn_id=(st.turn_id if st else None),
                          unit_id=(st.unit_id if st else None),
                          rendered_ms=round(float(m.get("rendered_ms") or 0.0), 1),
                          audible_stop_ts=float(audible_stop_ts))
        if s._flush_waiter is not None and not s._flush_waiter.done():
            s._flush_waiter.set_result(True)          # a stop from another tab was waiting
        return

    if t == "interrupt":
        if s.sink is not None and ws is not s.sink:
            await s.stop_reading()              # no audio after the flush
            await s.request_flush()
        await _stop_and_attribute(s, socks, "interrupt")
        s._paused_at = time.monotonic()
        if s.sink is None:
            await s.claim_sink(ws, socks)           # the answer will need a listener
        return

    if t == "play":
        if s.replaying:
            return
        if s.sink is not None and ws is not s.sink:
            # Play from another tab moves the voice there. Whatever the old
            # tab was hearing is cut at its playhead and picked up on the new
            # one from that sentence.
            if s.playing or (s._reader and not s._reader.done()) or s._answer is not None:
                await s.stop_reading()          # stop sending before the old tab flushes
                await s.request_flush()
                await _stop_and_attribute(s, socks, "handover")
        await s.claim_sink(ws, socks)
        if s._reader and not s._reader.done():
            return                                  # already reading
        if s.conv.pending_narration:
            await s.speak_backlog(socks)             # what was processed while no tab had the voice
        doc = s.library.current
        if (s._prompt is None and not s._session_started and not s.conv.welcomed
                and len(s.library.list()) > 1 and doc is not None and doc.name not in s._opened_by_listener):
            # The first play of the session, nothing started, nothing chosen:
            # which document? The default document opens itself on silence.
            await s.open_welcome(socks)
            return
        if (doc is not None and s._prompt is None and s.has_navigator(doc)
                and doc.name not in s._session_started and doc.name not in s._start_asked):
            # The first play of a ready document that was never invited (opened
            # without the voice, or re-opened at load): the invitation now.
            await s.open_start_choice(doc, socks)
            return
        if s._prompt is not None and s.prompt_kind() not in LOOP_PROMPTS:
            # Play pressed over an invitation or a choice: the button answers
            # it -- read. The map still comes first for a document not started.
            await s.resolve_prompt("chip", None)
        if (doc is not None and doc.name in s._session_started and s._paused_at is not None
                and time.monotonic() - s._paused_at > RESUME_CUE_AFTER_S and doc.session.read_cursor > 0):
            # Back after a long pause: say where we were before the clause.
            g = doc.grounding
            sec = g.section_of(min(doc.session.read_cursor, len(g.clauses) - 1))
            if sec is not None:
                s._pending_cue = f"We were in {sec['title']}. Carrying on."
                s.events.emit("resume_cue", document=doc.name, section=sec["id"],
                              paused_s=round(time.monotonic() - s._paused_at, 1))
        s._paused_at = None
        s.playing = True
        await broadcast({"type": "playing"}, socks)
        s._reader = asyncio.ensure_future(s.read_loop(socks))
        return

    if t == "pause":
        # The sink flushes and sends flush_ack first, exactly as for an
        # interrupt, so the boundary lands on the clause being heard. From any
        # other tab the server asks the sink for that ack.
        if s.sink is not None and ws is not s.sink:
            await s.stop_reading()
            await s.request_flush()
        await _stop_and_attribute(s, socks, "pause")
        s._paused_at = time.monotonic()
        return

    if t == "open":
        try:
            name = str(m.get("name", ""))
            cur = s.library.current
            # A page that loads re-opens the document it was on: not a choice,
            # nothing is invited. The first play asks.
            startup = cur is not None and cur.name == name and s.sink is None and name not in s._session_started
            s._opened_by_listener.add(name)
            await _switch_document(s, socks, name, ws, invite="none" if startup else "debounce")
        except LibraryError as e:
            await ws.send_str(json.dumps({"type": "error", "message": str(e)}))
        return

    if t == "jump":
        doc = s.library.current
        c = doc.grounding.by_id.get(str(m.get("unit_id", "")))
        if c:
            await s.jump_to(socks, c["index"], str(m.get("reason") or "spoiler_offer"), ws)
        return

    if t == "get_script":
        # Screen 3 ("Show script"): the same per-clause ledger the end-of-
        # document recap reads from (heard / truncated@ / skipped:*), shaped
        # for a heard / now / not-yet list. No clause id, offset, or turn id
        # crosses the wire -- only text and a three-way status, same rule as
        # everything else the listener page shows.
        doc = s.library.current
        if doc is None:
            await ws.send_str(json.dumps({"type": "script", "clauses": []}))
            return
        g = doc.grounding
        sess = doc.session
        rows = []
        for c in g.clauses:
            if not is_readable(c):
                continue
            state = sess.ledger.get(c["id"])
            if c["id"] == sess.last_heard_unit_id:
                status = "now"
            elif state == "heard" or (isinstance(state, str) and state.startswith("truncated@")):
                status = "heard"
            else:
                status = "not_yet"
            rows.append({"text": c["text_display"], "status": status})
        await ws.send_str(json.dumps({"type": "script", "clauses": rows}))
        return

    if t == "skip_forward":
        # The transport's forward control: the same "skip to the next
        # section" jump the typed/spoken "skip" intent already does -- no
        # new position semantics, just a direct button for it.
        doc = s.library.current
        g = doc.grounding
        sess = doc.session
        cur = g.section_of(max(sess.read_index, 0))
        nxt = next((x for x in g.sections if x["start"] > (cur["start"] if cur else -1)), None)
        if nxt:
            await s.jump_to(socks, nxt["start"], "skip", ws)
        return

    if t == "skip_back":
        # Symmetric with skip_forward: the section before the one containing
        # the read position, so pressing back always lands somewhere earlier
        # than what's on screen even from partway into a section.
        doc = s.library.current
        g = doc.grounding
        sess = doc.session
        cur = g.section_of(max(sess.read_index, 0))
        earlier = [x for x in g.sections if x["start"] < (cur["start"] if cur else 0)]
        prev = earlier[-1] if earlier else (g.sections[0] if g.sections else None)
        if prev:
            await s.jump_to(socks, prev["start"], "back", ws)
        return

    if t == "topic":
        doc = s.library.current
        g = doc.grounding
        sid = m.get("section_id")
        if sid is None:
            # "Read from the start"
            await s.jump_to(socks, 0, "chip", ws, label="the start")
            return
        sec = g.section_for_id(sid) or g.find_section(str(m.get("topic", "")))
        if sec:
            await s.offer_section(socks, sec, ws)
        return

    if t == "choice":
        await s.resolve_choice(socks, "now" if str(m.get("choice")) == "now" else "overview_first", ws)
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
        # One router for every utterance: the open prompt's grammar, then the
        # navigation regexes, then a question, then (prompt open, nothing
        # matched) the closed-set classifier. Matched BEFORE the reader is
        # stopped: a reply needs the prompt, which stop_reading clears, and a
        # jump does its own stop.
        was_reading = bool(s.playing or (s._reader and not s._reader.done()))
        for n in list(s.conv.narrators.values()):
            if not n.finished:
                n.cancel_line()                             # the listener speaks: the companion stops
        routed = await s.route_reply(q, socks, ws)
        route = routed["route"]
        s.events.emit("reply_routed", document=doc.name, route=route, intent=routed.get("intent"),
                      kind=routed.get("kind"), choice=routed.get("choice"), text=q[:200])
        sid = routed.get("section_id")
        s.events.emit("reply_understood", document=doc.name, intent=routed.get("intent"), section_id=sid,
                      row=routed.get("row"), via=routed.get("via", "rules"), ms=routed.get("ms", 0), text=q[:200])
        await broadcast({"type": "reply_understood", "intent": routed.get("intent"), "section_id": sid,
                         "section_title": next((x["title"] for x in g.sections if x["id"] == sid), None),
                         "row": routed.get("row"), "via": routed.get("via", "rules")}, socks)
        if s.sink is None:
            await s.claim_sink(ws, socks)           # someone has to hear what follows
        if route == "understood":
            return                                  # the Conversation took the action
        if route in ("pending", "llm_classify"):
            await s.resolve_prompt("reply", routed["choice"], ws=ws, data=routed.get("data"))
            return
        q = routed.get("question") or q             # the model's cleaned-up question, when it understood one
        nav = routed.get("nav")
        if not nav:
            if s._prompt is not None:
                # A question while a prompt is open answers the prompt with the
                # question; start_choice re-opens once, after the answer.
                await s.resolve_prompt("reply", "question", ws=ws, data={"text": q})
            # A question stops the voice. The Listener sends the interrupt itself
            # (flush_ack, interrupt, ask) so the boundary is the playhead; this is
            # the fallback for a client that asks while still playing, and for a
            # question asked over a spoken answer.
            if s.playing or (s._reader and not s._reader.done()) or s._answer is not None:
                if s.sink is not None and ws is not s.sink:
                    await s.stop_reading()
                    await s.request_flush()
                await _stop_and_attribute(s, socks, "ask")
        if nav:
            intent = nav["intent"]
            s.events.emit("navigation_intent", document=doc.name, intent=intent, question=q)
            if intent == "skip":
                cur = g.section_of(max(sess.read_index, 0))
                nxt = next((x for x in g.sections if x["start"] > (cur["start"] if cur else -1)), None)
                if nxt:
                    await s.jump_to(socks, nxt["start"], "skip", ws)
                return
            if intent == "goto":
                await s.jump_to(socks, nav["section"]["start"], "topic", ws)
                return
            if intent == "start":
                await s.jump_to(socks, 0, "chip", ws, label="the start")
                return
            if intent == "back":
                if s._jump_back and s._jump_back.get("unit_id") in g.by_id:
                    b = s._jump_back
                    await s.jump_to(socks, g.by_id[b["unit_id"]]["index"], "back", ws, char_start=b.get("char", 0))
                return
            if intent in ("now", "overview_first", "yes"):
                return                              # no prompt is open for it (the grammar takes it otherwise)
            if intent == "go_on":
                if s._prompt is not None:
                    await s.resolve_prompt("reply", "go_on" if s.prompt_kind() == "offer" else "carry_on", ws=ws)
                elif not s.playing and not (s._reader and not s._reader.done()):
                    await s._start_reading(socks)
                return
            if intent in ("every", "summarise"):
                if s.playing or (s._reader and not s._reader.done()) or s._answer is not None:
                    if s.sink is not None and ws is not s.sink:
                        await s.stop_reading()
                        await s.request_flush()
                    await _stop_and_attribute(s, socks, "ask")
                if intent == "every":
                    hits = nav["clauses"]
                    tag = nav["tag"].replace("_", " ")
                    if not hits:
                        answer = f"There are no clauses tagged {tag} in this document."
                    else:
                        parts = [f"{g._hit(c['index'], 1.0).citation_spoken()}: {spoken_text(c)}" for c in hits[:8]]
                        more = f" There are {len(hits) - 8} more; ask for the next ones." if len(hits) > 8 else ""
                        answer = f"There are {len(hits)} clauses tagged {tag}. " + " ".join(parts) + more
                    unit_id = hits[0]["id"] if hits else None
                else:
                    sec = nav["section"]
                    tagged = [c for c in g.clauses if sec["start"] <= c["index"] < sec["end"] and c.get("tags")]
                    lead = sec.get("brief") or f"{sec['title']}."
                    parts = [f"{g._hit(c['index'], 1.0).citation_spoken()}: {spoken_text(c)}" for c in tagged[:6]]
                    answer = f"{sec['title']}. {lead}" + (" " + " ".join(parts) if parts else "")
                    unit_id = sec["id"]
                s.events.emit("question_resolved", document=doc.name, kind="in_scope", unit_id=unit_id,
                              question=q, retrieval_path="extractive")
                s.events.emit("answer_grounded", document=doc.name, question=q, kind="in_scope",
                              unit_id=unit_id, retrieval_path="extractive", source="extractive")
                await broadcast({"type": "answer", "question": q, "kind": "in_scope", "unit_id": unit_id,
                                 "answer": answer, "source": "extractive", "retrieval_path": "extractive",
                                 "referral": s.referral_for(doc.name), "offer": False}, socks)
                if s.sink is None:
                    await s.claim_sink(ws, socks)
                if s._answer_task and not s._answer_task.done():
                    s._answer_task.cancel()
                s._answer_task = asyncio.ensure_future(s.speak_answer(answer, "in_scope", unit_id, socks))
                return
        # Deictic questions resolve against the clause the listener actually
        # heard last: _stop_and_attribute set last_heard_unit_id from the
        # flush_ack's context, which under lookahead is not the last clause
        # synthesised.
        r = g.resolve(q, sess.last_heard_unit_id, read_cursor=max(sess.read_index, 0))
        target = r.hits[0].unit_id if r.hits else (r.beyond[0].unit_id if r.beyond else None)
        sess.history.append((q, r.kind, target))
        s.events.emit("question_resolved", document=doc.name, kind=r.kind,
                      unit_id=target, question=q, last_heard_unit_id=sess.last_heard_unit_id,
                      retrieval_path=r.retrieval_path)
        heard = None
        if sess.last_heard_unit_id:
            c = g.by_id[sess.last_heard_unit_id]
            if sess.boundary_char < len(c["text_display"]):
                heard = c["text_display"][:sess.boundary_char]
        # Deictic ("what does that mean") and in-scope answers go through the
        # model when there is one; beyond_cursor, not_found and eligibility are
        # deterministic sentences and never do.
        source = "llm" if (s.llm is not None and r.kind in ("in_scope", "deictic")) else "extractive"
        try:
            answer = await g.answer(r, llm=s.llm, heard_text_of_reference=heard)
        except Exception as e:
            s.events.emit("llm_failed", error=str(e)[:200])
            source = "extractive"
            answer = await g.answer(r, llm=None, heard_text_of_reference=heard)
        s.events.emit("answer_source", source=source, kind=r.kind, unit_id=target)
        # One record per answered question saying which branch answered it.
        s.events.emit("answer_grounded", document=doc.name, question=q, kind=r.kind,
                      unit_id=target, retrieval_path=r.retrieval_path, source=source)
        await broadcast({
            "type": "answer", "question": q, "kind": r.kind, "unit_id": target,
            "answer": answer, "source": source, "retrieval_path": r.retrieval_path,
            "referral": s.referral_for(doc.name),
            "offer": r.kind == "beyond_cursor",
        }, socks)
        if s._answer_task and not s._answer_task.done():
            s._answer_task.cancel()
        if r.kind == "not_found":
            # Not in the document: a prompt, so the listener can carry on or
            # try another word, rather than an answer that waits on nothing.
            await s.open_not_found(socks, was_reading)
            return
        s._answer_task = asyncio.ensure_future(s.speak_answer(answer, r.kind, target, socks))
        return


# ==========================================================================
# app
# ==========================================================================

def _file_route(path: Path):
    async def handler(request):
        return web.FileResponse(path)
    return handler


def build_app(dev: bool = False, index_path: Path = INDEX,
              allow_upload: bool = True, warm_converter: bool = False) -> web.Application:
    app = web.Application(client_max_size=MAX_UPLOAD_BYTES + 1024 * 1024)
    app["session"] = ReaderSession(dev=dev, index_path=index_path, allow_upload=allow_upload)
    app.router.add_get("/api/status", api_status)
    app.router.add_get("/api/livekit/token", api_livekit_token)
    app.router.add_get("/api/contexts", api_contexts)
    app.router.add_get("/api/metrics", api_metrics)
    app.router.add_get("/api/events", api_events)
    app.router.add_get("/api/library", api_library)
    app.router.add_get("/api/traces", api_traces)
    app.router.add_post("/api/replay", api_replay)
    app.router.add_post("/documents", api_documents_post)
    app.router.add_get("/documents/{doc_id}/report", api_document_report)
    app.router.add_get("/documents/{doc_id}/enrichment", api_document_enrichment)
    app.router.add_post("/documents/{doc_id}/accept", api_document_accept)
    app.router.add_post("/api/dev/provider", api_dev_provider)
    app.router.add_post("/api/dev/enrich_rest", api_dev_enrich_rest)
    app.router.add_post("/api/dev/spoken_title", api_dev_spoken_title)
    app.router.add_delete("/documents/{doc_id}", api_documents_delete)
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
        # Warm the Docling converter so the first upload is not cold. In a
        # thread: building it loads the layout models and takes seconds.
        if warm_converter:
            def warm():
                try:
                    import ingest_structure as istr
                    ok = istr.warm()
                    a["session"].events.emit("docling_warm" if ok else "docling_unavailable")
                except Exception as e:
                    a["session"].events.emit("docling_unavailable", error=str(e)[:160])
            asyncio.get_running_loop().run_in_executor(None, warm)
        # Same for the answer model: a local Ollama model pays its load on the
        # first request, which would land on the voice line mid-read. Check it
        # is present, warm it, and put the outcome in the trace and /api/status.
        if a["session"].llm is not None:
            def warm_answers():
                sess = a["session"]
                try:
                    c = check_llm()
                    if not c["ok"]:
                        sess.llm_state = c
                        sess.events.emit("llm_unavailable", provider=c["provider"], model=c["model"],
                                         detail=c["detail"], hint=c["hint"])
                        return
                    w = warm_llm()
                    sess.llm_state = {**c, "ok": w["ok"], "detail": w["detail"] if not w["ok"] else c["detail"]}
                    sess.events.emit("llm_ready" if w["ok"] else "llm_unavailable",
                                     provider=c["provider"], model=c["model"],
                                     load_ms=w["load_ms"], detail=w["detail"])
                except Exception as e:
                    sess.llm_state = {"ok": False, "detail": str(e)[:160]}
                    sess.events.emit("llm_unavailable", error=str(e)[:160])
            asyncio.get_running_loop().run_in_executor(None, warm_answers)
    app.on_startup.append(on_start)
    return app


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--dev", action="store_true",
                    help="enable /api/dev/* : provider swapping and the diagnostics route's "
                         "controls. Implies --allow-upload. Never enable this for the judged flow.")
    ap.add_argument("--no-warm", action="store_true",
                    help="do not pre-build the Docling converter at start")
    args = ap.parse_args()
    # The session's asyncio locks are created here, so the loop the server
    # runs on must be this one: web.run_app would otherwise make a new loop
    # and a contended lock (an upload while another document's enrichment
    # holds it) fails with "attached to a different loop" on Python 3.9.
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    app = build_app(dev=args.dev, warm_converter=not args.no_warm)
    s = app["session"]
    print(f"session {s.id}  dev={args.dev}  upload={s.allow_upload}  "
          f"provider={os.environ.get('TTS_PROVIDER', 'rime')}  "
          f"answers={describe_llm()['active']}")
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
    print("upload: POST /documents runs the build-time ingestion, writes fixtures/, "
          "appends index.json with reviewed=false; /dev shows the report and accepts")
    web.run_app(app, host=args.host, port=args.port, print=None, loop=loop)
    return 0


if __name__ == "__main__":
    sys.exit(main())
