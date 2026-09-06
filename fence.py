"""
Generation fencing.

A single "cancelled" boolean breaks under nested interruptions: cancel
turn 1, start turn 2, cancel turn 2 -- a boolean can't tell you turn 1's
straggler and turn 2's straggler apart. So the fence is a monotonic
counter, and every in-flight thing is stamped with the turn_id that was
current when it was issued. Anything that resolves later gets checked
against the *current* turn_id, not the one it started with.

check() is meant to be called at every await boundary in the synth/
playback pipeline (after an await on the TTS stream, after an await on
a network send, etc.) -- anywhere a coroutine resumes after ceding
control, the world may have moved on underneath it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("fence")


class StaleGeneration(Exception):
    """Raised by check() when the caller's turn_id has been superseded.
    Callers should catch this, stop what they were doing, and let the
    fence's own logging record the result_fenced event -- do not also
    log it at the call site, or the ledger gets duplicate entries."""

    def __init__(self, issued_turn_id: int, current_turn_id: int, unit_id: Optional[str]):
        self.issued_turn_id = issued_turn_id
        self.current_turn_id = current_turn_id
        self.unit_id = unit_id
        super().__init__(
            f"stale generation: issued at turn {issued_turn_id}, "
            f"current turn is {current_turn_id} (unit={unit_id})"
        )


@dataclass
class Fence:
    """One Fence per session. Not thread-safe across processes -- this
    runs inside a single asyncio event loop per agent session."""

    _turn_id: int = field(default=0, init=False)
    _ledger: Optional[object] = field(default=None, init=False, repr=False)

    def attach_ledger(self, ledger) -> None:
        """Optional: wire a Ledger instance so result_fenced events get
        recorded automatically. Kept optional so fence.py has no hard
        dependency on ledger.py and can be unit-tested standalone."""
        self._ledger = ledger

    @property
    def current_turn_id(self) -> int:
        return self._turn_id

    def bump(self) -> int:
        """Called on detected speech-start (an interruption). Returns
        the new turn_id -- callers should stamp all newly-issued work
        with this value."""
        self._turn_id += 1
        logger.info("fence bump -> turn_id=%d", self._turn_id)
        return self._turn_id

    def stamp(self) -> int:
        """Read-only variant for issuing work without bumping (e.g. the
        very first turn, or a non-interrupting new unit)."""
        return self._turn_id

    def check(self, issued_turn_id: int, *, unit_id: Optional[str] = None) -> None:
        """Raise StaleGeneration if issued_turn_id no longer matches the
        current turn. Call this immediately after every await boundary
        in code that was stamped with issued_turn_id."""
        current = self._turn_id
        if issued_turn_id != current:
            if self._ledger is not None:
                self._ledger.log_result_fenced(
                    issued_turn_id=issued_turn_id,
                    current_turn_id=current,
                    unit_id=unit_id,
                )
            raise StaleGeneration(issued_turn_id, current, unit_id)

    def is_current(self, issued_turn_id: int) -> bool:
        """Non-raising check, for call sites that want to branch instead
        of unwinding via exception (e.g. a tight loop deciding whether
        to yield the next chunk)."""
        return issued_turn_id == self._turn_id
