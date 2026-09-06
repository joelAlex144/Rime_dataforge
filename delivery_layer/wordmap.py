"""Per-unit word map: Rime word timestamps ↔ characters of `text_display`.

Rime returns timestamps for the *spoken* tokens ("four", "b", "two"). The
ledger and the resume logic work in *display* characters ("4(b)(ii)"). The
normalizer's Segment list is the bridge: each segment knows its display
char range and its spoken tokens, so we align Rime's token stream to the
segments' token stream and inherit timings per segment.

Alignment is a token-level SequenceMatcher (Rime may merge, split or drop a
token relative to our spoken string — e.g. "twenty twenty-six" as one token
vs two). Unmatched segments are interpolated between their neighbours and
flagged `estimated=True` so the ledger can report how much of a boundary
came from real timestamps vs interpolation.

All times are unit-local milliseconds (0 = start of this unit's audio).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Sequence

from .normalize import Segment

_CLEAN = re.compile(r"[^a-z0-9]+")


def clean_tokens(s: str) -> list[str]:
    """Lowercase alphanumeric tokens. Hyphens and punctuation split tokens."""
    return [t for t in _CLEAN.split(s.lower()) if t]


@dataclass
class WordSpan:
    word: str            # display text of the span, e.g. "4(b)(ii)" or "premium,"
    t_start_ms: float
    t_end_ms: float
    char_start: int
    char_end: int
    estimated: bool = False


@dataclass
class WordMap:
    unit_id: str
    text_display: str
    spans: list[WordSpan] = field(default_factory=list)

    @property
    def duration_ms(self) -> float:
        return self.spans[-1].t_end_ms if self.spans else 0.0

    def last_complete_word(self, played_ms: float) -> WordSpan | None:
        """Last span whose audio fully finished by `played_ms`."""
        last = None
        for s in self.spans:
            if s.t_end_ms <= played_ms:
                last = s
            else:
                break
        return last

    def offset_at(self, played_ms: float) -> int:
        """Delivery boundary as a char offset into text_display.

        A word half-played is *not* counted as heard: the boundary sits at
        the end of the last fully rendered word. That is the conservative
        choice the acceptance test asserts (no clause marked delivered that
        was never played).
        """
        w = self.last_complete_word(played_ms)
        return w.char_end if w else 0

    def heard_text(self, played_ms: float) -> str:
        return self.text_display[: self.offset_at(played_ms)]

    def word_at_char(self, char: int) -> WordSpan | None:
        for s in self.spans:
            if s.char_start <= char < s.char_end:
                return s
        return None

    def clamp_to(self, audio_ms: float) -> "WordMap":
        """Clamp span times to the audio actually produced.

        Rime's word timings can run past the end of the audio by roughly
        100 ms. Left alone, the last word never satisfies t_end_ms <=
        rendered_ms, so a unit that played to completion would resolve as
        truncated one word short. Called on Done, when the true audio length
        is finally known. Mutates in place and returns self.
        """
        for sp in self.spans:
            if sp.t_start_ms > audio_ms:
                sp.t_start_ms = audio_ms
            if sp.t_end_ms > audio_ms:
                sp.t_end_ms = audio_ms
        return self

    def to_json(self) -> dict:
        return {
            "unit_id": self.unit_id,
            "text_display": self.text_display,
            "spans": [s.__dict__ for s in self.spans],
        }


def build_word_map(
    unit_id: str,
    text_display: str,
    segments: Sequence[Segment],
    ts_words: Sequence[str],
    ts_start_ms: Sequence[float],
    ts_end_ms: Sequence[float],
) -> WordMap:
    """Align Rime's (words, start, end) to normalizer segments."""
    if not (len(ts_words) == len(ts_start_ms) == len(ts_end_ms)):
        raise ValueError("timestamp arrays must be index-aligned")

    # Segment token stream: (segment_index, token). Punctuation-only segments
    # produce no tokens and are merged into the previous span below.
    seg_tokens: list[tuple[int, str]] = []
    for i, seg in enumerate(segments):
        for tok in clean_tokens(seg.spoken):
            seg_tokens.append((i, tok))

    # Rime token stream: Rime may emit "twenty-six" or "H." as one word.
    rime_tokens: list[tuple[int, str]] = []
    for j, w in enumerate(ts_words):
        for tok in clean_tokens(w):
            rime_tokens.append((j, tok))

    a = [t for _, t in seg_tokens]
    b = [t for _, t in rime_tokens]
    sm = SequenceMatcher(a=a, b=b, autojunk=False)

    # Collect (segment, rime_word) matches, then split any Rime word that
    # covers several segments (e.g. Rime emitted "water damage" as one word)
    # evenly across those segments so span times stay monotonic.
    matches: list[tuple[int, int]] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag != "equal":
            continue
        for k in range(i2 - i1):
            matches.append((seg_tokens[i1 + k][0], rime_tokens[j1 + k][0]))

    by_rime: dict[int, list[int]] = {}
    for si, rj in matches:
        lst = by_rime.setdefault(rj, [])
        if si not in lst:
            lst.append(si)

    seg_times: dict[int, list[float]] = {}
    for rj, sis in by_rime.items():
        st, en = float(ts_start_ms[rj]), float(ts_end_ms[rj])
        step = (en - st) / len(sis)
        for n, si in enumerate(sis):
            a, b = st + step * n, st + step * (n + 1)
            if si in seg_times:
                seg_times[si][0] = min(seg_times[si][0], a)
                seg_times[si][1] = max(seg_times[si][1], b)
            else:
                seg_times[si] = [a, b]

    # Build spans, merging tokenless segments (punctuation) into the previous span.
    spans: list[WordSpan] = []
    for i, seg in enumerate(segments):
        has_tokens = any(si == i for si, _ in seg_tokens)
        if not has_tokens and spans:
            spans[-1].char_end = seg.display_end
            spans[-1].word = text_display[spans[-1].char_start:spans[-1].char_end]
            continue
        st_en = seg_times.get(i)
        spans.append(WordSpan(
            word=seg.display_text(text_display),
            t_start_ms=st_en[0] if st_en else -1.0,
            t_end_ms=st_en[1] if st_en else -1.0,
            char_start=seg.display_start,
            char_end=seg.display_end,
            estimated=st_en is None,
        ))

    _interpolate(spans, total_end=float(ts_end_ms[-1]) if len(ts_end_ms) else 0.0)
    return WordMap(unit_id=unit_id, text_display=text_display, spans=spans)


def _interpolate(spans: list[WordSpan], total_end: float) -> None:
    """Fill estimated spans by splitting the gap between known neighbours evenly."""
    n = len(spans)
    i = 0
    while i < n:
        if not spans[i].estimated:
            i += 1
            continue
        j = i
        while j < n and spans[j].estimated:
            j += 1
        left = spans[i - 1].t_end_ms if i > 0 else 0.0
        right = spans[j].t_start_ms if j < n else max(total_end, left)
        if right < left:
            right = left
        step = (right - left) / (j - i)
        for k in range(i, j):
            spans[k].t_start_ms = left + step * (k - i)
            spans[k].t_end_ms = left + step * (k - i + 1)
        i = j
