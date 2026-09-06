"""
Scheduler.

Pulls document units in order, keeps a bounded number of synth requests
in flight (2-3, per spec) so playback stays ahead of the client without
over-committing to a TTS backend that might get cancelled mid-batch,
tags every chunk with (turn_id, unit_id, seq) for the fence to police,
and stops cleanly the instant a cancel comes through.

Talks to a TTS backend only through the shared interface (synth/cancel
-- see tts/fake.py's docstring for the exact shape). Never imports
tts.rime directly; the concrete backend is injected, so swapping fake
for real Rime is a constructor argument, not a code change.

Every unit dispatch is logged to the ledger as synth_requested *before*
synthesis starts -- this is what makes a cancelled-before-any-audio
unit resolvable as never_played rather than simply absent from the log.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import AsyncIterator, Callable, Optional, Protocol

from delivery_layer.fence import Fence, StaleGeneration
from delivery_layer.ledger import Ledger, WordSpanRecord
from delivery_layer.tts.tracked import TrackedUnit

logger = logging.getLogger("scheduler")

# 1, measured against live Rime. Two reasons, in order of severity.
#
# Rime's /ws3 will not maintain two simultaneous contextIds on one socket:
# "the events will contain the most recent context ID at the time that audio was
# requested". With a cap of 2 the scheduler issues unit N+1 while unit N is still
# streaming, and the live run showed the consequence exactly -- sec-5b-i received
# no first-byte, no timestamps and no Done at all, while sec-5b-ii took the
# stream. It fails silently: no error, just a clause the listener never hears.
# The fake multiplexes happily, so this is invisible until you run against Rime.
#
# Separately, `clear` does not stop audio already synthesised (23 s measured in
# preflight), so every extra in-flight unit is also more audio to fence and throw
# away on an interruption.
#
# Getting the lookahead back means one socket per in-flight context, i.e. a
# connection pool in tts/rime.py. That is the right fix and is recorded in
# docs/HANDOFF_TO_DELIVERY.md; until then correctness beats the buffer.
MAX_IN_FLIGHT = 1


@dataclass(frozen=True)
class Unit:
    """One clause/speech-unit from the fixture. Mirrors the schema
    agreed with the synthesis side: stable unit_id, human-readable
    label for re-entry cues, and both text forms (display vs spoken --
    normalization divergence is handled upstream of us)."""

    unit_id: str
    order: int
    text_display: str
    text_spoken: str
    clause_label: Optional[str] = None
    # Added for the synthesis seam: the normalizer's display<->spoken span map
    # is what lets word timings be aligned to text_display, and char_start is
    # non-zero for a resumed unit so its coverage is reported in the original
    # unit's coordinates. Both default to the previous behaviour.
    spoken_map: tuple = ()
    sentences: tuple = ()
    char_start: int = 0


class TTSBackend(Protocol):
    """Structural contract the scheduler depends on. tts/fake.py
    satisfies this today; tts/rime.py will satisfy it once A wires the
    real adapter. Kept here (not imported from tts/fake.py) so the
    scheduler's dependency is on the interface, not a concrete impl."""

    def cancel(self, context_id: str) -> None: ...

    def synth(self, unit: object, context_id: str) -> AsyncIterator[object]: ...


def _cancel(tts: object, context_id: str) -> None:
    """Cancel from sync code. TrackedTTS.cancel is a coroutine (the Rime
    adapter has to send on a socket), so it exposes cancel_nowait for these
    call sites. A backend with a plain sync cancel still works."""
    nowait = getattr(tts, "cancel_nowait", None)
    if nowait is not None:
        nowait(context_id)
    else:
        tts.cancel(context_id)


# Callback types the scheduler drives -- kept generic (not tied to
# playback_protocol's dataclasses directly) so this module doesn't need
# a LiveKit/data-channel dependency to be unit-tested.
OnAudioChunk = Callable[[str, int, str, int, int, int], "asyncio.Future | None"]
# (unit_id, turn_id, pcm_b64, seq, t_start_ms, t_end_ms) -> awaitable or None


class Scheduler:
    def __init__(
        self,
        *,
        fence: Fence,
        ledger: Ledger,
        tts: TTSBackend,
        provider_name: str,
        send_chunk: OnAudioChunk,
        max_in_flight: int = MAX_IN_FLIGHT,
    ) -> None:
        self._fence = fence
        self._ledger = ledger
        self._tts = tts
        self._provider_name = provider_name
        self._send_chunk = send_chunk
        self._max_in_flight = max_in_flight
        self._in_flight: set[str] = set()  # context_ids currently synthesizing
        self._active_context_by_unit: dict[str, str] = {}
        self._stopped = False

    def _context_id(self, turn_id: int, unit_id: str) -> str:
        # context_id must be unique per (turn, unit) so a stale
        # generation's cancel() can never accidentally affect a fresh
        # dispatch of the same unit_id under a later turn.
        return f"t{turn_id}:{unit_id}"

    async def stop(self, *, turn_id: int, unit_id: Optional[str] = None) -> None:
        """Cancel in-flight synthesis. unit_id=None cancels everything
        currently tracked (a full-turn cancel); otherwise cancels just
        that unit's active context, if any."""
        self._ledger.log_cancel_issued(turn_id=turn_id, unit_id=unit_id)
        targets = (
            list(self._active_context_by_unit.items())
            if unit_id is None
            else [(unit_id, self._active_context_by_unit[unit_id])]
            if unit_id in self._active_context_by_unit
            else []
        )
        for u_id, ctx_id in targets:
            _cancel(self._tts, ctx_id)

    async def run(self, units: list[Unit], *, turn_id: int) -> None:
        """Drives units through synthesis in document order, respecting
        max_in_flight. Raises StaleGeneration (propagated from fence
        checks) if the turn advances mid-run -- callers should treat
        that as a normal, expected way for this coroutine to end, not
        an error to log."""
        semaphore = asyncio.Semaphore(self._max_in_flight)
        tasks = []
        for unit in sorted(units, key=lambda u: u.order):
            await semaphore.acquire()
            self._fence.check(turn_id)  # bail out promptly if we've been superseded

            async def _run_one(u: Unit, sem: asyncio.Semaphore = semaphore) -> None:
                try:
                    await self._synth_unit(u, turn_id=turn_id)
                finally:
                    sem.release()

            tasks.append(asyncio.create_task(_run_one(unit)))

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=False)

    async def _synth_unit(self, unit: Unit, *, turn_id: int) -> None:
        context_id = self._context_id(turn_id, unit.unit_id)
        self._active_context_by_unit[unit.unit_id] = context_id
        self._ledger.register_unit_text(unit.unit_id, unit.text_display)
        self._ledger.log_synth_requested(
            turn_id=turn_id, unit_id=unit.unit_id, provider=self._provider_name
        )

        seq = 0
        try:
            tracked_unit = TrackedUnit(
                unit_id=unit.unit_id, text_display=unit.text_display,
                text_spoken=unit.text_spoken, spoken_map=tuple(unit.spoken_map),
                char_start=unit.char_start,
            )
            async for event in self._tts.synth(tracked_unit, context_id):
                # Check the fence AFTER every await boundary (the async
                # generator's __anext__ is exactly that boundary) --
                # this is the fence's documented usage pattern.
                try:
                    self._fence.check(turn_id, unit_id=unit.unit_id)
                except StaleGeneration:
                    # Already logged as result_fenced by fence.check().
                    # Make sure the backend actually stops, then exit --
                    # do not process this or any further event.
                    _cancel(self._tts, context_id)
                    return

                event_type = type(event).__name__
                if event_type == "Timestamps":
                    words = tuple(
                        WordSpanRecord(
                            word=w.word,
                            t_start_ms=w.t_start_ms,
                            t_end_ms=w.t_end_ms,
                            char_start=w.char_start,
                            char_end=w.char_end,
                        )
                        for w in event.words
                    )
                    self._ledger.register_word_map(unit.unit_id, words)
                elif event_type == "AudioChunk":
                    maybe_awaitable = self._send_chunk(
                        unit.unit_id,
                        turn_id,
                        event.pcm_b64,
                        seq,
                        event.t_start_ms,
                        event.t_end_ms,
                    )
                    if maybe_awaitable is not None:
                        await maybe_awaitable
                    seq += 1
                elif event_type == "Done":
                    # Synthesis complete. Delivery truth still comes from client
                    # acks, but the ledger needs the audio length to tell a unit
                    # that played to the end from one cut a word short.
                    self._ledger.register_unit_duration(
                        unit.unit_id, getattr(event, "total_duration_ms", 0)
                    )
        finally:
            self._active_context_by_unit.pop(unit.unit_id, None)
