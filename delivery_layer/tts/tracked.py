"""TrackedTTS: the seam between our Rime stream and their scheduler/ledger.

The two halves of this repo describe audio differently and neither should
change. Ours (tts/base.py) is a raw provider stream: PCM bytes, a words/
starts/ends triple, a Done. Theirs (scheduler.py, ledger.py) wants base64
chunks with a millisecond span each, and word timings already aligned to
`text_display` with character offsets. This module converts one into the
other and nothing else.

Four facts about Rime drive the conversion, all measured rather than assumed:

1. **The anchor is the first audio sample, not the synth request.** Word
   timestamps and the worklet's rendered_ms are both audio-clock values with
   t=0 at the unit's first sample. The delivery side's original docstrings
   said t=0 meant "synth requested"; that would have made every rendered_ms
   comparison drift by the time-to-first-byte. Chunk spans here are derived
   from cumulative bytes, so they sit on the same clock by construction.

2. **Rime emits `timestamps` per segment, interleaved with audio, not once
   up front.** So the word map is rebuilt and re-emitted every time new words
   arrive, each emission cumulative and replacing the last. A consumer that
   took only the first Timestamps would hold a word map covering the opening
   fragment of the clause.

3. **`clear` does not stop in-flight audio.** 23 s arrived after clear in
   preflight. At this iterator's level cancel() ends the stream with zero
   stragglers; at the wire level they are unbounded and counted as
   result_fenced by the provider. Their fence is the safety net either way.

4. **Timestamps can overshoot the audio by roughly 100 ms.** Left alone the
   last word never satisfies `t_end_ms <= rendered_ms`, so a unit played to
   completion would resolve one word short. Done carries the true audio
   length, so the map is clamped there.
"""
from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
from typing import Any, AsyncIterator, Optional, Sequence, Union

from ..events import EventLog
from ..normalize import Segment
from ..wordmap import WordMap, build_word_map
from .base import AudioChunk as RawAudioChunk
from .base import Done as RawDone
from .base import Timestamps as RawTimestamps
from .base import TTSError as RawError

# ---------------------------------------------------------------------------
# The delivery side's stream types, preserved exactly.
#
# scheduler.py dispatches on type(event).__name__, so these must keep these
# names. They were defined in their tts/fake.py, which this module replaces;
# the field lists are unchanged from that file.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WordTiming:
    word: str
    t_start_ms: int
    t_end_ms: int
    char_start: int
    char_end: int


@dataclass(frozen=True)
class AudioChunk:
    context_id: str
    chunk_index: int
    pcm_b64: str          # base64 little-endian int16 PCM, mono
    t_start_ms: int
    t_end_ms: int


@dataclass(frozen=True)
class Timestamps:
    context_id: str
    words: tuple


@dataclass(frozen=True)
class Done:
    context_id: str
    total_duration_ms: int


@dataclass
class TrackedUnit:
    """What TrackedTTS needs to align a unit's audio back to its display text.

    `char_start` is 0 for a normal unit. A resumed unit carries the offset of
    the sentence it restarts from, and every char offset this module reports
    is shifted by it, so the ledger records coverage in the ORIGINAL unit's
    coordinates rather than the fragment's.
    """
    unit_id: str
    text_display: str
    text_spoken: str
    spoken_map: Sequence = ()
    char_start: int = 0

    @classmethod
    def from_clause(cls, clause: dict, char_start: int = 0) -> "TrackedUnit":
        return cls(
            unit_id=clause["id"],
            text_display=clause["text_display"],
            text_spoken=clause["text_spoken"],
            spoken_map=clause.get("spoken_map", ()),
            char_start=char_start,
        )


def _segments(unit: TrackedUnit) -> list:
    """normalizer Segments from a fixture's spoken_map."""
    return [Segment(a, b, sp, replaced=unit.text_display[a:b] != sp)
            for a, b, sp in (unit.spoken_map or ())]


class TrackedTTS:
    """Wraps any TTSProvider. Does not subclass or modify one."""

    def __init__(self, provider: Any, events: Optional[EventLog] = None) -> None:
        self._provider = provider
        self.events = events or getattr(provider, "events", None) or EventLog()
        self._wordmaps: dict[str, WordMap] = {}
        self._pending: set = set()

    # ------------------------------------------------------------ passthrough
    @property
    def name(self) -> str:
        return getattr(self._provider, "name", "unknown")

    @property
    def sample_rate(self) -> int:
        return getattr(self._provider, "sample_rate", 24000)

    @property
    def descriptor(self) -> dict:
        return self._provider.descriptor

    async def connect(self) -> None:
        # The provider emits provider_active here. It is the only thing that
        # knows the real model, speaker and endpoint, which is why agent.py no
        # longer logs it (their open question 4).
        await self._provider.connect()

    async def close(self) -> None:
        await self._provider.close()

    def word_map(self, unit_id: str) -> Optional[WordMap]:
        """Latest map built for a unit. Partial until Done."""
        return self._wordmaps.get(unit_id)

    # ----------------------------------------------------------------- synth
    async def synth(self, unit: Union[TrackedUnit, str],
                    context_id: str) -> AsyncIterator[object]:
        """Yields their AudioChunk / Timestamps / Done for one unit.

        Accepts a bare string too, so a caller that has only spoken text still
        gets audio; it just gets no word map, because char offsets into
        text_display cannot be invented from the spoken form.
        """
        if isinstance(unit, str):
            unit = TrackedUnit(unit_id=context_id, text_display=unit,
                               text_spoken=unit, spoken_map=())

        segs = _segments(unit)
        words: list[str] = []
        starts: list[float] = []
        ends: list[float] = []
        sent_bytes = 0
        chunk_index = 0
        emitted_ts = False

        async for item in self._provider.synth(unit.text_spoken, context_id):
            if isinstance(item, RawAudioChunk):
                t_start = sent_bytes / 2 / self.sample_rate * 1000.0
                sent_bytes += len(item.pcm)
                t_end = sent_bytes / 2 / self.sample_rate * 1000.0
                yield AudioChunk(
                    context_id=context_id,
                    chunk_index=chunk_index,
                    pcm_b64=base64.b64encode(item.pcm).decode("ascii"),
                    t_start_ms=int(round(t_start)),
                    t_end_ms=int(round(t_end)),
                )
                chunk_index += 1

            elif isinstance(item, RawTimestamps):
                # Cumulative across segments: Rime sends these interleaved.
                words.extend(item.words)
                starts.extend(item.start_ms)
                ends.extend(item.end_ms)
                wm = self._build(unit, segs, words, starts, ends)
                if wm is not None and wm.spans:
                    self._wordmaps[unit.unit_id] = wm
                    emitted_ts = True
                    yield Timestamps(context_id, self._timings(wm, unit.char_start))

            elif isinstance(item, RawDone):
                audio_ms = sent_bytes / 2 / self.sample_rate * 1000.0
                wm = self._wordmaps.get(unit.unit_id)
                if wm is not None and wm.spans:
                    wm.clamp_to(audio_ms)
                    yield Timestamps(context_id, self._timings(wm, unit.char_start))
                elif not emitted_ts:
                    # No timestamps at all (a non-en/es language, say). Still
                    # emit Done so the scheduler's unit completes; the ledger
                    # will resolve this unit at clause granularity.
                    self.events.emit("wordmap_absent", context_id=context_id,
                                     unit_id=unit.unit_id)
                yield Done(context_id, int(round(audio_ms)))

            elif isinstance(item, RawError):
                self.events.emit("provider_error", context_id=context_id,
                                 message=item.message)
                return

    def _build(self, unit: TrackedUnit, segs, words, starts, ends) -> Optional[WordMap]:
        if not words or not segs:
            return None
        try:
            return build_word_map(unit.unit_id, unit.text_display, segs, words, starts, ends)
        except Exception as e:                       # alignment is best effort
            self.events.emit("wordmap_failed", unit_id=unit.unit_id, error=str(e))
            return None

    @staticmethod
    def _timings(wm: WordMap, char_start: int) -> tuple:
        return tuple(
            WordTiming(
                word=sp.word,
                t_start_ms=int(round(sp.t_start_ms)),
                t_end_ms=int(round(sp.t_end_ms)),
                char_start=sp.char_start + char_start,
                char_end=sp.char_end + char_start,
            )
            for sp in wm.spans
        )

    # ---------------------------------------------------------------- cancel
    async def cancel(self, context_id: Optional[str] = None) -> None:
        """Best effort and idempotent. The provider fences anything that
        arrives afterwards; see fact 3 in the module docstring."""
        await self._provider.cancel()

    def cancel_nowait(self, context_id: Optional[str] = None) -> None:
        """Sync entry point, so their scheduler's `self._tts.cancel(ctx)` call
        site works unchanged. Schedules the coroutine on the running loop.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None:
            # Keep a reference so the task is not garbage collected mid-flight.
            task = loop.create_task(self.cancel(context_id))
            self._pending.add(task)
            task.add_done_callback(self._pending.discard)
        else:
            asyncio.run(self.cancel(context_id))
