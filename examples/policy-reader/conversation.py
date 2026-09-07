"""The conversation layer's state and its router (v2).

One object per session holds the prompt state server.py used to keep by hand:
the one open prompt, the timeouts per kind, the kinds the listener has muted
by letting them time out twice, the question parked while a document is being
ingested. `route()` is the four-step order from CLAUDE.md:

  1. an exact option word or chip text for the open prompt, or a navigation
     phrase -> the rules, no model;
  2. `llm.understand(text, ctx)` -> intent + slots in a closed schema;
  3. execute: topic -> find_section first, else the model's section_id -> the
     cue "{heading}, about {m} minutes. Starting." -> jump; question -> the
     existing ask path; every other intent -> its action, read against the
     open prompt's options when one is open;
  4. floor: no model, timeout, or `unclear` -> the v1 route exactly as built.

Retrieval and every action stay deterministic; the model only says what was
meant, and only from the lists it was given. Nothing here touches
delivery_layer/ or the interruption path: a jump is still `jump_to`.
"""
from __future__ import annotations

import asyncio
import time
from typing import Optional

# How a rules-routed reply is named in reply_understood (the same vocabulary
# the model uses), so the trace reads the same whichever way a reply went.
_NAV_AS_INTENT = {"goto": "topic", "go_on": "carry_on", "now": "yes", "overview_first": "brief",
                  "every": "question", "summarise": "question"}


class Conversation:
    def __init__(self, session, timeouts: dict) -> None:
        self.s = session
        self.timeouts = timeouts                    # kind -> seconds, None for no timer
        self.prompt: Optional[dict] = None          # the one open prompt (see open_prompt)
        self.muted: set = set()                     # kinds the listener let time out twice
        self.timeout_counts: dict = {}
        self.parked: dict = {}                      # document name -> {"question", "phrase"}, asked during its ingest_wait
        self.narrators: dict = {}                   # document name -> the Narrator of its ingest or enrichment
        self.pending_narration: dict = {}           # document name -> {"reason"}: processed with no voice; told at first play
        self.fillers_used: set = set()              # companion fillers spoken this session (never repeated)
        self.deferred_start: set = set()            # documents ready for start_choice once they are opened
        self.welcomed: bool = False                 # the welcome prompt is asked once per session
        self.last_prompt_at: float = -1e9           # monotonic; section_end keeps 3 min from it
        self.last_listener_jump_at: float = -1e9
        self.jump_since_boundary: bool = False      # a listener-requested jump since the last section boundary

    # The question parked for a document, by name; the properties read the
    # current document's, which is what the older call sites meant.
    def park(self, name: str, question: str, phrase: Optional[str] = None) -> None:
        self.parked[name] = {"question": question, "phrase": phrase or question}

    def parked_for(self, name: Optional[str]) -> Optional[dict]:
        return self.parked.get(name) if name else None

    @property
    def parked_question(self) -> Optional[str]:
        doc = self.s.library.current
        p = self.parked_for(doc.name if doc else None)
        return p["question"] if p else None

    @property
    def parked_phrase(self) -> Optional[str]:
        doc = self.s.library.current
        p = self.parked_for(doc.name if doc else None)
        return p["phrase"] if p else None

    @property
    def narrator(self):
        """The narrator of the document in progress, if any (any of them)."""
        for n in self.narrators.values():
            if not n.finished:
                return n
        return None

    # ------------------------------------------------------------ state
    @property
    def kind(self) -> Optional[str]:
        return self.prompt["kind"] if self.prompt else None

    def note_opened(self, kind: str) -> None:
        self.last_prompt_at = time.monotonic()

    def note_timeout(self, kind: str) -> bool:
        """A prompt the listener let time out. Twice in a session mutes the
        kind for the rest of it; returns True the moment that happens."""
        self.timeout_counts[kind] = self.timeout_counts.get(kind, 0) + 1
        if self.timeout_counts[kind] >= 2 and kind not in self.muted:
            self.muted.add(kind)
            return True
        return False

    def note_listener_jump(self) -> None:
        self.last_listener_jump_at = time.monotonic()
        self.jump_since_boundary = True

    # ------------------------------------------------------------ context for the model
    def ctx(self) -> dict:
        s = self.s
        doc = s.library.current
        g = doc.grounding if doc else None
        pend = self.prompt
        if pend is not None and pend["kind"] == "welcome":
            sections = [{"id": d["name"], "title": d["title"]} for d in s.library.list()]
        else:
            sections = [{"id": x["id"], "title": x["title"]} for x in (g.sections if g else [])]
        last = None
        if g is not None and doc.session.last_heard_unit_id in g.by_id:
            sec = g.section_of(g.by_id[doc.session.last_heard_unit_id]["index"])
            last = sec["title"] if sec else None
        reading = bool(s.playing or (s._reader and not s._reader.done()))
        return {"prompt_kind": pend["kind"] if pend else None,
                "options": list(pend["options"]) if pend else [],
                "sections": sections,
                "row_labels": list((pend or {}).get("payload", {}).get("labels") or []) if pend and pend["kind"] == "table_choice" else [],
                "last_heard": last, "reading": reading}

    async def understand(self, text: str) -> tuple:
        """(understanding or None, ms). None: no model, timeout, or unclear."""
        if self.s.llm is None:
            return None, 0
        import llm as llm_mod
        t0 = time.monotonic()
        try:
            u = await asyncio.get_running_loop().run_in_executor(None, llm_mod.understand, text, self.ctx())
        except Exception:
            u = None
        ms = round((time.monotonic() - t0) * 1000)
        if not u or u.get("intent") in (None, "unclear"):
            return None, ms
        return u, ms

    # ------------------------------------------------------------ the router
    async def route(self, text: str, socks=None, ws=None) -> dict:
        """The four steps. Returns the routing dict the ask handler acts on:
        {route: pending|nav|question|llm_classify|understood, via: rules|llm,
        intent, section_id, row, ms, ...}. `understood` means the action was
        taken here (a prompt answered, a jump); `question` means the ask path
        continues with `question`."""
        s = self.s
        q = (text or "").strip()
        rules = await s.route_reply_rules(q, classify=False)
        if rules["route"] in ("pending", "nav"):
            return self._as_rules(rules, 0)
        u, ms = await self.understand(q)
        if u is None:
            floor = await s.route_reply_rules(q, classify=True)      # v1 exactly, classifier included
            return self._as_rules(floor, ms)
        done = await self.execute(u, q, socks, ws)
        if done is None:                                             # nothing to do with it: the floor
            floor = await s.route_reply_rules(q, classify=True)
            return self._as_rules(floor, ms)
        done.update({"via": "llm", "ms": ms})
        return done

    @staticmethod
    def _as_rules(r: dict, ms: int) -> dict:
        route = r["route"]
        if route in ("pending", "llm_classify"):
            intent = r.get("choice")
        elif route == "nav":
            intent = _NAV_AS_INTENT.get(r.get("intent"), r.get("intent"))
        else:
            intent = "question"
        sec = ((r.get("data") or {}).get("section") or (r.get("nav") or {}).get("section") or {})
        r.update({"via": "rules" if route != "llm_classify" else "llm_classify", "ms": ms,
                  "intent": intent, "section_id": sec.get("id") if isinstance(sec, dict) else None,
                  "row": (r.get("data") or {}).get("row")})
        return r

    # ------------------------------------------------------------ execution
    def _section_for(self, u: dict, text: str):
        g = self.s.library.current.grounding
        sec = g.find_section(text)
        if sec is None and u.get("section_id"):
            sec = next((x for x in g.sections if x["id"] == u["section_id"]), None)
        return sec

    def _prompt_choice(self, kind: str, intent: str, sec, u: dict, text: str):
        """What the intent means as an option of the open prompt; None when the
        prompt has no option for it (a global action or the floor follows)."""
        s = self.s
        pl = (self.prompt or {}).get("payload", {})
        if kind == "start_choice":
            if intent == "topic" and sec is not None:
                return "topic", {"section": sec, "text": text}
            if intent in ("brief", "no", "carry_on"):
                return "brief", None
            if intent == "start":
                return "start", None
        elif kind == "confirm_topic":
            if intent in ("yes", "start", "carry_on"):
                return "yes", None
            if intent in ("no", "brief"):
                return "no", None
        elif kind == "choice":
            if intent in ("yes", "start", "carry_on"):
                return "now", None
            if intent == "brief":
                return "overview_first", None
        elif kind == "offer":
            if intent == "yes":
                return "yes", None
            if intent in ("no", "carry_on", "skip"):
                return "go_on", None
        elif kind == "table_choice":
            labels = pl.get("labels") or []
            if intent == "row":
                i = labels.index(u["row"]) if u.get("row") in labels else s.row_index(text, labels)
                if i is not None:
                    return "row", {"row": i, "text": text}
            if intent == "all":
                return "all", None
            if intent in ("carry_on", "no", "skip"):
                return "carry_on", None
        elif kind == "pick_topic":
            if intent == "topic" and sec is not None:
                return "topic", {"section": sec}
            if intent in ("start", "carry_on", "no", "yes"):
                return "top", None
        elif kind == "section_end":
            if intent in ("carry_on", "yes", "no"):
                return "carry_on", None
            if intent == "topic" and sec is not None:
                return "topic", {"section": sec}
        elif kind == "not_found":
            if intent in ("carry_on", "yes", "no"):
                return "carry_on", None
        elif kind == "end_choice":
            if intent == "topic" and sec is not None:
                return "section", {"section": sec}
            if intent == "recap":
                return "recap", None
            if intent in ("no", "carry_on", "yes", "start"):
                return "stop", None
        elif kind == "welcome":
            if intent in ("start", "yes", "carry_on"):
                return "first", None
        return None, None

    async def execute(self, u: dict, text: str, socks, ws) -> Optional[dict]:
        s = self.s
        doc = s.library.current
        g, sess = doc.grounding, doc.session
        intent = u["intent"]
        kind = self.kind
        base = {"route": "understood", "intent": intent, "section_id": u.get("section_id"), "row": u.get("row")}
        # A question: the prompt, if any, is answered by it; the ask path follows.
        if intent == "question":
            q = u.get("question") or text
            if kind == "ingest_wait":
                await s.resolve_prompt("reply", "question", ws=ws, data={"text": q})
                return {**base, "action": "parked", "question": q}
            if kind is not None:
                await s.resolve_prompt("reply", "question", ws=ws, data={"text": q})
            return {**base, "route": "question", "question": q}
        # The welcome prompt names documents, not sections.
        if kind == "welcome":
            name = s.find_document_by_title(text) or (u.get("section_id") if intent == "topic" else None)
            if name:
                await s.resolve_prompt("reply", "title", ws=ws, data={"name": name})
                return {**base, "action": "open", "section_id": name}
            choice, data = self._prompt_choice(kind, intent, None, u, text)
            if choice is not None:
                await s.resolve_prompt("reply", choice, ws=ws, data=data)
                return {**base, "action": f"prompt:{choice}"}
            return None
        if intent == "topic":
            # A topic the model understood: find_section on the text first, else
            # the model's section_id; then the cue and the jump -- whatever
            # prompt is open, the jump closes it (by: chip).
            sec = self._section_for(u, text)
            if sec is None:
                return None                                          # no target in the list: the floor
            m = sec.get("est_minutes") or s._est_minutes(g, g.clauses[sec["start"]:sec["end"]])
            await s.jump_to(socks, sec["start"], "topic", ws,
                            cue=f"{sec['title']}, about {m} minute{'s' if m != 1 else ''}. Starting.")
            return {**base, "action": "jump", "section_id": sec["id"]}
        if kind is not None:
            choice, data = self._prompt_choice(kind, intent, None, u, text)
            if choice is not None:
                await s.resolve_prompt("reply", choice, ws=ws, data=data)
                return {**base, "action": f"prompt:{choice}"}
        # Global actions; an open prompt is closed by them (by: chip), as by any jump.
        if intent == "skip":
            cur = g.section_of(max(sess.read_index, 0))
            nxt = next((x for x in g.sections if x["start"] > (cur["start"] if cur else -1)), None)
            if nxt is None:
                return None
            await s.jump_to(socks, nxt["start"], "skip", ws)
            return {**base, "action": "jump", "section_id": nxt["id"]}
        if intent == "back":
            b = s._jump_back
            if not (b and b.get("unit_id") in g.by_id):
                return None
            await s.jump_to(socks, g.by_id[b["unit_id"]]["index"], "back", ws, char_start=b.get("char", 0))
            return {**base, "action": "jump"}
        if intent == "start":
            await s.jump_to(socks, 0, "chip", ws, label="the start")
            return {**base, "action": "jump"}
        if intent == "repeat":
            uid = sess.last_heard_unit_id
            if uid not in g.by_id:
                return None
            await s.jump_to(socks, g.by_id[uid]["index"], "repeat", ws, cue="Again.")
            return {**base, "action": "jump"}
        if intent == "recap":
            await s.speak_recap(socks)
            return {**base, "action": "recap"}
        if intent == "carry_on":
            if not s.playing and not (s._reader and not s._reader.done()):
                await s._start_reading(socks)
            return {**base, "action": "read"}
        if intent == "brief" and not s.playing and not (s._reader and not s._reader.done()):
            await s._start_reading(socks)                           # the overview comes first for a document not started
            return {**base, "action": "read"}
        return None
