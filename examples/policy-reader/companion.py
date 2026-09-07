"""The companion: no radio silence while a document is processed.

Two roles, one boundary. The COMPANION speaks progress, one engagement
question, acknowledgements, a plan and fillers, and may see only the title,
page and section counts, section titles, stage names, the listener's own
words and the ledger state. It never states or paraphrases anything the
document says -- no numbers, amounts, percentages, section citations. The
READER answers from the document, as before, through grounding.resolve and
the answer path with `answer_source` and `retrieval_path` in the trace. A
question parked during ingestion is stored, never answered here.

Text comes from templates first. A model (COMPANION_MODEL, default the
answer model) is used only to reword an acknowledgement, behind
`companion_guard`; on any failure the template is spoken. So the companion
works with no model at all.

`Narrator` runs from the upload handler while a tab has the voice: real
progress lines as stage events arrive, the engagement question at ~5 s, the
acknowledgement on a reply, the plan line at 20 s without one, and fillers
after every 12 s without a line (five templates in rotation, none repeated
until all have been heard; never a document fact). Every line is a `companion` unit spoken under the
session's speak lock, acked and interruptible; any listener reply cancels
the line in flight. `narration_gap_ms{max, count}` is reported per ingest.
"""
from __future__ import annotations

import asyncio
import os
import re
import time
from typing import Optional

COMPANION_QUESTION_AT_S = float(os.environ.get("COMPANION_QUESTION_AT_S", "5"))
COMPANION_PLAN_AT_S = float(os.environ.get("COMPANION_PLAN_AT_S", "20"))
COMPANION_FILLER_AFTER_S = float(os.environ.get("COMPANION_FILLER_AFTER_S", "12"))
COMPANION_MAX_FILLERS = 5                      # the rotation: five templates, cycled while processing goes on

ENGAGEMENT_QUESTION = "While I do this, is there anything you want to know from it? I'll look for it first."
PLAN_LINE = "Okay. Once I'm done I'll give you an overview, and the sections will show up below."
ACK_TEMPLATE = "Okay, I'll keep that in mind: {phrase}."
READY_LINE = "First, what you asked earlier."
OPENING_LINE = "Getting to know {title}: {n} section{s}."
TAKEOVER_LINE = "I'll pause here and go through {title}."
BACKLOG_LINE = "While you were away I went through {title}. It has {n} section{s}."
BACKLOG_PENDING_LINE = "While you were away I started going through {title}. I'm still on it."
DUPLICATE_LINE = "That looks like {title}, which is already here."
START_WITH = " Or shall I start with {phrase}?"
FILLERS = [
    "Still working. This one is on the longer side.",
    "About halfway through the sections.",          # only once the stage timings support it
    "Almost there.",
    "Bear with me, still going.",
    "Not long now.",
]
HALFWAY_FILLER = FILLERS[1]


# ------------------------------------------------------------------ templates
def progress_line(stage: str, status: str, extra: Optional[dict] = None) -> Optional[str]:
    """The line for a real stage event, or None for a stage that says nothing."""
    extra = extra or {}
    if status not in ("ok", "running"):
        return None
    if stage == "extract":
        pages = int(extra.get("pages") or 0)
        return f"Got the text, {pages} page{'s' if pages != 1 else ''}." if pages else "Got the text."
    if stage == "structure":
        titles = [str(t) for t in (extra.get("headings") or []) if str(t).strip()]
        n = int(extra.get("n_sections") or len(titles))
        if not n:
            return "I can see the layout now."
        first = ", ".join(titles[:3])
        more = ", and more" if n > 3 else ""
        return f"I can see {n} section{'s' if n != 1 else ''}: {first}{more}." if first else f"I can see {n} sections."
    if stage == "pii_scan":
        return "Checking the wording and any personal details."
    if stage == "enrich" and status == "running":
        return "Nearly there, putting an overview together."
    if stage == "done":
        return "Done."
    return None


def topic_phrase(reply_text: str, slot: Optional[str] = None) -> str:
    """What the acknowledgement names: the understanding's cleaned question
    when there is one, else the reply's first eight words."""
    src = (slot or reply_text or "").strip().rstrip("?.! ")
    words = src.split()
    phrase = " ".join(words[:8])
    return phrase.lower() if phrase else "that"


# ------------------------------------------------------------------ the guard
_DIGIT = re.compile(r"\d")
_CURRENCY = re.compile(r"[%$£€₹]|\b(?:rupees?|dollars?|pounds?|euros?|percent)\b", re.I)
_CITE = re.compile(r"\b(?:section|clause)s?\b", re.I)
_TOKEN = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> list:
    return _TOKEN.findall((text or "").lower())


def _fixture_text(fixture) -> str:
    if fixture is None:
        return ""
    if isinstance(fixture, str):
        return fixture
    if isinstance(fixture, dict):
        clauses = fixture.get("clauses") or []
    else:
        clauses = getattr(fixture, "clauses", None) or []
    return " ".join(c.get("text_display", "") for c in clauses if isinstance(c, dict))


def companion_guard(text: str, fixture=None) -> Optional[str]:
    """Why a line may not be spoken by the companion, or None when it may:
    a digit, a percentage or currency, a section or clause citation, or any
    six-word span that appears in the document's clause text (the companion
    must never paraphrase the document)."""
    t = text or ""
    if _DIGIT.search(t):
        return "digit"
    if _CURRENCY.search(t):
        return "currency"
    if _CITE.search(t):
        return "citation"
    body = " ".join(_tokens(_fixture_text(fixture)))
    if body:
        toks = _tokens(t)
        for i in range(0, max(0, len(toks) - 5)):
            span = " ".join(toks[i:i + 6])
            if f" {span} " in f" {body} ":
                return "fixture span"
    return None


# ------------------------------------------------------------------ the one model use
def companion_model() -> Optional[tuple]:
    """(provider, model, key) for the companion, or None: COMPANION_MODEL when
    set, else the answer model; no answer model configured -> no model."""
    import llm
    cfg = llm._config()
    if cfg is None:
        return None
    provider, model, key = cfg
    return provider, (os.environ.get("COMPANION_MODEL", "").strip() or model), key


def _think_off(model: str) -> dict:
    return {"think": False, "reasoning_effort": "none"} if model.lower().startswith("qwen") else {}


def phrase_ack(reply_text: str, slot: Optional[str] = None, fixture=None) -> tuple:
    """The acknowledgement for a parked question: (text, "template" | "model").
    The template names the topic phrase; a companion model may reword it, one
    short sentence, behind the guard; any failure is the template."""
    phrase = topic_phrase(reply_text, slot)
    template = ACK_TEMPLATE.format(phrase=phrase)
    cfg = companion_model()
    if cfg is None:
        return template, "template"
    provider, model, key = cfg
    import llm
    msgs = [{"role": "system", "content":
             "You reword one short acknowledgement for a voice assistant that is about to look something up. "
             "One sentence, under fifteen words, plain everyday words, warm and brief. Do not answer, add facts, "
             "numbers or names. Reply with the sentence only."},
            {"role": "user", "content": f'Acknowledgement to reword: "{template}"'}]
    try:
        out = llm._post(provider, key, model, msgs, max_tokens=40, timeout=3.0, raw=True, extra=_think_off(model))
    except Exception:
        return template, "template"
    out = " ".join((out or "").split()).strip().strip('"')
    content = [w for w in phrase.split() if len(w) >= 4]          # the rewording must still name the topic
    if (not out or len(out.split()) > 20 or companion_guard(out, fixture)
            or (content and not any(w in out.lower() for w in content))):
        return template, "template"
    return out, "model"


# ------------------------------------------------------------------ the narrator
class Narrator:
    """Ambient narration for one ingest, while a tab has the voice."""

    def __init__(self, session, socks, fillers_used: Optional[set] = None, doc=None, owner: str = "upload") -> None:
        self.s = session
        self.socks = socks
        self.doc = doc                                # the document being ingested or enriched: every line names it
        self.owner = owner                            # "upload": the upload handler finishes it; "open": _enrich does
        self.t_start = time.monotonic()
        self.last_spoken = self.t_start               # the filler clock: silence since the last line heard
        self.spoken: list = []                        # (t, source, text)
        self.queue: list = []                         # (source, text) pending real-event lines
        self.question_asked = False
        self.replied = False
        self.plan_spoken = False
        self.fillers_used = fillers_used if fillers_used is not None else set()   # session-wide: never repeated
        self.fillers_this_ingest = 0
        self.stages_done = 0
        self.finished = False
        self._cancel_token = 0
        self._task: Optional[asyncio.Task] = None

    # ---- lifecycle
    def start(self, opening_line: Optional[str] = None) -> None:
        if opening_line:
            self.queue.append(("event", opening_line))
        self._task = asyncio.ensure_future(self._run())

    async def finish(self, ok: bool = True) -> dict:
        """The ingest is over: the lines still queued, "Done.", the gap metric,
        the task stopped."""
        self.finished = True
        if self._task and not self._task.done():
            # Let a line in flight finish (and be counted) rather than cancel
            # it mid-unit; the loop exits on `finished`. Cancel only if stuck.
            try:
                await asyncio.wait_for(self._task, timeout=30.0)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                if not self._task.done():
                    self._task.cancel()
                    try:
                        await self._task
                    except (asyncio.CancelledError, Exception):
                        pass
        if ok:
            while self.queue:
                source, text = self.queue.pop(0)
                await self._speak(source, "template", text)
            await self._speak("event", "template", progress_line("done", "ok"))
        marks = [self.t_start] + [t for t, _, _ in self.spoken] + [time.monotonic()]
        gaps = [round((b - a) * 1000) for a, b in zip(marks, marks[1:])]
        metric = {"max": max(gaps) if gaps else 0, "count": len(self.spoken)}
        self.s.events.emit("narration_gap_ms", max=metric["max"], count=metric["count"], gaps=gaps,
                           document=self.doc.name if self.doc else None, owner=self.owner)
        return metric

    def prime(self, source: str, text: str) -> None:
        """A line spoken for this processing before the narrator existed (the
        takeover line): counted, and the filler clock starts from it."""
        now = time.monotonic()
        self.spoken.append((now, source, text))
        self.last_spoken = now

    def cancel_line(self) -> None:
        """Any listener reply: the line in flight stops, queued fillers drop."""
        self._cancel_token += 1

    # ---- inputs
    async def on_event(self, stage: str, status: str, detail: str = "", extra: Optional[dict] = None) -> None:
        extra = extra or {}
        if status == "ok" and stage in ("extract", "structure", "segment", "normalize", "pii_scan", "validate", "write"):
            self.stages_done += 1
        if stage == "structure" and status == "ok":
            titles = [str(t) for t in (extra.get("headings") or []) if str(t).strip()]
            await self.s.sections_found(titles[:40], self.socks)     # to the client now, before enrichment
        line = progress_line(stage, status, extra)
        if line and stage != "done":                  # "Done." is finish()'s
            self.queue.append(("event", line))

    async def on_reply(self, reply_text: str, slot: Optional[str] = None) -> str:
        """The engagement question answered: acknowledge (template, or the
        companion model behind the guard). Returns the topic phrase."""
        self.replied = True
        self.queue = [q for q in self.queue if q[0] == "event"]
        phrase = topic_phrase(reply_text, slot)
        text, origin = await asyncio.get_running_loop().run_in_executor(None, phrase_ack, reply_text, slot, None)
        await self._speak("ack", origin, text)
        return phrase

    # ---- the timeline
    async def _run(self) -> None:
        try:
            while not self.finished:
                await asyncio.sleep(0.25)
                now = time.monotonic()
                if self.queue:
                    source, text = self.queue.pop(0)
                    await self._speak(source, "template", text)
                    continue
                if not self.question_asked and now - self.t_start >= COMPANION_QUESTION_AT_S:
                    self.question_asked = True
                    if self.s.prompt_kind() is None and self.s.voice_free() is None:
                        self.s.events.emit("companion_spoken", source="question", origin="template",
                                           text=ENGAGEMENT_QUESTION, document=self.doc.name if self.doc else None)
                        self.spoken.append((time.monotonic(), "question", ENGAGEMENT_QUESTION))
                        self.last_spoken = time.monotonic()
                        await self.s.open_ingest_wait(self.socks, text=ENGAGEMENT_QUESTION, doc=self.doc)
                    continue
                if (self.question_asked and not self.replied and not self.plan_spoken
                        and now - self.t_start >= COMPANION_PLAN_AT_S):
                    self.plan_spoken = True
                    if self.s.prompt_kind() == "ingest_wait":
                        await self.s.resolve_prompt("timeout", None)     # no further questions this ingest
                    await self._speak("plan", "template", PLAN_LINE)
                    continue
                if now - self.last_spoken >= COMPANION_FILLER_AFTER_S:
                    line = self._next_filler()
                    if line:
                        await self._speak("filler", "template", line)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.s.events.emit("companion_error", error=str(e)[:200])

    def _next_filler(self) -> Optional[str]:
        """The next filler in rotation: one not yet heard this session first;
        once all five have been heard the rotation starts again, so a long
        generation is never left in silence."""
        allowed = [l for l in FILLERS if not (l == HALFWAY_FILLER and self.stages_done < 3)]
        fresh = [l for l in allowed if l not in self.fillers_used]
        if not fresh:
            self.fillers_used.clear()
            fresh = allowed
        line = fresh[0]
        self.fillers_used.add(line)
        self.fillers_this_ingest += 1
        return line

    async def _speak(self, source: str, origin: str, text: Optional[str]) -> None:
        if not text or (self.finished and source not in ("event", "ack")):
            return
        token = self._cancel_token
        st = await self.s.speak_companion(text, source, origin, self.socks, lambda: self._cancel_token != token,
                                          doc=self.doc)
        if st is not None:
            self.spoken.append((time.monotonic(), source, text))
            self.last_spoken = time.monotonic()
