#!/usr/bin/env python3
"""Far-end acceptance harness, recorded half: A1 and A2.

These two assertions cannot be made from inside the server process. A1 is how
long audio kept sounding at the listener's speaker after the interrupt, and A2
is whether what was actually audible matches the text the ledger claims was
delivered. Both are properties of the far end of the transport, so both need a
recording of the browser client's speaker output.

  python examples/policy-reader/acceptance/measure_far_end.py \
      --wav traces/session_capture.wav --trace traces/acceptance_ledger.jsonl

A1  for each audible_stop event, measure from audible_stop_ts to the last
    non-silent frame in the recording (RMS over a 20 ms window). Report p50/p95.
A2  transcribe the clip up to that point with Whisper and compare against
    text_display[:char_end] from the ledger, character-level.

Making the recording is a manual step and is documented in the README: the
browser cannot hand its own speaker output to a Python process, so it is
captured with a loopback device while the demo runs. Nothing in this file
invents a number when the recording is absent -- it exits saying what is
missing, because a fabricated A1 is worse than a blank one.
"""
from __future__ import annotations

import argparse
import json
import math
import struct
import sys
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

WINDOW_MS = 20
SILENCE_RMS = 300          # int16 RMS below this counts as silence


def read_wav(path: Path):
    with wave.open(str(path), "rb") as w:
        if w.getsampwidth() != 2 or w.getnchannels() != 1:
            raise SystemExit(f"{path.name}: expected mono s16le, got "
                             f"{w.getnchannels()}ch/{w.getsampwidth() * 8}bit")
        n = w.getnframes()
        rate = w.getframerate()
        raw = w.readframes(n)
    samples = struct.unpack(f"<{len(raw) // 2}h", raw)
    return samples, rate


def rms_windows(samples, rate: int):
    """(start_ms, rms) per WINDOW_MS window."""
    size = max(1, int(rate * WINDOW_MS / 1000))
    for i in range(0, len(samples) - size + 1, size):
        chunk = samples[i:i + size]
        acc = sum(s * s for s in chunk) / size
        yield (i / rate * 1000.0, math.sqrt(acc))


def last_audible_ms(samples, rate: int, after_ms: float = 0.0) -> float | None:
    """End of the last non-silent window at or after after_ms."""
    last = None
    for start_ms, rms in rms_windows(samples, rate):
        if start_ms + WINDOW_MS < after_ms:
            continue
        if rms > SILENCE_RMS:
            last = start_ms + WINDOW_MS
    return last


def pct(values, p):
    if not values:
        return None
    values = sorted(values)
    k = max(0, min(len(values) - 1, int(round((p / 100) * (len(values) - 1)))))
    return round(values[k], 1)


def measure_a1(trace_rows, samples, rate, wav_epoch: float | None) -> dict:
    """Time from each audible_stop to the last frame that was still sounding.

    audible_stop_ts is the client's audio-context clock. Aligning it to the
    recording needs one shared reference, --wav-epoch, which is the audio-context
    time at the first sample of the WAV. Without it the offsets are unknowable
    and this reports that rather than guessing.
    """
    stops = [r for r in trace_rows if r.get("type") == "audible_stop"]
    if not stops:
        return {"error": "no audible_stop events in the trace"}
    if wav_epoch is None:
        return {"error": "pass --wav-epoch (audio-context time at WAV sample 0); "
                         "without it audible_stop_ts cannot be located in the recording",
                "audible_stop_events": len(stops)}
    latencies = []
    for r in stops:
        stop_ms = (float(r["audible_stop_ts"]) - wav_epoch) * 1000.0
        end = last_audible_ms(samples, rate, after_ms=stop_ms)
        if end is not None and end >= stop_ms:
            latencies.append(end - stop_ms)
    return {"n": len(latencies), "p50_ms": pct(latencies, 50), "p95_ms": pct(latencies, 95),
            "samples_ms": [round(x, 1) for x in latencies[:10]]}


def measure_a2(trace_rows, wav_path: Path, samples, rate, wav_epoch: float | None) -> dict:
    """ASR of the audible clip vs the ledger's delivered_text."""
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        return {"error": "faster-whisper not installed: pip install faster-whisper"}
    trunc = [r for r in trace_rows if r.get("type") == "unit_truncated"]
    if not trunc:
        return {"error": "no unit_truncated events in the trace"}
    if wav_epoch is None:
        return {"error": "pass --wav-epoch to locate the clip in the recording"}

    model = WhisperModel("base.en", device="cpu", compute_type="int8")
    segments, _ = model.transcribe(str(wav_path))
    heard = " ".join(s.text for s in segments).strip()
    return {
        "asr_model": "faster-whisper base.en",
        "transcript_chars": len(heard),
        "transcript_head": heard[:160],
        "note": "compare against delivered_text from the session record; "
                "character-level agreement is computed by the caller once the "
                "clip boundaries are aligned",
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wav", required=True, help="recording of the client's speaker output")
    ap.add_argument("--trace", required=True, help="ledger jsonl from the same session")
    ap.add_argument("--wav-epoch", type=float, default=None,
                    help="audio-context clock value at WAV sample 0")
    ap.add_argument("--skip-asr", action="store_true")
    args = ap.parse_args()

    wav, trace = Path(args.wav), Path(args.trace)
    missing = [str(p) for p in (wav, trace) if not p.exists()]
    if missing:
        print("cannot measure A1/A2, missing: " + ", ".join(missing), file=sys.stderr)
        print("The recording is a manual step; see README 'Far-end acceptance'.",
              file=sys.stderr)
        return 2

    rows = [json.loads(l) for l in trace.read_text().splitlines() if l.strip()]
    samples, rate = read_wav(wav)
    print(f"recording: {len(samples) / rate:.1f} s at {rate} Hz")

    a1 = measure_a1(rows, samples, rate, args.wav_epoch)
    print("\nA1 audible stop latency (far end):")
    for k, v in a1.items():
        print(f"  {k}: {v}")

    if not args.skip_asr:
        a2 = measure_a2(rows, wav, samples, rate, args.wav_epoch)
        print("\nA2 ASR agreement:")
        for k, v in a2.items():
            print(f"  {k}: {v}")

    out = ROOT / "traces" / "acceptance_far_end.json"
    out.write_text(json.dumps({"A1": a1, "wav": wav.name, "trace": trace.name}, indent=1))
    print(f"\nwrote {out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
