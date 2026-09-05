"""Append-only, timestamped event log.

Every component writes here. This file *is* the evidence artifact: the
acceptance harness and RIME_EVIDENCE.md are generated from it, never from
in-memory state.

Event types used across the layer (keep this list in sync with the README):
  provider_active, synth_requested, synth_first_byte, synth_done,
  frames_played, cancel_issued, audible_stop, unit_truncated,
  result_fenced, position_saved, position_restored, timestamps_received
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
