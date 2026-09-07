#!/usr/bin/env python3
"""Build-time enrichment: generated navigator fields written into a fixture.

    python scripts/enrich.py examples/policy-reader/fixtures/policy.json
    python scripts/enrich.py --all            # every fixture in index.json
    python scripts/enrich.py <fixture> --force

Runs after ingestion, with ENRICH_PROVIDER=ollama|groq|none. Idempotent per
document: fields already present are kept unless --force. Every generated
field is `{"text"|"tags"|..., "generated": true, "provider": "<name>"}`, so
nothing generated can be mistaken for document text, and the ingest
idempotency test is scoped to clause ids and clause text.

The output guard (2a) applies to every generated string: no advice, no
importance ranking, no second-person situational language. A string that
fails is regenerated once with the reason appended; on a second failure the
mechanical form is used and the rejection is logged in the ingest report.

The server calls enrich_fixture() on entry -- the `enrich` upload stage, or
the first open of a fixture without a navigator -- with NAVIGATOR_FIELDS
first and the rest in the background. Nothing here runs inside the read
loop: the reader reads the fixture.
"""
from __future__ import annotations

import argparse
import glob
import json
import re
import sys
import time
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "examples" / "policy-reader"))

from delivery_layer.enrich.provider import (                        # noqa: E402
    NoneProvider, ProviderError, make_enrich_provider, parse_json_object)

FIXTURES = ROOT / "examples" / "policy-reader" / "fixtures"

# What the listener needs before the first word is read: the overview and the
# chips. Generated on entry (upload, or first open of an unenriched fixture)
# while the rest -- tags, suggested questions, table shapes -- follows in the
# background. Order matters: overview and topics read `sections`.
NAVIGATOR_FIELDS = ("overview", "sections", "topics")
ALL_FIELDS = ("overview", "sections", "topics", "tags", "suggested_questions", "tables")
INDEX = FIXTURES / "index.json"
CHARS_PER_SECOND_DEFAULT = 14.0

TAGS = ("exclusion", "waiting_period", "deadline", "amount", "obligation", "definition",
        "procedure", "contact")
TOPICS = ("Claims process", "Exclusions", "Waiting periods", "Premium and charges",
          "Cancellation and refunds", "Definitions", "Grievances and contacts")
READ_FROM_START = "Read from the start"
TAG_BATCH = 20

# 2a: what a generated string may not contain. Descriptive framing only.
_FORBIDDEN = re.compile(
    r"\b(you should|important|make sure|recommend(?:ed|s)?|beware|key thing|crucial|must know|be careful)\b", re.I)
ALLOWED_VERBS = "covers, lists, defines, describes, sets out, explains the procedure for"

SYSTEM = (
    "You describe insurance and loan documents for a reader that reads them aloud to a listener. "
    "You DESCRIBE and EXTRACT; you never interpret, advise, rank by importance, or address the "
    "listener's situation. Do not use second person about the listener's circumstances. "
    f"Framing verbs you may use: {ALLOWED_VERBS}. Never use: you should, important, make sure, "
    "recommend, beware, key thing, crucial, must know, be careful. Plain English, no markdown. "
    "Answer ONLY in the JSON shape requested."
)


def measured_chars_per_second() -> tuple[float, str]:
    """Chars/second from measured Rime audio in traces (preflight or bench),
    else the reader's constant. Listening time is computed, never guessed."""
    chars = ms = 0
    files = sorted(glob.glob(str(ROOT / "traces" / "bench_latency*.json*"))) + \
        sorted(glob.glob(str(ROOT / "traces" / "preflight_*.jsonl")))
    for f in files:
        req: dict = {}
        try:
            for line in open(f, encoding="utf-8"):
                r = json.loads(line)
                if r.get("type") == "synth_requested":
                    req[r["context_id"]] = r.get("chars", 0)
                elif r.get("type") == "synth_done" and r.get("context_id") in req and r.get("audio_ms"):
                    chars += req[r["context_id"]]
                    ms += r["audio_ms"]
        except (OSError, ValueError):
            continue
    if ms > 0 and chars > 0:
        return chars / ms * 1000.0, f"measured from {len(files)} trace file(s)"
    return CHARS_PER_SECOND_DEFAULT, "reader default (no measured trace)"


def normalise(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]", " ", (text or "").lower())


_CLAUSE_ID = re.compile(r"\b(?:sec|p|t|r)-?\d+(?:-[a-z0-9]+)*\b|\bclause id\b", re.I)


def guard(text: str) -> Optional[str]:
    """None if the string passes; else the reason. A clause id in spoken text
    ("mentioned in sec-1-p2") is machinery leaking into the voice."""
    m = _FORBIDDEN.search(text or "")
    if m:
        return f"forbidden phrase {m.group(0)!r}"
    m = _CLAUSE_ID.search(text or "")
    return f"clause id {m.group(0)!r}" if m else None


def gen(text: str, provider) -> dict:
    return {"text": text, "generated": True, "provider": provider.name}


def spoken_minutes(chars: int, cps: float) -> int:
    return max(1, round(chars / cps / 60))


def section_spans(doc: dict) -> list[dict]:
    """Top-level sections: from the map when the structure pass wrote one
    (heading clause plus the clauses under it), else -- for a fixture that
    predates it, like the hero fixture -- from runs of section_title, the
    same derivation grounding.py uses. The section id is its first clause."""
    clauses = doc["clauses"]
    by_id = {c["id"]: c for c in clauses}
    heads = [m for m in doc.get("map") or [] if m.get("id") in by_id]
    out = []
    if heads:
        for i, m in enumerate(heads):
            start = by_id[m["id"]]["index"]
            end = by_id[heads[i + 1]["id"]]["index"] if i + 1 < len(heads) else len(clauses)
            body = [c for c in clauses[start + 1:end] if c.get("kind", "body") in ("body", "definition")]
            out.append({"id": m["id"], "title": m["title"], "start": start, "end": end, "body": body,
                        "chars": sum(len(c["text_display"]) for c in clauses[start:end]
                                     if c.get("kind", "body") in ("heading", "body", "definition", "table_stub"))})
        return out
    runs: list[list[dict]] = []
    for c in clauses:
        if c.get("kind", "body") == "boilerplate":
            continue
        if not runs or runs[-1][0]["section_title"] != c["section_title"]:
            runs.append([c])
        else:
            runs[-1].append(c)
    for run in runs:
        body = [c for c in run if c.get("kind", "body") in ("body", "definition")]
        out.append({"id": run[0]["id"], "title": run[0]["section_title"], "start": run[0]["index"],
                    "end": run[-1]["index"] + 1, "body": body,
                    "chars": sum(len(c["text_display"]) for c in run)})
    return out


class Enricher:
    def __init__(self, provider, cps: float, cps_source: str, log=sys.stderr) -> None:
        self.p = provider
        self.cps = cps
        self.cps_source = cps_source
        self.rejections: list[dict] = []
        self.log = log

    # ---------------------------------------------------------------- core
    def ask(self, user: str, max_tokens: int = 600) -> str:
        return self.p.complete(SYSTEM, user, max_tokens=max_tokens)

    def guarded(self, field: str, user: str, max_tokens: int, key: str, fallback: str) -> str:
        """Generate one string; regenerate once on a guard failure; then fall back."""
        reasons = []
        prompt = user
        for attempt in (1, 2):
            try:
                reply = parse_json_object(self.ask(prompt, max_tokens))
                text = str(reply.get(key, "")).strip()
            except (ProviderError, ValueError, json.JSONDecodeError) as e:
                text, reason = "", f"unparseable reply: {e}"
            else:
                reason = guard(text) if text else "empty"
            if text and not reason:
                return text
            reasons.append(reason)
            prompt = user + f"\n\nYour previous answer was rejected: {reason}. Rewrite it without that."
        self.rejections.append({"field": field, "reasons": reasons, "fallback": fallback[:120]})
        print(f"  guard: {field} rejected twice ({reasons[-1]}); mechanical fallback", file=self.log)
        return fallback

    # -------------------------------------------------------------- fields
    def overview(self, doc: dict, sections: list[dict]) -> dict:
        total_chars = sum(len(c["text_display"]) for c in doc["clauses"] if c.get("kind", "body") != "boilerplate")
        minutes = spoken_minutes(total_chars, self.cps)
        listing = "\n".join(f"- {s['title']} ({len(s['body'])} clauses)" for s in sections)
        issuer = doc.get("source", {}).get("path_or_url", "")
        user = (f"Document title: {doc['title']}\nSource file: {issuer}\nSections, in order:\n{listing}\n\n"
                "Write 3 to 5 sentences saying what this document is, who issues it, and how it is "
                "organised (name the sections in order). Do not mention listening time. "
                'Reply as JSON: {"overview": "..."}')
        mechanical = (f"This document, {doc['title']}, has {len(sections)} sections. "
                      "I'll read them in order; interrupt me any time.")
        text = self.guarded("overview", user, 400, "overview", mechanical)
        text = text.rstrip() + f" Listening from start to finish takes about {minutes} minute{'s' if minutes != 1 else ''}."
        return {**gen(text, self.p), "listening_minutes": minutes,
                "chars_per_second": round(self.cps, 2), "chars_per_second_source": self.cps_source}

    def briefs(self, sections: list[dict]) -> list[dict]:
        out = []
        for s in sections:
            sample = "\n".join(c["text_display"][:200] for c in s["body"][:12])
            user = (f"Section heading: {s['title']}\nFirst clauses under it:\n{sample}\n\n"
                    "Write ONE descriptive line (under 25 words) saying what this section covers, "
                    f"starting with one of: {ALLOWED_VERBS}. "
                    'Reply as JSON: {"brief": "..."}')
            n = len(s["body"])
            mechanical = f"{s['title']}. {n} item{'' if n == 1 else 's'}."
            text = self.guarded(f"brief:{s['id']}", user, 120, "brief", mechanical)
            out.append({"id": s["id"], "title": s["title"], "brief": gen(text, self.p),
                        "est_minutes": spoken_minutes(s["chars"], self.cps)})
        return out

    @staticmethod
    def _tag_candidates(doc: dict) -> list:
        return [c for c in doc["clauses"] if c.get("kind", "body") in ("body", "definition", "table_row")]

    def _tags_batch(self, doc: dict, batch: list, k: int) -> dict:
        """One model call: tags for ~20 clauses, written onto the clauses."""
        listing = "\n".join(f"{c['id']}: {c['text_display'][:300]}" for c in batch)
        user = (f"Tag each clause with zero or more of exactly these tags: {', '.join(TAGS)}.\n"
                "exclusion = something not covered; waiting_period = a period before cover applies; "
                "deadline = a time limit to act; amount = a sum, limit, rate or percentage; "
                "obligation = something a party must do; definition = a defined term; "
                "procedure = steps to follow; contact = a phone, email, address or office.\n\n"
                f"{listing}\n\n"
                'Reply as JSON only: {"<clause_id>": ["tag", ...], ...} with every clause id present.')
        result: dict = {}
        try:
            reply = parse_json_object(self.ask(user, 600))
        except (ProviderError, ValueError, json.JSONDecodeError) as e:
            self.rejections.append({"field": f"tags:batch{k}", "reasons": [str(e)]})
            return result
        ids = {c["id"] for c in batch}
        for cid, tg in reply.items():
            if cid in ids and isinstance(tg, list):
                result[cid] = [t for t in tg if t in TAGS]
        for c in batch:
            if c["id"] in result:
                c["tags"] = {"tags": result[c["id"]], "generated": True, "provider": self.p.name}
        return result

    def tags(self, doc: dict) -> dict:
        """clause_id -> [tags], batched ~20 clauses per request; JSON only."""
        cands = self._tag_candidates(doc)
        result: dict = {}
        for i in range(0, len(cands), TAG_BATCH):
            result.update(self._tags_batch(doc, cands[i:i + TAG_BATCH], i // TAG_BATCH))
        return result

    def _questions_for(self, doc: Optional[dict], s: dict) -> list:
        """One model call: the suggested questions for one section, each
        clause_id verified in that section; written into doc["sections"]."""
        if not s["body"]:
            return []
        listing = "\n".join(f"{c['id']}: {c['text_display'][:220]}" for c in s["body"][:25])
        user = (f"Section: {s['title']}\nClauses:\n{listing}\n\n"
                "Write 2 or 3 questions a listener might ask that ONE of these clauses answers "
                "directly, phrased as a question about what the document says (never about the "
                "listener's own situation). "
                'Reply as JSON only: {"questions": [{"text": "...", "clause_id": "..."}]}')
        try:
            reply = parse_json_object(self.ask(user, 400))
        except (ProviderError, ValueError, json.JSONDecodeError) as e:
            self.rejections.append({"field": f"questions:{s['id']}", "reasons": [str(e)]})
            return []
        ids = {c["id"] for c in s["body"]}
        kept = []
        for q in reply.get("questions", []) or []:
            text, cid = str(q.get("text", "")).strip(), str(q.get("clause_id", ""))
            if not text or cid not in ids:
                continue                      # a pointer that does not resolve is dropped
            if guard(text):
                self.rejections.append({"field": f"question:{s['id']}", "reasons": [guard(text)], "dropped": text[:80]})
                continue
            kept.append({**gen(text, self.p), "clause_id": cid})
        kept = kept[:3]
        if kept and doc is not None:
            for sec in doc.get("sections") or []:
                if sec["id"] == s["id"]:
                    sec["suggested_questions"] = kept
        return kept

    def questions(self, sections: list[dict]) -> dict:
        """section id -> [{text, clause_id}], each clause_id verified in that section."""
        out: dict = {}
        for s in sections:
            kept = self._questions_for(None, s)
            if kept:
                out[s["id"]] = kept
        return out

    def topics(self, sections: list[dict]) -> list[dict]:
        listing = "\n".join(f"- {s['title']}" for s in sections)
        user = (f"Section headings, in order:\n{listing}\n\nFor each topic below, name the ONE heading "
                "from the list that covers it, copied exactly, or null if none does.\n"
                f"Topics: {', '.join(TOPICS)}\n"
                'Reply as JSON only: {"<topic>": "<heading or null>", ...}')
        chosen: dict = {}
        try:
            chosen = parse_json_object(self.ask(user, 400))
        except (ProviderError, ValueError, json.JSONDecodeError) as e:
            self.rejections.append({"field": "topics", "reasons": [str(e)]})
        by_title = {s["title"].strip().lower(): s for s in sections}
        out = []
        for topic in TOPICS:
            # Mechanical first: a heading that names the topic wins ("General
            # Exclusions" for Exclusions). The model only decides when no
            # heading does; a small model pointed Exclusions at Definitions.
            head_word = topic.split()[0].lower().rstrip("s")
            named = [s for s in sections if head_word in normalise(s["title"])]
            if named:
                s = named[0]
            else:
                h = chosen.get(topic)
                s = by_title.get(str(h or "").strip().lower())
            if s is None:
                continue                          # not in this document: no chip
            out.append({"topic": topic, "section_id": s["id"], "heading": s["title"],
                        "generated": True, "provider": self.p.name})
        out.append({"topic": READ_FROM_START, "section_id": None, "heading": None, "generated": False})
        return out

    def _table(self, doc: dict, stub: dict) -> None:
        """One table: its spoken description and one sentence per row."""
        clauses = doc["clauses"]
        rows = [c for c in clauses if c.get("kind") == "table_row" and c.get("parent") == stub["id"]]
        n_cols = max((c["text_display"].count(";") + 1 for c in rows), default=0)
        if not rows:
            return
        listing = "\n".join(c["text_display"][:240] for c in rows[:12])
        user = (f"A table in the document, stub text: {stub['text_display']}\nRows:\n{listing}\n\n"
                "Describe the table's SHAPE in one or two sentences: what it is a table of, how "
                "many columns and what they are, and the row labels in order. End with "
                '"Which one do you want?". Reply as JSON: {"description": "..."}')
        desc = self.guarded(f"table:{stub['id']}", user, 250, "description", stub["text_display"])
        stub["spoken_override"] = gen(desc, self.p)
        if len(rows) <= 6 and n_cols <= 3:
            stub["read_inline"] = True
        for r in rows:
            user = (f"Row of a table: {r['text_display']}\nWrite it as ONE natural spoken sentence. "
                    'Reply as JSON: {"sentence": "..."}')
            sent = self.guarded(f"row:{r['id']}", user, 120, "sentence", r["text_display"])
            r["spoken_override"] = gen(sent, self.p)
            if stub.get("read_inline"):
                r["read_inline"] = True

    def tables(self, doc: dict) -> None:
        for stub in [c for c in doc["clauses"] if c.get("kind") == "table_stub"]:
            self._table(doc, stub)

    # ---------------------------------------------------------------- steps
    def steps(self, doc: dict, fields=None, force: bool = False) -> list:
        """The rest fields as one-call steps [(name, callable)], so a server
        can run them one executor call at a time and yield between them: a tag
        batch, one section's questions, one table. Each callable writes its
        result into `doc`; finish() records what was done."""
        from functools import partial
        sections = section_spans(doc)
        e = doc.setdefault("enrichment", {})
        prev = set() if force else set(e.get("done", []))
        wanted = set(fields or ALL_FIELDS)

        def need(field: str) -> bool:
            return field in wanted and (force or field not in prev)
        out = []
        if need("tags"):
            cands = self._tag_candidates(doc)
            for i in range(0, len(cands), TAG_BATCH):
                out.append((f"tags:batch{i // TAG_BATCH}", partial(self._tags_batch, doc, cands[i:i + TAG_BATCH], i // TAG_BATCH)))
        if need("suggested_questions"):
            for s in sections:
                if s["body"]:
                    out.append((f"questions:{s['id']}", partial(self._questions_for, doc, s)))
        if need("tables"):
            for stub in [c for c in doc["clauses"] if c.get("kind") == "table_stub"]:
                out.append((f"table:{stub['id']}", partial(self._table, doc, stub)))
        return out

    def finish(self, doc: dict, done: list, t0: float, fields=None, force: bool = False) -> dict:
        """Record what this run generated in doc["enrichment"]."""
        e = doc.setdefault("enrichment", {})
        elapsed = round((time.monotonic() - t0) * 1000)
        prev_done = set(e.get("done", []))
        e.update({"provider": self.p.name, "model": getattr(self.p, "model", None), "elapsed_ms": elapsed,
                  "fields": list(done), "done": sorted(prev_done | set(done)),
                  "guard_rejections": (e.get("guard_rejections") or []) + self.rejections if fields else self.rejections,
                  "tokens": {"in": getattr(self.p, "tokens_in", None), "out": getattr(self.p, "tokens_out", None)}})
        return e

    # ---------------------------------------------------------------- run
    def enrich(self, doc: dict, force: bool = False, fields=None) -> dict:
        """`fields`: subset of ALL_FIELDS to generate (default all). Fields
        outside the subset are left exactly as they are, done or not."""
        t0 = time.monotonic()
        sections = section_spans(doc)
        e = doc.setdefault("enrichment", {})
        # Idempotent per document: a field done by an earlier run (recorded in
        # enrichment.done) is not generated again unless --force.
        prev = set() if force else set(e.get("done", []))
        wanted = set(fields or ALL_FIELDS)
        done = []

        def need(field: str) -> bool:
            return field in wanted and (force or field not in prev)
        if need("overview"):
            doc["overview"] = self.overview(doc, sections); done.append("overview")
        if need("sections"):
            doc["sections"] = self.briefs(sections); done.append("sections")
        if need("topics"):
            doc["topics"] = self.topics(sections); done.append("topics")
        rest = [f for f in ("tags", "suggested_questions", "tables") if need(f)]
        for _name, fn in self.steps(doc, fields=rest, force=force):
            fn()
        done.extend(rest)
        if force:
            e["done"] = sorted(set(e.get("done", [])))
        else:
            e["done"] = sorted(prev)
        return self.finish(doc, done, t0, fields=fields, force=force)


def write_fixture(path: Path, doc: dict, info: Optional[dict] = None) -> None:
    """The fixture back to disk, and its enrichment block into the ingest report."""
    path.write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    rep_name = doc.get("report_file")
    if info is not None and rep_name and (path.parent / rep_name).exists():
        rp = path.parent / rep_name
        rep = json.loads(rp.read_text(encoding="utf-8"))
        rep["enrichment"] = info
        rp.write_text(json.dumps(rep, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")


def enrich_fixture(path: Path, provider, force: bool = False, log=sys.stderr, fields=None) -> dict:
    doc = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(provider, NoneProvider):
        print(f"{path.name}: ENRICH_PROVIDER=none, nothing generated (mechanical map is used)", file=log)
        return {"provider": "none", "fields": []}
    cps, src = measured_chars_per_second()
    info = Enricher(provider, cps, src, log=log).enrich(doc, force=force, fields=fields)
    write_fixture(path, doc, info)
    print(f"{path.name}: {provider.name}/{getattr(provider, 'model', '')} {info['elapsed_ms']} ms, "
          f"fields {info['fields']}, {len(info['guard_rejections'])} guard rejection(s)", file=log)
    return info


def main() -> int:
    ap = argparse.ArgumentParser(description="Build-time enrichment of fixtures.")
    ap.add_argument("fixture", nargs="?", help="a fixture .json")
    ap.add_argument("--all", action="store_true", help="every fixture registered in index.json")
    ap.add_argument("--force", action="store_true", help="regenerate fields that already exist")
    ap.add_argument("--provider", default=None, help="override ENRICH_PROVIDER")
    args = ap.parse_args()
    try:
        provider = make_enrich_provider(args.provider)
    except ProviderError as e:
        print(f"enrich: {e}", file=sys.stderr)
        return 1
    paths: list[Path] = []
    if args.all:
        idx = json.loads(INDEX.read_text(encoding="utf-8"))
        paths = [FIXTURES / e["path"] for e in idx["documents"]]
    elif args.fixture:
        paths = [Path(args.fixture)]
    else:
        ap.error("give a fixture or --all")
    for p in paths:
        enrich_fixture(p, provider, force=args.force)
    return 0


if __name__ == "__main__":
    sys.exit(main())
