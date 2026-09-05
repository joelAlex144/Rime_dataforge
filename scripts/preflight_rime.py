#!/usr/bin/env python3
"""Preflight the exact shipped Rime path. Exit non-zero on any failed assertion.

Opens /ws3 with the same RimeConfig the agent uses, synthesises ONE clause
from the fixture, and asserts:
  1. provider_active was emitted with modelId/speaker/lang/format/rate/endpoint
  2. at least one PCM chunk arrived and total bytes are even (s16le)
  3. a timestamps message arrived, arrays are index-aligned, non-decreasing
  4. audio duration implied by bytes ≈ last word end (within 1.5 s) — catches
     a wrong samplingRate or a silently swapped audioFormat
  5. the word map built from those timestamps covers the clause end-to-end
  6. (optional, --clear) how many bytes arrive AFTER `clear` — the fence evidence

Writes traces/preflight_<utc>.jsonl (event log) and traces/preflight_<utc>.wav.
Run before every demo recording and before submission.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from delivery_layer.events import EventLog                      # noqa: E402
from delivery_layer.normalize import normalize_with_map          # noqa: E402
from delivery_layer.tts.base import AudioChunk, Done, Timestamps, TTSError  # noqa: E402
from delivery_layer.tts.rime import RimeConfig, RimeTTS          # noqa: E402
from delivery_layer.wordmap import build_word_map                # noqa: E402

FIXTURE = ROOT / "examples" / "policy-reader" / "fixtures" / "policy.json"


def fail(msg: str) -> None:
    print(f"PREFLIGHT FAIL: {msg}", file=sys.stderr)
    sys.exit(1)


async def run(args) -> None:
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    events = EventLog(ROOT / "traces" / f"preflight_{stamp}.jsonl", session_id=f"preflight-{stamp}")
    cfg = RimeConfig.from_env()
    tts = RimeTTS(cfg, events)

    clauses = json.loads(FIXTURE.read_text())["clauses"]
    clause = next(c for c in clauses if c["id"] == args.unit)
    print(f"unit {clause['id']}: {clause['text_display'][:80]}...")

    await tts.connect()
    active = events.of_type("provider_active")
    if not active:
        fail("no provider_active event")
    for k in ("modelId", "speaker", "lang", "audioFormat", "samplingRate", "endpoint"):
        if k not in active[0]:
            fail(f"provider_active missing {k}")
    print("provider_active:", {k: active[0][k] for k in ("provider", "modelId", "speaker", "lang", "audioFormat", "samplingRate", "endpoint")})

    pcm = bytearray()
    ts: Timestamps | None = None
    done: Done | None = None
    t0 = time.monotonic()
    async for item in tts.synth(clause["text_spoken"], f"{clause['id']}#preflight"):
        if isinstance(item, AudioChunk):
            pcm.extend(item.pcm)
        elif isinstance(item, Timestamps):
            ts = item if ts is None else Timestamps(item.context_id, ts.words + item.words,
                                                    ts.start_ms + item.start_ms, ts.end_ms + item.end_ms)
        elif isinstance(item, TTSError):
            fail(f"provider error: {item.message}")
        elif isinstance(item, Done):
            done = item
    wall_ms = (time.monotonic() - t0) * 1000

    if done is None:
        fail("stream ended without Done")
    if len(pcm) == 0:
        fail("no audio bytes")
    if len(pcm) % 2:
        fail("odd PCM byte count — not s16le?")
    if pcm[:4] == b"RIFF":
        fail("got a WAV header: audioFormat was not honoured as raw pcm")
    audio_ms = len(pcm) / 2 / cfg.sampling_rate * 1000
    print(f"audio: {len(pcm)} bytes = {audio_ms:.0f} ms @ {cfg.sampling_rate} Hz; ttfb {done.ttfb_ms} ms; wall {wall_ms:.0f} ms")

    if ts is None or not ts.words:
        fail("no timestamps received (lang must be en/es for word timestamps)")
    if not (len(ts.words) == len(ts.start_ms) == len(ts.end_ms)):
        fail("timestamp arrays not index-aligned")
    if any(b < a for a, b in zip(ts.start_ms, ts.start_ms[1:])):
        fail("timestamps not monotonic — check RIME_TIMESTAMP_CLOCK")
    if ts.start_ms[0] > 2000 and cfg.timestamp_clock == "per_context":
        fail(f"first word starts at {ts.start_ms[0]:.0f} ms; clock looks cumulative — set RIME_TIMESTAMP_CLOCK=cumulative")
    drift = abs(audio_ms - ts.end_ms[-1])
    print(f"timestamps: {len(ts.words)} words, last end {ts.end_ms[-1]:.0f} ms, drift vs audio {drift:.0f} ms")
    if drift > 1500:
        fail(f"audio/timestamp drift {drift:.0f} ms > 1500 ms — samplingRate or format mismatch")

    spoken, segs = normalize_with_map(clause["text_display"])
    wm = build_word_map(clause["id"], clause["text_display"], segs, ts.words, ts.start_ms, ts.end_ms)
    est = sum(1 for s in wm.spans if s.estimated)
    print(f"word map: {len(wm.spans)} spans, {est} interpolated; boundary@50% -> {wm.offset_at(audio_ms/2)}/{len(clause['text_display'])} chars")
    if wm.spans[-1].char_end != len(clause["text_display"]):
        fail("word map does not reach end of text_display")
    if est > len(wm.spans) * 0.3:
        fail(f"{est}/{len(wm.spans)} spans interpolated — alignment is broken for this speaker")

    wav_path = ROOT / "traces" / f"preflight_{stamp}.wav"
    with wave.open(str(wav_path), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(cfg.sampling_rate); w.writeframes(bytes(pcm))
    print(f"wrote {wav_path.relative_to(ROOT)} — listen to it; numbers must be right.")

    if args.clear:
        # Fence evidence: start a long unit, cancel after first chunk, count leakage.
        long_text = " ".join(c["text_spoken"] for c in clauses[10:14])
        leaked_before = tts.fenced_bytes
        gen_started = time.monotonic()
        async for item in tts.synth(long_text, "preflight#clear"):
            if isinstance(item, AudioChunk):
                await tts.cancel()
                break
        await asyncio.sleep(args.clear_wait)
        leaked = tts.fenced_bytes - leaked_before
        leaked_ms = leaked / 2 / cfg.sampling_rate * 1000
        events.emit("clear_leak_measured", bytes_after_clear=leaked, ms_after_clear=round(leaked_ms, 1),
                    wait_s=args.clear_wait)
        print(f"after clear: {leaked} bytes ({leaked_ms:.0f} ms of audio) arrived and were fenced. "
              f"This is the floor `clear` leaves; audible stop must come from the client flush.")

    await tts.close()
    events.emit("preflight_ok", unit=clause["id"], audio_ms=round(audio_ms, 1), words=len(ts.words))
    events.close()
    print(f"PREFLIGHT OK -> {events.path.relative_to(ROOT)}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--unit", default="sec-4b-vii", help="clause id to synthesise")
    ap.add_argument("--clear", action="store_true", help="also measure audio leakage after `clear`")
    ap.add_argument("--clear-wait", type=float, default=6.0)
    asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    main()
