#!/usr/bin/env python3
"""Number round-trip: golden set -> normalizer -> Rime -> Whisper -> compare.

For each row in tests/numbers.jsonl:
  1. normalize(display) must equal the committed `spoken` (offline assertion)
  2. synthesise `spoken` with the shipped Rime config, save WAV
  3. transcribe with Whisper (faster-whisper if installed, else openai-whisper)
  4. normalise the transcript with the SAME normalizer (Whisper writes "$1,000"
     and "2.5%" back as digits) and compare token-wise to `spoken`

Score = token-level agreement after normalisation. A row passes if every
*number-bearing* token matches; filler differences ("a"/"the") are ignored.

This is a correctness floor, not a claim: it tells us whether the shipped
voice reads "4(b)(ii)" and "$1,842.00" the way the document means them.

Outputs traces/number_roundtrip_<utc>.jsonl and .json, plus traces/numbers/*.wav.
Optional --textnorm calls Rime's text-normalisation HTTP endpoint (set
RIME_TEXTNORM_URL) and records its output next to ours for comparison only.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import wave
from difflib import SequenceMatcher
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from delivery_layer.events import EventLog                      # noqa: E402
from delivery_layer.normalize import normalize                   # noqa: E402
from delivery_layer.tts.base import AudioChunk, Done, TTSError  # noqa: E402
from delivery_layer.tts.rime import RimeConfig, RimeTTS          # noqa: E402
from delivery_layer.wordmap import clean_tokens                  # noqa: E402

GOLDEN = ROOT / "tests" / "numbers.jsonl"
_NUMBERISH = set("""zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen
fifteen sixteen seventeen eighteen nineteen twenty thirty forty fifty sixty seventy eighty ninety hundred
thousand million billion first second third fourth fifth sixth seventh eighth ninth tenth eleventh twelfth
thirteenth fourteenth fifteenth twentieth thirtieth point percent dollars dollar cents cent oh""".split())


def load_golden() -> list[dict]:
    return [json.loads(l) for l in GOLDEN.read_text().splitlines() if l.strip()]


def make_transcriber(model_size: str):
    try:
        from faster_whisper import WhisperModel
        m = WhisperModel(model_size, compute_type="int8")

        def tr(path: str) -> str:
            segs, _ = m.transcribe(path, language="en", beam_size=5)
            return " ".join(s.text for s in segs).strip()
        return tr, f"faster-whisper/{model_size}"
    except ImportError:
        pass
    try:
        import whisper
        m = whisper.load_model(model_size)
        return (lambda p: m.transcribe(p, language="en")["text"].strip()), f"openai-whisper/{model_size}"
    except ImportError:
        return None, None


def compare(expected_spoken: str, transcript: str) -> dict:
    exp = clean_tokens(expected_spoken)
    got = clean_tokens(normalize(transcript))
    sm = SequenceMatcher(a=exp, b=got, autojunk=False)
    missing_numbers = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        for t in exp[i1:i2]:
            if t in _NUMBERISH or t.isdigit() or len(t) == 1:  # single letters: "b", "H", "O"
                missing_numbers.append(t)
    return {"expected_tokens": exp, "got_tokens": got, "ratio": round(sm.ratio(), 3),
            "number_tokens_missed": missing_numbers, "pass": not missing_numbers}


async def synth_to_wav(tts: RimeTTS, text: str, cid: str, path: Path) -> dict:
    pcm = bytearray()
    t0 = time.monotonic()
    async for item in tts.synth(text, cid):
        if isinstance(item, AudioChunk):
            pcm.extend(item.pcm)
        elif isinstance(item, TTSError):
            return {"error": item.message}
        elif isinstance(item, Done):
            break
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(tts.sample_rate); w.writeframes(bytes(pcm))
    return {"bytes": len(pcm), "ms": round((time.monotonic() - t0) * 1000, 1)}


def rime_textnorm(text: str) -> str | None:
    url = os.environ.get("RIME_TEXTNORM_URL")
    if not url:
        return None
    import requests
    r = requests.post(url, headers={"Authorization": f"Bearer {os.environ['RIME_API_KEY']}",
                                    "Content-Type": "application/json"},
                      json={"text": text}, timeout=30)
    if r.status_code != 200:
        return f"<http {r.status_code}>"
    body = r.json()
    return body.get("text") or body.get("normalized") or json.dumps(body)


async def run(args) -> int:
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    events = EventLog(ROOT / "traces" / f"number_roundtrip_{stamp}.jsonl", session_id=f"numbers-{stamp}")
    rows = load_golden()

    # 1. offline: normalizer must reproduce the golden spoken form
    bad = [(r["display"], normalize(r["display"]), r["spoken"]) for r in rows if normalize(r["display"]) != r["spoken"]]
    if bad:
        for d, got, exp in bad:
            print(f"NORMALIZER MISMATCH\n  display: {d}\n  got:     {got}\n  want:    {exp}", file=sys.stderr)
        return 1
    print(f"normalizer reproduces all {len(rows)} golden rows")
    if args.offline:
        return 0

    transcribe, asr_name = make_transcriber(args.whisper)
    if transcribe is None:
        print("no whisper backend (pip install faster-whisper); synthesising only", file=sys.stderr)

    cfg = RimeConfig.from_env()
    tts = RimeTTS(cfg, events)
    await tts.connect()
    wav_dir = ROOT / "traces" / "numbers"
    wav_dir.mkdir(parents=True, exist_ok=True)

    results = []
    for i, r in enumerate(rows):
        wav = wav_dir / f"{i:02d}.wav"
        meta = await synth_to_wav(tts, r["spoken"], f"num-{i}", wav)
        rec = {"i": i, "display": r["display"], "spoken": r["spoken"], "wav": str(wav.relative_to(ROOT)), **meta}
        if args.textnorm:
            rec["rime_textnorm"] = rime_textnorm(r["display"])
        if transcribe and "error" not in meta:
            rec["transcript"] = transcribe(str(wav))
            rec.update(compare(r["spoken"], rec["transcript"]))
        events.emit("number_roundtrip_row", **rec)
        results.append(rec)
        flag = "PASS" if rec.get("pass") else ("----" if "pass" not in rec else "FAIL")
        print(f"{flag} {r['display']!r:<45} -> {rec.get('transcript', '')!r}")
    await tts.close()

    scored = [r for r in results if "pass" in r]
    summary = {
        "run_at": stamp, "provider": tts.descriptor, "asr": asr_name, "n": len(results),
        "n_scored": len(scored), "n_pass": sum(1 for r in scored if r["pass"]),
        "failures": [{"display": r["display"], "transcript": r["transcript"], "missed": r["number_tokens_missed"]}
                     for r in scored if not r["pass"]],
    }
    out = ROOT / "traces" / f"number_roundtrip_{stamp}.json"
    out.write_text(json.dumps(summary, indent=1))
    events.close()
    print(f"\n{summary['n_pass']}/{summary['n_scored']} pass ({asr_name}) -> {out.relative_to(ROOT)}")
    return 0 if not summary["failures"] else 2


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true", help="only check normalizer against the golden set")
    ap.add_argument("--whisper", default="small.en")
    ap.add_argument("--textnorm", action="store_true", help="also record Rime textnorm output (RIME_TEXTNORM_URL)")
    sys.exit(asyncio.run(run(ap.parse_args())))


if __name__ == "__main__":
    main()
