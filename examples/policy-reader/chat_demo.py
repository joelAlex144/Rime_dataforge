#!/usr/bin/env python3
"""Text REPL over any fixture, exercising the grounding layer with no audio.

The point is to reproduce the *positional* behaviour of the voice reader in a
form you can drive from a keyboard: the spoiler gate sees a real read cursor,
deictic questions resolve against the last clause actually heard (including a
partially heard one), and resume lands on a sentence boundary. No Rime, no
LiveKit, no audio clock -- `stop <chars>` stands in for the delivery boundary
that WordMap.offset_at() computes from real playback acks.

  python examples/policy-reader/chat_demo.py --fixture fixtures/policy.json

Commands are case-insensitive and prefix-matchable (`rea`, `res`, `w`, `l`).
Anything that is not a command is treated as a question.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).parent))

from delivery_layer.events import EventLog           # noqa: E402
from delivery_layer.normalize import normalize       # noqa: E402
from delivery_layer.resume import resume_point       # noqa: E402
from grounding import Grounding                      # noqa: E402

COMMANDS = ["read", "stop", "resume", "jump", "where", "ledger", "quit", "help"]

HELP = """commands (prefix-matchable):
  read [<id>]   read the next clause, or a named one; advances the cursor
  stop <chars>  interrupt the current clause at char offset <chars>
  resume        re-enter at the sentence containing the cut, then mark it read
  jump <id>     move the read cursor to a clause
  where         cursor / last-heard / boundary
  ledger        what this session heard; `ledger all` lists every clause
  quit          exit
anything else is a question."""


def match_command(token: str) -> Optional[str]:
    """Exact match, else a unique prefix. Returns None when it is not a command."""
    t = token.lower()
    if t in COMMANDS:
        return t
    hits = [c for c in COMMANDS if c.startswith(t)]
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        return "\0ambiguous:" + ",".join(hits)
    return None


# --------------------------------------------------------------------------
# optional LLM path -- plain HTTP, no SDK, key from the environment only
# --------------------------------------------------------------------------

def _post(provider: str, key: str, model: str, messages: list) -> str:
    import requests

    system = "\n".join(m["content"] for m in messages if m["role"] == "system")
    turns = [m for m in messages if m["role"] != "system"]
    if provider == "anthropic":
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": model, "max_tokens": 1024, "system": system, "messages": turns},
            timeout=60)
        r.raise_for_status()
        d = r.json()
        if d.get("stop_reason") == "refusal":
            return "I can't answer that one from the document. Ask the insurer or lender."
        return "".join(b.get("text", "") for b in d.get("content", []) if b.get("type") == "text").strip()
    if provider == "openai":
        r = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}", "content-type": "application/json"},
            json={"model": model, "messages": messages}, timeout=60)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip()
    raise RuntimeError(f"unknown LLM_PROVIDER {provider!r}; use 'anthropic' or 'openai'")


def make_llm():
    """Returns an async llm(messages)->str, or None for extractive mode."""
    key = os.environ.get("LLM_API_KEY", "").strip()
    if not key:
        return None
    provider = os.environ.get("LLM_PROVIDER", "anthropic").strip().lower()
    model = os.environ.get("LLM_MODEL", "").strip() or (
        "claude-opus-5" if provider == "anthropic" else "gpt-4o-mini")

    async def llm(messages: list) -> str:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _post, provider, key, model, messages)

    return llm


# --------------------------------------------------------------------------
# the session
# --------------------------------------------------------------------------

class ChatSession:
    """All REPL behaviour. `handle(line) -> str` so tests can drive it directly."""

    def __init__(self, fixture: str | Path, events: Optional[EventLog] = None, llm=None) -> None:
        self.g = Grounding(fixture)
        self.ev = events or EventLog(None, session_id="chat-demo")
        self.llm = llm
        self.cursor = 0                       # index of the NEXT clause to read
        self.read_index = -1                  # index of the last clause read
        self.current_id: Optional[str] = None  # clause most recently started
        self.last_heard_id: Optional[str] = None
        self.boundary = 0                     # chars of last_heard actually heard
        self.status: dict[str, str] = {}      # id -> "heard" | "truncated@N"
        self.done = False

    # ---------------------------------------------------------------- utils
    def _clause(self, cid: str) -> Optional[dict]:
        return self.g.by_id.get(cid)

    def _heard_text(self) -> Optional[str]:
        if not self.last_heard_id:
            return None
        c = self.g.by_id[self.last_heard_id]
        if self.boundary >= len(c["text_display"]):
            return None
        return c["text_display"][:self.boundary]

    # ------------------------------------------------------------ commands
    def cmd_read(self, arg: str) -> str:
        if arg:
            c = self._clause(arg.strip())
            if not c:
                return f"no clause {arg.strip()!r}"
            self.cursor = c["index"] + 1
        else:
            if self.cursor >= len(self.g.clauses):
                return "end of document; nothing left to read."
            c = self.g.clauses[self.cursor]
            self.cursor += 1
        self.read_index = c["index"]
        self.current_id = c["id"]
        self.last_heard_id = c["id"]
        self.boundary = len(c["text_display"])
        self.status[c["id"]] = "heard"
        self.ev.emit("unit_heard", context_id=c["id"], char_end=self.boundary,
                     of=len(c["text_display"]))
        return f"[{c['id']}] {c['section_title']}\n{c['text_display']}"

    def cmd_stop(self, arg: str) -> str:
        if not self.current_id:
            return "nothing is being read; use `read` first."
        try:
            n = int(arg.strip())
        except (TypeError, ValueError):
            return "usage: stop <chars>"
        c = self.g.by_id[self.current_id]
        total = len(c["text_display"])
        n = max(0, min(n, total))
        self.last_heard_id = c["id"]
        self.boundary = n
        if n >= total:
            # Cutting at or past the end is not an interruption; recording it as
            # one would put a false truncation in the ledger.
            self.status[c["id"]] = "heard"
            self.ev.emit("unit_heard", context_id=c["id"], char_end=n, of=total)
            return f"[{c['id']}] already fully heard ({total} chars); nothing was cut."
        self.status[c["id"]] = f"truncated@{n}"
        self.ev.emit("unit_truncated", context_id=c["id"], char_end=n, of=total)
        # cursor deliberately not advanced: the listener never heard the rest.
        return (f"[{c['id']}] interrupted at {n}/{total} chars\n"
                f"{c['text_display'][:n]} [interrupted]")

    def cmd_resume(self, arg: str) -> str:
        if not self.last_heard_id:
            return "nothing to resume."
        c = self.g.by_id[self.last_heard_id]
        rp = resume_point(c["id"], c["text_display"], c["sentences"], self.boundary,
                          c["section_title"])
        self.ev.emit("position_restored", unit_id=rp.unit_id, sentence_index=rp.sentence_index,
                     char_start=rp.char_start)
        self.status[c["id"]] = "heard"
        self.boundary = len(c["text_display"])
        self.cursor = max(self.cursor, c["index"] + 1)
        if not rp.text:
            return f"[{c['id']}] whole clause already heard; nothing to re-read."
        return (f"[{c['id']}] resume at sentence {rp.sentence_index}, char {rp.char_start}\n"
                f"{normalize(rp.spoken_prefix + rp.text)}")

    def cmd_jump(self, arg: str) -> str:
        c = self._clause(arg.strip())
        if not c:
            return f"no clause {arg.strip()!r}"
        self.ev.emit("position_saved", unit_id=self.current_id, cursor=self.cursor)
        self.cursor = c["index"]
        self.ev.emit("position_restored", unit_id=c["id"], sentence_index=0, char_start=0)
        return f"cursor -> [{c['id']}] {c['section_title']} (index {c['index']})"

    def cmd_where(self, arg: str) -> str:
        nxt = self.g.clauses[self.cursor] if self.cursor < len(self.g.clauses) else None
        return (f"cursor     : {nxt['id'] if nxt else '(end)'} index {self.cursor}\n"
                f"last read  : {self.current_id or '(none)'} index {self.read_index}\n"
                f"last heard : {self.last_heard_id or '(none)'} boundary {self.boundary} chars")

    def cmd_ledger(self, arg: str) -> str:
        show_all = arg.strip().lower().startswith("a")
        lines = []
        never = 0
        for c in self.g.clauses:
            st = self.status.get(c["id"], "never-sent")
            if st == "never-sent" and not show_all:
                never += 1
                continue
            lines.append(f"  {c['id']:<16} {st}")
        if not show_all:
            lines.append(f"  … {never} never-sent (use `ledger all` to list them)")
        return "ledger (this session):\n" + "\n".join(lines)

    def cmd_help(self, arg: str) -> str:
        return HELP

    def cmd_quit(self, arg: str) -> str:
        self.done = True
        return "bye."

    # ------------------------------------------------------------ question
    def ask(self, question: str) -> str:
        r = self.g.resolve(question, self.last_heard_id, read_cursor=max(self.read_index, 0))
        target = r.hits[0].unit_id if r.hits else (r.beyond[0].unit_id if r.beyond else "-")
        self.ev.emit("question_resolved", kind=r.kind, unit_id=target, question=question)
        ans = asyncio.run(self.g.answer(r, llm=self.llm,
                                        heard_text_of_reference=self._heard_text()))
        return f"[{r.kind}] -> {target}\nA (spoken): {normalize(ans)}"

    # ------------------------------------------------------------- dispatch
    def handle(self, line: str) -> str:
        line = (line or "").strip()
        if not line:
            return ""
        head, _, rest = line.partition(" ")
        cmd = match_command(head)
        if cmd and cmd.startswith("\0ambiguous:"):
            return f"ambiguous command {head!r}: could be {cmd.split(':', 1)[1]}"
        if cmd:
            self.ev.emit("repl_command", command=cmd, arg=rest.strip())
            return getattr(self, f"cmd_{cmd}")(rest)
        return self.ask(line)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fixture", default="fixtures/policy.json")
    ap.add_argument("--keep-trace", action="store_true", help="keep traces/chat_demo.jsonl on exit")
    args = ap.parse_args()

    fx = Path(args.fixture)
    if not fx.is_absolute() and not fx.exists():
        fx = Path(__file__).parent / args.fixture
    if not fx.exists():
        print(f"no such fixture: {args.fixture}", file=sys.stderr)
        return 1

    trace = ROOT / "traces" / "chat_demo.jsonl"
    ev = EventLog(trace, session_id="chat-demo")
    llm = make_llm()
    s = ChatSession(fx, ev, llm)
    print(f"{s.g.title} — {len(s.g.clauses)} clauses")
    if llm is None:
        print("[extractive — no LLM key]")
    print(HELP)
    try:
        while not s.done:
            try:
                line = input("\n> ")
            except (EOFError, KeyboardInterrupt):
                break
            out = s.handle(line)
            if out:
                print(out)
    finally:
        ev.close()
        if not args.keep_trace and trace.exists():
            trace.unlink()
        elif args.keep_trace:
            print(f"\ntrace -> {trace.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
