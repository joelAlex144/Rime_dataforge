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

logger = logging.getLogger("scheduler")

MAX_IN_FLIGHT = 3


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


class TTSBackend(Protocol):
    """Structural contract the scheduler depends on. tts/fake.py
    satisfies this today; tts/rime.py will satisfy it once A wires the
    real adapter. Kept here (not imported from tts/fake.py) so the
    scheduler's dependency is on the interface, not a concrete impl."""

    def cancel(self, context_id: str) -> None: ...

    def synth(self, text: str, context_id: str) -> AsyncIterator[object]: ...


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
            self._tts.cancel(ctx_id)

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
            async for event in self._tts.synth(unit.text_spoken, context_id):
                # Check the fence AFTER every await boundary (the async
                # generator's __anext__ is exactly that boundary) --
                # this is the fence's documented usage pattern.
                try:
                    self._fence.check(turn_id, unit_id=unit.unit_id)
                except StaleGeneration:
                    # Already logged as result_fenced by fence.check().
                    # Make sure the backend actually stops, then exit --
                    # do not process this or any further event.
                    self._tts.cancel(context_id)
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
                    pass  # synthesis complete; delivery truth still comes from client acks
        finally:
            self._active_context_by_unit.pop(unit.unit_id, None)
