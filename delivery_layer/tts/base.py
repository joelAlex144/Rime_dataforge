"""Provider-neutral TTS interface.

The layer never talks to a vendor directly; it talks to `TTSProvider`. Rime
is the default (tts/rime.py). `FakeTTS` (tts/fake.py) exists so the ledger,
scheduler and tests run with no network and no key. Any fallback provider
must emit `provider_active` so the active provider is observable in the
event log (brief requirement: fallbacks disclosed, active provider visible).

Stream contract for `synth(text, context_id)`:
  zero or more AudioChunk / Timestamps in any interleaving, then exactly one
  Done — unless `cancel()` was called, in which case the iterator simply
  ends (no Done) and any late server messages for that context are dropped
  and logged as `result_fenced`.

Timestamps are unit-local milliseconds: 0 = first sample of this unit.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import AsyncIterator, Protocol, Union, runtime_checkable


@dataclass(frozen=True)
class AudioChunk:
    context_id: str
    pcm: bytes          # raw signed 16-bit little-endian mono at provider.sample_rate
    seq: int            # 0-based within this context


@dataclass(frozen=True)
class Timestamps:
    context_id: str
    words: list[str]
    start_ms: list[float]
    end_ms: list[float]


@dataclass(frozen=True)
class Done:
    context_id: str
    total_bytes: int
    ttfb_ms: float | None = None
    total_ms: float | None = None


@dataclass(frozen=True)
class TTSError:
    context_id: str | None
    message: str
    raw: dict = field(default_factory=dict)


StreamItem = Union[AudioChunk, Timestamps, Done, TTSError]


@runtime_checkable
class TTSProvider(Protocol):
    name: str
    sample_rate: int

    @property
    def descriptor(self) -> dict:
        """Everything the README must state: model, speaker, lang, endpoint, format, transport."""
        ...

    async def connect(self) -> None: ...

    def synth(self, text: str, context_id: str) -> AsyncIterator[StreamItem]: ...

    async def cancel(self) -> None:
        """Best-effort stop of everything in flight. Must be idempotent and fast."""
        ...

    async def close(self) -> None: ...
