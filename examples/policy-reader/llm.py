"""The optional LLM behind grounded answers. Key lives server-side only.

No answer model configured -> make_llm() returns None and answers are
extractive (cite and re-read the clause). Configured -> an async callable the
grounding module feeds its no-interpretation prompt. Provider and model from
LLM_PROVIDER / LLM_MODEL. Moved out of chat_demo.py so the web server and the
REPL share one client.

LLM_PROVIDER=ollama needs no key: it talks to a local Ollama server over its
OpenAI-compatible /v1/chat/completions (LLM_BASE_URL, default
http://localhost:11434/v1). The shipped local model is granite4.2:3b -- small
enough to sit in a 6 GB GPU next to its KV cache, tuned for answering from
supplied passages, and it does not think before it speaks, so the voice line
is not left waiting on a reasoning trace. LLM_BASE_URL also overrides the
endpoint for provider=openai, so any OpenAI-compatible server (vLLM, LM Studio)
works.

The answer is on the voice path: the reader is stopped while it is produced.
So every request is deterministic (temperature 0), capped to a spoken reply
(LLM_MAX_TOKENS, default 256) and bounded by LLM_TIMEOUT_S (default 30);
past that the server logs `llm_failed` and speaks the extractive answer
instead. `check_llm()` / `warm_llm()` are run once at server start so the
first question does not pay the model load, and `python llm.py --check`
verifies the configured path from a terminal.
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
import time

DEFAULT_OLLAMA_MODEL = "granite4.2:3b"


def _timeout() -> float:
    try:
        return float(os.environ.get("LLM_TIMEOUT_S", "30"))
    except ValueError:
        return 30.0


def _max_tokens() -> int:
    try:
        return int(os.environ.get("LLM_MAX_TOKENS", "256"))
    except ValueError:
        return 256


_THINK = re.compile(r"<think>.*?</think>|</?think>", re.S | re.I)
_MD = re.compile(r"\*\*|__|`+|^#{1,6}\s*|^\s*(?:[-*\u2022]|\d+[.)])\s+|^\*\*?answer:?\*\*?\s*", re.M | re.I)


def spoken(text: str) -> str:
    """What reaches the voice line. granite4.2:3b sometimes opens with an empty
    think block (a lone "?" then "</think>") and answers in markdown with bold,
    a list and a final "**Answer:**" line; Rime would read the tags and the
    asterisks. Strip those, keep the words, collapse whitespace."""
    t = _THINK.sub(" ", text or "")
    t = re.sub(r"(?im)^\s*\**\s*answer\s*:\s*\**\s*", "", t)   # a closing "**Answer:**" label
    t = _MD.sub("", t)
    # A leading fragment of the question echoed back ("amount? The premium is ...").
    t = re.sub(r"^\s*(?:\S+\s+){0,6}\S*\?\s+(?=\S)", "", t.strip())
    lines = [ln.strip() for ln in t.splitlines()]
    lines = [ln for ln in lines if ln and ln not in ("?", ":")]
    return re.sub(r"\s+", " ", " ".join(lines)).strip()


def _post(provider: str, key: str, model: str, messages: list, max_tokens: int | None = None,
          timeout: float | None = None, raw: bool = False, extra: dict | None = None) -> str:
    """One chat completion. `raw` returns the content untouched for the JSON
    callers (classify, map_topic, understand); a spoken answer goes through
    spoken() so no tag or markdown reaches the voice line. `extra` is merged
    into the OpenAI-compatible request body (think-off flags for a Qwen
    companion model); unknown keys are ignored by the server."""
    import requests

    max_tokens = max_tokens or _max_tokens()
    timeout = timeout or _timeout()
    clean = (lambda t: t.strip()) if raw else spoken
    system = "\n".join(m["content"] for m in messages if m["role"] == "system")
    turns = [m for m in messages if m["role"] != "system"]
    if provider == "anthropic":
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": model, "max_tokens": max_tokens, "temperature": 0,
                  "system": system, "messages": turns},
            timeout=timeout)
        r.raise_for_status()
        d = r.json()
        if d.get("stop_reason") == "refusal":
            return "I can't answer that one from the document. Ask the insurer or lender."
        return clean("".join(b.get("text", "") for b in d.get("content", []) if b.get("type") == "text"))
    if provider in ("openai", "ollama"):
        base = _base_url(provider)
        headers = {"content-type": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        r = requests.post(
            f"{base}/chat/completions", headers=headers,
            json={"model": model, "messages": messages, "stream": False,
                  "temperature": 0, "max_tokens": max_tokens, **(extra or {})},
            timeout=timeout)
        r.raise_for_status()
        return clean(r.json()["choices"][0]["message"]["content"])
    raise RuntimeError(f"unknown LLM_PROVIDER {provider!r}; use 'anthropic', 'openai' or 'ollama'")


def _json_block(text: str) -> dict:
    """The first {...} in a reply, or ValueError."""
    import json
    t = (text or "").strip()
    a, b = t.find("{"), t.rfind("}")
    if a < 0 or b <= a:
        raise ValueError("no JSON object in the reply")
    return json.loads(t[a:b + 1])


def classify(text: str, options: list) -> str:
    """Closed-set classification of a listener's reply to an open prompt: one
    of `options`, or "question". JSON in and out, temperature 0, max_tokens
    20, a 5 s timeout. Any failure -- no model configured, timeout, junk,
    an option that is not in the list -- is "question": a reply the rules did
    not understand is treated as a question, never as a navigation decision
    the model made up. This is one of the model's four roles."""
    cfg = _config()
    if cfg is None or not (text or "").strip() or not options:
        return "question"
    provider, model, key = cfg
    opts = [str(o) for o in options] + ["question"]
    msgs = [{"role": "system", "content": "You classify one short spoken reply into exactly one option. "
             'Reply with JSON only: {"choice": "<one option, copied exactly>"}'},
            {"role": "user", "content": "OPTIONS: " + " | ".join(opts) + "\nREPLY: " + text.strip()[:300]
             + '\nIf the reply asks something about the document, or fits no option, the choice is "question".'}]
    try:
        raw = _post(provider, key, model, msgs, max_tokens=20, timeout=5.0, raw=True)
        picked = str(_json_block(raw).get("choice", "")).strip()
    except Exception:
        return "question"
    for o in opts:
        if picked.lower() == o.lower():
            return o
    return "question"


def map_topic(text: str, sections: list) -> str | None:
    """The listener named a topic no heading matched: given the section list
    [{id, title}], pick ONE section id or none. Closed set -- anything that is
    not an id from the list is None, so the model can never name a jump target
    outside the document. Any failure is None."""
    cfg = _config()
    if cfg is None or not (text or "").strip() or not sections:
        return None
    provider, model, key = cfg
    ids = [str(s["id"]) for s in sections]
    listing = "\n".join(f"{s['id']}: {s['title']}" for s in sections)
    msgs = [{"role": "system", "content": "You map a listener's topic to ONE section of a document, or to none. "
             'Reply with JSON only: {"section": "<an id from the list, or none>"}'},
            {"role": "user", "content": f"SECTIONS:\n{listing}\n\nTOPIC: {text.strip()[:200]}"}]
    try:
        raw = _post(provider, key, model, msgs, max_tokens=30, timeout=5.0, raw=True)
        sid = str(_json_block(raw).get("section", "")).strip()
    except Exception:
        return None
    return sid if sid in ids else None


INTENTS = ("topic", "question", "brief", "start", "carry_on", "skip", "back", "row", "all",
           "yes", "no", "recap", "repeat", "unclear")
UNCLEAR = {"intent": "unclear", "section_id": None, "row": None, "question": None}


def understand(text: str, ctx: dict) -> dict:
    """What the listener meant: intent plus slots, as a closed schema.

        {"intent": one of INTENTS, "section_id": <id from ctx["sections"] or null>,
         "row": <label from ctx["row_labels"] or null>, "question": <cleaned question or null>}

    `ctx` carries the open prompt (`prompt_kind`, `options`), the section list
    `[{id, title}]`, the row labels when a table prompt is open, the last heard
    heading and whether reading is in progress. Granite via _post, JSON only,
    temperature 0, max_tokens 80, 4 s. A section_id or row that is not in the
    supplied lists is dropped and the intent becomes "unclear"; any failure --
    no model, timeout, junk -- is {"intent": "unclear"}, and the caller falls
    back to the rules. The model never chooses a target outside the lists."""
    cfg = _config()
    if cfg is None or not (text or "").strip():
        return dict(UNCLEAR)
    provider, model, key = cfg
    sections = ctx.get("sections") or []
    ids = [str(s["id"]) for s in sections]
    labels = [str(x) for x in (ctx.get("row_labels") or [])]
    listing = "\n".join(f"  {s['id']}: {s['title']}" for s in sections) or "  (none)"
    msgs = [{"role": "system", "content":
             "You interpret one short spoken reply from a person who is having a document read aloud. "
             "Return JSON only, with exactly these keys: "
             '{"intent": "<one of: ' + "|".join(INTENTS) + '>", '
             '"section_id": "<an id from SECTIONS, or null>", "row": "<a label from ROWS, or null>", '
             '"question": "<the question, cleaned up, or null>"}. '
             "topic = they name a subject to hear; question = they ask something about the document; "
             "brief = the short overview; start = read from the beginning; carry_on = keep reading; "
             "skip = the next section; back = where they were; row/all = a table row or every row; "
             "yes/no = an answer to the prompt; recap = what was covered; repeat = say that again; "
             "unclear = none of these. Never invent an id or a label."},
            {"role": "user", "content":
             f"OPEN PROMPT: {ctx.get('prompt_kind') or 'none'} options {list(ctx.get('options') or [])}\n"
             f"SECTIONS:\n{listing}\nROWS: {labels or '(none)'}\n"
             f"LAST HEARD: {ctx.get('last_heard') or '(nothing yet)'}\nREADING: {'yes' if ctx.get('reading') else 'no'}\n"
             f"REPLY: {text.strip()[:300]}"}]
    try:
        raw = _post(provider, key, model, msgs, max_tokens=80, timeout=4.0, raw=True)
        d = _json_block(raw)
    except Exception:
        return dict(UNCLEAR)
    intent = str(d.get("intent") or "").strip().lower()
    if intent not in INTENTS:
        return dict(UNCLEAR)
    out = {"intent": intent, "section_id": None, "row": None, "question": None}
    sid = d.get("section_id")
    if sid not in (None, "", "null"):
        if str(sid) in ids:
            out["section_id"] = str(sid)
        else:
            return dict(UNCLEAR)                     # a target outside the list: not trusted at all
    row = d.get("row")
    if row not in (None, "", "null"):
        match = next((lab for lab in labels if lab.lower() == str(row).strip().lower()), None)
        if match is None:
            return dict(UNCLEAR)
        out["row"] = match
    q = d.get("question")
    if isinstance(q, str) and q.strip() and q.strip().lower() != "null":
        out["question"] = q.strip()
    return out


def _base_url(provider: str) -> str:
    base = os.environ.get("LLM_BASE_URL", "").strip().rstrip("/")
    if base:
        return base
    return "http://localhost:11434/v1" if provider == "ollama" else "https://api.openai.com/v1"


def _default_model(provider: str) -> str:
    return {"anthropic": "claude-opus-5", "openai": "gpt-4o-mini",
            "ollama": DEFAULT_OLLAMA_MODEL}.get(provider, "")


def _config():
    """(provider, model, key) or None when no answer model is configured."""
    provider = os.environ.get("LLM_PROVIDER", "anthropic").strip().lower()
    key = os.environ.get("LLM_API_KEY", "").strip()
    if not key and provider != "ollama":
        return None
    model = os.environ.get("LLM_MODEL", "").strip() or _default_model(provider)
    return provider, model, key


def make_llm():
    """Returns an async llm(messages)->str, or None for extractive mode."""
    cfg = _config()
    if cfg is None:
        return None
    provider, model, key = cfg

    async def llm(messages: list) -> str:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _post, provider, key, model, messages)

    return llm


def describe_llm() -> dict:
    """For /api/status: which answer source is active, never the key."""
    cfg = _config()
    if cfg is None:
        return {"active": "extractive", "provider": None, "model": None}
    provider, model, _ = cfg
    return {"active": "llm", "provider": provider, "model": model}


def check_llm() -> dict:
    """Is the configured answer model reachable and present? Never raises.

    {"ok": bool, "provider", "model", "detail", "hint"}. For ollama/openai the
    server's /v1/models list is consulted; a missing local model gets the
    exact `ollama pull` to run. Anthropic is not probed (a key is all we can
    check without spending a request)."""
    cfg = _config()
    if cfg is None:
        return {"ok": False, "provider": None, "model": None,
                "detail": "no answer model configured; answers are extractive", "hint": ""}
    provider, model, key = cfg
    out = {"ok": True, "provider": provider, "model": model, "detail": "configured", "hint": ""}
    if provider == "anthropic":
        return out
    import requests
    base = _base_url(provider)
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    try:
        r = requests.get(f"{base}/models", headers=headers, timeout=5)
        r.raise_for_status()
        ids = [m.get("id", "") for m in r.json().get("data", [])]
    except Exception as e:
        out.update(ok=False, detail=f"{provider} unreachable at {base}: {e}"[:160],
                   hint=("start Ollama (`ollama serve`); from WSL2 set LLM_BASE_URL to the "
                         "Windows host IP if localhost does not reach it") if provider == "ollama" else "")
        return out
    # Ollama lists "granite4.2:3b"; a bare "granite4.2" request resolves to ":latest".
    want = model if ":" in model else f"{model}:latest"
    if ids and want not in ids and model not in ids:
        out.update(ok=False, detail=f"model {model!r} not present on {base}",
                   hint=f"ollama pull {model}" if provider == "ollama" else "")
        return out
    out["detail"] = f"reachable at {base}; {len(ids)} model(s) listed"
    return out


def warm_llm() -> dict:
    """Load the model into memory with a one-token request so the first spoken
    answer does not pay the cold start. Returns {"ok", "load_ms", "detail"}."""
    cfg = _config()
    if cfg is None or cfg[0] == "anthropic":
        return {"ok": True, "load_ms": 0, "detail": "nothing to warm"}
    provider, model, key = cfg
    t0 = time.monotonic()
    try:
        _post(provider, key, model, [{"role": "user", "content": "Reply with OK."}], max_tokens=1)
    except Exception as e:
        return {"ok": False, "load_ms": round((time.monotonic() - t0) * 1000, 1),
                "detail": str(e)[:160]}
    return {"ok": True, "load_ms": round((time.monotonic() - t0) * 1000, 1), "detail": "warm"}


def _main(argv: list[str]) -> int:
    """`python llm.py --check`: verify the configured answer path end to end
    and time one grounded-style round trip. Exit 0 only if the model answered."""
    if "--check" not in argv:
        print(__doc__)
        return 0
    d = describe_llm()
    print(f"answers: {d['active']}  provider={d['provider']}  model={d['model']}")
    c = check_llm()
    print(f"check:   {'ok' if c['ok'] else 'FAIL'}  {c['detail']}")
    if not c["ok"]:
        if c["hint"]:
            print(f"hint:    {c['hint']}")
        return 1
    w = warm_llm()
    print(f"warm:    {'ok' if w['ok'] else 'FAIL'}  {w['load_ms']} ms  {w['detail']}")
    if not w["ok"]:
        return 1
    provider, model, key = _config()
    msgs = [{"role": "system", "content": "Answer only from the document text. One or two spoken sentences. Never interpret."},
            {"role": "user", "content": "DOCUMENT TEXT:\n[Section 4, clause b]\nThe waiting period for pre-existing "
                                        "diseases is thirty six months of continuous coverage.\n\nQUESTION: what does that mean"}]
    t0 = time.monotonic()
    ans = _post(provider, key, model, msgs)
    ms = round((time.monotonic() - t0) * 1000, 1)
    print(f"answer:  {ms} ms\n  {ans}")
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
