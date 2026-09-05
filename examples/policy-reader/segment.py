"""Shared segmentation primitives for fixture builders.

`sentence_spans` is a verbatim copy of the function in
`fixtures/build_fixture.py`. It is duplicated rather than imported because
`build_fixture.py` is frozen (it produces the hero fixture and must not be
touched), and importing it would execute its module-level SECTIONS tables.
The two copies must stay identical; `tests/test_ingest.py` asserts that.

Everything else here is used only by `scripts/ingest.py`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Optional

# --------------------------------------------------------------------------
# sentence splitting (copy of build_fixture.sentence_spans)
# --------------------------------------------------------------------------

_SENT_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z\"$(])|(?<=;)\s+")


def sentence_spans(text: str) -> list[list[int]]:
    """Char spans of sentences. Splits on . ! ? followed by whitespace+capital, and on any semicolon.
    Decimal numbers ("4.2.1", "$1,842.00") never match because no whitespace follows the dot."""
    spans, start = [], 0
    for m in _SENT_END.finditer(text):
        spans.append([start, m.start()])
        start = m.end()
    spans.append([start, len(text)])
    return spans


# --------------------------------------------------------------------------
# blocks
# --------------------------------------------------------------------------

@dataclass
class Block:
    """One extracted unit of source text, before numbering analysis."""
    kind: str          # "heading" | "para" | "list_item" | "table_row"
    text: str
    level: int = 0     # heading level, 1..4; 0 for non-headings


# --------------------------------------------------------------------------
# numbering markers
# --------------------------------------------------------------------------

ROMAN_RE = re.compile(r"^(?=[ivxlcdm]+$)m*(?:cm|cd|d?c{0,3})(?:xc|xl|l?x{0,3})(?:ix|iv|v?i{0,3})$", re.I)
AMBIGUOUS_TOKENS = {"i", "v", "x", "l", "c", "d", "m"}
_AMBIGUOUS = AMBIGUOUS_TOKENS  # back-compat alias

# Ordered: the first pattern that matches wins.
_KEYWORD = re.compile(
    r"^(?:§\s*|(?:section|article|part|clause|schedule|annex)\s+)([0-9]+|[ivxlcdm]+)\b\s*[.:\)\-–—]?\s*(.*)$", re.I)
_DOTTED = re.compile(r"^([0-9]+(?:\.[0-9]+)+)\s*[.)\-–—]?\s+(\S.*)$")
_PLAIN_NUM = re.compile(r"^([0-9]{1,3})\s*[.)]\s+(\S.*)$")
_PAREN_TOKEN = re.compile(r"^\(([A-Za-z]{1,5}|[0-9]{1,3})\)\s*(.*)$")
_BARE_TOKEN = re.compile(r"^([A-Za-z]{1,5})\s*[.)]\s+(\S.*)$")


@dataclass
class Marker:
    kind: str          # "num" | "alpha" | "roman"
    token: str         # normalised token: "4", "b", "ii"
    rest: str          # text after the marker
    dotted: Optional[list[str]] = None   # for "4.2.1" -> ["4","2","1"]
    keyword: bool = False                # matched "Section N" / "Article N" etc


def _classify_token(tok: str, prev_roman_at_depth: bool) -> str:
    """alpha vs roman for a single token.

    Multi-character roman numerals (ii, iv, vii) are unambiguous. A single
    letter is ambiguous: "(i)" is roman when it opens a roman run and alpha
    when it simply follows "(h)". We resolve it with the caller's context.
    """
    t = tok.lower()
    if t.isdigit():
        return "num"
    if len(t) > 1 and ROMAN_RE.match(t):
        return "roman"
    if len(t) == 1 and t in _AMBIGUOUS and prev_roman_at_depth:
        return "roman"
    return "alpha"


def parse_marker(line: str, prev_roman: bool = False) -> Optional[Marker]:
    """Recognise a clause/heading marker at the start of `line`."""
    s = line.strip()
    if not s:
        return None

    m = _KEYWORD.match(s)
    if m:
        tok = m.group(1)
        kind = "num" if tok.isdigit() else "roman"
        return Marker(kind, tok.lower(), m.group(2).strip(), keyword=True)

    m = _DOTTED.match(s)
    if m:
        parts = m.group(1).split(".")
        return Marker("num", parts[0], m.group(2).strip(), dotted=parts)

    m = _PLAIN_NUM.match(s)
    if m:
        return Marker("num", m.group(1), m.group(2).strip())

    m = _PAREN_TOKEN.match(s)
    if m:
        tok = m.group(1)
        return Marker(_classify_token(tok, prev_roman), tok.lower(), m.group(2).strip())

    m = _BARE_TOKEN.match(s)
    if m:
        tok = m.group(1)
        kind = _classify_token(tok, prev_roman)
        # "The." or "See." are not markers; require a short token.
        if kind == "alpha" and len(tok) > 1:
            return None
        return Marker(kind, tok.lower(), m.group(2).strip())
    return None


def path_to_id(parts: Iterable[str], prefix: str = "sec") -> str:
    """['4','b','ii'] -> 'sec-4b-ii';  ['4','2','1'] -> 'sec-4-2-1'."""
    p = [str(x).lower() for x in parts if str(x) != ""]
    if not p:
        return prefix
    head = p[0]
    rest = p[1:]
    # A single alphabetic subsection letter binds directly to the section
    # number, matching policy.json ("sec-4b-ii").
    if rest and len(rest[0]) == 1 and rest[0].isalpha():
        head = head + rest[0]
        rest = rest[1:]
    return "-".join([prefix, head] + rest)


def path_to_human(parts: Iterable[str]) -> str:
    """['4','b','ii'] -> '4(b)(ii)'."""
    p = [str(x) for x in parts if str(x) != ""]
    if not p:
        return ""
    return p[0] + "".join(f"({x})" for x in p[1:])


# --------------------------------------------------------------------------
# cleanup passes
# --------------------------------------------------------------------------

_TOC_LINE = re.compile(r".*?[\s.…]{2,}\d{1,4}\s*$|^.{3,80}\s+\d{1,4}\s*$")


def strip_toc(lines: list[str], run: int = 8) -> list[str]:
    """Drop any run of more than `run` consecutive lines that end in a number.

    That shape is a table of contents; reading one aloud is useless and it
    pollutes the numbering hierarchy with fake markers.
    """
    keep = [True] * len(lines)
    i = 0
    while i < len(lines):
        j = i
        while j < len(lines) and lines[j].strip() and _TOC_LINE.match(lines[j].strip()):
            j += 1
        if j - i > run:
            for k in range(i, j):
                keep[k] = False
            i = j
        else:
            i = max(j, i + 1)
    return [l for l, k in zip(lines, keep) if k]


def join_hyphenated(text: str) -> str:
    """PDF extraction leaves 'compen-\\nsation'. Rejoin, keep real line breaks."""
    return re.sub(r"(\w)-\s*\n\s*(\w)", r"\1\2", text)


def drop_repeated_lines(pages: list[str], threshold: float = 0.5) -> list[str]:
    """Remove running headers/footers: a line appearing on > threshold of pages."""
    if len(pages) < 3:
        return pages
    counts: dict[str, int] = {}
    for p in pages:
        for line in {l.strip() for l in p.splitlines() if l.strip()}:
            counts[line] = counts.get(line, 0) + 1
    cutoff = max(2, int(len(pages) * threshold))
    boiler = {l for l, n in counts.items() if n > cutoff and len(l) < 120}
    out = []
    for p in pages:
        out.append("\n".join(l for l in p.splitlines() if l.strip() not in boiler))
    return out


_PAGE_NUM = re.compile(r"^\s*(?:page\s+)?[-–—\[]?\s*\d{1,4}\s*(?:of\s+\d{1,4})?\s*[\]\-–—]?\s*$", re.I)


def drop_page_numbers(lines: list[str]) -> list[str]:
    return [l for l in lines if not _PAGE_NUM.match(l)]


# --------------------------------------------------------------------------
# length normalisation
# --------------------------------------------------------------------------

def split_long(text: str, max_chars: int) -> list[str]:
    """Split at sentence boundaries so no piece exceeds max_chars where possible."""
    if len(text) <= max_chars:
        return [text]
    spans = sentence_spans(text)
    pieces, cur_start, cur_end = [], spans[0][0], spans[0][0]
    for s, e in spans:
        if cur_end > cur_start and (e - cur_start) > max_chars:
            pieces.append(text[cur_start:cur_end].strip())
            cur_start = s
        cur_end = e
    tail = text[cur_start:cur_end].strip()
    if tail:
        pieces.append(tail)
    return [p for p in pieces if p] or [text]


SUFFIXES = "abcdefghijklmnopqrstuvwxyz"


def word_count(text: str) -> int:
    return len(text.split())
