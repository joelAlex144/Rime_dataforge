"""Append-only, timestamped event log.

Every component writes here. This file *is* the evidence artifact: the
acceptance harness and RIME_EVIDENCE.md are generated from it, never from
in-memory state.

Both halves of the layer write here. The synthesis adapter emits the
provider and stream events; delivery_layer/ledger.py emits the delivery
events through this same log, so one file answers "what was heard" without
joining two files whose clocks never agreed.

Event types used across the layer (keep this list in sync with the README):
  synthesis side : provider_active, synth_requested, synth_first_byte,
                   synth_done, timestamps_received, result_fenced
  delivery side  : frames_played, cancel_issued, audible_stop,
                   unit_truncated, unit_skipped, position_saved, position_restored, jump

`unit_skipped` is written once per skipped clause at session start, with
`reason: boilerplate` (page furniture, registration lines, placeholders) or
`reason: table_on_request` (table rows that are spoken only when asked for),
so the per-session record of a document is complete: every clause is heard,
truncated, never sent, or skipped -- nothing is silently absent.

Every name in delivery_layer.ledger.EventType already matched a name here,
so nothing had to be renamed when the two halves were joined.

`jump` {from_unit, to_unit, reason, turn_id} is written once per jump (a topic
chip, a spoken topic, the spoiler-gate offer, "skip it", "go back"). A jump is
the interruption path with a different resume target: the unit sounding is
truncated at the client boundary, every readable clause between the cut and
the target is `unit_skipped` with `reason: jump`, `position_saved` (label
before_jump) records where it left, `position_restored` (reason jump) the
target, and a cue unit is spoken before the target.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Optional


class EventLog:
    def __init__(self, path: Optional[os.PathLike | str] = None, session_id: str = "local") -> None:
        self.path = Path(path) if path else None
        self.session_id = session_id
        self._lock = threading.Lock()
        self.records: list[dict[str, Any]] = []
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = self.path.open("a", encoding="utf-8")
        else:
            self._fh = None

    @staticmethod
    def now_ms() -> float:
        return time.monotonic() * 1000.0

    def emit(self, type: str, **fields: Any) -> dict[str, Any]:
        rec = {
            "ts_ms": round(self.now_ms(), 3),
            "wall": time.time(),
            "session_id": self.session_id,
            "type": type,
            **fields,
        }
        with self._lock:
            self.records.append(rec)
            if self._fh:
                self._fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                self._fh.flush()
        return rec

    def of_type(self, type: str) -> list[dict[str, Any]]:
        return [r for r in self.records if r["type"] == type]

    def close(self) -> None:
        if self._fh:
            self._fh.close()
            self._fh = None
