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
"""
from __future__ import annotations

import json
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Optional

_TOKEN = re.compile(r"[a-z0-9]+")
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
}

_SECTION_REF = re.compile(r"\b(?:section|part|clause)\s+(\d+)(?:\s*\(?([a-z])\)?)?(?:\s*\(?([ivx]+|\d+)\)?)?", re.I)


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
    kind: str                       # deictic | in_scope | beyond_cursor | not_found
    question: str
    hits: list[Hit] = field(default_factory=list)
    beyond: list[Hit] = field(default_factory=list)   # best hits that the spoiler gate withheld
    reference_unit_id: Optional[str] = None
    read_cursor: int = 0

    @property
    def reference(self) -> Optional[Hit]:
        return self.hits[0] if self.hits else None


SYSTEM_PROMPT = """You are reading an insurance policy aloud to someone who is listening, not reading.
They interrupted with a question. Answer ONLY from the DOCUMENT TEXT provided.

Rules:
- Cite the section aloud at the start, e.g. "Section 4(b)(ii), water damage, says ..."
- Use the document's own words where possible. Keep it to two or three spoken sentences.
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


class Grounding:
    def __init__(self, fixture_path: str | Path) -> None:
        doc = json.loads(Path(fixture_path).read_text())
        self.title = doc["title"]
        self.clauses: list[dict] = sorted(doc["clauses"], key=lambda c: c["index"])
        self.by_id = {c["id"]: c for c in self.clauses}
        self.bm25 = BM25([tokenize(c["section_title"] + " " + c["text_display"]) for c in self.clauses])
        self.vocab = sorted({t for d in self.bm25.docs for t in d})

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
    def retrieve(self, question: str, read_cursor: int, k: int = 3, allow_ahead: bool = False) -> GroundingResult:
        """BM25 within scope; hits beyond the cursor are reported separately."""
        query = self.expand(tokenize(question))
        scope = range(0, min(read_cursor + 1, len(self.clauses)))
        in_scope = [self._hit(i, s) for i, s in self.bm25.rank(query, scope)[:k]]
        ahead = [self._hit(i, s) for i, s in self.bm25.rank(query, range(read_cursor + 1, len(self.clauses)))[:k]]
        if allow_ahead:
            merged = sorted(in_scope + ahead, key=lambda h: -h.score)[:k]
            return GroundingResult("in_scope", question, merged, [], None, read_cursor)
        best_ahead = ahead[0].score if ahead else 0.0
        best_here = in_scope[0].score if in_scope else 0.0
        if in_scope and best_here >= 0.6 * best_ahead and best_here >= 1.0:
            return GroundingResult("in_scope", question, in_scope, ahead, None, read_cursor)
        if ahead and best_ahead > 0:
            return GroundingResult("beyond_cursor", question, [], ahead, None, read_cursor)
        return GroundingResult("not_found", question, [], [], None, read_cursor)

    def resolve(self, question: str, last_heard_unit_id: Optional[str], read_cursor: int) -> GroundingResult:
        """Entry point used by the agent. Deictic -> last heard clause. Explicit
        section ref -> that clause (gated). Otherwise BM25 within scope."""
        if self.is_eligibility_question(question):
            # Retrieve the criteria exactly as normal, then tag the result. The
            # spoiler gate still wins: we never pull an unread clause forward
            # just because the question was phrased as an eligibility ask.
            r = self._resolve_plain(question, last_heard_unit_id, read_cursor)
            return r if r.kind == "beyond_cursor" else GroundingResult(
                "eligibility", question, r.hits, r.beyond, r.reference_unit_id, read_cursor)
        return self._resolve_plain(question, last_heard_unit_id, read_cursor)

    def _resolve_plain(self, question: str, last_heard_unit_id: Optional[str], read_cursor: int) -> GroundingResult:
        if last_heard_unit_id and self.is_deictic(question):
            c = self.by_id[last_heard_unit_id]
            return GroundingResult("deictic", question, [self._hit(c["index"], 1.0)], [], last_heard_unit_id, read_cursor)
        ref = self.resolve_section_ref(question)
        if ref:
            c = self.by_id[ref]
            hit = self._hit(c["index"], 1.0)
            if c["index"] > read_cursor:
                return GroundingResult("beyond_cursor", question, [], [hit], None, read_cursor)
            return GroundingResult("in_scope", question, [hit], [], ref, read_cursor)
        return self.retrieve(question, read_cursor)

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
