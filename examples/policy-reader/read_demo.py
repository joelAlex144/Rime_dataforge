#!/usr/bin/env python3
"""Minimal end-to-end of the Person-A slice, offline by default.

Streams clauses through the provider (FakeTTS unless TTS_PROVIDER=rime),
builds the word map from the returned timestamps, simulates an interruption
at --cut-ms, prints what was heard, resolves a deictic question against the
last *heard* clause, and prints the resume point.

  python examples/policy-reader/read_demo.py --start sec-4b-v --cut-ms 2500 -q "what does that mean"
  TTS_PROVIDER=rime RIME_SPEAKER=bancroft python examples/policy-reader/read_demo.py ...
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).parent))

from delivery_layer.events import EventLog                       # noqa: E402
from delivery_layer.normalize import Segment, normalize          # noqa: E402
from delivery_layer.resume import resume_point                   # noqa: E402
from delivery_layer.tts import make_provider                     # noqa: E402
from delivery_layer.tts.base import AudioChunk, Done, Timestamps # noqa: E402
from delivery_layer.wordmap import build_word_map                # noqa: E402
from grounding import Grounding                                  # noqa: E402

FIX = Path(__file__).parent / "fixtures" / "policy.json"


async def main(args) -> None:
    ev = EventLog(ROOT / "traces" / "read_demo.jsonl", session_id="read-demo")
    g = Grounding(FIX)
    tts = make_provider(ev)
    await tts.connect()
    print("provider:", json.dumps(tts.descriptor))

    start = g.by_id[args.start]["index"]
    last_heard_id, cut_char, played_ms = None, 0, 0.0
    for c in g.clauses[start:start + args.units]:
        segs = [Segment(a, b, sp, replaced=c["text_display"][a:b] != sp) for a, b, sp in c["spoken_map"]]
        ts = None
        played = 0.0
        async for item in tts.synth(c["text_spoken"], c["id"]):
            if isinstance(item, Timestamps):
                ts = item
            elif isinstance(item, AudioChunk):
                # A real client acks rendered frames; here we simulate the ack clock.
                played += len(item.pcm) / 2 / tts.sample_rate * 1000
                ev.emit("frames_played", context_id=c["id"], rendered_ms=round(played, 1))
                if args.cut_ms and played >= args.cut_ms and c is g.clauses[start + args.units - 1]:
                    await tts.cancel()
                    ev.emit("audible_stop", context_id=c["id"], rendered_ms=round(played, 1))
                    break
            elif isinstance(item, Done):
                pass
        wm = build_word_map(c["id"], c["text_display"], segs, ts.words, ts.start_ms, ts.end_ms)
        cut_char = wm.offset_at(played)
        heard = wm.heard_text(played)
        truncated = cut_char < len(c["text_display"])
        ev.emit("unit_truncated" if truncated else "unit_heard", context_id=c["id"], rendered_ms=round(played, 1),
                char_end=cut_char, of=len(c["text_display"]))
        last_heard_id = c["id"] if heard.strip() else last_heard_id
        played_ms = played
        print(f"\n[{c['id']}] {'TRUNCATED' if truncated else 'heard'} @ {played:.0f} ms -> {cut_char}/{len(c['text_display'])} chars")
        print(f"  heard: {heard!r}{' [interrupted]' if truncated else ''}")

    if args.question:
        c = g.by_id[last_heard_id]
        r = g.resolve(args.question, last_heard_id, read_cursor=c["index"])
        ans = await g.answer(r, heard_text_of_reference=heard if truncated else None)
        print(f"\nQ: {args.question}\n[{r.kind}] resolves to {r.hits[0].unit_id if r.hits else r.beyond[0].unit_id}")
        print("A (spoken):", normalize(ans))
        c_last = g.by_id[last_heard_id]
        rp = resume_point(c_last["id"], c_last["text_display"], c_last["sentences"], cut_char, c_last["section_title"])
        ev.emit("position_restored", unit_id=rp.unit_id, sentence_index=rp.sentence_index, char_start=rp.char_start)
        print(f"\nresume: {rp.unit_id} sentence {rp.sentence_index} @ char {rp.char_start}")
        print("  ->", normalize(rp.spoken_prefix + rp.text)[:160], "...")
    await tts.close()
    ev.close()
    print(f"\nevents -> {ev.path.relative_to(ROOT)}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="sec-4b-v")
    ap.add_argument("--units", type=int, default=3)
    ap.add_argument("--cut-ms", type=float, default=2500)
    ap.add_argument("-q", "--question", default="what does that mean")
    asyncio.run(main(ap.parse_args()))
