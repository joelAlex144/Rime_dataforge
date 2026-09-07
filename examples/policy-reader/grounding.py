"""Grounded Q&A over the pre-chunked policy.

Three rules, all enforced here rather than in the prompt alone:

1. **Deictic questions resolve to the last clause actually heard.**
   "What does that mean?" is answered from `last_heard_unit_id`, which the
   ledger supplies from client-acknowledged playback — never from the last
   clause *sent*. This is half of the acceptance claim.

2. **Spoiler gate.** Retrieval scope defaults to clauses at or before the
   read cursor. A better hit further down is not read; the agent offers to
   jump instead. Reading ahead of the listener is a position leak.

3. **No interpretation.** The LLM sees document text only, must cite the
   section aloud, and must redirect to the insurer/lender for anything the
   text does not settle. `answer()` also has a deterministic extractive mode
   (no LLM) so the demo path degrades to "read the clause" rather than to
   free-form advice if the LLM is unavailable.

Retrieval is an in-memory BM25 (no dependency). Retrieval quality is
explicitly out of scope; this only needs to find the right clause in a
213-clause fixture.

Resolution order, explicit and testable (each result carries `retrieval_path`):

  1. deictic      "that" / "repeat" -> last_heard_unit_id, no retrieval at all
  2. definition   "what is X" / "what does X mean" -> the definition clause for
                  X from the ingest-time `terms` index; no spoiler gate, a
                  definition is reference text, not narrative
  3. section_ref  "section 4 b 2" -> that clause (gated)
  4. bm25         BM25 over body / table_row / definition clauses only (never
                  boilerplate, never a heading on its own), each score
                  multiplied by a proximity prior from the last-heard clause,
                  then the spoiler gate
  5. none         not_found

TODO(embeddings): not in this pass. Plan, gated on a miss in the scripted
question set (scripts/check_grounding.py): compute a local MiniLM / bge-small
embedding per retrievable clause at BUILD time and store it in the fixture;
at question time embed the question locally (no network), rank by cosine,
and fuse with the BM25 ranking by reciprocal rank fusion before the prior
and the spoiler gate. Nothing about this is a runtime model call to a
service, and it is not started until a scripted question actually misses.
"""
from __future__ import annotations

import json
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Optional

# Proximity prior on BM25 scores, anchored on the last-heard clause's section
# (or the cursor's, before anything is heard). A question usually concerns what
# was just read; the prior says so without ever hiding a strong hit elsewhere.
PRIOR_SAME_SECTION = 1.0        # the last-heard clause and its section siblings
PRIOR_ADJACENT_SECTION = 0.7    # the sections immediately before and after
PRIOR_ELSEWHERE = 0.4           # everything else, at or before the cursor
DEFINITION_OVERLAP = 0.8        # stemmed-token overlap needed for a fuzzy term match

RETRIEVABLE_KINDS = ("body", "table_row", "definition")
RETRIEVAL_PATHS = ("deictic", "definition", "section_ref", "bm25", "none", "suggested", "extractive")

# Navigation and extractive intents. Matched in this pattern layer, like the
# deictic and eligibility patterns, and answered from the fixture with no
# retrieval and no model. Tags are the fixed set scripts/enrich.py writes.
NAV_TAGS = ("exclusion", "waiting_period", "deadline", "amount", "obligation", "definition",
            "procedure", "contact")
_TAG_WORDS = {
    "exclusion": r"exclusions?|things? (?:that are )?not covered|what(?:'s| is)n'?t covered",
    "waiting_period": r"waiting periods?",
    "deadline": r"deadlines?|time limits?|due dates?",
    "amount": r"amounts?|limits?|sums?|charges?|fees?",
    "obligation": r"obligations?|duties|things? i (?:must|have to) do",
    "definition": r"definitions?|defined terms?",
    "procedure": r"procedures?|steps|process(?:es)?",
    "contact": r"contacts?|contact details|phone numbers?|helplines?",
}
_TAG_ALT = "|".join(f"(?P<{k}>{v})" for k, v in _TAG_WORDS.items())
_NAV_EVERY = re.compile(r"^\s*(?:read(?: me| out)?|list|give me|what are|tell me)\s+(?:all|every|the|each)?\s*(?:of )?(?:the )?"
                        r"(?:" + _TAG_ALT + r")(?:\s+(?:in|of) (?:this|the) (?:document|policy|section))?\s*[?.!]*$", re.I)
_NAV_WHAT_IN = re.compile(r"^\s*what\s+(?:" + _TAG_ALT + r")\s+(?:are|is)(?: there)?\s+(?:in|of)\s+(?:this|the)\s+"
                          r"(?:document|policy|section)\s*[?.!]*$", re.I)
_NAV_SUMMARISE = re.compile(r"^\s*(?:summari[sz]e|sum up|what(?:'s| is) in)\s+(?:this|the current|the)\s+section\s*[?.!]*$", re.I)
_NAV_SKIP = re.compile(r"^\s*(?:skip(?: it| this(?: section| one)?)?|next section|move on)\s*[.!]*$", re.I)
_NAV_GO_ON = re.compile(r"^\s*(?:go on|carry on|continue|keep going|keep reading|read on|no(?: thanks)?)\s*[.!]*$", re.I)
_NAV_YES = re.compile(r"^\s*(?:yes|yeah|yep|sure|ok(?:ay)?|please|go ahead|i want that|want that)\s*[.!]*$", re.I)
_NAV_BACK = re.compile(r"^\s*(?:go |take me )?back(?: to where (?:i|we) (?:was|were))?\s*[.!]*$", re.I)
_NAV_START = re.compile(r"^\s*(?:read )?from the (?:start|beginning|top)\s*[.!]*$", re.I)
_NAV_GOTO = re.compile(r"^\s*(?:go to|jump to|take me to|read(?: me)?|open)\s+(?:the )?(.+?)\s*(?:section|part|clause)?\s*[.!]*$", re.I)
_NAV_NOW = re.compile(r"^\s*(?:now|read it now|right now|straight away)\s*[.!]*$", re.I)
_NAV_OVERVIEW_FIRST = re.compile(r"^\s*(?:overview first|the rest of the overview|finish the overview|overview)\s*[.!]*$", re.I)

_TOKEN = re.compile(r"[a-z0-9]+")
# Short replies to a spoken prompt, by shape. The server reads a reply against
# the open prompt's options with these before anything else.
_REPLY_ALL = re.compile(r"^\s*(?:all(?: of them)?|every(?:thing| one)?|all the rows|read (?:them|all|everything)(?: all)?|"
                        r"the whole table|the lot)\s*[.!]*$", re.I)
_REPLY_NO = re.compile(r"^\s*(?:no|nope|nah|not really|nothing|neither|none|no thanks|no thank you|skip(?: it)?)\s*[.!]*$", re.I)
_REPLY_BRIEF = re.compile(r"^\s*(?:the )?(?:brief|overview|summary|give me the brief|brief me|just the brief|"
                          r"the brief please)\s*[.!?]*$", re.I)
_REPLY_START = re.compile(r"^\s*(?:start|read|read it|just read|read it all|read the whole thing|read everything|"
                          r"(?:read )?from the (?:start|beginning|top)|start reading|begin|the start)\s*[.!]*$", re.I)
# An utterance with a question word or a "?" is a question, never a bare topic:
# "what is the premium" must go to retrieval, "premium" alone is a jump.
_QUESTION_WORD = re.compile(r"\b(?:what|how|why|when|where|which|who|does|do|is|are|can|could|would|should|"
                            r"explain|mean|means|meaning|tell)\b", re.I)


def is_question_like(text: str) -> bool:
    return "?" in (text or "") or bool(_QUESTION_WORD.search(text or ""))


def reply_shape(text: str) -> Optional[str]:
    """The shape of a short reply to a prompt: yes | no | now | overview_first |
    brief | start | all | carry_on, or None when it is none of those (a topic
    name, a row label, a question)."""
    q = (text or "").strip()
    if not q:
        return None
    if _NAV_YES.match(q):
        return "yes"
    if _NAV_NOW.match(q):
        return "now"
    if _NAV_OVERVIEW_FIRST.match(q):
        return "overview_first"
    if _REPLY_ALL.match(q):
        return "all"
    if _REPLY_BRIEF.match(q):
        return "brief"
    if _REPLY_START.match(q) or _NAV_START.match(q):
        return "start"
    if _REPLY_NO.match(q):
        return "no"
    if _NAV_GO_ON.match(q):
        return "carry_on"
    return None
_STOP = set("""a an and are as at be by for from has have if in is it its of on or that the this
to we will with you your our us not any other than which who whom under
i do does how what when where why can could would should my me mean means say tell about
ii iii iv vi vii viii ix xi xii""".split())

_DEICTIC = re.compile(
    r"\b(that|this|it|those|these|what you (just )?said|last (part|bit|one|clause)|"
    r"repeat|again|come again|what does (that|this|it) mean|say (that|it) again)\b", re.I)

# Second-person outcome asks. The reader may read criteria aloud and cite them;
# it must never apply them to the listener. Matching is on the shape of the
# *ask*, not the topic, so "what does coverage C cover" stays a content question.
_ELIGIBILITY = re.compile(
    r"\b(?:am\s+i\s+(?:eligible|covered|entitled|able)"
    r"|does\s+(?:this|that|it)\s+apply\s+to\s+me"
    r"|do\s+i\s+(?:qualify|have\s+to|need\s+to)"
    r"|can\s+i\s+(?:claim|get|apply|qualify|still)"
    r"|will\s+(?:they|you|it|this)\s+(?:pay|cover|reimburse)"
    r"|will\s+i\s+(?:get|be|receive)"
    r"|would\s+(?:this|that|it)\s+cover\s+(?:my|me|mine)"
    r"|is\s+my\s+\w+\s+covered"
    r"|should\s+i)\b", re.I)

ELIGIBILITY_REFUSAL = ("I can't tell you whether that applies to you — I can only read what the "
                       "document says. For a decision, contact the insurer or lender.")

# Tiny synonym table for the demo questions. Retrieval quality is out of scope;
# this only keeps the spoiler gate from misfiring on obvious paraphrases.
_SYNONYMS = {
    "sue": ["suit", "action"], "lawsuit": ["suit", "action"], "start": ["begins", "period", "effective"],
    "end": ["ends", "expiration"], "expire": ["ends", "expiration"], "cancel": ["cancellation"],
    "mold": ["fungi"], "flood": ["water", "flood"], "pay": ["premium", "payable"], "price": ["premium"],
    "cost": ["premium"], "mould": ["fungi", "mold"], "storm": ["windstorm", "hail"],
    "die": ["death", "dies"], "died": ["death"], "dying": ["death"],
}

_SECTION_REF = re.compile(r"\b(?:section|part|clause)\s+(\d+)(?:\s*\(?([a-z])\)?)?(?:\s*\(?([ivx]+|\d+)\)?)?", re.I)

# "what is X", "what does X mean", "define X", "meaning of X", "what's X".
_DEFINITION_Q = re.compile(
    r"^\s*(?:what\s+is|what'?s|what\s+are|what\s+does|define|(?:the\s+)?meaning\s+of|"
    r"definition\s+of|what\s+do\s+you\s+mean\s+by)\s+(?:an?\s+|the\s+)?(.+?)"
    r"(?:\s+(?:mean|means|defined|refer\s+to))?\s*[?.!]*\s*$", re.I)
_DEFINITION_SECTION = re.compile(r"definition|interpretation", re.I)
# "Bodily injury means ...", "The words you and your refer to ...", '"Insured" means ...'
_TERM_LEAD = re.compile(
    r"^(?:the\s+(?:words?|terms?)\s+)?[\"\u201c']?([A-Za-z][A-Za-z0-9 ,/'\-]{0,60}?)[\"\u201d']?"
    r"\s+(?:means?|refers?\s+to|shall\s+mean|is\s+defined\s+as|include[s]?)\b", re.I)
_ROW_TERM = re.compile(r"^Row:\s*(?:term|word|expression|definition\s+of)\s*:\s*([^;]+?)\s*;", re.I)


def normalise_term(term: str) -> str:
    return " ".join(_TOKEN.findall(term.lower()))


def stem(tok: str) -> str:
    for suf in ("ing", "es", "ed", "s"):
        if tok.endswith(suf) and len(tok) - len(suf) >= 4:
            return tok[: -len(suf)]
    return tok


def clause_kind(c: dict) -> str:
    """The structure-pass `kind`, with the hero fixture's implicit kinds.

    policy.json predates the structure pass and carries no `kind`: its
    Definitions section is the definition set, everything else is body.
    """
    k = c.get("kind")
    if k in (None, "clause"):
        return "definition" if _DEFINITION_SECTION.search(c.get("section_title", "")) else "body"
    return k


def is_readable(c: dict) -> bool:
    """Spoken in linear playback? Boilerplate never; table rows only on request,
    unless enrichment flagged a small table `read_inline`."""
    if clause_kind(c) == "boilerplate":
        return False
    return not c.get("spoken_on_request", False) or bool(c.get("read_inline", False))


def skip_reason(c: dict) -> Optional[str]:
    if clause_kind(c) == "boilerplate":
        return "boilerplate"
    if c.get("spoken_on_request", False) and not c.get("read_inline", False):
        return "table_on_request"
    return None


def spoken_text(c: dict) -> str:
    """What the reader sends to the voice: the generated spoken form when
    enrichment wrote one (table stubs and rows), else the normalised text."""
    ov = c.get("spoken_override")
    if isinstance(ov, dict) and ov.get("text"):
        return ov["text"]
    return c["text_spoken"]


def tokenize(text: str) -> list[str]:
    return [t for t in _TOKEN.findall(text.lower()) if t not in _STOP]


class BM25:
    def __init__(self, docs: list[list[str]], k1: float = 1.5, b: float = 0.75) -> None:
        self.k1, self.b = k1, b
        self.docs = docs
        self.n = len(docs)
        self.avgdl = sum(len(d) for d in docs) / max(1, self.n)
        self.tf = [Counter(d) for d in docs]
        df: Counter[str] = Counter()
        for d in docs:
            df.update(set(d))
        self.idf = {t: math.log(1 + (self.n - n + 0.5) / (n + 0.5)) for t, n in df.items()}

    def score(self, query: list[str], i: int) -> float:
        tf, dl = self.tf[i], len(self.docs[i])
        s = 0.0
        for q in query:
            if q not in tf:
                continue
            f = tf[q]
            s += self.idf.get(q, 0.0) * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * dl / self.avgdl))
        return s

    def rank(self, query: list[str], candidates: range | list[int]) -> list[tuple[int, float]]:
        scored = [(i, self.score(query, i)) for i in candidates]
        scored = [(i, s) for i, s in scored if s > 0]
        return sorted(scored, key=lambda x: -x[1])


@dataclass
class Hit:
    unit_id: str
    index: int
    score: float
    section: int
    section_title: str
    text_display: str
    text_spoken: str
    path: Optional[str] = None      # human numbering path from the fixture, e.g. "4(b)(ii)"

    def citation_spoken(self) -> str:
        # Ingested fixtures carry an explicit numbering path; policy.json does not,
        # so its "sec-4b-ii" ids are decoded as before. Unnumbered documents
        # (ids like "sec-3-p7") have no section number worth speaking: cite the
        # heading instead of reading "Section 3(p7)" aloud.
        if self.path:
            return f"Section {self.path}, {self.section_title}"
        parts = self.unit_id.split("-")
        if len(parts) == 3 and re.fullmatch(r"\d+[a-z]?", parts[1]) and re.fullmatch(r"[ivxlcdm]+", parts[2]):
            sub, item = parts[1], parts[2]
            return (f"Section {sub[0:-1] if sub[-1].isalpha() else sub}"
                    f"{'(' + sub[-1] + ')' if sub[-1].isalpha() else ''}({item}), {self.section_title}")
        return self.section_title or f"Section {self.section}"


@dataclass
class GroundingResult:
    kind: str                       # deictic | in_scope | beyond_cursor | not_found | eligibility
    question: str
    hits: list[Hit] = field(default_factory=list)
    beyond: list[Hit] = field(default_factory=list)   # best hits that the spoiler gate withheld
    reference_unit_id: Optional[str] = None
    read_cursor: int = 0
    retrieval_path: str = "none"    # deictic | definition | section_ref | bm25 | none

    @property
    def reference(self) -> Optional[Hit]:
        return self.hits[0] if self.hits else None


SYSTEM_PROMPT = """You are reading an insurance policy aloud to someone who is listening, not reading.
They interrupted with a question. Answer ONLY from the DOCUMENT TEXT provided.

Rules:
- Cite the section aloud at the start, e.g. "Section 4(b)(ii), water damage, says ..."
- Use the document's own words where possible (except for a restatement, below). Keep it to two or three spoken sentences.
- For "what does that mean", "explain that" or "in plain words": restate the clause in plain everyday language.
  Add nothing that is not in the text, and do not repeat the clause word for word. A restatement is not
  interpretation; advice and outcomes stay forbidden.
- Do NOT interpret, advise, predict a claim outcome, or say what the listener should do.
- If the document text does not answer the question, say exactly that and tell them to ask their insurer or lender.
- Never mention clauses that are not in DOCUMENT TEXT. Never guess at numbers.
- Speak numbers and section references in words, the way they are written in DOCUMENT TEXT (spoken form).
- Do not read ahead: if you are told a clause is further down, only offer to jump there.
- NEVER decide whether the listener personally qualifies, is eligible, is covered, or will be paid.
  If they ask "am I eligible", "do I qualify", "can I claim", "will they pay", or anything similar,
  read the criteria and cite the section, then say exactly:
  "I can't tell you whether that applies to you — I can only read what the document says. For a
  decision, contact the insurer or lender." Never answer such a question with yes or no.
"""


_CHIP_ITEMS = re.compile(r"\.\s*\d+\s+items?\.?\s*$")
_CHIP_NUMBERING = re.compile(r"^\s*(?:(?:section|part|chapter|clause|article)\s+)?"
                             r"(?:\d+(?:\.\d+)*[.):\-]?|[ivxlc]+[.)]|[a-z][.)])\s*(?=[A-Za-z(])", re.I)
_CHIP_TAIL_WORDS = {"of", "and", "the", "to", "for", "or", "in", "on", "a", "an", "by", "with"}


def chip_name(title: str, max_words: int = 4) -> str:
    """A section heading as a chip: no numbering, no "N items" tail, four
    words at most, title case for an all-caps heading; "" when the heading
    has no words."""
    t = _CHIP_ITEMS.sub("", title or "").strip()
    t = _CHIP_NUMBERING.sub("", t)
    t = t.strip(" :;,.\u2013\u2014-=")
    if not re.search(r"[A-Za-z]{2,}", t) or re.match(r"^page\s*\d+$", t, re.I):
        return ""                                  # page furniture is not a topic
    words = t.split()
    if len(words) > max_words:
        words = words[:max_words]
        while len(words) > 1 and words[-1].lower() in _CHIP_TAIL_WORDS:
            words.pop()
    t = " ".join(words)
    if t.isupper():
        t = " ".join(w if (w.startswith("(") and len(w.strip("()")) <= 5) else w.capitalize() for w in t.split())
    return t


class Grounding:
    def __init__(self, fixture_path: str | Path) -> None:
        doc = json.loads(Path(fixture_path).read_text(encoding="utf-8"))
        self._doc = doc
        self._sections = None
        self.title = doc["title"]
        self.clauses: list[dict] = sorted(doc["clauses"], key=lambda c: c["index"])
        self.by_id = {c["id"]: c for c in self.clauses}
        # The document map the reader speaks once at session start: ordered
        # top-level headings with child counts. Written by the structure pass;
        # derived from section titles for fixtures that predate it.
        self.map: list[dict] = doc.get("map") or self._derive_map()
        self.kinds = {c["id"]: clause_kind(c) for c in self.clauses}
        # BM25 is indexed over every clause so positions line up with `index`;
        # ranking only ever considers the retrievable kinds.
        self.bm25 = BM25([tokenize(c["section_title"] + " " + c["text_display"]) for c in self.clauses])
        self.retrievable = [i for i, c in enumerate(self.clauses) if self.kinds[c["id"]] in RETRIEVABLE_KINDS]
        self.vocab = sorted({t for i in self.retrievable for t in self.bm25.docs[i]})
        # Definition index: normalised term -> clause id. From the fixture's
        # `terms` block when the structure pass wrote one, else built here from
        # the definition clauses' own lead-ins ("Bodily injury means ...").
        self.terms: dict[str, str] = {normalise_term(k): v for k, v in (doc.get("terms") or {}).items()}
        if not self.terms:
            for c in self.clauses:
                if self.kinds[c["id"]] != "definition":
                    continue
                m = _ROW_TERM.match(c["text_display"]) or _TERM_LEAD.match(c["text_display"])
                if m:
                    self.terms.setdefault(normalise_term(m.group(1)), c["id"])
        self._term_tokens = {t: {stem(x) for x in t.split()} for t in self.terms}

    def _derive_map(self) -> list[dict]:
        out: list[dict] = []
        for c in self.clauses:
            if clause_kind(c) == "boilerplate":
                continue
            if not out or out[-1]["title"] != c["section_title"]:
                out.append({"title": c["section_title"], "section": c["section"], "children": 0})
            out[-1]["children"] += 1
        return out

    def map_sentence(self, noun: str = "document") -> str:
        """Mechanical, spoken once at session start. The only count spoken
        outside a heading signpost."""
        n = len(self.map)
        return (f"This {noun} has {n} section{'s' if n != 1 else ''}. "
                f"I'll read them in order; interrupt me any time.")

    def skipped(self) -> list[tuple[str, str]]:
        """(clause id, reason) for every clause linear playback never sends."""
        out = []
        for c in self.clauses:
            r = skip_reason(c)
            if r:
                out.append((c["id"], r))
        return out

    def next_readable(self, index: int) -> int:
        """First readable clause index at or after `index` (len(clauses) if none)."""
        while index < len(self.clauses) and not is_readable(self.clauses[index]):
            index += 1
        return index

    # ------------------------------------------------------------ navigator
    @property
    def sections(self) -> list[dict]:
        """Top-level sections with their clause index range, from the fixture's
        `sections` block (enrichment) or the map; each has id, title, start, end,
        brief (text or None), est_minutes, suggested_questions."""
        if getattr(self, "_sections", None) is not None:
            return self._sections
        raw = self._doc.get("sections") or []
        by_id = {s["id"]: s for s in raw}
        heads = [m for m in self.map if m.get("id") in self.by_id] or []
        out = []
        if not heads:                                   # fixture with no map block: section_title runs
            for c in self.clauses:
                if clause_kind(c) == "boilerplate":
                    continue
                if not out or out[-1]["title"] != c["section_title"]:
                    out.append({"id": c["id"], "title": c["section_title"], "start": c["index"], "end": c["index"] + 1})
                else:
                    out[-1]["end"] = c["index"] + 1
        else:
            for i, m in enumerate(heads):
                start = self.by_id[m["id"]]["index"]
                end = self.by_id[heads[i + 1]["id"]]["index"] if i + 1 < len(heads) else len(self.clauses)
                out.append({"id": m["id"], "title": m["title"], "start": start, "end": end})
        for sec in out:
            e = by_id.get(sec["id"], {})
            brief = e.get("brief")
            sec["brief"] = brief.get("text") if isinstance(brief, dict) else None
            sec["est_minutes"] = e.get("est_minutes")
            sec["suggested_questions"] = [q for q in (e.get("suggested_questions") or [])
                                          if q.get("clause_id") in self.by_id]
        self._sections = out
        return out

    def section_of(self, index: int) -> Optional[dict]:
        for sec in self.sections:
            if sec["start"] <= index < sec["end"]:
                return sec
        return None

    def section_at_start(self, index: int) -> Optional[dict]:
        """The section that begins exactly at this clause index (a transition)."""
        for sec in self.sections:
            if sec["start"] == index:
                return sec
        return None

    @property
    def topics(self) -> list[dict]:
        """Chips for the opening screen, five to seven of them: the generated
        topics whose pointer resolves, then top-level section headings
        (cleaned, four words at most) until there are five, then "Read from
        the start". Every pointer resolves or is dropped."""
        out: list[dict] = []
        seen: set = set()
        for t in self._doc.get("topics") or []:
            sid = t.get("section_id")
            if sid is None or sid not in self.by_id or sid in seen:
                continue
            out.append(t)
            seen.add(sid)
        if len(out) < 5:
            names = {o["topic"].lower() for o in out}
            for sec in self.sections:
                if sec["id"] in seen:
                    continue
                name = chip_name(sec["title"])
                if not name or name.lower() in names:
                    continue
                out.append({"topic": name, "section_id": sec["id"], "heading": sec["title"], "generated": False})
                seen.add(sec["id"])
                names.add(name.lower())
                if len(out) >= 5:
                    break
        if len(out) < 5:
            # Few top-level sections (a loan MITC with three): the sub-headings
            # fill the chips; each jumps to its own span (section_for_id).
            names = {o["topic"].lower() for o in out}
            for c in self.clauses:
                if clause_kind(c) != "heading" or c["id"] in seen:
                    continue
                name = chip_name(c.get("text_display", ""))
                if not name or name.lower() in names:
                    continue
                out.append({"topic": name, "section_id": c["id"], "heading": _CHIP_ITEMS.sub("", c["text_display"]).strip(),
                            "generated": False})
                seen.add(c["id"])
                names.add(name.lower())
                if len(out) >= 5:
                    break
        out = out[:6]
        out.append({"topic": "Read from the start", "section_id": None, "heading": None, "generated": False})
        return out

    def section_for_id(self, sid: Optional[str]) -> Optional[dict]:
        """The section a chip or a reply points at: a top-level section by id,
        else the span of a heading clause (to the next heading of the same
        or a higher level, within its section)."""
        if not sid:
            return None
        sec = next((x for x in self.sections if x["id"] == sid), None)
        if sec is not None:
            return sec
        c = self.by_id.get(sid)
        if not c or clause_kind(c) != "heading":
            return None
        start = c["index"]
        end = len(self.clauses)
        level = c.get("level") or 0
        for d in self.clauses[start + 1:]:
            if clause_kind(d) == "heading" and (d.get("level") or 0) <= level:
                end = d["index"]
                break
        enclosing = self.section_of(start)
        if enclosing is not None and enclosing["end"] > start:
            end = min(end, enclosing["end"])
        return {"id": sid, "title": _CHIP_ITEMS.sub("", c["text_display"]).strip(), "start": start, "end": end,
                "brief": None, "est_minutes": None, "suggested_questions": []}

    @property
    def overview_text(self) -> Optional[str]:
        ov = self._doc.get("overview")
        return ov.get("text") if isinstance(ov, dict) and ov.get("text") else None

    def find_section(self, phrase: str) -> Optional[dict]:
        """A topic chip name, or a heading (exact, then substring, then token overlap)."""
        q = normalise_term(phrase)
        if not q:
            return None
        for t in self.topics:
            if t.get("section_id") and normalise_term(t["topic"]) == q:
                return next((s for s in self.sections if s["id"] == t["section_id"]), None)
        secs = self.sections
        for s in secs:
            if normalise_term(s["title"]) == q:
                return s
        for s in secs:
            if q in normalise_term(s["title"]):
                return s
        qs = {stem(x) for x in q.split() if x not in _STOP}
        best, score = None, 0.0
        for s in secs:
            ts = {stem(x) for x in normalise_term(s["title"]).split() if x not in _STOP}
            if ts and qs:
                sc = len(ts & qs) / len(qs)
                if sc > score:
                    best, score = s, sc
        return best if score >= 0.6 else None

    def tagged(self, tag: str, section: Optional[dict] = None) -> list[dict]:
        """Clauses carrying a tag (from enrichment), in reading order; cited, never ranked."""
        out = []
        for c in self.clauses:
            t = c.get("tags")
            tags = t.get("tags") if isinstance(t, dict) else (t or [])
            if tag in tags and (section is None or section["start"] <= c["index"] < section["end"]):
                out.append(c)
        return out

    def navigation_intent(self, question: str, at_index: int = 0) -> Optional[dict]:
        """Navigation and extractive intents, matched before any retrieval.

        Returns {"intent": ..., ...} or None. Intents: skip, go_on, yes, back,
        start, now, overview_first, goto (with section), every (with tag),
        summarise (with section). Never touches BM25.
        """
        q = (question or "").strip()
        if not q:
            return None
        if _NAV_SKIP.match(q):
            return {"intent": "skip"}
        if _NAV_YES.match(q):
            return {"intent": "yes"}
        if _NAV_GO_ON.match(q):
            return {"intent": "go_on"}
        if _NAV_BACK.match(q):
            return {"intent": "back"}
        if _NAV_START.match(q):
            return {"intent": "start"}
        if _NAV_NOW.match(q):
            return {"intent": "now"}
        if _NAV_OVERVIEW_FIRST.match(q):
            return {"intent": "overview_first"}
        m = _NAV_EVERY.match(q) or _NAV_WHAT_IN.match(q)
        if m:
            tag = next(k for k in NAV_TAGS if m.group(k))
            return {"intent": "every", "tag": tag, "clauses": self.tagged(tag)}
        if _NAV_SUMMARISE.match(q):
            sec = self.section_of(at_index)
            return {"intent": "summarise", "section": sec} if sec else None
        m = _NAV_GOTO.match(q)
        if m:
            sec = self.find_section(m.group(1))
            if sec:
                return {"intent": "goto", "section": sec}
        # a bare topic chip name spoken -- never an utterance shaped like a
        # question ("what is the premium" is retrieval, "premium" is a jump)
        if is_question_like(q):
            return None
        sec = self.find_section(q)
        if sec and len(q.split()) <= 5:
            return {"intent": "goto", "section": sec}
        return None

    def expand(self, query: list[str]) -> list[str]:
        """Crude prefix expansion so 'wind' also hits 'windstorm', 'cancel' hits 'cancellation'."""
        out = list(query)
        for q in query:
            stems = {q}
            for suf in ("ing", "es", "ed", "s"):
                if q.endswith(suf) and len(q) - len(suf) >= 4:
                    stems.add(q[: -len(suf)])
            for st in stems:
                out.extend(_SYNONYMS.get(st, []))
                if len(st) >= 4:
                    out.extend(v for v in self.vocab if v != q and v.startswith(st))
        return out

    # ------------------------------------------------------------ definitions
    def lookup_definition(self, question: str) -> Optional[str]:
        """Clause id of the definition the question asks for, or None.

        Exact normalised term first, then stemmed-token overlap of at least
        DEFINITION_OVERLAP of the term's tokens. Only definition clauses are
        ever returned, whatever BM25 would have preferred.
        """
        m = _DEFINITION_Q.match(question or "")
        if not m or not self.terms:
            return None
        asked = normalise_term(m.group(1))
        if not asked:
            return None
        if asked in self.terms:
            return self.terms[asked]
        asked_stems = {stem(t) for t in asked.split() if t not in _STOP}
        if not asked_stems:
            return None
        best, best_score = None, 0.0
        for term, toks in self._term_tokens.items():
            if not toks:
                continue
            score = len(toks & asked_stems) / len(toks)
            if score > best_score or (score == best_score and best is not None and len(term) > len(best)):
                best, best_score = term, score
        if best is not None and best_score >= DEFINITION_OVERLAP:
            return self.terms[best]
        return None

    # ------------------------------------------------------------ proximity
    def _anchor_section(self, last_heard_unit_id: Optional[str], read_cursor: int) -> Optional[int]:
        if last_heard_unit_id and last_heard_unit_id in self.by_id:
            return self.by_id[last_heard_unit_id]["section"]
        if 0 <= read_cursor < len(self.clauses):
            return self.clauses[read_cursor]["section"]
        return None

    def proximity_prior(self, index: int, anchor_section: Optional[int]) -> float:
        if anchor_section is None:
            return PRIOR_SAME_SECTION
        d = abs(self.clauses[index]["section"] - anchor_section)
        if d == 0:
            return PRIOR_SAME_SECTION
        if d == 1:
            return PRIOR_ADJACENT_SECTION
        return PRIOR_ELSEWHERE

    # ------------------------------------------------------------ helpers
    def _hit(self, i: int, score: float) -> Hit:
        c = self.clauses[i]
        return Hit(c["id"], c["index"], score, c["section"], c["section_title"], c["text_display"],
                   c["text_spoken"], c.get("path"))

    def resolve_section_ref(self, question: str) -> Optional[str]:
        """'section 4 b 2' / 'Section 4(b)(ii)' -> 'sec-4b-ii' if it exists."""
        m = _SECTION_REF.search(question)
        if not m:
            return None
        sec, letter, item = m.group(1), (m.group(2) or "").lower(), (m.group(3) or "").lower()
        if item.isdigit():
            romans = ["i", "ii", "iii", "iv", "v", "vi", "vii", "viii", "ix", "x", "xi", "xii", "xiii", "xiv", "xv"]
            item = romans[int(item) - 1] if 0 < int(item) <= len(romans) else item
        cands = [c["id"] for c in self.clauses
                 if c["section"] == int(sec) and (not letter or (c["subsection"] or "") == letter)
                 and (not item or c["item"] == item)]
        return cands[0] if cands else None

    @staticmethod
    def is_deictic(question: str) -> bool:
        q = question.strip()
        if not _DEICTIC.search(q):
            return False
        content = tokenize(_DEICTIC.sub(" ", q))
        return len(content) <= 1

    @staticmethod
    def is_eligibility_question(question: str) -> bool:
        """Is the listener asking us to apply the document's criteria to them?"""
        return bool(_ELIGIBILITY.search(question or ""))

    # ------------------------------------------------------------ retrieval
    def retrieve(self, question: str, read_cursor: int, k: int = 3, allow_ahead: bool = False,
                 last_heard_unit_id: Optional[str] = None) -> GroundingResult:
        """Proximity-boosted BM25 within scope; hits beyond the cursor are
        reported separately and the spoiler gate decides between them."""
        query = self.expand(tokenize(question))
        anchor = self._anchor_section(last_heard_unit_id, read_cursor)
        cut = min(read_cursor + 1, len(self.clauses))
        scope = [i for i in self.retrievable if i < cut]
        beyond = [i for i in self.retrievable if i >= cut]

        # The prior orders candidates WITHIN scope (what was just read wins a
        # tie); the spoiler gate below compares raw scores, so the prior never
        # pushes a question beyond the cursor that plain BM25 would answer here.
        def ranked(cands):
            scored = [(i, s * self.proximity_prior(i, anchor), s) for i, s in self.bm25.rank(query, cands)]
            return sorted(scored, key=lambda x: -x[1])

        here_ranked = ranked(scope)
        ahead_ranked = ranked(beyond)
        in_scope = [self._hit(i, w) for i, w, _ in here_ranked[:k]]
        ahead = [self._hit(i, w) for i, w, _ in ahead_ranked[:k]]
        if allow_ahead:
            merged = sorted(in_scope + ahead, key=lambda h: -h.score)[:k]
            return GroundingResult("in_scope", question, merged, [], None, read_cursor, "bm25")
        best_ahead = max((raw for _, _, raw in ahead_ranked), default=0.0)
        best_here = max((raw for _, _, raw in here_ranked), default=0.0)
        if in_scope and best_here >= 0.6 * best_ahead and best_here >= 1.0:
            return GroundingResult("in_scope", question, in_scope, ahead, None, read_cursor, "bm25")
        if ahead and best_ahead > 0:
            return GroundingResult("beyond_cursor", question, [], ahead, None, read_cursor, "bm25")
        return GroundingResult("not_found", question, [], [], None, read_cursor, "none")

    def resolve(self, question: str, last_heard_unit_id: Optional[str], read_cursor: int) -> GroundingResult:
        """Entry point used by the agent. Deictic -> last heard clause. Explicit
        section ref -> that clause (gated). Otherwise BM25 within scope."""
        if self.is_eligibility_question(question):
            # Retrieve the criteria exactly as normal, then tag the result. The
            # spoiler gate still wins: we never pull an unread clause forward
            # just because the question was phrased as an eligibility ask.
            r = self._resolve_plain(question, last_heard_unit_id, read_cursor)
            return r if r.kind == "beyond_cursor" else GroundingResult(
                "eligibility", question, r.hits, r.beyond, r.reference_unit_id, read_cursor,
                r.retrieval_path)
        return self._resolve_plain(question, last_heard_unit_id, read_cursor)

    def _resolve_plain(self, question: str, last_heard_unit_id: Optional[str], read_cursor: int) -> GroundingResult:
        # 1. deictic: the clause the listener actually heard last. No retrieval.
        if last_heard_unit_id and self.is_deictic(question):
            c = self.by_id[last_heard_unit_id]
            return GroundingResult("deictic", question, [self._hit(c["index"], 1.0)], [],
                                   last_heard_unit_id, read_cursor, "deictic")
        # 2. definition lookup: reference text, so the spoiler gate does not apply.
        did = self.lookup_definition(question)
        if did:
            c = self.by_id[did]
            return GroundingResult("in_scope", question, [self._hit(c["index"], 1.0)], [],
                                   did, read_cursor, "definition")
        # 3. an explicit section reference, gated.
        ref = self.resolve_section_ref(question)
        if ref:
            c = self.by_id[ref]
            hit = self._hit(c["index"], 1.0)
            if c["index"] > read_cursor:
                return GroundingResult("beyond_cursor", question, [], [hit], None, read_cursor, "section_ref")
            return GroundingResult("in_scope", question, [hit], [], ref, read_cursor, "section_ref")
        # 4. proximity-boosted BM25 with the spoiler gate; 5. none.
        return self.retrieve(question, read_cursor, last_heard_unit_id=last_heard_unit_id)

    # ------------------------------------------------------------ answering
    @staticmethod
    def build_prompt(result: GroundingResult, heard_text_of_reference: Optional[str] = None) -> list[dict]:
        """Chat messages for the LLM. `heard_text_of_reference` is the truncated
        delivered text of the reference clause (with [interrupted] marker) when
        the question landed mid-clause; it tells the model what was actually heard."""
        blocks = []
        for h in result.hits:
            blocks.append(f"[{h.citation_spoken()}]\n{h.text_spoken}")
        if result.kind == "beyond_cursor":
            blocks.append("(The relevant clause is further down and has NOT been read yet. Offer to jump to it; do not read it.)")
        if heard_text_of_reference is not None:
            blocks.append(f"LISTENER HEARD SO FAR OF THE CURRENT CLAUSE:\n{heard_text_of_reference} [interrupted]")
        user = f"DOCUMENT TEXT:\n" + ("\n\n".join(blocks) if blocks else "(no matching clause)") + f"\n\nQUESTION: {result.question}"
        return [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user}]

    async def answer(self, result: GroundingResult, llm: Optional[Callable[[list[dict]], Awaitable[str]]] = None,
                     heard_text_of_reference: Optional[str] = None) -> str:
        """Spoken answer. With no LLM, extractive: cite and re-read the clause."""
        if result.kind == "beyond_cursor":
            h = result.beyond[0]
            return f"That's covered further down, in {h.citation_spoken()}. Want me to jump there, or keep going from where we were?"
        if result.kind == "not_found":
            return "I can't find that in the policy text I have. For that one, you'd want to ask the insurer directly. Shall I carry on?"
        if result.kind == "eligibility":
            # Deterministic on purpose: this answer never goes through the LLM,
            # so no sampling accident can turn it into a yes or a no. The same
            # rule is in SYSTEM_PROMPT as defence in depth for anything that
            # slips past _ELIGIBILITY and reaches the model.
            if result.hits:
                h = result.hits[0]
                return f"{h.citation_spoken()}, says: {h.text_spoken} {ELIGIBILITY_REFUSAL}"
            return ELIGIBILITY_REFUSAL
        if llm is None:
            h = result.hits[0]
            return f"{h.citation_spoken()}, says: {h.text_spoken}"
        return await llm(self.build_prompt(result, heard_text_of_reference))


if __name__ == "__main__":
    import asyncio
    import sys
    g = Grounding(Path(__file__).with_name("fixtures") / "policy.json")
    cursor = int(sys.argv[1]) if len(sys.argv) > 1 else 40
    for q in sys.argv[2:] or ["what does that mean", "what is the deductible for wind", "how long do I have to sue you",
                             "what is in section 4 b 7"]:
        r = g.resolve(q, g.clauses[cursor]["id"], cursor)
        print(f"\nQ: {q}\n[{r.kind}] " + asyncio.run(g.answer(r)))
