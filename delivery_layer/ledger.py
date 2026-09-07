"""
Append-only event ledger + delivery-boundary resolution.

This is the evidence artifact. Every event type from the architecture
doc gets one JSONL line, timestamped, append-only, never mutated:

    synth_requested, frames_played, cancel_issued, audible_stop,
    unit_truncated, result_fenced, position_saved, position_restored,
    provider_active

At session end, resolve() walks the log and derives:
  - one DeliveryRecord per unit (heard / truncated-at-offset / never_played,
    plus the resolved word-level boundary)
  - a session_record.json summarizing the whole session

Boundary resolution rule (per spec):
  given rendered_ms for a unit and its word map [(word, t_start, t_end,
  char_start, char_end), ...]:
    - the last word with t_end_ms <= rendered_ms is fully delivered
    - a word with t_start_ms < rendered_ms < t_end_ms is "straddling" --
      marked partial, not counted as delivered
    - a unit with no ack at all (never got a PlaybackAck or FlushAck) is
      never_played, regardless of whether synthesis completed
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional, Sequence, Union

from delivery_layer.events import EventLog


class EventType(str, Enum):
    SYNTH_REQUESTED = "synth_requested"
    FRAMES_PLAYED = "frames_played"
    CANCEL_ISSUED = "cancel_issued"
    AUDIBLE_STOP = "audible_stop"
    UNIT_TRUNCATED = "unit_truncated"
    UNIT_SKIPPED = "unit_skipped"          # never sent by design: boilerplate, table_on_request, or jump
    JUMP = "jump"                          # {from_unit, to_unit, reason, turn_id}
    RESULT_FENCED = "result_fenced"
    POSITION_SAVED = "position_saved"
    POSITION_RESTORED = "position_restored"
    PROVIDER_ACTIVE = "provider_active"


class DeliveryStatus(str, Enum):
    HEARD = "heard"                    # fully delivered, no truncation
    TRUNCATED = "truncated_at_offset"  # partially heard, cut mid-unit
    NEVER_PLAYED = "never_played"      # no ack ever received


@dataclass(frozen=True)
class WordSpanRecord:
    word: str
    t_start_ms: int
    t_end_ms: int
    char_start: int
    char_end: int


@dataclass
class DeliveryRecord:
    unit_id: str
    turn_id: int
    status: DeliveryStatus
    rendered_ms: Optional[int]
    delivered_text: str          # text_display truncated at the boundary
    boundary_word_index: Optional[int]   # index of last fully-delivered word, or None
    straddling_word: Optional[str]       # word cut mid-pronunciation, if any


class Ledger:
    """One Ledger per session. Writes are synchronous appends -- cheap,
    and correctness here matters more than write throughput."""

    def __init__(self, events: Union[EventLog, str, Path]):
        """Takes the session's EventLog so one file holds everything.

        The synthesis adapter emits provider_active / synth_requested /
        result_fenced through the same log the ledger writes frames_played /
        unit_truncated / position_saved into, so a trace can be read top to
        bottom without joining two files whose clocks never agreed.

        A path is still accepted and wraps itself in an EventLog, so their
        call sites and any standalone use keep working unchanged.
        """
        if isinstance(events, EventLog):
            self.events = events
        else:
            self.events = EventLog(Path(events), session_id="ledger")
        self.path = self.events.path
        # unit_id -> word map, registered by the synthesis side when
        # WordTimestamps arrives, so resolve() doesn't need a second pass
        # over raw protocol messages.
        self._word_maps: dict[str, Sequence[WordSpanRecord]] = {}
        self._text_display: dict[str, str] = {}
        # Set from Done. A unit whose acks reached its audio length is fully
        # heard even if its last word timing is missing or overshot.
        self._duration_ms: dict[str, int] = {}
        # resumed unit id -> (original unit id, char_start). A resumed unit is
        # a fragment of its original; when the fragment is heard to the end the
        # original has been heard in full, across two turns.
        self._resumed: dict[str, tuple] = {}
        # Highest rendered_ms acked per unit, tracked as events are logged.
        # resolve() re-reads and re-parses the whole file, which is the right
        # shape for running standalone against a committed trace but ruinous in
        # a poll loop: on a slow filesystem it blocks the event loop long enough
        # that the Rime socket's keepalive misses its pong and the connection is
        # dropped with a 1011 ping timeout.
        self._max_rendered: dict[str, int] = {}

    # -- low-level append -------------------------------------------------

    def _write(self, event_type: EventType, **fields) -> None:
        # EventLog stamps ts_ms/wall/session_id and writes the line. Event
        # names are unchanged: every name in EventType already matches the
        # list at the top of delivery_layer/events.py.
        self.events.emit(event_type.value, **fields)

    def close(self) -> None:
        self.events.close()

    def __enter__(self) -> "Ledger":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- registration (not events -- just data resolve() needs later) -----

    def register_unit_text(self, unit_id: str, text_display: str) -> None:
        self._text_display[unit_id] = text_display

    def register_word_map(self, unit_id: str, words: Sequence[WordSpanRecord]) -> None:
        """Last write wins, deliberately.

        Rime emits timestamps per segment rather than once up front, so this is
        called several times per unit and each call carries a cumulative map
        that supersedes the previous one. At interrupt time the newest map may
        still be partial -- that is correct, and resolution stays conservative
        because a word with no timing cannot be counted as delivered.
        """
        self._word_maps[unit_id] = words

    def register_unit_duration(self, unit_id: str, total_duration_ms: int) -> None:
        self._duration_ms[unit_id] = int(total_duration_ms or 0)

    def register_resume(self, resumed_unit_id: str, original_unit_id: str,
                        char_start: int) -> None:
        self._resumed[resumed_unit_id] = (original_unit_id, int(char_start))

    def delivered_char_end(self, unit_id: str) -> int:
        """Resolved delivery boundary for a unit, in text_display chars.

        This is what the resume anchor is computed from: the position the
        listener's acks actually reached, not where synthesis stopped.

        In-memory and allocation-light on purpose -- it is called in a poll
        loop while audio is streaming, and must never touch the disk.
        """
        rendered = self._max_rendered.get(unit_id)
        words = self._word_maps.get(unit_id)
        if rendered is None or not words:
            return 0
        boundary_index, _straddling = self._resolve_boundary(rendered, words)
        if boundary_index is None:
            return 0
        return words[boundary_index].char_end

    # -- event logging, one method per event type --------------------------

    def log_synth_requested(self, *, turn_id: int, unit_id: str, provider: str) -> None:
        self._write(EventType.SYNTH_REQUESTED, turn_id=turn_id, unit_id=unit_id, provider=provider)

    def log_frames_played(self, *, turn_id: int, unit_id: str, rendered_ms: int) -> None:
        self._max_rendered[unit_id] = max(
            self._max_rendered.get(unit_id, 0), int(rendered_ms))
        self._write(EventType.FRAMES_PLAYED, turn_id=turn_id, unit_id=unit_id, rendered_ms=rendered_ms)

    def log_cancel_issued(self, *, turn_id: int, unit_id: Optional[str]) -> None:
        self._write(EventType.CANCEL_ISSUED, turn_id=turn_id, unit_id=unit_id)

    def log_audible_stop(self, *, turn_id: int, unit_id: str, rendered_ms: int, audible_stop_ts: float) -> None:
        self._max_rendered[unit_id] = max(
            self._max_rendered.get(unit_id, 0), int(rendered_ms))
        self._write(
            EventType.AUDIBLE_STOP,
            turn_id=turn_id,
            unit_id=unit_id,
            rendered_ms=rendered_ms,
            audible_stop_ts=audible_stop_ts,
        )

    def log_unit_truncated(self, *, turn_id: int, unit_id: str, rendered_ms: int) -> None:
        self._max_rendered[unit_id] = max(
            self._max_rendered.get(unit_id, 0), int(rendered_ms))
        self._write(EventType.UNIT_TRUNCATED, turn_id=turn_id, unit_id=unit_id, rendered_ms=rendered_ms)

    def log_result_fenced(self, *, issued_turn_id: int, current_turn_id: int, unit_id: Optional[str]) -> None:
        self._write(
            EventType.RESULT_FENCED,
            issued_turn_id=issued_turn_id,
            current_turn_id=current_turn_id,
            unit_id=unit_id,
        )

    def log_position_saved(self, *, turn_id: int, unit_id: str, anchor: str) -> None:
        self._write(EventType.POSITION_SAVED, turn_id=turn_id, unit_id=unit_id, anchor=anchor)

    def log_position_restored(self, *, turn_id: int, unit_id: str, anchor: str) -> None:
        self._write(EventType.POSITION_RESTORED, turn_id=turn_id, unit_id=unit_id, anchor=anchor)

    def log_provider_active(self, *, provider: str, reason: str = "default") -> None:
        """reason distinguishes the default path from a disclosed
        fallback (e.g. reason='rime_unavailable_fallback_to_fake')."""
        self._write(EventType.PROVIDER_ACTIVE, provider=provider, reason=reason)

    # -- resolution ---------------------------------------------------------

    @staticmethod
    def _resolve_boundary(
        rendered_ms: Optional[int], words: Sequence[WordSpanRecord]
    ) -> tuple[Optional[int], Optional[str]]:
        """Returns (boundary_word_index, straddling_word)."""
        if rendered_ms is None or not words:
            return None, None
        boundary_index: Optional[int] = None
        straddling: Optional[str] = None
        for i, w in enumerate(words):
            if w.t_end_ms <= rendered_ms:
                boundary_index = i
            elif w.t_start_ms < rendered_ms < w.t_end_ms:
                straddling = w.word
                break
            else:
                break
        return boundary_index, straddling

    def resolve(self) -> list[DeliveryRecord]:
        """Replay the ledger file and produce one DeliveryRecord per
        unit. Reads from disk (not in-memory events) so this can also be
        run standalone against a committed trace file."""
        events: list[dict] = []
        if self.path is not None and Path(self.path).exists():
            with open(self.path) as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    rec = json.loads(line)
                    # EventLog writes "type"; the delivery side's own older
                    # traces write "event". Accept both so a committed trace
                    # from either half still resolves.
                    if "event" not in rec and "type" in rec:
                        rec["event"] = rec["type"]
                    events.append(rec)
        else:
            events = [{**r, "event": r.get("type")} for r in self.events.records]

        # last-write-wins per unit for rendered_ms; frames_played gives
        # running progress, audible_stop/unit_truncated give the final
        # value at cutoff. We want the max across all of them, since
        # rendered_ms is monotonic non-decreasing within a unit.
        rendered_ms_by_unit: dict[str, int] = {}
        turn_id_by_unit: dict[str, int] = {}
        truncated_units: set[str] = set()
        acked_units: set[str] = set()
        all_units_seen: set[str] = set()

        for ev in events:
            unit_id = ev.get("unit_id")
            if unit_id is None:
                continue
            all_units_seen.add(unit_id)
            if ev["event"] in (
                EventType.FRAMES_PLAYED.value,
                EventType.AUDIBLE_STOP.value,
                EventType.UNIT_TRUNCATED.value,
            ):
                rendered_ms_by_unit[unit_id] = max(
                    rendered_ms_by_unit.get(unit_id, 0), ev.get("rendered_ms", 0)
                )
                acked_units.add(unit_id)
                turn_id_by_unit[unit_id] = ev.get("turn_id", turn_id_by_unit.get(unit_id, -1))
            if ev["event"] in (EventType.AUDIBLE_STOP.value, EventType.UNIT_TRUNCATED.value):
                truncated_units.add(unit_id)
            if ev["event"] == EventType.SYNTH_REQUESTED.value:
                turn_id_by_unit.setdefault(unit_id, ev.get("turn_id", -1))

        records: list[DeliveryRecord] = []
        for unit_id in sorted(all_units_seen):
            words = self._word_maps.get(unit_id, [])
            text_display = self._text_display.get(unit_id, "")
            turn_id = turn_id_by_unit.get(unit_id, -1)

            if unit_id not in acked_units:
                records.append(
                    DeliveryRecord(
                        unit_id=unit_id,
                        turn_id=turn_id,
                        status=DeliveryStatus.NEVER_PLAYED,
                        rendered_ms=None,
                        delivered_text="",
                        boundary_word_index=None,
                        straddling_word=None,
                    )
                )
                continue

            rendered_ms = rendered_ms_by_unit[unit_id]
            boundary_index, straddling = self._resolve_boundary(rendered_ms, words)

            if boundary_index is not None:
                last_delivered_char_end = words[boundary_index].char_end
                delivered_text = text_display[:last_delivered_char_end]
            else:
                delivered_text = ""

            # Done seen and the acks reached the audio length -> fully heard,
            # whatever the word map says. Word timings can overshoot the audio
            # by ~100 ms, which would otherwise leave a completed unit looking
            # one word short forever.
            duration = self._duration_ms.get(unit_id)
            played_to_end = duration is not None and duration > 0 and rendered_ms >= duration
            if played_to_end:
                status = DeliveryStatus.HEARD
                delivered_text = text_display
            elif unit_id in truncated_units:
                status = DeliveryStatus.TRUNCATED
            else:
                status = DeliveryStatus.HEARD

            records.append(
                DeliveryRecord(
                    unit_id=unit_id,
                    turn_id=turn_id,
                    status=status,
                    rendered_ms=rendered_ms,
                    delivered_text=delivered_text,
                    boundary_word_index=boundary_index,
                    straddling_word=straddling,
                )
            )

        # A resumed fragment that was heard to the end completes its original:
        # the listener has now heard the whole clause, across two turns.
        by_id = {r.unit_id: r for r in records}
        for resumed_id, (original_id, _char_start) in self._resumed.items():
            frag = by_id.get(resumed_id)
            orig = by_id.get(original_id)
            if frag is None or orig is None:
                continue
            if frag.status == DeliveryStatus.HEARD and orig.status != DeliveryStatus.HEARD:
                by_id[original_id] = DeliveryRecord(
                    unit_id=orig.unit_id, turn_id=orig.turn_id,
                    status=DeliveryStatus.HEARD, rendered_ms=orig.rendered_ms,
                    delivered_text=self._text_display.get(original_id, orig.delivered_text),
                    boundary_word_index=orig.boundary_word_index,
                    straddling_word=None,
                )
        return [by_id[r.unit_id] for r in records]

    def write_session_record(self, out_path: str | Path) -> dict:
        records = self.resolve()
        summary = {
            "generated_at": time.time(),
            "protocol_version": 1,
            "unit_count": len(records),
            "heard": sum(1 for r in records if r.status == DeliveryStatus.HEARD),
            "truncated": sum(1 for r in records if r.status == DeliveryStatus.TRUNCATED),
            "never_played": sum(1 for r in records if r.status == DeliveryStatus.NEVER_PLAYED),
            "units": [
                {
                    **asdict(r),
                    "status": r.status.value,
                }
                for r in records
            ],
        }
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(summary, indent=2))
        return summary
