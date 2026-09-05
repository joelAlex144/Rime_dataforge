#!/usr/bin/env python3
"""Rime synthesis latency bench: 50 fixture units, cold vs warm.

  cold  = fresh WebSocket per unit (connect + first synth)     -> N_COLD units
  warm  = one persistent connection, units back to back        -> N_WARM units

Records per unit: chars, ttfb_ms (request -> first audio byte), total_ms
(request -> Done), audio_ms (from byte count), rtf (total_ms / audio_ms).
Reports p50 / p95 / max per condition. Never reports a best case.

Outputs:
  traces/latency_bench_<utc>.jsonl   raw per-unit rows + provider descriptor
  traces/latency_bench_<utc>.json    summary (what goes in RIME_EVIDENCE.md)

These numbers are *synthesis* latency measured at our server, not audible
latency at the listener. Audible-stop latency is measured by the acceptance
harness at the far end of the transport and reported separately.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from delivery_layer.events import EventLog                      # noqa: E402
from delivery_layer.tts.base import AudioChunk, Done, TTSError  # noqa: E402
from delivery_layer.tts.rime import RimeConfig, RimeTTS          # noqa: E402

FIXTURE = ROOT / "examples" / "policy-reader" / "fixtures" / "policy.json"


def pct(xs: list[float], p: float) -> float:
    if not xs:
        return float("nan")
    xs = sorted(xs)
    k = (len(xs) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def summarise(rows: list[dict]) -> dict:
    out = {"n": len(rows)}
    for key in ("ttfb_ms", "total_ms", "rtf"):
        vals = [r[key] for r in rows if r.get(key) is not None]
        out[key] = {"p50": round(pct(vals, 0.5), 1), "p95": round(pct(vals, 0.95), 1),
                    "max": round(max(vals), 1) if vals else None, "mean": round(statistics.fmean(vals), 1) if vals else None}
    return out


async def synth_one(tts: RimeTTS, text: str, cid: str) -> dict:
    t0 = time.monotonic()
    nbytes, ttfb = 0, None
    async for item in tts.synth(text, cid):
        if isinstance(item, AudioChunk):
            if ttfb is None:
                ttfb = (time.monotonic() - t0) * 1000
            nbytes += len(item.pcm)
        elif isinstance(item, TTSError):
            return {"context_id": cid, "error": item.message}
        elif isinstance(item, Done):
            break
    total = (time.monotonic() - t0) * 1000
    audio_ms = nbytes / 2 / tts.sample_rate * 1000
    return {"context_id": cid, "chars": len(text), "ttfb_ms": round(ttfb, 1) if ttfb else None,
            "total_ms": round(total, 1), "audio_ms": round(audio_ms, 1),
            "rtf": round(total / audio_ms, 3) if audio_ms else None}


async def run(args) -> None:
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    log_path = ROOT / "traces" / f"latency_bench_{stamp}.jsonl"
    events = EventLog(log_path, session_id=f"bench-{stamp}")
    cfg = RimeConfig.from_env()
    clauses = json.loads(FIXTURE.read_text())["clauses"]
    units = clauses[args.offset: args.offset + args.n]
    if len(units) < args.n:
        print(f"only {len(units)} units available", file=sys.stderr)

    cold_rows, warm_rows = [], []

    # ---- cold: new connection per unit
    for c in units[: args.cold]:
        tts = RimeTTS(cfg, events)
        t_conn = time.monotonic()
        await tts.connect()
        row = await synth_one(tts, c["text_spoken"], f"{c['id']}#cold")
        row["connect_ms"] = round((time.monotonic() - t_conn) * 1000 - row["total_ms"], 1)
        row["condition"] = "cold"
        events.emit("bench_row", **row)
        cold_rows.append(row)
        await tts.close()
        print(f"cold  {c['id']:<12} ttfb {row.get('ttfb_ms')} ms  total {row['total_ms']} ms  audio {row.get('audio_ms')} ms")

    # ---- warm: one persistent connection
    tts = RimeTTS(cfg, events)
    await tts.connect()
    for c in units:
        row = await synth_one(tts, c["text_spoken"], f"{c['id']}#warm")
        row["condition"] = "warm"
        events.emit("bench_row", **row)
        warm_rows.append(row)
        print(f"warm  {c['id']:<12} ttfb {row.get('ttfb_ms')} ms  total {row['total_ms']} ms  audio {row.get('audio_ms')} ms")
    await tts.close()

    summary = {
        "run_at": stamp,
        "provider": tts.descriptor,
        "fixture": str(FIXTURE.relative_to(ROOT)),
        "units": [c["id"] for c in units],
        "cold": summarise([r for r in cold_rows if "error" not in r]),
        "warm": summarise([r for r in warm_rows if "error" not in r]),
        "errors": [r for r in cold_rows + warm_rows if "error" in r],
        "measured_at": "server (synthesis only; not audible latency at the listener)",
        "raw": str(log_path.relative_to(ROOT)),
    }
    out = ROOT / "traces" / f"latency_bench_{stamp}.json"
    out.write_text(json.dumps(summary, indent=1))
    events.close()
    print(json.dumps({k: summary[k] for k in ("cold", "warm")}, indent=1))
    print(f"-> {out.relative_to(ROOT)}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=50, help="warm units")
    ap.add_argument("--cold", type=int, default=10, help="cold units (fresh connection each)")
    ap.add_argument("--offset", type=int, default=20, help="first fixture index to use")
    asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    main()
