#!/usr/bin/env python3
"""Pull Rime's live voice catalog and verify the shipped speaker/model/lang exists.

Exit 0  -> configured combination is on the live catalog (snapshot saved to traces/)
Exit 1  -> not found / catalog unreachable  (this is a submission blocker)

Why: the brief requires a current production model/voice/language at
submission time and forbids copying a stale speaker list into the app. This
script is the only source of the speaker list; the app never hardcodes one.
It runs in CI-style before every demo recording and its snapshot is committed.

Usage:
  RIME_API_KEY=... RIME_SPEAKER=bancroft python scripts/fetch_voices.py
  python scripts/fetch_voices.py --list          # print speakers for RIME_MODEL_ID / RIME_LANG
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import requests

CATALOG_URL = os.environ.get("RIME_CATALOG_URL", "https://users.rime.ai/data/voices/all-v2.json")
ROOT = Path(__file__).resolve().parents[1]

# Rime's catalog uses both ISO-639-1 and 639-3 codes in different places.
_LANG_ALIASES = {"en": {"en", "eng"}, "es": {"es", "spa"}, "fr": {"fr", "fra"}, "de": {"de", "deu"},
                 "hi": {"hi", "hin"}, "ja": {"ja", "jpn"}, "pt": {"pt", "por"}, "ar": {"ar", "ara"}}


def lang_matches(a: str, b: str) -> bool:
    a, b = a.lower(), b.lower()
    for group in _LANG_ALIASES.values():
        if a in group and b in group:
            return True
    return a == b


def walk_entries(data):
    """Yield (path, dict) for every dict in the catalog; the shape isn't guaranteed."""
    stack = [("", data)]
    while stack:
        path, node = stack.pop()
        if isinstance(node, dict):
            yield path, node
            for k, v in node.items():
                stack.append((f"{path}/{k}", v))
        elif isinstance(node, list):
            for i, v in enumerate(node):
                stack.append((f"{path}[{i}]", v))


def find_speaker(data, speaker: str, model: str, lang: str) -> list[dict]:
    """Return catalog entries that name this speaker and are consistent with model+lang.

    Matching is deliberately loose on field names (name/speaker/id; model/modelId/models;
    lang/language/languages) because the catalog shape has changed before."""
    hits = []
    for path, node in walk_entries(data):
        name = str(node.get("name") or node.get("speaker") or node.get("id") or "").lower()
        if name != speaker.lower():
            # Some shapes key speakers by name: {"coda": {"en": ["bancroft", ...]}} or {"bancroft": {...}}
            continue
        models = node.get("model") or node.get("modelId") or node.get("models") or ""
        langs = node.get("lang") or node.get("language") or node.get("languages") or ""
        models = models if isinstance(models, list) else [models]
        langs = langs if isinstance(langs, list) else [langs]
        model_ok = (not any(models)) or any(str(m).lower() == model.lower() for m in models) or model.lower() in path.lower()
        lang_ok = (not any(langs)) or any(lang_matches(str(l), lang) for l in langs) or any(lang_matches(seg, lang) for seg in path.split("/"))
        hits.append({"path": path, "entry": node, "model_ok": model_ok, "lang_ok": lang_ok})
    if hits:
        return hits
    # Shape B: speaker names appear as bare strings in lists, e.g. data["coda"]["eng"] == [...]
    for path, node in walk_entries(data):
        for k, v in node.items():
            if isinstance(v, list) and any(isinstance(x, str) and x.lower() == speaker.lower() for x in v):
                p = f"{path}/{k}"
                hits.append({"path": p, "entry": {"names": v[:5], "n": len(v)},
                             "model_ok": model.lower() in p.lower(),
                             "lang_ok": any(lang_matches(seg, lang) for seg in p.split("/"))})
    return hits


def list_speakers(data, model: str, lang: str) -> list[str]:
    names = set()
    for path, node in walk_entries(data):
        name = node.get("name") or node.get("speaker")
        if name and (model.lower() in path.lower() or str(node.get("model") or node.get("modelId") or "").lower() == model.lower()):
            names.add(str(name))
        for k, v in node.items():
            p = f"{path}/{k}".lower()
            if isinstance(v, list) and model.lower() in p and any(lang_matches(seg, lang) for seg in p.split("/")):
                names.update(x for x in v if isinstance(x, str))
    return sorted(names)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true", help="list speakers for the configured model/lang")
    ap.add_argument("--speaker", default=os.environ.get("RIME_SPEAKER", ""))
    ap.add_argument("--model", default=os.environ.get("RIME_MODEL_ID", "coda"))
    ap.add_argument("--lang", default=os.environ.get("RIME_LANG", "en"))
    args = ap.parse_args()

    key = os.environ.get("RIME_API_KEY")
    if not key:
        print("RIME_API_KEY not set", file=sys.stderr)
        return 1

    r = requests.get(CATALOG_URL, headers={"Authorization": f"Bearer {key}"}, timeout=30)
    if r.status_code != 200:
        print(f"catalog fetch failed: HTTP {r.status_code} {r.text[:200]}", file=sys.stderr)
        return 1
    data = r.json()

    snap = ROOT / "traces" / f"rime_catalog_{time.strftime('%Y%m%d')}.json"
    snap.parent.mkdir(exist_ok=True)
    snap.write_text(json.dumps({"fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                "url": CATALOG_URL, "catalog": data}, indent=1))
    print(f"catalog snapshot -> {snap.relative_to(ROOT)}")

    if args.list:
        names = list_speakers(data, args.model, args.lang)
        print(f"{len(names)} speakers for model={args.model} lang={args.lang}:")
        print(", ".join(names) if names else "(none found — inspect the snapshot; the shape may have changed)")
        return 0

    if not args.speaker:
        print("RIME_SPEAKER not set (or pass --speaker)", file=sys.stderr)
        return 1

    hits = find_speaker(data, args.speaker, args.model, args.lang)
    ok = [h for h in hits if h["model_ok"] and h["lang_ok"]]
    verdict = {
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "speaker": args.speaker, "modelId": args.model, "lang": args.lang,
        "found_on_live_catalog": bool(ok),
        "matches": [{"path": h["path"], "model_ok": h["model_ok"], "lang_ok": h["lang_ok"]} for h in hits],
    }
    (ROOT / "traces" / "rime_catalog_check.json").write_text(json.dumps(verdict, indent=1))
    if ok:
        print(f"OK: speaker={args.speaker} modelId={args.model} lang={args.lang} is on the live catalog ({ok[0]['path']})")
        return 0
    print(f"FAIL: speaker={args.speaker} not found for modelId={args.model} lang={args.lang}.", file=sys.stderr)
    if hits:
        print("  Found the speaker under other model/lang paths:", [h["path"] for h in hits], file=sys.stderr)
    print("  Run with --list to see valid speakers. Do NOT submit until this passes.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
