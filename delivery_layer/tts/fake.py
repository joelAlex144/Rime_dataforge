"""FakeTTS — same contract as RimeTTS, no network.

Used by unit tests, by the ledger/scheduler while Rime is unavailable, and
as the *disclosed* fallback. It emits `provider_active` with
provider="fake" so a session that ran on the fake is visible in the trace
and can never be mistaken for a Rime run.

Audio: silence (or a low tone) at `ms_per_word` per spoken token, streamed
in `chunk_ms` pieces with an optional artificial delay so cancel/fence
behaviour can be exercised deterministically.
"""
from __future__ import annotations

import asyncio
import math
import struct
from typing import AsyncIterator

from ..events import EventLog
from ..wordmap import clean_tokens
from .base import AudioChunk, Done, StreamItem, Timestamps


class FakeTTS:
    name = "fake"

    def __init__(self, events: EventLog | None = None, sample_rate: int = 24000,
                 ms_per_word: float = 320.0, chunk_ms: float = 80.0,
                 realtime: bool = False, tone_hz: float | None = None) -> None:
        self.events = events or EventLog()
        self.sample_rate = sample_rate
        self.ms_per_word = ms_per_word
        self.chunk_ms = chunk_ms
        self.realtime = realtime
        self.tone_hz = tone_hz
        self._generation = 0
        self._active: dict[str, int] = {}

    @property
    def descriptor(self) -> dict:
        return {"provider": self.name, "modelId": "fake", "speaker": "fake", "lang": "en",
                "audioFormat": "pcm", "samplingRate": self.sample_rate,
                "endpoint": "in-process", "transport": "in-process"}

    async def connect(self) -> None:
        self.events.emit("provider_active", **self.descriptor)

    async def close(self) -> None:
        self._active.clear()

    def _pcm(self, ms: float, phase: int) -> bytes:
        n = int(self.sample_rate * ms / 1000)
        if not self.tone_hz:
            return b"\x00\x00" * n
        return b"".join(struct.pack("<h", int(8000 * math.sin(2 * math.pi * self.tone_hz * (phase + i) / self.sample_rate)))
                        for i in range(n))

    async def synth(self, text: str, context_id: str) -> AsyncIterator[StreamItem]:
        gen = self._generation
        self._active[context_id] = gen
        self.events.emit("synth_requested", provider=self.name, context_id=context_id, generation=gen, chars=len(text))
        words = text.split()
        starts, ends, t = [], [], 0.0
        for w in words:
            dur = self.ms_per_word * max(1, len(clean_tokens(w)))
            starts.append(t)
            ends.append(t + dur)
            t += dur
        try:
            yield Timestamps(context_id, words, starts, ends)
            seq, sent_ms, total = 0, 0.0, 0
            while sent_ms < t:
                if self._active.get(context_id) != gen or gen != self._generation:
                    self.events.emit("result_fenced", provider=self.name, context_id=context_id, msg_type="chunk")
                    return
                ms = min(self.chunk_ms, t - sent_ms)
                if self.realtime:
                    await asyncio.sleep(ms / 1000)
                else:
                    await asyncio.sleep(0)
                pcm = self._pcm(ms, int(sent_ms * self.sample_rate / 1000))
                total += len(pcm)
                yield AudioChunk(context_id, pcm, seq)
                seq += 1
                sent_ms += ms
            self.events.emit("synth_done", context_id=context_id, bytes=total, audio_ms=round(t, 1))
            yield Done(context_id, total, ttfb_ms=0.0, total_ms=t if self.realtime else 0.0)
        finally:
            self._active.pop(context_id, None)

    async def cancel(self) -> None:
        self._generation += 1
        stale = list(self._active)
        self._active.clear()
        self.events.emit("cancel_issued", provider=self.name, generation=self._generation, fenced_contexts=stale)
