#!/usr/bin/env python3
"""Run the scripted question set against a fixture and print hit / miss.

This is evidence of grounding CORRECTNESS on a fixed script, not of retrieval
quality: each question carries the listener's position and the branch and
clause it must resolve to. A miss prints the branch actually taken and the
clause actually returned, so the trace shows what happened, not just that it
did not.

  python scripts/check_grounding.py                       # hero fixture
  python scripts/check_grounding.py --questions examples/policy-reader/fixtures/policy.questions.json
  python scripts/check_grounding.py --out traces/grounding_check.json

Exit 0 when every question hits, 1 otherwise. No network, no model, no key.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "examples" / "policy-reader"))
from grounding import Grounding  # noqa: E402

FIXTURES = ROOT / "examples" / "policy-reader" / "fixtures"


def run(questions_path: Path) -> tuple[list[dict], dict]:
    spec = json.loads(questions_path.read_text(encoding="utf-8"))
    fixture = questions_path.parent / spec["fixture"]
    g = Grounding(fixture)
    rows = []
    for q in spec["questions"]:
        cursor = g.by_id[q["cursor"]]["index"] if q.get("cursor") else 0
        r = g.resolve(q["question"], q.get("last_heard"), read_cursor=cursor)
        if r.hits:
            actual_unit = r.hits[0].unit_id
        elif r.beyond:
            actual_unit = r.beyond[0].unit_id
        else:
            actual_unit = None
        hit = (r.retrieval_path == q["expected_path"]
               and actual_unit == q.get("expected_unit")
               and (not q.get("expected_kind") or r.kind == q["expected_kind"]))
        rows.append({
            "question": q["question"], "last_heard": q.get("last_heard"), "cursor": q.get("cursor"),
            "expected_path": q["expected_path"], "expected_unit": q.get("expected_unit"),
            "expected_kind": q.get("expected_kind"),
            "actual_path": r.retrieval_path, "actual_unit": actual_unit, "actual_kind": r.kind,
            "hit": hit,
        })
    summary = {"fixture": spec["fixture"], "questions": len(rows), "hits": sum(r["hit"] for r in rows),
               "by_path": {}}
    for r in rows:
        d = summary["by_path"].setdefault(r["expected_path"], {"n": 0, "hits": 0})
        d["n"] += 1
        d["hits"] += int(r["hit"])
    return rows, summary


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--questions", default=str(FIXTURES / "policy.questions.json"))
    ap.add_argument("--out", default=None, help="write rows + summary as JSON (the evidence file)")
    args = ap.parse_args()
    rows, summary = run(Path(args.questions))
    w = max(len(r["question"]) for r in rows)
    print(f"{'':4s} {'question':{w}s}  {'expected':24s}  actual")
    for r in rows:
        mark = "HIT " if r["hit"] else "MISS"
        exp = f"{r['expected_path']}/{r['expected_unit']}"
        act = f"{r['actual_path']}/{r['actual_unit']} ({r['actual_kind']})"
        print(f"{mark} {r['question']:{w}s}  {exp:24s}  {act}")
    print(f"\n{summary['hits']} of {summary['questions']} hit  "
          + "  ".join(f"{k}: {v['hits']}/{v['n']}" for k, v in summary["by_path"].items()))
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({"generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                   "summary": summary, "rows": rows}, indent=1), encoding="utf-8")
        print(f"wrote {out}")
    return 0 if summary["hits"] == summary["questions"] else 1


if __name__ == "__main__":
    sys.exit(main())
