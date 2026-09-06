"""Rime TTS over the `/ws3` JSON WebSocket.

Shipped path (must match README + RIME_EVIDENCE.md exactly):
  endpoint    wss://users-ws.rime.ai/ws3   (RIME_WS_URL)
  modelId     coda                          (RIME_MODEL_ID) — explicit, never omitted
  speaker     from live catalog             (RIME_SPEAKER)  — verified by scripts/fetch_voices.py
  lang        en                            (RIME_LANG)
  audioFormat pcm  (s16le mono)             (RIME_AUDIO_FORMAT)
  samplingRate 24000                        (RIME_SAMPLING_RATE)
  transport   WSS to Rime; WebRTC (LiveKit) to the client

Facts this adapter is built around (verified with the Phase-0 probe; re-run
scripts/preflight_rime.py before submission):
  * All synthesis params are URL query params fixed at connect time. Only
    `{"text", "contextId"}` messages and `{"operation": clear|flush|eos}`
    flow over the socket.
  * Omitting `modelId` or misspelling it silently serves Mist v3. We always
    send it and log it in `provider_active`; we cannot infer it from the
    stream, so the catalog check in fetch_voices.py is the guard.
  * Word timestamps arrive as `{"type":"timestamps","word_timestamps":
    {"words":[...],"start":[...],"end":[...]}}` with times in *seconds*.
    Whether the clock restarts per context is a probe result; set
    RIME_TIMESTAMP_CLOCK=per_context|cumulative accordingly.
  * `clear` does NOT stop an in-flight synthesis; audio for the cleared
    context can keep arriving. That is why cancel() bumps a generation and
    fences by contextId: late audio is dropped and counted, never played.
    Audible stop is the client's job (flush its worklet queue).
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import time
from dataclasses import dataclass, field
from typing import AsyncIterator, Optional
from urllib.parse import urlencode

from ..events import EventLog
from .base import AudioChunk, Done, StreamItem, Timestamps, TTSError


@dataclass
class RimeConfig:
    api_key: str
    ws_url: str = "wss://users-ws.rime.ai/ws3"
    model_id: str = "coda"
    speaker: str = ""
    lang: str = "en"
    audio_format: str = "pcm"
    sampling_rate: int = 24000
    segment: str = "bySentence"      # never | bySentence | immediate
    speed_alpha: float = 1.0         # only speed control that works over WS (timeScaleFactor is HTTP-only)
    timestamp_clock: str = "per_context"  # or "cumulative" — from probe
    extra_query: dict = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> "RimeConfig":
        key = os.environ.get("RIME_API_KEY", "")
        if not key:
            raise RuntimeError("RIME_API_KEY is not set (server-side env only; never in source).")
        speaker = os.environ.get("RIME_SPEAKER", "")
        if not speaker:
            raise RuntimeError("RIME_SPEAKER is not set. Pick one from scripts/fetch_voices.py output.")
        return cls(
            api_key=key,
            ws_url=os.environ.get("RIME_WS_URL", cls.ws_url),
            model_id=os.environ.get("RIME_MODEL_ID", cls.model_id),
            speaker=speaker,
            lang=os.environ.get("RIME_LANG", cls.lang),
            audio_format=os.environ.get("RIME_AUDIO_FORMAT", cls.audio_format),
            sampling_rate=int(os.environ.get("RIME_SAMPLING_RATE", cls.sampling_rate)),
            segment=os.environ.get("RIME_SEGMENT", cls.segment),
            speed_alpha=float(os.environ.get("RIME_SPEED_ALPHA", cls.speed_alpha)),
            timestamp_clock=os.environ.get("RIME_TIMESTAMP_CLOCK", cls.timestamp_clock),
        )

    def query(self) -> dict:
        q = {
            "speaker": self.speaker,
            "modelId": self.model_id,
            "audioFormat": self.audio_format,
            "lang": self.lang,
            "samplingRate": self.sampling_rate,
            "segment": self.segment,
            "speedAlpha": self.speed_alpha,
        }
        q.update(self.extra_query)
        return q

    def url(self) -> str:
        return f"{self.ws_url}?{urlencode(self.query())}"

    def public(self) -> dict:
        """Safe to log / print. Never includes the key."""
        d = self.query()
        d["endpoint"] = self.ws_url
        d["transport"] = "wss (Rime) -> webrtc (LiveKit) -> client"
        return d


class _Ctx:
    __slots__ = ("queue", "generation", "seq", "bytes", "t_req", "t_first", "t_origin_ms",
                 "carry", "odd_seen")

    def __init__(self, generation: int) -> None:
        self.queue: asyncio.Queue[Optional[StreamItem]] = asyncio.Queue()
        self.generation = generation
        self.seq = 0
        self.bytes = 0            # bytes actually YIELDED (even), not bytes received
        self.t_req = time.monotonic()
        self.t_first: float | None = None
        self.t_origin_ms: float | None = None
        # Rime /ws3 splits its 1024-byte PCM blocks at arbitrary byte offsets
        # (829+195, 1006+18 ...), so a chunk can end mid-sample. A held odd
        # byte is prepended to the next chunk so every yielded chunk is a whole
        # number of s16le samples and alignment is never lost.
        self.carry: bytes = b""
        self.odd_seen = 0


class RimeTTS:
    name = "rime"

    def __init__(self, cfg: RimeConfig, events: EventLog | None = None) -> None:
        self.cfg = cfg
        self.events = events or EventLog()
        self.sample_rate = cfg.sampling_rate
        self._ws = None
        self._reader: asyncio.Task | None = None
        self._ctx: dict[str, _Ctx] = {}
        self._generation = 0
        self._send_lock = asyncio.Lock()
        self.fenced_bytes = 0
        self.fenced_messages = 0

    # ------------------------------------------------------------ descriptor
    @property
    def descriptor(self) -> dict:
        return {"provider": self.name, **self.cfg.public()}

    # ------------------------------------------------------------ lifecycle
    async def connect(self) -> None:
        import websockets  # lazy: tests and the fake provider don't need it

        t0 = time.monotonic()
        self._ws = await websockets.connect(
            self.cfg.url(),
            additional_headers={"Authorization": f"Bearer {self.cfg.api_key}"},
            max_size=None,
        )
        self._reader = asyncio.create_task(self._read_loop(), name="rime-reader")
        self.events.emit("provider_active", connect_ms=round((time.monotonic() - t0) * 1000, 1), **self.descriptor)

    async def close(self) -> None:
        if self._ws is None:
            return
        try:
            await self._send({"operation": "eos"})
        except Exception:
            pass
        if self._reader:
            self._reader.cancel()
        await self._ws.close()
        self._ws = None
        for c in self._ctx.values():
            c.queue.put_nowait(None)
        self._ctx.clear()

    async def _send(self, obj: dict) -> None:
        assert self._ws is not None, "call connect() first"
        async with self._send_lock:
            await self._ws.send(json.dumps(obj))

    # ------------------------------------------------------------ synthesis
    async def synth(self, text: str, context_id: str) -> AsyncIterator[StreamItem]:
        if context_id in self._ctx:
            raise ValueError(f"contextId {context_id!r} already in flight")
        ctx = _Ctx(self._generation)
        self._ctx[context_id] = ctx
        self.events.emit("synth_requested", provider=self.name, context_id=context_id,
                         generation=ctx.generation, chars=len(text))
        try:
            await self._send({"text": text, "contextId": context_id})
            await self._send({"operation": "flush"})   # one unit == one flush; no batching across units
            while True:
                item = await ctx.queue.get()
                if item is None:          # cancelled
                    return
                yield item
                if isinstance(item, (Done, TTSError)):
                    return
        finally:
            self._ctx.pop(context_id, None)

    async def cancel(self) -> None:
        """Bump generation, fence every in-flight context, send `clear`.

        `clear` alone is not a stop. Audio already synthesised keeps
        streaming; the reader drops it by contextId and counts bytes so the
        evidence shows how much would have leaked without the fence.
        """
        self._generation += 1
        stale = list(self._ctx.keys())
        for cid in stale:
            self._ctx[cid].queue.put_nowait(None)
        t0 = time.monotonic()
        if self._ws is not None:
            try:
                await self._send({"operation": "clear"})
            except Exception as e:  # socket gone — fencing still holds
                self.events.emit("cancel_error", error=str(e))
        self.events.emit("cancel_issued", provider=self.name, generation=self._generation,
                         fenced_contexts=stale, send_ms=round((time.monotonic() - t0) * 1000, 2))

    # ------------------------------------------------------------ reader
    async def _read_loop(self) -> None:
        assert self._ws is not None
        try:
            async for raw in self._ws:
                if isinstance(raw, (bytes, bytearray)):
                    # /ws3 is JSON; a binary frame means the wrong endpoint or format.
                    self.events.emit("provider_protocol_error", note="binary frame on /ws3")
                    continue
                try:
                    ev = json.loads(raw)
                except json.JSONDecodeError:
                    self.events.emit("provider_protocol_error", note="non-json", head=raw[:80])
                    continue
                self._dispatch(ev)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.events.emit("provider_disconnected", error=str(e))
            for c in self._ctx.values():
                c.queue.put_nowait(TTSError(None, f"connection lost: {e}"))

    def _dispatch(self, ev: dict) -> None:
        typ = ev.get("type")
        cid = ev.get("contextId")
        ctx = self._ctx.get(cid) if cid else None

        if typ == "error" and ctx is None:
            self.events.emit("provider_error", raw=ev)
            for c in self._ctx.values():
                c.queue.put_nowait(TTSError(None, str(ev.get("message") or ev)))
            return

        if ctx is None or ctx.generation != self._generation:
            # Stale or unknown context: fence it. This is the leak that
            # `clear` alone would have let through.
            nbytes = _chunk_bytes(ev)
            self.fenced_messages += 1
            self.fenced_bytes += nbytes
            self.events.emit("result_fenced", provider=self.name, context_id=cid, msg_type=typ,
                             bytes=nbytes, generation_now=self._generation)
            return

        if typ == "chunk" or "data" in ev or "audio" in ev:
            b64 = ev.get("data") or ev.get("audio") or ""
            raw = base64.b64decode(b64) if b64 else b""
            if ctx.t_first is None:
                ctx.t_first = time.monotonic()
                self.events.emit("synth_first_byte", context_id=cid,
                                 ttfb_ms=round((ctx.t_first - ctx.t_req) * 1000, 1))
            if len(raw) % 2:
                ctx.odd_seen += 1
            pcm = ctx.carry + raw
            ctx.carry = b""
            if len(pcm) % 2:
                # Hold the trailing byte; it is the first half of a sample whose
                # second half arrives in the next chunk.
                ctx.carry = pcm[-1:]
                pcm = pcm[:-1]
            if not pcm:
                return
            ctx.queue.put_nowait(AudioChunk(cid, pcm, ctx.seq))
            ctx.seq += 1
            ctx.bytes += len(pcm)
            return

        if typ == "timestamps":
            wt = ev.get("word_timestamps") or ev.get("wordTimestamps") or {}
            words = list(wt.get("words", []))
            start = [float(x) * 1000.0 for x in wt.get("start", [])]
            end = [float(x) * 1000.0 for x in wt.get("end", [])]
            if self.cfg.timestamp_clock == "cumulative" and start:
                if ctx.t_origin_ms is None:
                    ctx.t_origin_ms = start[0]
                start = [s - ctx.t_origin_ms for s in start]
                end = [e - ctx.t_origin_ms for e in end]
            self.events.emit("timestamps_received", context_id=cid, n=len(words),
                             last_end_ms=round(end[-1], 1) if end else None)
            ctx.queue.put_nowait(Timestamps(cid, words, start, end))
            return

        if typ == "done":
            now = time.monotonic()
            if ctx.carry:
                # A lone byte at the end of the stream can never form a sample.
                self.events.emit("odd_tail_byte_dropped", context_id=cid)
                ctx.carry = b""
            self.events.emit("chunk_realigned", context_id=cid, odd_chunks=ctx.odd_seen)
            done = Done(cid, ctx.bytes,
                        ttfb_ms=round((ctx.t_first - ctx.t_req) * 1000, 1) if ctx.t_first else None,
                        total_ms=round((now - ctx.t_req) * 1000, 1))
            self.events.emit("synth_done", context_id=cid, bytes=ctx.bytes,
                             audio_ms=round(ctx.bytes / 2 / self.sample_rate * 1000, 1),
                             ttfb_ms=done.ttfb_ms, total_ms=done.total_ms)
            ctx.queue.put_nowait(done)
            return

        if typ == "error":
            self.events.emit("provider_error", context_id=cid, raw=ev)
            ctx.queue.put_nowait(TTSError(cid, str(ev.get("message") or ev), ev))
            return

        self.events.emit("provider_unknown_message", context_id=cid, keys=list(ev.keys()))


def _chunk_bytes(ev: dict) -> int:
    b64 = ev.get("data") or ev.get("audio")
    if not b64:
        return 0
    # base64 length -> byte length without decoding
    pad = b64.count("=")
    return (len(b64) * 3) // 4 - pad
