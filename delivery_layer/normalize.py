"""Display text → spoken text, with a span map.

Why we own this instead of trusting the TTS provider:
  * A wrong number read aloud undercuts the whole delivery claim, so the
    spoken form of every currency / percentage / date / section ref /
    policy number must be *deterministic and testable* (tests/numbers.jsonl).
  * The word map (wordmap.py) needs to know that display span "4(b)(ii)"
    became spoken tokens "four b two", so a boundary landing on "b" maps
    back to the right characters in `text_display`.

`normalize_with_map()` returns the spoken string plus a list of Segments,
one per display token (or per replaced span), each carrying its display
char range and its spoken form. `normalize()` is the string-only shortcut.

Rime `/textnorm`: an optional server-side normaliser can be compared
against these rules with scripts/number_roundtrip.py (--textnorm). It is
never in the shipped path — the rules below are the source of truth so the
golden set stays reproducible offline.
"""
from __future__ import annotations

import contextvars
import re
from dataclasses import dataclass

# --------------------------------------------------------------- number words

_ONES = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
         "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen",
         "seventeen", "eighteen", "nineteen"]
_TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]
_SCALES = [(1_000_000_000, "billion"), (1_000_000, "million"), (1_000, "thousand")]
_ORD_IRREG = {"one": "first", "two": "second", "three": "third", "five": "fifth",
              "eight": "eighth", "nine": "ninth", "twelve": "twelfth"}
_MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August",
           "September", "October", "November", "December"]
_ROMAN = {"i": 1, "v": 5, "x": 10, "l": 50, "c": 100}


def cardinal(n: int) -> str:
    if n < 0:
        return "minus " + cardinal(-n)
    if n < 20:
        return _ONES[n]
    if n < 100:
        t, o = divmod(n, 10)
        return _TENS[t] + ("-" + _ONES[o] if o else "")
    if n < 1000:
        h, r = divmod(n, 100)
        return _ONES[h] + " hundred" + (" " + cardinal(r) if r else "")
    for value, name in _SCALES:
        if n >= value:
            q, r = divmod(n, value)
            return cardinal(q) + " " + name + (" " + cardinal(r) if r else "")
    raise ValueError(n)


def ordinal(n: int) -> str:
    words = cardinal(n)
    head, sep, last = words.rpartition("-") if "-" in words else words.rpartition(" ")
    if last in _ORD_IRREG:
        last = _ORD_IRREG[last]
    elif last.endswith("y"):
        last = last[:-1] + "ieth"
    else:
        last = last + "th"
    return head + sep + last


def year_words(y: int) -> str:
    if 2000 <= y <= 2009:
        return "two thousand" + (" " + _ONES[y - 2000] if y > 2000 else "")
    if 1100 <= y <= 1999 or 2010 <= y <= 2099:
        hi, lo = divmod(y, 100)
        return cardinal(hi) + " " + ("hundred" if lo == 0 else ("oh " + _ONES[lo] if lo < 10 else cardinal(lo)))
    return cardinal(y)


def digits_words(s: str) -> str:
    return " ".join(_ONES[int(c)] for c in s if c.isdigit())


def decimal_words(s: str) -> str:
    whole, _, frac = s.partition(".")
    out = cardinal(int(whole.replace(",", "")))
    if frac:
        out += " point " + digits_words(frac)
    return out


def roman_to_int(s: str) -> int | None:
    s = s.lower()
    if not s or any(c not in _ROMAN for c in s):
        return None
    total = 0
    for i, c in enumerate(s):
        v = _ROMAN[c]
        if i + 1 < len(s) and _ROMAN[s[i + 1]] > v:
            total -= v
        else:
            total += v
    return total


def paren_part(p: str) -> str:
    """'(b)' -> 'b', '(ii)' -> 'two', '(II)' -> 'two', '(3)' -> 'three'."""
    inner = p.strip("()")
    if inner.isdigit():
        return cardinal(int(inner))
    low = inner.lower()
    r = roman_to_int(low)
    if r is not None and (len(low) > 1 or low in ("i", "v", "x")):
        return cardinal(r)
    return low


# Standalone list markers. "(ii)", "(IV)", "clauses (i) and (ii)", a line that
# starts "iii." or "iv)", and "Section IV" / "Part II". Multi-letter numerals
# are always roman. A single "i", "v" or "x" is a letter in an (a)...(z) list
# and a numeral in an (i)...(x) list, so it is read as a number only when the
# same text also carries an unambiguous roman marker; the flag is set per
# text by normalize_with_map (a ContextVar, so worker threads do not share it).
_ROMAN_CONTEXT: contextvars.ContextVar = contextvars.ContextVar("roman_context", default=False)
_ROMAN_MULTI = re.compile(r"\((?:ii|iii|iv|vi|vii|viii|ix|xi|xii|xiii|xiv|xv|xvi|xvii|xviii|xix|xx)\)|"
                          r"(?:^|(?<=[\n.;:]\s))(?:ii|iii|iv|vi|vii|viii|ix)[.)]\s", re.I | re.M)


def _roman_marker(raw: str) -> str | None:
    """Spoken cardinal for a roman list marker, or None to leave it alone."""
    low = raw.lower()
    n = roman_to_int(low)
    if n is None or n > 40:
        return None
    if len(low) == 1 and not _ROMAN_CONTEXT.get():
        return None
    return cardinal(n)


# ------------------------------------------------------------------- handlers

def _h_phone(m: re.Match) -> str:
    """Digits one by one, a comma pause between groups.

    "022 - 4890 3009" -> "zero two two, four eight nine zero, three zero zero nine".
    Without this the cardinal rule read an STD code and two groups as three
    numbers ("twenty-two, four thousand eight hundred ninety, ...").
    """
    raw = m.group(0)
    groups = [g for g in re.split(r"[\s\-\u2013()]+", raw.replace("+", "plus ")) if g]
    out = []
    for g in groups:
        if g == "plus":
            out.append("plus")
        elif g.isdigit():
            out.append(" ".join(_ONES[int(c)] for c in g))
    return ", ".join(out).replace("plus, ", "plus ")


def _h_policy(m: re.Match) -> str:
    parts = m.group(0).split("-")
    spoken = []
    for part in parts:
        if part.isdigit():
            spoken.append(digits_words(part))
        else:
            spoken.append(" ".join(c.upper() if c.isalpha() else _ONES[int(c)] for c in part))
    return ", ".join(spoken)


def _h_currency(m: re.Match) -> str:
    whole = int(m.group("cur_whole").replace(",", ""))
    cents = m.group("cur_cents")
    scale = (m.group("cur_scale") or "").lower()
    if scale:
        return f"{decimal_words(m.group('cur_whole') + ('.' + cents if cents else ''))} {scale} dollars"
    if cents and len(cents) != 2:  # "$2.5" — not a cents amount
        return decimal_words(m.group("cur_whole") + "." + cents) + " dollars"
    out = cardinal(whole) + (" dollar" if whole == 1 else " dollars")
    if cents and int(cents):
        c = int(cents)
        out += " and " + cardinal(c) + (" cent" if c == 1 else " cents")
    return out


def _h_percent(m: re.Match) -> str:
    return decimal_words(m.group("pct_num")) + " percent"


def _h_date_long(m: re.Match) -> str:
    return f"{m.group('dl_month')} {ordinal(int(m.group('dl_day')))}, {year_words(int(m.group('dl_year')))}"


def _h_date_num(m: re.Match) -> str:
    mo, d, y = int(m.group("dn_m")), int(m.group("dn_d")), int(m.group("dn_y"))
    # The fixture convention is month first. An Indian wording writes
    # 31/12/2024: when the first field cannot be a month and the second can,
    # it is day first. Anything still invalid is left as written rather than
    # crashing the whole ingest on one date.
    if mo > 12 and 1 <= d <= 12:
        mo, d = d, mo
    if not (1 <= mo <= 12 and 1 <= d <= 31):
        return m.group(0)
    return f"{_MONTHS[mo - 1]} {ordinal(d)}, {year_words(y)}"


def _h_date_iso(m: re.Match) -> str:
    y, mo, d = int(m.group("di_y")), int(m.group("di_m")), int(m.group("di_d"))
    if not (1 <= mo <= 12 and 1 <= d <= 31):
        return m.group(0)
    return f"{_MONTHS[mo - 1]} {ordinal(d)}, {year_words(y)}"


def _h_section(m: re.Match) -> str:
    g = m.groupdict()
    label = g.get("sec_label") or g.get("sd_label")
    num = g.get("sec_num") or g.get("sd_num")
    parens = g.get("sec_parens") or ""
    spoken = " point ".join(cardinal(int(p)) for p in num.split("."))
    for p in re.findall(r"\([^)]+\)", parens):
        spoken += " " + paren_part(p)
    return (label + " " if label else "") + spoken


def _h_time(m: re.Match) -> str:
    h, mi = int(m.group("tm_h")), int(m.group("tm_m"))
    out = cardinal(h) + " " + ("o'clock" if mi == 0 else ("oh " + _ONES[mi] if mi < 10 else cardinal(mi)))
    ampm = (m.group("tm_ap") or "").lower().replace(".", "")
    if ampm:
        out += " " + " ".join(ampm)
    return out


def _h_ordinal(m: re.Match) -> str:
    return ordinal(int(m.group("ord_num")))


def _h_decimal(m: re.Match) -> str:
    return decimal_words(m.group(0))


def _h_roman_paren(m: re.Match) -> str:
    spoken = _roman_marker(m.group("rp"))
    if spoken is None:
        return m.group(0)
    # A marker that opens a sentence gets a pause after it: "(ii) The insured
    # shall" -> "two, The insured shall"; mid-sentence "clause (ii) above" does not.
    before = m.string[:m.start()].rstrip()
    if not before or before.endswith((".", ":", ";", "\n")):
        return spoken + ","
    return spoken


def _h_roman_line(m: re.Match) -> str:
    spoken = _roman_marker(m.group("rl"))
    return (spoken + ",") if spoken is not None else m.group(0)


def _h_roman_label(m: re.Match) -> str:
    n = roman_to_int(m.group("rlab_n").lower())
    if n is None or n > 40:
        return m.group(0)
    return m.group("rlab_l") + " " + cardinal(n)


def _h_int(m: re.Match) -> str:
    raw = m.group(0)
    n = int(raw.replace(",", ""))
    if "," not in raw and len(raw) == 4 and 1900 <= n <= 2099:
        return year_words(n)
    return cardinal(n)


_PATTERNS = [
    ("policy", r"\b[A-Z]{1,4}-\d[\dA-Z]*(?:-[\dA-Z]+)+\b", _h_policy),
    # Phone numbers, before anything numeric. Every pattern needs a separator
    # or a recognisable prefix, so a bare run of digits (a policy or account
    # number) never matches and still reads as a number.
    ("phone", r"(?<![\w/])(?:"
              r"\+91[\s\-]?\d{5}[\s\-]?\d{5}"                       # +91 98765 43210
              r"|1800[\s\-]\d{3}[\s\-]\d{4}"                         # 1800 266 4545
              r"|1800[\s\-]\d{2,4}[\s\-]\d{2,4}(?:[\s\-]\d{2,4})?"  # 1800-22-9090, 1800-4254-732
              r"|\(\d{2,4}\)\s*\d{4}\s*[\s\-]?\d{4}"                # (022) 4890 3009
              r"|\d{2,5}\s*[\-\u2013]\s*\d{4}\s*[\s\-]?\d{4}"        # 022 - 4890 3009
              r"|\d{2,5}\s+\d{4}\s+\d{4}"                            # 022 4890 3009
              r")(?![\w/])", _h_phone),
    ("currency", r"\$\s?(?P<cur_whole>\d{1,3}(?:,\d{3})+|\d+)(?:\.(?P<cur_cents>\d+))?(?:\s?(?P<cur_scale>million|billion|thousand))?", _h_currency),
    ("percent", r"(?P<pct_num>\d+(?:\.\d+)?)\s?(?:%|percent\b)", _h_percent),
    ("date_long", r"\b(?P<dl_month>January|February|March|April|May|June|July|August|September|October|November|December)\s+(?P<dl_day>\d{1,2})(?:st|nd|rd|th)?,?\s+(?P<dl_year>\d{4})\b", _h_date_long),
    ("date_num", r"\b(?P<dn_m>\d{1,2})/(?P<dn_d>\d{1,2})/(?P<dn_y>\d{4})\b", _h_date_num),
    ("date_iso", r"\b(?P<di_y>\d{4})-(?P<di_m>\d{2})-(?P<di_d>\d{2})\b", _h_date_iso),
    ("section", r"\b(?:(?P<sec_label>Sections?|Parts?|Paragraphs?|Clauses?|Articles?|Items?)\s+)?(?P<sec_num>\d+(?:\.\d+)*)(?P<sec_parens>(?:\([A-Za-z0-9]{1,4}\))+)", _h_section),
    ("section_dotted", r"\b(?P<sd_label>Sections?|Parts?|Paragraphs?|Clauses?|Articles?)\s+(?P<sd_num>\d+(?:\.\d+)+)\b", _h_section),
    # Roman list markers and labels: "(ii)", "(IV)", "Section IV", a line
    # starting "iii." -- see _roman_marker for the single-letter rule.
    ("roman_paren", r"(?<![\w)])\((?P<rp>[ivxIVX]{1,6})\)", _h_roman_paren),
    ("roman_label", r"\b(?P<rlab_l>Sections?|Parts?|Schedules?|Chapters?|Annexures?|Clauses?|Articles?|Phases?|Stages?|Tables?)\s+(?P<rlab_n>[IVX]{1,6})\b(?![.\d])", _h_roman_label),
    ("roman_line", r"(?:^|(?<=[.;:]\s))(?P<rl>[ivxIVX]{1,6})[.)](?=\s)", _h_roman_line),
    ("time", r"\b(?P<tm_h>\d{1,2}):(?P<tm_m>\d{2})(?:\s?(?P<tm_ap>[apAP]\.?[mM]\.?))?", _h_time),
    ("ordinal", r"\b(?P<ord_num>\d+)(?:st|nd|rd|th)\b", _h_ordinal),
    ("decimal", r"\b\d+\.\d+\b", _h_decimal),
    ("integer", r"\b\d{1,3}(?:,\d{3})+\b|\b\d+\b", _h_int),
]

_MASTER = re.compile("|".join(f"(?P<{name}>{pat})" for name, pat, _ in _PATTERNS))
_HANDLERS = {name: fn for name, _, fn in _PATTERNS}


@dataclass(frozen=True)
class Segment:
    """One display span and what is spoken for it."""
    display_start: int
    display_end: int
    spoken: str
    replaced: bool = False

    def display_text(self, text: str) -> str:
        return text[self.display_start:self.display_end]


def _gap_segments(text: str, start: int, end: int) -> list[Segment]:
    return [Segment(start + m.start(), start + m.end(), m.group(0))
            for m in re.finditer(r"\S+", text[start:end])]


def normalize_with_map(text: str) -> tuple[str, list[Segment]]:
    segments: list[Segment] = []
    cursor = 0
    _ROMAN_CONTEXT.set(bool(_ROMAN_MULTI.search(text)))
    for m in _MASTER.finditer(text):
        segments.extend(_gap_segments(text, cursor, m.start()))
        segments.append(Segment(m.start(), m.end(), _HANDLERS[m.lastgroup](m), replaced=True))
        cursor = m.end()
    segments.extend(_gap_segments(text, cursor, len(text)))

    out = []
    prev_end = None
    for seg in segments:
        if prev_end is not None:
            out.append(text[prev_end:seg.display_start])  # original whitespace (or nothing)
        out.append(seg.spoken)
        prev_end = seg.display_end
    return "".join(out), segments


def normalize(text: str) -> str:
    return normalize_with_map(text)[0]


if __name__ == "__main__":  # quick manual check
    import sys
    for line in sys.stdin:
        print(normalize(line.rstrip("\n")))
