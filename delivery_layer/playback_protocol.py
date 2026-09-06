"""
Wire protocol between server (scheduler/agent) and client (AudioWorklet/SDK).

Versioned, explicit dataclasses in both directions. Nothing in this file
talks to LiveKit, Rime, or the ledger directly -- it is pure data shape.

Convention: all timestamps in this protocol are milliseconds on a *unit-local*
AUDIO clock. t=0 is the FIRST AUDIO SAMPLE of the unit, not the moment
synthesis was requested.

This was corrected during integration: the original convention here said
t=0 == synth_requested. Rime's word timestamps and the worklet's rendered_ms
are both audio-clock values, so anchoring the protocol at the request would
have offset every boundary comparison in ledger.py by the time to first byte
-- a drift that grows with provider latency and never shows up as an error.
Both sides must agree on this anchor.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Union

# 2: UnitStart gained char_start, so a resumed unit can be shown at its real
#    position inside the original clause instead of at character 0.
PROTOCOL_VERSION = 2


# ---------------------------------------------------------------------------
# Shared identifiers
# ---------------------------------------------------------------------------

# turn_id: monotonically increasing int, bumped by the agent on every
#   detected speech-start (interruption). Never reused.
# unit_id: stable clause id from the fixture, e.g. "sec-4b-ii".
# seq: monotonically increasing int *within* a unit, for chunk ordering.


class MessageType(str, Enum):
    # server -> client
    UNIT_START = "unit_start"
    AUDIO_CHUNK = "audio_chunk"
    WORD_TIMESTAMPS = "word_timestamps"
    UNIT_DONE = "unit_done"
    CANCEL = "cancel"

    # client -> server
    PLAYBACK_ACK = "playback_ack"
    FLUSH_ACK = "flush_ack"
    CLIENT_ERROR = "client_error"


# ---------------------------------------------------------------------------
# Server -> Client
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UnitStart:
    """Announces a new unit is about to stream. Client should prepare a
    fresh PCM queue keyed by (turn_id, unit_id).

    char_start is the offset of this unit's text inside the ORIGINAL clause.
    It is 0 for a normal unit. A resumed unit -- id `<unit_id>/resume#<turn>`
    -- carries the offset of the sentence it restarts from, so the client can
    highlight the remainder in place rather than from the top of the clause.
    """

    type: MessageType = field(default=MessageType.UNIT_START, init=False)
    version: int = field(default=PROTOCOL_VERSION, init=False)
    turn_id: int
    unit_id: str
    seq: int
    sample_rate_hz: int
    channels: int
    char_start: int = 0


@dataclass(frozen=True)
class AudioChunk:
    """PCM audio bytes for a unit. pcm_b64 is base64-encoded little-endian
    int16 PCM (matches Rime's /ws3 output format; fake TTS emits the same
    shape). t_start_ms/t_end_ms are the unit-local timeline this chunk
    covers, so the client can compute rendered_ms without decoding audio."""

    type: MessageType = field(default=MessageType.AUDIO_CHUNK, init=False)
    version: int = field(default=PROTOCOL_VERSION, init=False)
    turn_id: int
    unit_id: str
    seq: int
    chunk_index: int
    pcm_b64: str
    t_start_ms: int
    t_end_ms: int


@dataclass(frozen=True)
class WordSpan:
    word: str
    t_start_ms: int
    t_end_ms: int
    char_start: int
    char_end: int


@dataclass(frozen=True)
class WordTimestamps:
    """Word-level alignment for a unit, aligned to text_display (not
    text_spoken -- normalization divergence is absorbed upstream, on the
    synthesis side, before this message is built)."""

    type: MessageType = field(default=MessageType.WORD_TIMESTAMPS, init=False)
    version: int = field(default=PROTOCOL_VERSION, init=False)
    turn_id: int
    unit_id: str
    words: tuple[WordSpan, ...]


@dataclass(frozen=True)
class UnitDone:
    """Signals synthesis is complete for this unit -- not that it was
    heard. Delivery truth comes only from PlaybackAck/FlushAck."""

    type: MessageType = field(default=MessageType.UNIT_DONE, init=False)
    version: int = field(default=PROTOCOL_VERSION, init=False)
    turn_id: int
    unit_id: str
    total_duration_ms: int


@dataclass(frozen=True)
class Cancel:
    """Server tells client: stop everything belonging to turns < turn_id
    (or a specific unit_id, if given). Client must drop queued PCM for
    the cancelled scope and immediately emit a FlushAck."""

    type: MessageType = field(default=MessageType.CANCEL, init=False)
    version: int = field(default=PROTOCOL_VERSION, init=False)
    turn_id: int
    unit_id: Optional[str] = None  # None => cancel all units on this turn


# ---------------------------------------------------------------------------
# Client -> Server
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PlaybackAck:
    """Emitted every ~100ms during normal playback. rendered_ms is the
    count of samples actually rendered by the AudioWorklet, converted to
    ms via sample_rate -- NOT samples enqueued. This is the only source
    of truth for "what was heard"."""

    type: MessageType = field(default=MessageType.PLAYBACK_ACK, init=False)
    version: int = field(default=PROTOCOL_VERSION, init=False)
    turn_id: int
    unit_id: str
    rendered_ms: int
    client_clock_ts: float  # audio-context clock, seconds, monotonic


@dataclass(frozen=True)
class FlushAck:
    """Emitted immediately on flush/interrupt. audible_stop_ts is read
    from the audio hardware clock at the moment playback actually
    stopped, not wall-clock time of the flush call -- this is what
    audible_stop_latency is measured against."""

    type: MessageType = field(default=MessageType.FLUSH_ACK, init=False)
    version: int = field(default=PROTOCOL_VERSION, init=False)
    turn_id: int
    unit_id: str
    rendered_ms: int
    audible_stop_ts: float  # audio-context clock, seconds, monotonic


@dataclass(frozen=True)
class ClientError:
    type: MessageType = field(default=MessageType.CLIENT_ERROR, init=False)
    version: int = field(default=PROTOCOL_VERSION, init=False)
    turn_id: int
    unit_id: Optional[str]
    message: str


ServerMessage = Union[UnitStart, AudioChunk, WordTimestamps, UnitDone, Cancel]
ClientMessage = Union[PlaybackAck, FlushAck, ClientError]


# ---------------------------------------------------------------------------
# (De)serialization helpers -- data channel carries JSON text frames.
# ---------------------------------------------------------------------------

import json
from dataclasses import asdict

_TYPE_TO_CLASS: dict[str, type] = {
    MessageType.UNIT_START: UnitStart,
    MessageType.AUDIO_CHUNK: AudioChunk,
    MessageType.WORD_TIMESTAMPS: WordTimestamps,
    MessageType.UNIT_DONE: UnitDone,
    MessageType.CANCEL: Cancel,
    MessageType.PLAYBACK_ACK: PlaybackAck,
    MessageType.FLUSH_ACK: FlushAck,
    MessageType.CLIENT_ERROR: ClientError,
}


def encode(msg) -> str:
    d = asdict(msg)
    d["type"] = msg.type.value
    if "words" in d:
        d["words"] = [tuple(w) if not isinstance(w, dict) else w for w in d["words"]]
    return json.dumps(d)


def decode(raw: str):
    d = json.loads(raw)
    msg_type = MessageType(d.pop("type"))
    # A version 1 UnitStart has no char_start; the dataclass default of 0 is
    # the correct reading of it, so old traces still decode.
    d.pop("version", None)
    cls = _TYPE_TO_CLASS[msg_type]
    if cls is WordTimestamps:
        d["words"] = tuple(WordSpan(**w) for w in d["words"])
    return cls(**d)
