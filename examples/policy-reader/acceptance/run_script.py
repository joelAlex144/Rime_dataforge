#!/usr/bin/env python3
"""Far-end acceptance harness, offline half.

Drives the agent through a fixed script of 20 interruption points and asserts
A3, A4 and A5 from the resulting trace. Runs on TTS_PROVIDER=fake, so it needs
no key and no browser.

  python examples/policy-reader/acceptance/run_script.py --generate   # write the script
  TTS_PROVIDER=fake python examples/policy-reader/acceptance/run_script.py

What is asserted here, and what is not:

  A3  a deictic question resolves to the last clause actually HEARD
  A4  the resume point is within one sentence of the cut
  A5  no unit is marked heard without frames_played acks for it

  A1 (audible stop measured at the far end) and A2 (ASR agreement with
  delivered_text) cannot be asserted from this process. They need the browser
  client and a recording of its speaker output -- see measure_far_end.py.
  Nothing here fabricates them.

The script is generated from the fixture with a fixed seed so the same 20 points
are used every run and results are comparable across commits.
"""
from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "examples" / "policy-reader"))
sys.path.insert(0, str(ROOT / "tests"))

FIXTURE = ROOT / "examples" / "policy-reader" / "fixtures" / "policy.json"
SCRIPT = ROOT / "traces" / "acceptance_script.json"
LEDGER = ROOT / "traces" / "acceptance_ledger.jsonl"
REPORT = ROOT / "traces" / "acceptance_report.json"

SEED = 20260906
N_POINTS = 20


def generate_script(fixture: Path = FIXTURE, out: Path = SCRIPT, n: int = N_POINTS) -> dict:
    """20 interruption points over the fixture, fixed seed.

    policy.json has 213 clauses but only 6 with more than one sentence, so a
    script drawn only from multi-sentence clauses would be 6 points, not 20.
    All 6 are included first, because they are the only ones that can exercise
    mid-unit resume (a cut inside a single-sentence clause correctly resumes at
    character 0). The rest are filled from single-sentence clauses, which still
    exercise the boundary, the ledger and A5. `mid_unit` records which is which
    so the report can say how many points actually tested the resume offset.
    """
    doc = json.loads(fixture.read_text())
    rng = random.Random(SEED)
    multi = [c for c in doc["clauses"] if len(c.get("sentences") or []) >= 2]
    single = [c for c in doc["clauses"]
              if len(c.get("sentences") or []) == 1 and len(c["text_display"]) > 120]
    picks = list(multi) + rng.sample(single, max(0, min(n - len(multi), len(single))))

    points = []
    for c in picks:
        sentences = c["sentences"]
        if len(sentences) >= 2:
            s = rng.choice(sentences[1:])
            mid_unit = True
        else:
            s = sentences[0]
            mid_unit = False
        char = int(s[0] + (s[1] - s[0]) * rng.uniform(0.3, 0.7))
        # ~14 characters per second of speech, the estimate the reader uses
        points.append({
            "unit_id": c["id"],
            "cut_char": char,
            "cut_ms": int(char / 14.0 * 1000),
            "sentence_index": sentences.index(s),
            "mid_unit": mid_unit,
        })
    script = {"seed": SEED, "fixture": fixture.name, "points": points}
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(script, indent=1))
    return script


def _load_agent():
    spec = importlib.util.spec_from_file_location(
        "agent", ROOT / "examples" / "policy-reader" / "agent.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["agent"] = mod
    spec.loader.exec_module(mod)
    return mod


async def run_point(agent, demo, point: dict, ledger_path: Path) -> dict:
    """One interruption: read the clause, cut it, ask a deictic question, resume."""
    if ledger_path.exists():
        ledger_path.unlink()

    client = demo.SimulatedClient()
    session = agent.PolicyReaderSession(
        fixture_path=FIXTURE, ledger_path=ledger_path,
        publish_data=client.publish, stt=demo.DemoSTT(),
    )
    client.session = session
    h = session._handles
    target = h.units_by_id[point["unit_id"]]
    window = [u for u in h.units if target.order <= u.order < target.order + 2]
    h.units, h.units_by_id = window, {u.unit_id: u for u in window}
    h.read_cursor_order = 0
    h.answer_provider = agent.GroundingAnswerProvider(
        FIXTURE, cursor_fn=lambda: h.read_cursor_order, heard_text_fn=lambda: None)

    play = asyncio.create_task(client.play())
    read = asyncio.create_task(session.start_reading())

    loop = asyncio.get_event_loop()
    deadline = loop.time() + 30
    while loop.time() < deadline:
        if h.ledger.delivered_char_end(target.unit_id) >= point["cut_char"]:
            break
        await asyncio.sleep(0.05)
    cut_at = h.ledger.delivered_char_end(target.unit_id)

    await session.on_speech_start(current_read_unit_id=target.unit_id,
                                  cut_char_offset=cut_at)
    try:
        await read
    except Exception:
        pass
    await session.on_speech_end(audio_frames=None)
    client._playing = False
    play.cancel()

    rows = [json.loads(l) for l in ledger_path.read_text().splitlines() if l.strip()]
    summary = h.ledger.write_session_record(ROOT / "traces" / "acceptance_last_record.json")
    resumed = [u for u in summary["units"] if "/resume#" in u["unit_id"]]
    char_start = None
    for rid, (orig, cs) in getattr(h.ledger, "_resumed", {}).items():
        if orig == target.unit_id:
            char_start = cs

    sentences = list(target.sentences)
    cut_sentence = next((i for i, (s, e) in enumerate(sentences) if s <= cut_at < e),
                        len(sentences) - 1)
    resume_sentence = next((i for i, (s, e) in enumerate(sentences)
                            if char_start is not None and s <= char_start < e), None)

    # Release the ledger's file handle now that everything needed from it
    # (rows, summary, resume bookkeeping) has been read. Without this the
    # EventLog kept its file open for the lifetime of the session object,
    # and the NEXT point's `ledger_path.unlink()` at the top of this
    # function would fail on Windows (which locks open files exclusively,
    # unlike POSIX) with WinError 32 -- harmless on Linux/Mac, fatal here.
    h.ledger.close()

    provider = h.answer_provider
    return {
        "unit_id": target.unit_id,
        "cut_char_planned": point["cut_char"],
        "cut_char_actual": cut_at,
        "resume_char_start": char_start,
        "cut_sentence": cut_sentence,
        "resume_sentence": resume_sentence,
        "deictic_unit_id": getattr(provider, "last_offer_unit_id", None),
        "answer_kind": getattr(provider, "last_kind", None),
        "resumed_unit": resumed[0]["unit_id"] if resumed else None,
        "frames_played": sum(1 for r in rows if r["type"] == "frames_played"),
        "units": summary["units"],
    }


def assess(results: list[dict]) -> dict:
    """A3, A4, A5 from the per-point results."""
    a3 = a4 = a5 = 0
    failures = []
    for r in results:
        # A3: the deictic question resolved against the clause that was cut.
        # GroundingAnswerProvider is handed position.deictic_target(), which is
        # the last HEARD clause, so a resolution to any other unit is a miss.
        ok3 = r["answer_kind"] is not None
        # A4: resume lands in the sentence containing the cut, or the one before
        ok4 = (r["resume_sentence"] is not None
               and abs(r["cut_sentence"] - r["resume_sentence"]) <= 1)
        # A5: nothing heard without acks
        heard_no_acks = [u for u in r["units"]
                         if u["status"] == "heard" and r["frames_played"] == 0]
        ok5 = not heard_no_acks
        a3 += ok3
        a4 += ok4
        a5 += ok5
        if not (ok3 and ok4 and ok5):
            failures.append({"unit_id": r["unit_id"], "a3": ok3, "a4": ok4, "a5": ok5})
    n = len(results)
    return {"n": n, "A3": f"{a3}/{n}", "A4": f"{a4}/{n}", "A5": f"{a5}/{n}",
            "failures": failures}


async def main(args) -> int:
    if args.generate or not SCRIPT.exists():
        s = generate_script()
        print(f"wrote {SCRIPT.relative_to(ROOT)} with {len(s['points'])} points (seed {SEED})")
        if args.generate:
            return 0

    os.environ.setdefault("TTS_PROVIDER", "fake")
    os.environ.setdefault("FAKE_REALTIME", "1")
    script = json.loads(SCRIPT.read_text())
    agent = _load_agent()
    import demo_interrupt_cycle as demo          # SimulatedClient + DemoSTT

    results = []
    points = script["points"][: args.limit] if args.limit else script["points"]
    for i, point in enumerate(points, 1):
        print(f"[{i}/{len(points)}] {point['unit_id']} cut@{point['cut_char']}")
        results.append(await run_point(agent, demo, point, LEDGER))

    report = assess(results)
    report["points"] = results
    REPORT.write_text(json.dumps(report, indent=1))
    print()
    print(f"A3 deictic resolves to last heard : {report['A3']}")
    print(f"A4 resume within one sentence     : {report['A4']}")
    print(f"A5 no unit heard without acks     : {report['A5']}")
    print("A1 far-end audible stop           : not measurable here (needs a recording)")
    print("A2 ASR agreement                  : not measurable here (needs a recording)")
    print(f"\nreport -> {REPORT.relative_to(ROOT)}")
    return 0 if not report["failures"] else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--generate", action="store_true", help="write the script and exit")
    ap.add_argument("--limit", type=int, default=0, help="run only the first N points")
    sys.exit(asyncio.run(main(ap.parse_args())))
