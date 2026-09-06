#!/usr/bin/env python3
"""Text REPL over a document library, exercising the grounding layer with no audio.

The point is to reproduce the *positional* behaviour of the voice reader in a
form you can drive from a keyboard: the spoiler gate sees a real read cursor,
deictic questions resolve against the last clause actually heard (including a
partially heard one), and resume lands on a sentence boundary. No Rime, no
LiveKit, no audio clock -- `stop <chars>` stands in for the delivery boundary
that WordMap.offset_at() computes from real playback acks.

  python examples/policy-reader/chat_demo.py                     # whole library
  python examples/policy-reader/chat_demo.py --fixture fixtures/policy.json

Selecting a document at runtime is supported; ingesting one is not.
`fixtures/index.json` is the only source of documents that can be opened.

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
from delivery_layer.position import resume_point       # noqa: E402
from library import Library, LibraryError            # noqa: E402

# How many arguments each command takes. This is not cosmetic: without it,
# "do I qualify" prefix-matches `docs` and a listener's question silently
# becomes a command. A no-argument command followed by words is a question.
ARITY = {
    "read": "optional", "stop": "required", "resume": "none", "jump": "required",
    "where": "none", "ledger": "optional", "quit": "none", "help": "none",
    "docs": "none", "open": "required",
}
COMMANDS = list(ARITY)

HELP = """commands (prefix-matchable):
  docs          list the documents in the library
  open <name>   switch to a document, keeping your place in the one you leave
  read [<id>]   read the next clause, or a named one; advances the cursor
  stop <chars>  interrupt the current clause at char offset <chars>
  resume        re-enter at the sentence containing the cut, then mark it read
  jump <id>     move the read cursor to a clause
  where         cursor / last-heard / boundary
  ledger        what this session heard; `ledger all` lists every clause
  quit          exit
anything else is a question, answered from the CURRENT document only."""


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
    """All REPL behaviour. `handle(line) -> str` so tests can drive it directly.

    Position state lives on the Document's Session, not here, so switching
    documents saves and restores it for free.
    """

    def __init__(self, fixture_or_library, events: Optional[EventLog] = None, llm=None) -> None:
        self.ev = events or EventLog(None, session_id="chat-demo")
        if isinstance(fixture_or_library, Library):
            self.lib = fixture_or_library
        else:
            self.lib = Library.single(fixture_or_library, self.ev)
        if self.lib.current is None:
            first = self.lib.list()
            if first:
                self.lib.open(first[0]["name"])
        self.llm = llm
        self.done = False

    # ------------------------------------------------- current doc shortcuts
    @property
    def doc(self):
        return self.lib.current

    @property
    def g(self):
        return self.lib.current.grounding

    @property
    def sess(self):
        return self.lib.current.session

    # Backwards-compatible aliases onto the current document's session.
    @property
    def cursor(self) -> int:
        return self.sess.read_cursor

    @property
    def read_index(self) -> int:
        return self.sess.read_index

    @property
    def current_id(self) -> Optional[str]:
        return self.sess.current_unit_id

    @property
    def last_heard_id(self) -> Optional[str]:
        return self.sess.last_heard_unit_id

    @property
    def boundary(self) -> int:
        return self.sess.boundary_char

    @property
    def status(self) -> dict:
        return self.sess.ledger

    # ---------------------------------------------------------------- utils
    def _clause(self, cid: str) -> Optional[dict]:
        return self.g.by_id.get(cid)

    def _heard_text(self) -> Optional[str]:
        s = self.sess
        if not s.last_heard_unit_id:
            return None
        c = self.g.by_id[s.last_heard_unit_id]
        if s.boundary_char >= len(c["text_display"]):
            return None
        return c["text_display"][:s.boundary_char]

    # ------------------------------------------------------------- library
    def cmd_docs(self, arg: str) -> str:
        rows = self.lib.list()
        if not rows:
            return "the library is empty; nothing is registered in index.json"
        cur = self.doc.name if self.doc else None
        out = ["documents (index.json):"]
        for r in rows:
            mark = "*" if r["name"] == cur else " "
            out.append(f" {mark} {r['name']:<20} {r['clause_count']:>4} clauses  {r['title']}")
        out.append("  * = current;  open <name> to switch")
        return "\n".join(out)

    def cmd_open(self, arg: str) -> str:
        if not arg.strip():
            return "usage: open <name>"
        try:
            doc = self.lib.open(arg.strip())
        except LibraryError as e:
            return str(e)
        s = doc.session
        where = (f"resuming at index {s.read_cursor}"
                 if s.read_cursor or s.last_heard_unit_id else "starting at the beginning")
        return f"[{doc.name}] {doc.title} — {len(doc.grounding.clauses)} clauses, {where}"

    # ------------------------------------------------------------ commands
    def cmd_read(self, arg: str) -> str:
        s = self.sess
        if arg:
            c = self._clause(arg.strip())
            if not c:
                return f"no clause {arg.strip()!r}"
            s.read_cursor = c["index"] + 1
        else:
            if s.read_cursor >= len(self.g.clauses):
                return "end of document; nothing left to read."
            c = self.g.clauses[s.read_cursor]
            s.read_cursor += 1
        s.read_index = c["index"]
        s.current_unit_id = c["id"]
        s.last_heard_unit_id = c["id"]
        s.boundary_char = len(c["text_display"])
        s.ledger[c["id"]] = "heard"
        self.ev.emit("unit_heard", document=self.doc.name, context_id=c["id"],
                     char_end=s.boundary_char, of=len(c["text_display"]))
        return f"[{c['id']}] {c['section_title']}\n{c['text_display']}"

    def cmd_stop(self, arg: str) -> str:
        s = self.sess
        if not s.current_unit_id:
            return "nothing is being read; use `read` first."
        try:
            n = int(arg.strip())
        except (TypeError, ValueError):
            return "usage: stop <chars>"
        c = self.g.by_id[s.current_unit_id]
        total = len(c["text_display"])
        n = max(0, min(n, total))
        s.last_heard_unit_id = c["id"]
        s.boundary_char = n
        if n >= total:
            # Cutting at or past the end is not an interruption; recording it as
            # one would put a false truncation in the ledger.
            s.ledger[c["id"]] = "heard"
            self.ev.emit("unit_heard", document=self.doc.name, context_id=c["id"],
                         char_end=n, of=total)
            return f"[{c['id']}] already fully heard ({total} chars); nothing was cut."
        s.ledger[c["id"]] = f"truncated@{n}"
        self.ev.emit("unit_truncated", document=self.doc.name, context_id=c["id"],
                     char_end=n, of=total)
        # cursor deliberately not advanced: the listener never heard the rest.
        return (f"[{c['id']}] interrupted at {n}/{total} chars\n"
                f"{c['text_display'][:n]} [interrupted]")

    def cmd_resume(self, arg: str) -> str:
        s = self.sess
        if not s.last_heard_unit_id:
            return "nothing to resume."
        c = self.g.by_id[s.last_heard_unit_id]
        rp = resume_point(c["id"], c["text_display"], c["sentences"], s.boundary_char,
                          c["section_title"])
        self.ev.emit("position_restored", document=self.doc.name, unit_id=rp.unit_id,
                     sentence_index=rp.sentence_index, char_start=rp.char_start)
        s.ledger[c["id"]] = "heard"
        s.boundary_char = len(c["text_display"])
        s.read_cursor = max(s.read_cursor, c["index"] + 1)
        if not rp.text:
            return f"[{c['id']}] whole clause already heard; nothing to re-read."
        return (f"[{c['id']}] resume at sentence {rp.sentence_index}, char {rp.char_start}\n"
                f"{normalize(rp.spoken_prefix + rp.text)}")

    def cmd_jump(self, arg: str) -> str:
        s = self.sess
        c = self._clause(arg.strip())
        if not c:
            return f"no clause {arg.strip()!r}"
        self.ev.emit("position_saved", document=self.doc.name, unit_id=s.current_unit_id,
                     cursor=s.read_cursor)
        s.read_cursor = c["index"]
        self.ev.emit("position_restored", document=self.doc.name, unit_id=c["id"],
                     sentence_index=0, char_start=0)
        return f"cursor -> [{c['id']}] {c['section_title']} (index {c['index']})"

    def cmd_where(self, arg: str) -> str:
        s = self.sess
        nxt = self.g.clauses[s.read_cursor] if s.read_cursor < len(self.g.clauses) else None
        return (f"document   : {self.doc.name}\n"
                f"cursor     : {nxt['id'] if nxt else '(end)'} index {s.read_cursor}\n"
                f"last read  : {s.current_unit_id or '(none)'} index {s.read_index}\n"
                f"last heard : {s.last_heard_unit_id or '(none)'} boundary {s.boundary_char} chars")

    def cmd_ledger(self, arg: str) -> str:
        s = self.sess
        show_all = arg.strip().lower().startswith("a")
        lines, never = [], 0
        for c in self.g.clauses:
            st = s.status_of(c["id"])
            if st == "never_sent" and not show_all:
                never += 1
                continue
            lines.append(f"  {c['id']:<16} {st}")
        if not show_all:
            lines.append(f"  … {never} never_sent (use `ledger all` to list them)")
        return f"ledger for [{self.doc.name}]:\n" + "\n".join(lines)

    def cmd_help(self, arg: str) -> str:
        return HELP

    def cmd_quit(self, arg: str) -> str:
        self.done = True
        return "bye."

    # ------------------------------------------------------------ question
    def ask(self, question: str) -> str:
        s = self.sess
        r = self.g.resolve(question, s.last_heard_unit_id, read_cursor=max(s.read_index, 0))
        target = r.hits[0].unit_id if r.hits else (r.beyond[0].unit_id if r.beyond else "-")
        s.history.append((question, r.kind, target))
        self.ev.emit("question_resolved", document=self.doc.name, kind=r.kind,
                     unit_id=target, question=question)
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
        # A no-argument command with words after it is a question, not a command.
        # This is what keeps "do I qualify" from being read as `docs`.
        if cmd and ARITY[cmd] == "none" and rest.strip():
            cmd = None
        if cmd:
            self.ev.emit("repl_command", command=cmd, arg=rest.strip(),
                         document=self.doc.name if self.doc else None)
            return getattr(self, f"cmd_{cmd}")(rest)
        return self.ask(line)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fixture", default=None,
                    help="open a single fixture instead of the whole library")
    ap.add_argument("--index", default=None, help="path to fixtures/index.json")
    ap.add_argument("--keep-trace", action="store_true", help="keep traces/chat_demo.jsonl on exit")
    args = ap.parse_args()

    trace = ROOT / "traces" / "chat_demo.jsonl"
    ev = EventLog(trace, session_id="chat-demo")

    here = Path(__file__).parent
    if args.fixture:
        fx = Path(args.fixture)
        if not fx.is_absolute() and not fx.exists():
            fx = here / args.fixture
        if not fx.exists():
            print(f"no such fixture: {args.fixture}", file=sys.stderr)
            return 1
        lib = Library.single(fx, ev)
    else:
        index = Path(args.index) if args.index else here / "fixtures" / "index.json"
        if not index.exists():
            print(f"no registry at {index}. Run scripts/ingest.py, or pass --fixture.",
                  file=sys.stderr)
            return 1
        lib = Library(index, ev)
        if not lib.list():
            print(f"{index} registers no documents.", file=sys.stderr)
            return 1

    llm = make_llm()
    s = ChatSession(lib, ev, llm)
    print(f"library: {len(lib.list())} document(s) — `docs` to list, `open <name>` to switch")
    print(f"current: [{s.doc.name}] {s.doc.title} — {len(s.g.clauses)} clauses")
    if llm is None:
        print("[extractive — no LLM key]")
    print(HELP)
    try:
        while not s.done:
            try:
                line = input(f"\n[{s.doc.name}] > ")
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
