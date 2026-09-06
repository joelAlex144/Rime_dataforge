"""
Fake TTS backend -- same interface the real Rime adapter (tts/rime.py,
owned by A) will implement, so everything downstream (scheduler, fence,
ledger) can be built and tested without live Rime access.

PROVISIONAL: tts/base.py (the actual interface definition) is A's file
and doesn't exist yet. The types below (AudioChunk, Timestamps, Done,
WordTiming) are my best-guess shape of what that interface will expose,
based on the agreed synth() signature:

    synth(text: str, context_id: str) -> AsyncIterator[AudioChunk | Timestamps | Done]
    cancel(context_id: str) -> None

Once A commits tts/base.py, this file should import its types instead of
defining local copies -- the local definitions here are ONLY to unblock
work before that contract lands. Two things this fake deliberately
mirrors because they affect correctness downstream:

  1. Unit-local ms timeline: t=0 is "synth requested" (see
     playback_protocol.py's documented convention). If A's real adapter
     anchors timestamps at first-byte instead, every consumer of
     Timestamps (ledger boundary resolution, scheduler) needs to know.
  2. cancel() is "best-effort, may not suppress everything already
     in-flight" -- the fake intentionally sometimes yields one more
     chunk after cancel() is called, to exercise the fence's
     drop-stale-results path (fence.py + ledger's result_fenced) rather
     than assume the backend is perfectly cooperative.
"""

from __future__ import annotations

import asyncio
import math
import struct
from dataclasses import dataclass
from typing import AsyncIterator, Optional, Union

SAMPLE_RATE_HZ = 24_000
CHANNELS = 1
CHUNK_MS = 100  # matches the client's ~100ms ack cadence
TONE_HZ = 440.0


@dataclass(frozen=True)
class AudioChunk:
    context_id: str
    chunk_index: int
    pcm_b64: str  # base64 little-endian int16 PCM, matches wire format
    t_start_ms: int
    t_end_ms: int


@dataclass(frozen=True)
class WordTiming:
    word: str
    t_start_ms: int
    t_end_ms: int
    char_start: int
    char_end: int


@dataclass(frozen=True)
class Timestamps:
    context_id: str
    words: tuple[WordTiming, ...]


@dataclass(frozen=True)
class Done:
    context_id: str
    total_duration_ms: int


TTSEvent = Union[AudioChunk, Timestamps, Done]


def _synthetic_word_timings(text: str, ms_per_char: float = 45.0) -> tuple[WordTiming, ...]:
    """Fabricate plausible word timings: split on whitespace, allocate
    duration proportional to word length (plus a fixed gap), track
    char offsets into `text` so downstream alignment-to-text_display
    logic can be exercised the same way it will be against real Rime
    timestamps."""
    words: list[WordTiming] = []
    t_cursor = 0.0
    char_cursor = 0
    for raw_word in text.split(" "):
        # find this word's actual position in text (handles repeated words correctly
        # since we advance char_cursor monotonically)
        start = text.index(raw_word, char_cursor)
        end = start + len(raw_word)
        duration = max(80.0, len(raw_word) * ms_per_char)
        words.append(
            WordTiming(
                word=raw_word,
                t_start_ms=int(t_cursor),
                t_end_ms=int(t_cursor + duration),
                char_start=start,
                char_end=end,
            )
        )
        t_cursor += duration + 40.0  # inter-word gap
        char_cursor = end
    return tuple(words)


def _sine_pcm_chunk(duration_ms: int, t_offset_ms: int) -> bytes:
    """Generate a chunk of 16-bit PCM sine wave, phase-continuous across
    chunks (uses t_offset_ms so consecutive chunks don't click)."""
    n_samples = int(SAMPLE_RATE_HZ * duration_ms / 1000)
    t0 = t_offset_ms / 1000.0
    samples = []
    for i in range(n_samples):
        t = t0 + i / SAMPLE_RATE_HZ
        val = int(3000 * math.sin(2 * math.pi * TONE_HZ * t))
        samples.append(val)
    return struct.pack(f"<{n_samples}h", *samples)


class FakeTTS:
    """Drop-in stand-in for the real Rime adapter. One instance can
    serve multiple concurrent contexts (unit synth calls)."""

    def __init__(self) -> None:
        self._cancelled_contexts: set[str] = set()

    def cancel(self, context_id: str) -> None:
        self._cancelled_contexts.add(context_id)

    async def synth(self, text: str, context_id: str) -> AsyncIterator[TTSEvent]:
        words = _synthetic_word_timings(text)
        total_duration_ms = words[-1].t_end_ms if words else 0

        yield Timestamps(context_id=context_id, words=words)

        chunk_index = 0
        t = 0
        # One-shot grace: the first time we observe cancellation, allow
        # exactly one more chunk through (simulating network/buffering
        # slop in a real backend), then stop unconditionally. This must
        # be a one-shot transition, not "re-armed whenever cancelled",
        # or a persistently-cancelled context never actually terminates.
        cancel_grace_used = False
        while t < total_duration_ms:
            # Simulate real synthesis taking wall-clock time per chunk,
            # so cancel() has a realistic window to race against.
            await asyncio.sleep(CHUNK_MS / 1000 * 0.3)

            if context_id in self._cancelled_contexts:
                if not cancel_grace_used:
                    cancel_grace_used = True
                else:
                    return

            duration = min(CHUNK_MS, total_duration_ms - t)
            pcm_bytes = _sine_pcm_chunk(duration, t)
            import base64

            yield AudioChunk(
                context_id=context_id,
                chunk_index=chunk_index,
                pcm_b64=base64.b64encode(pcm_bytes).decode("ascii"),
                t_start_ms=t,
                t_end_ms=t + duration,
            )
            chunk_index += 1
            t += duration

        if context_id not in self._cancelled_contexts:
            yield Done(context_id=context_id, total_duration_ms=total_duration_ms)

        self._cancelled_contexts.discard(context_id)
