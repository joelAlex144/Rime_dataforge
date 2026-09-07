#!/usr/bin/env python3
"""Build-time document ingestion: a file or URL in, a committed fixture out.

BUILD TIME ONLY. Nothing in the agent path calls this. A developer runs it,
eyeballs the segmentation with --review, and commits the resulting JSON.
There is no runtime upload and no runtime URL fetching anywhere in the
delivery layer; this script is the only place a network fetch happens, and
its product is a file in version control.

  python scripts/ingest.py <path-or-url> --out examples/policy-reader/fixtures/<name>.json \
      [--title "..."] [--id-prefix sec] [--min-clause-chars 40] [--max-clause-chars 600] \
      [--synthetic] [--dry-run] [--ocr] [--fold-lists CHARS] [--source auto|pdf|text]

Output schema is exactly fixtures/policy.json's, plus optional per-clause
fields (`kind`, `path`, and from the structure pass `spoken_on_request`,
`has_placeholder`, `parent`, `page`) and two optional document blocks (`map`,
`terms`). grounding.py / wordmap.py / position.py / read_demo.py all read
that schema, so it does not change.

Structure pass (`--source pdf`, also accepted for .docx): Docling's layout
model classifies every element deterministically -- no LLM -- and the
lossless DoclingDocument is saved next to the fixture as
`<name>.docling.json` (the reproducibility artifact; commit it). Docling is a
BUILD dependency only (requirements-build.txt), runs on the developer's
machine, never in CI or at demo time. One-time model download (~500 MB):

    pip install -r requirements-build.txt
    docling-tools models download

Re-running on the same input produces a byte-identical fixture: nothing
time-dependent is written for a local file.

Exit codes:  0 ok   1 extraction or validation failure   2 refused (PII / unsafe)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "examples" / "policy-reader"))

from delivery_layer.normalize import normalize_with_map          # noqa: E402
from segment import (                                            # noqa: E402
    AMBIGUOUS_TOKENS, ROMAN_RE,
    Block, Marker, drop_page_numbers, drop_repeated_lines, join_hyphenated,
    parse_marker, path_to_human, path_to_id, sentence_spans, split_long,
    strip_toc, word_count,
)

MIN_EXTRACT_CHARS = 500
BULLET_CONTEXT_WORDS = 6


def die(msg: str, code: int = 1) -> "None":
    print(f"ingest: {msg}", file=sys.stderr)
    sys.exit(code)


def int_to_roman(n: int) -> str:
    vals = [(1000, "m"), (900, "cm"), (500, "d"), (400, "cd"), (100, "c"), (90, "xc"),
            (50, "l"), (40, "xl"), (10, "x"), (9, "ix"), (5, "v"), (4, "iv"), (1, "i")]
    out = []
    for v, s in vals:
        while n >= v:
            out.append(s)
            n -= v
    return "".join(out)


# ==========================================================================
# extraction
# ==========================================================================

# ---- text-dump hygiene ----------------------------------------------------
# A PDF dumped to text carries page furniture that is not the document:
# `=== PAGE n ===` markers, "Page x of y", lines of dots or dashes, form
# blanks (underscore runs) and checkbox glyphs. The glyphs are the Unicode
# box set and the Private Use Area that Wingdings/Symbol characters land in.
_PAGE_MARK = re.compile(r"^\s*=+\s*PAGE\s+\d+\s*=+\s*$", re.I)
_PAGE_OF = re.compile(r"\bPage\s+\d{1,4}\s+of\s+\d{1,4}\b", re.I)
_UNDERSCORE_RUN = re.compile(r"_{3,}")
_GLYPH = re.compile(r"[\u2610\u2611\u2612\u25a1\u25a0\u25a2\u25a3\u25cb\u25cf\u25ef\u25fb\u25fc\ue000-\uf8ff]")
_NO_ALNUM = re.compile(r"^[\W_]+$")
_FORM_FIELD = re.compile(r":\s*(?:_{2,}|blank)\s*$", re.I)


def split_pages(text: str) -> list[str]:
    """A text dump with `=== PAGE n ===` markers, split into its pages; one
    page when there are no markers."""
    parts = re.split(r"(?mi)^\s*=+\s*PAGE\s+\d+\s*=+\s*$", text)
    return parts if len(parts) > 1 else [text]


def clean_lines(lines: list[str]) -> list[str]:
    """Page furniture out of a text dump, line by line: page markers and
    "Page x of y" go; a line of punctuation, underscores or box glyphs goes;
    checkbox glyphs inside a line go; a run of underscores reads as "blank".
    A dropped line leaves a blank line behind, so the paragraphs on either
    side of it stay separate."""
    out: list[str] = []
    for line in lines:
        s = line.strip()
        if not s:
            out.append("")
            continue
        if _PAGE_MARK.match(s):
            out.append("")
            continue
        s = _PAGE_OF.sub(" ", s)
        s = _GLYPH.sub(" ", s)
        s = " ".join(s.split())
        if not s or _NO_ALNUM.match(s):
            out.append("")
            continue
        s = " ".join(_UNDERSCORE_RUN.sub(" blank ", s).split())
        out.append(s)
    return out


def extract_txt(path: Path) -> tuple[list[Block], str]:
    raw = path.read_text(encoding="utf-8", errors="replace")
    markdown = path.suffix.lower() in (".md", ".markdown")
    pages = ["\n".join(clean_lines(p.splitlines())) for p in split_pages(raw)]
    pages = drop_repeated_lines(pages)            # running headers/footers, txt as well as pdf
    return blocks_from_lines("\n".join(pages).splitlines(), markdown=markdown), raw


def extract_pdf(path: Path) -> tuple[list[Block], str]:
    try:
        from pypdf import PdfReader
    except ImportError:
        try:
            from PyPDF2 import PdfReader  # type: ignore
        except ImportError:
            die("PDF input needs pypdf:  pip install pypdf")
    reader = PdfReader(str(path))
    pages = ["\n".join(clean_lines((p.extract_text() or "").splitlines())) for p in reader.pages]
    pages = drop_repeated_lines(pages)
    raw = join_hyphenated("\n".join(pages))
    lines = drop_page_numbers(raw.splitlines())
    return blocks_from_lines(lines), raw


def extract_docx(path: Path) -> tuple[list[Block], str]:
    try:
        import docx  # python-docx
    except ImportError:
        die("DOCX input needs python-docx:  pip install python-docx")
    d = docx.Document(str(path))
    blocks: list[Block] = []
    raw_parts: list[str] = []
    for p in d.paragraphs:
        t = p.text.strip()
        if not t:
            continue
        raw_parts.append(t)
        style = (p.style.name or "").lower() if p.style is not None else ""
        if style.startswith("heading"):
            lvl = int(re.sub(r"\D", "", style) or 1)
            blocks.append(Block("heading", t, min(lvl, 4)))
        elif style.startswith("list"):
            blocks.append(Block("list_item", t))
        else:
            blocks.append(Block("para", t))
    for table in d.tables:
        head = [c.text.strip() for c in table.rows[0].cells] if table.rows else []
        for row in table.rows[1:]:
            cells = [c.text.strip() for c in row.cells]
            txt = row_sentence(head, cells)
            if txt:
                blocks.append(Block("table_row", txt))
                raw_parts.append(txt)
    return blocks, "\n".join(raw_parts)


def row_sentence(head: list[str], cells: list[str]) -> str:
    pairs = []
    for i, v in enumerate(cells):
        v = " ".join(v.split())
        if not v:
            continue
        k = " ".join(head[i].split()) if i < len(head) and head[i].strip() else f"column {i + 1}"
        pairs.append(f"{k}: {v}")
    return ("Row: " + "; ".join(pairs) + ".") if pairs else ""


def extract_html(source: str, is_url: bool) -> tuple[list[Block], str, dict]:
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        die("HTML input needs beautifulsoup4:  pip install beautifulsoup4")
    meta: dict = {}
    if is_url:
        import requests
        headers = {"User-Agent": "rime-delivery-layer ingest (build-time; contact repo owner)"}
        r = requests.get(source, timeout=45, headers=headers)
        if r.status_code != 200:
            die(f"fetch failed: HTTP {r.status_code}")
        html = r.text
        meta["fetched_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    else:
        html = Path(source).read_text(encoding="utf-8", errors="replace")
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header", "form", "noscript", "aside"]):
        tag.decompose()
    # Site chrome that is not document text. These are generic component names,
    # not one site's markup, but the list is empirical -- extend it when a new
    # source leaks furniture into the fixture, and record that in fixtures/README.md.
    for sel in ("nav", "footer", "banner", "cookie", "breadcrumb", "menu", "sidebar", "skip",
                "feedback", "survey", "improve", "share", "related", "contents-list",
                "print-link", "pagination", "search", "subscri"):
        for tag in soup.find_all(attrs={"class": re.compile(sel, re.I)}):
            tag.decompose()
        for tag in soup.find_all(attrs={"id": re.compile(sel, re.I)}):
            tag.decompose()

    blocks: list[Block] = []
    raw_parts: list[str] = []
    seen_tables: set[int] = set()
    for el in soup.find_all(["h1", "h2", "h3", "h4", "p", "li", "table"]):
        if el.name == "table":
            seen_tables.add(id(el))
            rows = el.find_all("tr")
            head: list[str] = []
            if rows:
                head = [c.get_text(" ", strip=True) for c in rows[0].find_all(["th", "td"])]
            for tr in rows[1:] if head else rows:
                cells = [c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"])]
                txt = row_sentence(head, cells)
                if txt:
                    blocks.append(Block("table_row", txt))
                    raw_parts.append(txt)
            continue
        if el.find_parent("table") is not None:
            continue
        text = " ".join(el.get_text(" ", strip=True).split())
        if not text:
            continue
        raw_parts.append(text)
        if el.name.startswith("h"):
            blocks.append(Block("heading", text, int(el.name[1])))
        elif el.name == "li":
            blocks.append(Block("list_item", text))
        else:
            blocks.append(Block("para", text))
    return blocks, "\n".join(raw_parts), meta


_MD_HEADING = re.compile(r"^(#{1,4})\s+(.*)$")
_MD_BULLET = re.compile(r"^\s*[-*+•]\s+(.*)$")


def blocks_from_lines(lines: list[str], markdown: bool = True) -> list[Block]:
    lines = strip_toc([l.rstrip() for l in lines])
    lines = drop_page_numbers(lines)
    blocks: list[Block] = []
    buf: list[str] = []
    heuristic: set = set()                   # indices of headings the case rule produced

    def flush() -> None:
        if buf:
            t = " ".join(" ".join(buf).split())
            if t:
                blocks.append(Block("para", t))
            buf.clear()

    for line in lines:
        s = line.strip()
        if not s:
            flush()
            continue
        m = _MD_HEADING.match(s) if markdown else None
        if m:
            flush()
            blocks.append(Block("heading", m.group(2).strip(), len(m.group(1))))
            continue
        b = _MD_BULLET.match(s)
        if b:
            flush()
            blocks.append(Block("list_item", b.group(1).strip()))
            continue
        if parse_marker(s):
            # A clause marker at line start opens a new block even without a
            # blank line before it; continuation lines still join it.
            flush()
            buf.append(s)
            continue
        # An ALL-CAPS or short unpunctuated standalone line reads as a heading --
        # never a single word, a form field ("APPLICATION NO.: ____"), a line
        # that is mostly a number (a phone, a pin code), one that dangles on
        # "/" or a dash, one with table symbols (">", "="), or one that
        # repeats a word ("Nil Nil Nil": a row of a dumped table).
        words = s.split()
        if len(s) < 90 and not s.endswith((".", ";", ":", ",", "/", "-", "\u2013", "\u2014", "(", "&")) \
                and re.search(r"[A-Za-z]", s) \
                and (s.isupper() or (s.istitle() and len(words) <= 10)) and not parse_marker(s) \
                and len(words) >= 2 and not _FORM_FIELD.search(s) \
                and sum(ch.isdigit() for ch in s) < 4 and not any(ch in s for ch in "<>=%|") \
                and len({w.lower() for w in words}) == len(words):
            flush()
            blocks.append(Block("heading", s, 2))
            heuristic.add(len(blocks) - 1)
            continue
        buf.append(s)
    flush()
    # A heuristic heading heads something: one followed directly by another
    # heading, or by nothing, was a line of a dumped table and is body text.
    for i in sorted(heuristic):
        if i + 1 >= len(blocks) or blocks[i + 1].kind == "heading":
            blocks[i] = Block("para", blocks[i].text)
    return blocks


# ==========================================================================
# segmentation
# ==========================================================================

@dataclass
class Raw:
    text: str
    section: int
    section_title: str
    subsection: Optional[str] = None
    item: Optional[str] = None
    path: Optional[str] = None
    kind: str = "clause"
    id_parts: list[str] = field(default_factory=list)
    # structure pass
    spoken_on_request: bool = False
    has_placeholder: bool = False
    parent_parts: Optional[list] = None     # table_row -> its table_stub
    page: int = 0
    demoted_by: str = ""
    label: str = ""
    level: int = 0
    children: int = 0                       # heading: direct body children


def apply_bullet_context(blocks: list[Block]) -> list[Block]:
    """Rule 5, as a pre-pass so both segmenters stay simple.

    A bullet under six words cannot be understood on its own ("50% of your
    pension contributions"), so it borrows its introducing sentence. When that
    happens the introducer must NOT also survive as its own clause, or the
    short-clause merge in rule 4 folds it into the very bullet that already
    quotes it and you get "Expenses can include: Expenses can include: ...".
    """
    out: list[Block] = []
    i = 0
    while i < len(blocks):
        b = blocks[i]
        if b.kind == "para" and b.text.rstrip().endswith(":"):
            j = i + 1
            run = []
            while j < len(blocks) and blocks[j].kind == "list_item":
                run.append(blocks[j])
                j += 1
            if run:
                intro = b.text.rstrip()
                short = [r for r in run if word_count(r.text) < BULLET_CONTEXT_WORDS]
                if short:
                    for r in run:
                        text = f"{intro} {r.text}" if r in short else r.text
                        out.append(Block("list_item", text))
                else:
                    out.append(b)          # bullets stand alone; keep the introducer
                    out.extend(run)
                i = j
                continue
        out.append(b)
        i += 1
    return out


INDEX_FILENAME = "index.json"


def update_index(out: Path, doc: dict, name: str, referral: str = None, index_path=None) -> dict:
    """Append or update this document's entry in fixtures/index.json.

    The registry is the only source of documents the reader can open, so a
    fixture that is written but not registered is invisible at runtime -- which
    is deliberate: registration is the moment a document becomes selectable.
    """
    index = Path(index_path) if index_path else out.parent / INDEX_FILENAME
    data = {"documents": []}
    if index.exists():
        try:
            data = json.loads(index.read_text(encoding="utf-8"))
        except ValueError:
            die(f"{index} is not valid JSON; fix or delete it before ingesting")
        data.setdefault("documents", [])

    rel = os.path.relpath(out.resolve(), index.parent.resolve()).replace(os.sep, "/")
    entry = {
        "name": name,
        "doc_id": doc.get("doc_id", name),
        "title": doc["title"],
        "spoken_title": doc.get("spoken_title"),
        "path": rel,
        "source": doc["source"],
        "clause_count": doc["clause_count"],
        # A person pressing Accept (or editing this file) is the review. A new
        # document is in the library at once, flagged, and readable unless the
        # structure pass found no body text at all.
        "reviewed": False,
        "readable": bool(doc.get("readable", True)),
        "report": doc.get("report_file"),
        "referral": referral or "the team that publishes this document",
        "ingested_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }

    by_name = [e for e in data["documents"] if e.get("name") == name]
    clash = [e for e in data["documents"] if e.get("name") != name and e.get("path") == rel]
    if clash:
        raise IngestError(f"{index.name} already registers {rel} under the name {clash[0]['name']!r}; "
                          f"remove that entry or pass --name {clash[0]['name']}")
    if by_name:
        if by_name[0].get("path") != rel:
            die(f"{index.name} already has a document named {name!r} at "
                f"{by_name[0].get('path')!r}. Names must be unique -- pass --name <other>.")
        entry["reviewed"] = bool(by_name[0].get("reviewed", False))    # a re-ingest keeps the review
        by_name[0].update(entry)
        action = "updated"
    else:
        data["documents"].append(entry)
        action = "registered"
    data["documents"].sort(key=lambda e: e.get("name", ""))
    index.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"{action} {name!r} in {index.name} ({len(data['documents'])} documents)", file=sys.stderr)
    return entry


def _is_title(rest: str) -> bool:
    """Does the text after a marker read as a heading rather than a clause?"""
    r = (rest or "").strip()
    return bool(r) and len(r) < 80 and not r.endswith((".", ";", ":", "!", "?")) and len(r.split()) <= 10


def looks_numbered(blocks: list[Block]) -> bool:
    """True when the document carries its own clause numbering worth honouring."""
    hits = 0
    for b in blocks:
        if b.kind in ("para", "heading"):
            m = parse_marker(b.text)
            if m and (m.keyword or m.dotted or m.kind in ("alpha", "roman")):
                hits += 1
    return hits >= max(5, len(blocks) // 20)


def segment(blocks: list[Block], opts) -> list[Raw]:
    blocks = apply_bullet_context(blocks)
    return _segment_numbered(blocks, opts) if looks_numbered(blocks) else _segment_headed(blocks, opts)


def _segment_headed(blocks: list[Block], opts) -> list[Raw]:
    """Rule 2: headings are sections, paragraphs are clauses. ids sec-<n>-p<k>."""
    out: list[Raw] = []
    sec_no, sec_title, k = 0, "", 0
    for b in blocks:
        if b.kind == "heading":
            sec_no += 1
            sec_title = b.text
            k = 0
            continue
        if sec_no == 0:                     # text before the first heading
            sec_no, sec_title = 1, "Introduction"
        text = b.text
        k += 1
        out.append(Raw(text=text, section=sec_no, section_title=sec_title,
                       kind="table_row" if b.kind == "table_row" else "clause",
                       id_parts=[str(sec_no), f"p{k}"]))
    return out


def _segment_numbered(blocks: list[Block], opts) -> list[Raw]:
    """Rule 1: build a hierarchy from the document's own numbering."""
    out: list[Raw] = []
    sec_no, sec_title = 0, ""
    letter: Optional[str] = None
    roman_n = 0
    prev_roman = False
    introducer: Optional[str] = None

    def next_item() -> str:
        nonlocal roman_n
        roman_n += 1
        return int_to_roman(roman_n)

    for b in blocks:
        text = b.text
        m = parse_marker(text, prev_roman=prev_roman)

        # Disambiguate a lone i/v/x. "(i)" straight after "(a)" opens a nested
        # roman run; "(i)" straight after "(h)" is simply the next letter.
        if m and m.kind == "alpha" and len(m.token) == 1 and m.token in AMBIGUOUS_TOKENS:
            expected = chr(ord(letter) + 1) if (letter and len(letter) == 1 and letter.isalpha()) else "a"
            if m.token != expected:
                m = Marker("roman", m.token, m.rest, m.dotted, m.keyword)

        if b.kind == "heading" and not (m and (m.dotted or m.keyword)):
            sec_no += 1
            sec_title = text
            letter, roman_n, prev_roman, introducer = None, 0, False, None
            continue

        if m and m.keyword:                                   # "Section 4 ..."
            sec_no = int(m.token) if m.token.isdigit() else sec_no + 1
            letter, roman_n, prev_roman = None, 0, False
            if _is_title(m.rest):
                # "Section 4 Perils Insured Against" is a heading, not a clause.
                sec_title = m.rest
                continue
            sec_title = sec_title or f"Section {m.token}"
            if not m.rest:
                continue
            text = m.rest
            out.append(Raw(text=text, section=sec_no, section_title=sec_title,
                           subsection=None, item=next_item(), path=path_to_human([str(sec_no)]),
                           kind="clause", id_parts=[str(sec_no), int_to_roman(roman_n)]))
            continue

        if m and m.dotted:                                    # "4.2.1 ..."
            parts = m.dotted
            sec_no = int(parts[0]) if parts[0].isdigit() else sec_no
            if len(parts) == 1:
                sec_title = m.rest[:120] or sec_title
            out.append(Raw(text=m.rest or text, section=sec_no, section_title=sec_title,
                           subsection=None, item=None, path=".".join(parts),
                           kind="clause", id_parts=parts))
            prev_roman = False
            continue

        if m and m.kind == "alpha":                           # "(b) ..."
            letter = m.token
            roman_n, prev_roman = 0, False
            if not m.rest:
                continue
            item = next_item()
            out.append(Raw(text=m.rest, section=sec_no or 1, section_title=sec_title,
                           subsection=letter, item=item,
                           path=path_to_human([str(sec_no or 1), letter, item]),
                           kind="clause", id_parts=[str(sec_no or 1), letter, item]))
            continue

        if m and m.kind == "roman":                           # "(ii) ..."
            prev_roman = True
            item = m.token
            roman_n = max(roman_n, len(m.token))
            body = m.rest or text
            parts = [str(sec_no or 1)] + ([letter] if letter else []) + [item]
            out.append(Raw(text=body, section=sec_no or 1, section_title=sec_title,
                           subsection=letter, item=item, path=path_to_human(parts),
                           kind="clause", id_parts=parts))
            continue

        if m and m.kind == "num" and not m.keyword:           # "4. ..."
            sec_no = int(m.token)
            letter, roman_n, prev_roman = None, 0, False
            if _is_title(m.rest):
                sec_title = m.rest
                continue
            if not m.rest:
                continue
            item = next_item()
            out.append(Raw(text=m.rest, section=sec_no, section_title=sec_title,
                           subsection=None, item=item, path=path_to_human([str(sec_no), item]),
                           kind="clause", id_parts=[str(sec_no), item]))
            continue

        # unmarked body text / bullet / table row
        if sec_no == 0:
            sec_no, sec_title = 1, sec_title or "Introduction"
        item = next_item()
        parts = [str(sec_no)] + ([letter] if letter else []) + [item]
        out.append(Raw(text=text, section=sec_no, section_title=sec_title,
                       subsection=letter, item=item, path=path_to_human(parts),
                       kind="table_row" if b.kind == "table_row" else "clause",
                       id_parts=parts))
    return out


def merge_and_split(raws: list[Raw], opts) -> list[Raw]:
    """Rule 4 then rule 3: absorb runt clauses forward, then split oversized ones."""
    merged: list[Raw] = []
    carry: Optional[Raw] = None
    for r in raws:
        if carry is not None:
            r = Raw(text=(carry.text.rstrip() + " " + r.text).strip(), section=r.section,
                    section_title=r.section_title, subsection=r.subsection, item=r.item,
                    path=r.path, kind=r.kind, id_parts=r.id_parts)
            carry = None
        if len(r.text) < opts.min_clause_chars and r.kind != "table_row":
            carry = r
            continue
        merged.append(r)
    if carry is not None:                       # trailing runt: keep it rather than lose text
        merged.append(carry)

    out: list[Raw] = []
    for r in merged:
        pieces = split_long(r.text, opts.max_clause_chars)
        if len(pieces) == 1:
            out.append(r)
            continue
        for n, piece in enumerate(pieces):
            suffix = chr(ord("a") + n) if n < 26 else f"x{n}"
            out.append(Raw(text=piece, section=r.section, section_title=r.section_title,
                           subsection=r.subsection, item=r.item,
                           path=(r.path + f"-{suffix}") if r.path else None,
                           kind=r.kind, id_parts=list(r.id_parts) + [suffix]))
    return out


def to_records(raws: list[Raw], opts) -> list[dict]:
    seen: dict[str, int] = {}
    records: list[dict] = []
    for i, r in enumerate(raws):
        cid = path_to_id(r.id_parts, prefix=opts.id_prefix)
        if cid in seen:
            seen[cid] += 1
            # "-dupN", never "-N": a bare number collides with dotted numbering
            # ("2.2" is sec-2-2), which is exactly where duplicates arise.
            new = f"{cid}-dup{seen[cid]}"
            print(f"warning: duplicate id {cid} -> {new}", file=sys.stderr)
            cid = new
        else:
            seen[cid] = 1
        spoken, segs = normalize_with_map(r.text)
        rec = {
            "id": cid,
            "index": i,
            "section": r.section,
            "section_title": r.section_title,
            "subsection": r.subsection,
            "item": r.item,
            "text_display": r.text,
            "text_spoken": spoken,
            "sentences": sentence_spans(r.text),
            "spoken_map": [[s.display_start, s.display_end, s.spoken] for s in segs],
        }
        if r.kind != "clause":
            rec["kind"] = r.kind
        if r.path:
            rec["path"] = r.path
        if r.spoken_on_request:
            rec["spoken_on_request"] = True
        if r.has_placeholder:
            rec["has_placeholder"] = True
        if r.parent_parts:
            rec["parent"] = path_to_id(r.parent_parts, prefix=opts.id_prefix)
        if r.page:
            rec["page"] = r.page
        if r.demoted_by:
            rec["demoted_by"] = r.demoted_by
        if r.kind == "boilerplate" and r.label:
            rec["label"] = r.label
        if r.kind == "heading":
            rec["level"] = r.level or 1
            rec["children"] = r.children
        records.append(rec)
    return records


# ==========================================================================
# structure pass (Docling): typed blocks -> clauses with stable ids
# ==========================================================================

MAX_CLAUSE_CHARS_HARD = 1000
_DEFINITIONS_HEADING = re.compile(
    r"definition|interpretation|special meaning|meaning of certain words|defined terms|glossary", re.I)
# A root heading: the document's own top-level division. Its token may be a
# number, a roman numeral, or a single letter ("PART A", "Schedule B").
_ROOT_HEADING = re.compile(
    r"^\s*(section|part|chapter|schedule|annexure|annex|article|clause)\s+([0-9]{1,3}|[IVXLCDM]{1,6}|[A-Z])\b"
    r"[\s:.\-\u2013\u2014]*(.*)$", re.I)
_TERM_LEAD = re.compile(
    r"^(?:the\s+(?:words?|terms?)\s+)?[\"\u201c']?([A-Za-z][A-Za-z0-9 ,/'\-]{0,60}?)[\"\u201d']?"
    r"\s+(?:means?|refers?\s+to|shall\s+mean|is\s+defined\s+as)\b", re.I)


def _heading_anchor(text: str, ordinal: int) -> tuple[str, Optional[str], str]:
    """(anchor token, human token, title) from the document's own numbering.

    "4. Exclusions" -> ("4", "4", "Exclusions"); "D. Exclusions" -> ("d", "D", "Exclusions");
    "Section 5 General" -> ("5", "5", "General"); unnumbered -> (str(ordinal), None, text).
    """
    m = parse_marker(text)
    if m and m.token and (m.keyword or m.dotted or m.kind in ("num", "alpha", "roman")):
        tok = ".".join(m.dotted) if m.dotted else m.token
        title = m.rest.strip() or text
        return tok.lower(), tok, title
    return str(ordinal), None, text


class _AnchorStack:
    """Heading hierarchy from the document's own markers, so ids follow its
    numbering tree: "1.2" under "Part II" is sec-pii-1-2, not a second sec-1-2.

    Depth comes from the marker, not from the layout model's heading level
    (unreliable on DOCX, where every heading may be level 1):
      keyword ("Section 4", "Part II", "Annexure A")  -> depth 1, the root
      dotted  ("1.2.3")                               -> depths 2..n+1 under a root, else 1..n
      plain   ("4.", "D.", "(ii)")                    -> one below the root, else depth 1
      none                                            -> one below the root, token = ordinal
    """

    def __init__(self) -> None:
        self.stack: list[tuple[int, str, Optional[str]]] = []      # (depth, token, human)
        self.kinds: dict[int, str] = {}                            # depth -> marker kind

    @property
    def _base(self) -> int:
        return 1 if self.stack and self.stack[0][0] == 1 and self.stack[0][2] and \
            self.stack[0][2].startswith("\u00a7") else 0

    def push(self, marker, ordinal: int, root: Optional[tuple] = None) -> int:
        """Returns the depth of the heading just pushed. `root` is (word, token)
        for a top-level division heading such as ("Part", "A")."""
        if root is not None:
            word, token = root
            tok = f"{word[0].lower()}{token.lower()}"                       # Part II -> "pii"
            self.stack = [(1, tok, "\u00a7" + word.title() + " " + token)]
            self.kinds = {1: "root"}
            return 1
        base = self._base
        if marker is not None and marker.dotted:
            parts = list(marker.dotted)
            self.stack = [e for e in self.stack if e[0] <= base]
            self.kinds = {d: k for d, k in self.kinds.items() if d <= base}
            for i, t in enumerate(parts):
                self.stack.append((base + 1 + i, t.lower(), t))
                self.kinds[base + 1 + i] = "dotted"
            return base + len(parts)
        if marker is not None and marker.token:
            # "(I)" parses as an ambiguous alpha; for headings any token that
            # reads as a roman numeral is one, so "(II)" is its sibling.
            kind = "roman" if ROMAN_RE.fullmatch(marker.token) else marker.kind
        else:
            kind = "plain"
        # A marker of the same kind as one already on the stack is a sibling
        # at that depth; a different kind nests one level deeper. So "(II)"
        # after "(I)" replaces it, while "2." under "(I)" sits inside it. An
        # unnumbered heading is a section title: a sibling of the current
        # unnumbered level if there is one, else of the level under the root.
        same = [d for d, k in self.kinds.items() if k == kind and d > base]
        if same:
            depth = min(same)
        elif kind == "plain":
            depth = base + 1
        else:
            depth = (max(self.kinds) + 1) if self.kinds else base + 1
        self.stack = [e for e in self.stack if e[0] < depth]
        self.kinds = {d: k for d, k in self.kinds.items() if d < depth}
        if marker is not None and marker.token:
            self.stack.append((depth, marker.token.lower(), marker.token))
        else:
            self.stack.append((depth, str(ordinal), None))
        self.kinds[depth] = kind
        return depth

    @property
    def parts(self) -> list[str]:
        return [t for _, t, _ in self.stack]

    @property
    def human(self) -> Optional[str]:
        hs = [(h or "").lstrip("\u00a7") for _, _, h in self.stack]
        return ".".join(hs) if all(hs) else None       # None once any level is unnumbered


TOC_RUN = 6


def demote_heading_runs(sblocks, notes: list[str]) -> None:
    """A run of TOC_RUN or more consecutive headings with nothing under them is
    a table of contents: the same headings recur later with their bodies, and
    keeping the run would duplicate every root id and read the contents aloud."""
    i = 0
    while i < len(sblocks):
        if sblocks[i].kind != "heading":
            i += 1
            continue
        j = i
        while j < len(sblocks) and sblocks[j].kind == "heading":
            j += 1
        if j - i >= TOC_RUN:
            for k in range(i, j):
                sblocks[k].kind = "boilerplate"
                sblocks[k].demoted_by = "toc_run"
            notes.append(f"table of contents: {j - i} consecutive headings demoted to boilerplate")
        i = max(j, i + 1)


def segment_structured(sblocks, opts) -> tuple[list[Raw], list[str]]:
    """Blocks from the structure pass -> Raws. Headings are clauses (kind
    heading) and the id anchor for everything under them; bodies keep their own
    markers; tables are a stub plus rows; boilerplate keeps its place."""
    out: list[Raw] = []
    notes: list[str] = []
    demote_heading_runs(sblocks, notes)
    ordinal = 0
    anchors = _AnchorStack()
    aparts: list[str] = ["intro"]
    hpath: Optional[str] = None
    title = "Introduction"
    in_definitions = False
    k = b_n = 0
    heading: Optional[Raw] = None
    stub_parts: dict[str, list] = {}
    # Body-level numbering under the current heading, as the numbered
    # segmenter tracks it: "(a)" nests under the numbered paragraph above it,
    # "(ii)" under the letter, and a list that restarts ("a." again after an
    # introducing paragraph, "1." again under a bold sub-title the layout model
    # did not call a heading) nests under that paragraph. Reset at every heading.
    sub: list[str] = []            # numbered/dotted paragraph token(s)
    letter: Optional[str] = None
    roman: Optional[str] = None    # current roman item under the letter
    in_definitions_body = False
    last_p: Optional[str] = None   # most recent unmarked paragraph token
    last_key: Optional[tuple] = None
    last_roman = 0

    def roman_value(t: str) -> int:
        vals = {"i": 1, "v": 5, "x": 10, "l": 50, "c": 100, "d": 500, "m": 1000}
        total, prev = 0, 0
        for ch in reversed(t.lower()):
            v = vals.get(ch, 0)
            total += -v if v < prev else v
            prev = max(prev, v)
        return total

    def fresh_p() -> str:
        # A list that restarts without a new paragraph between still restarts
        # under something: mint the paragraph the document implies.
        nonlocal k, last_p
        k += 1
        last_p = f"p{k}"
        return last_p

    for b in sblocks:
        if b.kind == "heading":
            ordinal += 1
            root_m = _ROOT_HEADING.match(b.text)
            if root_m:
                title = root_m.group(3).strip() or b.text
                depth = anchors.push(None, ordinal, (root_m.group(1), root_m.group(2)))
            else:
                m = parse_marker(b.text)
                if not (m and m.token and (m.dotted or m.kind in ("num", "alpha", "roman"))):
                    m = None
                title = (m.rest.strip() or b.text) if m else b.text
                depth = anchors.push(m, ordinal)
            aparts, hpath = anchors.parts, anchors.human
            in_definitions = bool(_DEFINITIONS_HEADING.search(title))
            k = b_n = 0
            sub, letter, roman, last_p, last_key, last_roman = [], None, None, None, None, 0
            in_definitions_body = False
            heading = Raw(text=title, section=ordinal, section_title=title, kind="heading",
                          id_parts=list(aparts), path=hpath, page=b.page, label=b.label, level=depth)
            out.append(heading)
            continue
        sec = ordinal if ordinal else 1
        anchor = aparts
        if b.kind in ("body", "definition"):
            m = parse_marker(b.text)
            if m and m.keyword:
                # "Clause D (2) of this Policy ..." is a sentence that opens
                # with a cross-reference, not a numbered paragraph.
                m = None
            kind = m.kind if (m and m.token) else None
            tok = m.token.lower() if (m and m.token) else ""
            # "4. Special meaning of certain words: ..." opens a definitions
            # run in body text; it ends at the next numbered paragraph.
            if kind in ("num", "dotted") or (m and m.dotted):
                in_definitions_body = bool(_DEFINITIONS_HEADING.search(b.text[:80]))
            if kind == "alpha" and len(tok) == 1 and tok in AMBIGUOUS_TOKENS:
                # "(i)" straight after "(h)" is the next letter; anywhere else
                # it opens a nested roman run.
                expected = chr(ord(letter) + 1) if (letter and len(letter) == 1 and letter.isalpha()) else "a"
                if tok != expected:
                    kind = "roman"
            if m and m.dotted:                                    # "4.1.2 ..."
                key = ("dotted",) + tuple(int(x) if x.isdigit() else x for x in m.dotted)
                restart = last_key is not None and last_key[0] == "dotted" and key <= last_key
                base_p = [fresh_p()] if restart else ([last_p] if (last_p and last_p in sub[:1]) else [])
                sub = base_p + [t.lower() for t in m.dotted]
                letter, roman, last_key, last_roman = None, None, key, 0
                parts = anchor + sub
                path = path_to_human([hpath or ".".join(anchor)] + list(m.dotted))
            elif kind == "num":                                   # "3. ..."
                key = ("num", int(tok) if tok.isdigit() else tok)
                restart = last_key is not None and last_key[0] == "num" and key <= last_key
                base_p = [fresh_p()] if restart else ([last_p] if (last_p and last_p in sub[:1]) else [])
                sub = base_p + [tok]
                letter, roman, last_key, last_roman = None, None, key, 0
                parts = anchor + sub
                path = path_to_human([hpath or ".".join(anchor)] + sub)
            elif kind == "alpha":                                 # "(a) ..."
                if letter and tok <= letter:
                    if roman:
                        # "a) b) c)" under "(ii)": a second-level list.
                        sub = sub + [letter, roman]
                        roman = None
                    else:
                        sub = [fresh_p()]                         # a list restarting anew
                letter, last_roman = tok, 0
                parts = anchor + sub + [letter]
                path = path_to_human([hpath or ".".join(anchor)] + sub + [m.token])
            elif kind == "roman":                                 # "(ii) ..."
                val = roman_value(tok)
                if last_roman and val <= last_roman:
                    sub, letter = [fresh_p()], None               # a roman list restarting
                roman, last_roman = tok, val
                parts = anchor + sub + ([letter] if letter else []) + [tok]
                path = path_to_human([hpath or ".".join(anchor)] + sub + ([letter] if letter else []) + [m.token])
            elif kind:
                parts = anchor + sub + [tok]
                path = path_to_human([hpath or ".".join(anchor)] + sub + [m.token])
            else:
                sub = [fresh_p()]
                letter, roman, last_key, last_roman = None, None, None, 0
                parts = anchor + [last_p]
                path = None
            out.append(Raw(text=b.text, section=sec, section_title=title,
                           kind="definition" if (in_definitions or in_definitions_body) else "body",
                           id_parts=parts, path=path,
                           has_placeholder=b.has_placeholder, page=b.page, label=b.label))
            continue
        if b.kind == "table_stub":
            parts = anchor + [b.table_key]
            stub_parts[b.table_key] = parts
            out.append(Raw(text=b.text, section=sec, section_title=title, kind="table_stub",
                           id_parts=parts, page=b.page, label=b.label))
            continue
        if b.kind == "table_row":
            parent = stub_parts.get(b.table_key, anchor + [b.table_key])
            out.append(Raw(text=b.text, section=sec, section_title=title, kind="table_row",
                           id_parts=parent + [f"r{b.extra.get('row', 0)}"], spoken_on_request=True,
                           parent_parts=parent, page=b.page, label=b.label))
            continue
        # boilerplate: keeps its place in reading order, never sent
        b_n += 1
        out.append(Raw(text=b.text, section=sec, section_title=title, kind="boilerplate",
                       id_parts=anchor + [f"b{b_n}"], page=b.page, demoted_by=b.demoted_by,
                       label=b.label, has_placeholder=b.has_placeholder))

    # Children: every spoken item directly under a heading -- its body and
    # definition clauses, its table stubs, and its immediate sub-headings.
    hstack: list[Raw] = []
    for r in out:
        if r.kind == "heading":
            while hstack and hstack[-1].level >= r.level:
                hstack.pop()
            if hstack:
                hstack[-1].children += 1
            r.children = 0
            hstack.append(r)
        elif r.kind in ("body", "definition", "table_stub") and hstack:
            hstack[-1].children += 1
    # A heading with nothing under it heads nothing. It is almost always a
    # list item or a short paragraph the layout model labelled as a heading
    # ("Civil Commotion;"), so it is read as body text, never dropped.
    for r in out:
        if r.kind == "heading" and r.children == 0:
            r.kind = "body"
            r.level = 0
            notes.append(f"childless heading read as body: {r.section_title[:60]!r}")
    # Signposts: mechanical, from the tree. "{heading}. {n} items."
    for r in out:
        if r.kind == "heading":
            n = r.children
            r.text = f"{r.section_title}. {n} item{'' if n == 1 else 's'}."
    return out, notes


def split_structured(raws: list[Raw], opts, notes: list[str]) -> list[Raw]:
    """Rule 3 only (no runt merge: the layout model's paragraphs stand). Pieces
    are suffixed -s1, -s2 ... and every split is logged for review."""
    out: list[Raw] = []
    for r in raws:
        if r.kind in ("heading", "table_stub", "boilerplate"):
            out.append(r)
            continue
        pieces = split_long(r.text, opts.max_clause_chars)
        if len(pieces) == 1 and len(r.text) <= MAX_CLAUSE_CHARS_HARD:
            out.append(r)
            continue
        cid = path_to_id(r.id_parts, prefix=opts.id_prefix)
        # A single sentence longer than the hard limit has no sentence boundary
        # to split at: break it at the last word boundary before the limit.
        fixed = []
        for piece in pieces:
            while len(piece) > MAX_CLAUSE_CHARS_HARD:
                cut = piece.rfind(", ", 0, MAX_CLAUSE_CHARS_HARD)
                if cut < MAX_CLAUSE_CHARS_HARD // 2:
                    cut = piece.rfind(" ", 0, MAX_CLAUSE_CHARS_HARD)
                head, piece = piece[:cut + 1].strip(), piece[cut + 1:].strip()
                fixed.append(head)
                notes.append(f"hard split {cid}: a {len(head) + len(piece)}-char sentence with no "
                             f"sentence boundary, cut at a word boundary")
            fixed.append(piece)
        pieces = [x for x in fixed if x]
        notes.append(f"split {cid}: {len(r.text)} chars -> {len(pieces)} pieces "
                     f"({', '.join(str(len(p)) for p in pieces)})")
        for n, piece in enumerate(pieces, start=1):
            out.append(Raw(text=piece, section=r.section, section_title=r.section_title,
                           subsection=r.subsection, item=r.item,
                           path=(r.path + f"-s{n}") if r.path else None, kind=r.kind,
                           id_parts=list(r.id_parts) + [f"s{n}"], spoken_on_request=r.spoken_on_request,
                           has_placeholder=r.has_placeholder, parent_parts=r.parent_parts,
                           page=r.page, label=r.label))
    return out


def fold_lists(raws: list[Raw], max_chars: int, notes: list[str]) -> list[Raw]:
    """Rule 5 for the structure pass: a run of marker-led items under one
    heading folds into one clause (with its introducer, when the paragraph
    before ends in a colon) up to `max_chars`, then a new clause begins with
    the next item. The layout model yields one clause per list item, which
    put a 12k-word wording at ~700 clauses; a listener hears a list as one
    unit. Ids: the first item's. Markers stay in the text."""
    out: list[Raw] = []
    buf: Optional[Raw] = None
    folded = 0

    def led(r: Raw) -> bool:
        m = parse_marker(r.text)
        return bool(m and m.token and not m.keyword)

    def flush():
        nonlocal buf
        if buf is not None:
            out.append(buf)
            buf = None

    for r in raws:
        if r.kind not in ("body", "definition"):
            flush()
            out.append(r)
            continue
        if buf is not None and r.kind == buf.kind and r.section == buf.section and led(r) \
                and len(buf.text) + 1 + len(r.text) <= max_chars:
            buf.text = buf.text + " " + r.text
            buf.has_placeholder = buf.has_placeholder or r.has_placeholder
            folded += 1
            continue
        flush()
        if led(r) or r.text.rstrip().endswith(":"):
            buf = Raw(**{**r.__dict__})
        else:
            out.append(r)
    flush()
    if folded:
        notes.append(f"folded {folded} list items into their runs (max {max_chars} chars)")
    return out


def merge_runts(raws: list[Raw], min_chars: int, notes: list[str]) -> list[Raw]:
    """Rule 4: a body clause shorter than `min_chars` is carried into the next
    body clause of the same heading, which keeps its own id."""
    out: list[Raw] = []
    carry: Optional[Raw] = None
    merged = 0
    for r in raws:
        if carry is not None:
            if r.kind in ("body", "definition") and r.section == carry.section:
                r = Raw(**{**r.__dict__})
                r.text = (carry.text.rstrip() + " " + r.text).strip()
                r.has_placeholder = r.has_placeholder or carry.has_placeholder
                merged += 1
            else:
                out.append(carry)
            carry = None
        if r.kind in ("body", "definition") and len(r.text) < min_chars:
            carry = r
            continue
        out.append(r)
    if carry is not None:
        out.append(carry)
    if merged:
        notes.append(f"merged {merged} short clauses (< {min_chars} chars) into the next")
    return out


def build_map(records: list[dict]) -> list[dict]:
    """Ordered top-level headings with direct body child counts."""
    heads = [r for r in records if r.get("kind") == "heading"]
    if not heads:
        return []
    top = min(r.get("level", 1) or 1 for r in heads)
    tops = [r for r in heads if (r.get("level", 1) or 1) == top]
    # The document title is the first heading, unnumbered, followed by the
    # numbered divisions; it is spoken as a heading but is not a section.
    if len(tops) > 1 and tops[0] is heads[0] and not tops[0].get("path") and any(t.get("path") for t in tops[1:]):
        tops = tops[1:]
    return [{"id": r["id"], "title": r["section_title"], "section": r["section"],
             "children": r.get("children", 0)} for r in tops]


def normalised_text_hash(text: str) -> str:
    """sha256 of the text with case, whitespace and punctuation removed: the
    same document saved twice (a re-export, a different line wrap) hashes
    the same, so an upload can be told it is already here."""
    norm = re.sub(r"[^a-z0-9]+", "", (text or "").lower())
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


def text_fingerprint(path: Path) -> Optional[str]:
    """normalised_text_hash of a file's text by the light extractors (no
    layout model), the same at ingest and at upload. None when the text
    cannot be read cheaply."""
    p = Path(path)
    suf = p.suffix.lower()
    try:
        if suf in (".txt", ".md", ".markdown"):
            return normalised_text_hash(p.read_text(encoding="utf-8", errors="replace"))
        if suf == ".docx":
            import docx
            d = docx.Document(str(p))
            parts = [para.text for para in d.paragraphs]
            for t in d.tables:
                for row in t.rows:
                    parts.extend(c.text for c in row.cells)
            return normalised_text_hash("\n".join(parts))
        if suf in (".html", ".htm"):
            return normalised_text_hash(re.sub(r"<[^>]+>", " ", p.read_text(encoding="utf-8", errors="replace")))
        if suf == ".pdf":
            try:
                from pypdf import PdfReader
            except ImportError:
                return None
            return normalised_text_hash("\n".join((pg.extract_text() or "") for pg in PdfReader(str(p)).pages))
    except Exception:
        return None
    return None


# ---- the fixture builder's preamble ---------------------------------------
# Every fixtures/source/fixture_*.docx opens with the builder's own block: a
# paragraph "Fixture document for the delivery-aware reader ...", a provenance
# table (Document type / Issuer / source / UIN / reference / Source pages /
# Word count (body) / Section headings / Retrieved / Source URL) and a
# paragraph "Provenance and handling note ...". None of it is the document.
PREAMBLE_INTRO = "Fixture document for the delivery-aware reader"
PREAMBLE_NOTE = "Provenance and handling note"
PREAMBLE_LABELS = ("Document type", "Issuer / source", "UIN / reference", "Source pages",
                   "Word count (body)", "Section headings", "Retrieved", "Source URL")


def strip_fixture_preamble(sblocks: list) -> tuple:
    """Detect the fixture builder's preamble by its exact markers and take it
    out of the block stream. Returns (blocks, provenance|None): the intro, the
    table's rows as {label: value} and the handling note, for the fixture's
    top-level "provenance" and the ingest report -- never clauses."""
    texts = [(b.text or "") for b in sblocks]
    start = next((i for i, t in enumerate(texts[:12]) if t.strip().startswith(PREAMBLE_INTRO)), None)
    if start is None:
        return sblocks, None
    end = next((i for i in range(start, min(len(texts), start + 40)) if PREAMBLE_NOTE in texts[i]), None)
    if end is None:
        end = start
        for i in range(start + 1, min(len(texts), start + 40)):
            if any(texts[i].lstrip().startswith(lab) for lab in PREAMBLE_LABELS) or sblocks[i].kind in ("table_stub", "table_row"):
                end = i
            elif sblocks[i].kind == "heading":
                break
    rows: dict = {}
    for b in sblocks[start:end + 1]:
        t = (b.text or "").strip()
        for lab in PREAMBLE_LABELS:
            if t.startswith(lab) and ":" in t:
                rows[lab] = t.split(":", 1)[1].strip().rstrip(".").strip()
                break
    note_text = texts[end].strip() if PREAMBLE_NOTE in texts[end] else ""
    if PREAMBLE_NOTE in note_text and not note_text.startswith(PREAMBLE_NOTE):
        note_text = note_text[note_text.index(PREAMBLE_NOTE):]
    provenance = {"intro": texts[start].strip(), "rows": rows, "note": note_text,
                  "blocks_dropped": end - start + 1}
    return sblocks[:start] + sblocks[end + 1:], provenance


def spoken_title_for(sblocks, doc_title: str, path: Optional[Path] = None) -> str:
    """What the voice will call the document: the file's own title property
    (docx), else the first real heading the structure pass found, else the
    title cleaned of underscores, UINs and fixture tags."""
    from library import clean_title
    if path is not None and Path(path).suffix.lower() == ".docx":
        try:
            import docx
            t = (docx.Document(str(path)).core_properties.title or "").strip()
            if 2 <= len(t.split()) <= 14:
                return clean_title(t)
        except Exception:
            pass
    for b in sblocks or []:
        if b.kind != "heading":
            continue
        words = b.text.split()
        if 2 <= len(words) <= 14 and re.search(r"[A-Za-z]{3,}", b.text) and not _NO_ALNUM.match(b.text):
            return clean_title(b.text)
    return clean_title(doc_title)


def carry_enrichment(old: dict, doc: dict, notes: list) -> None:
    """A re-ingest keeps what the model generated where it still fits: a
    section's brief and questions by title (a question only if its clause
    is still in that section), a clause's tags and spoken override where
    its text is unchanged, and the overview, topics and enrichment block
    only when the section list is the same. Anything else regenerates on
    the next open."""
    if not old.get("enrichment"):
        return
    try:
        import enrich as _en
        spans = _en.section_spans(doc)
    except Exception as e:                       # pragma: no cover - the enrich module is optional here
        notes.append(f"enrichment not carried over: {e}")
        return
    old_secs = {s.get("title"): s for s in old.get("sections") or []}
    kept = []
    for sp in spans:
        os_ = old_secs.get(sp["title"])
        if not os_:
            continue
        sec = {"id": sp["id"], "title": sp["title"]}
        for k in ("brief", "est_minutes"):
            if k in os_:
                sec[k] = os_[k]
        body_ids = {c["id"] for c in sp["body"]}
        qs = [q for q in os_.get("suggested_questions") or [] if q.get("clause_id") in body_ids]
        if qs:
            sec["suggested_questions"] = qs
        kept.append(sec)
    if kept:
        doc["sections"] = kept
    old_by_id = {c["id"]: c for c in old.get("clauses") or []}
    n_tags = 0
    for c in doc["clauses"]:
        oc = old_by_id.get(c["id"])
        if oc and oc.get("text_display") == c.get("text_display"):
            for k in ("tags", "spoken_override", "read_inline"):
                if k in oc:
                    c[k] = oc[k]
            n_tags += "tags" in oc
    olds = old.get("sections") or []
    same = [(s.get("id"), s.get("title")) for s in olds] == [(sp["id"], sp["title"]) for sp in spans]
    if same:
        for k in ("overview", "topics", "enrichment"):
            if k in old:
                doc[k] = old[k]
        notes.append("enrichment carried over: the section list is unchanged")
    else:
        notes.append(f"enrichment partly carried: {len(kept)}/{len(spans)} section briefs and {n_tags} "
                     "clause tags kept; overview and topics regenerate on the next open")


def build_terms(records: list[dict]) -> dict:
    """normalised term -> clause id, from definition clauses and definitions-table rows."""
    from grounding import normalise_term
    terms: dict = {}
    for r in records:
        kind = r.get("kind")
        if kind == "definition":
            m = _TERM_LEAD.match(r["text_display"])
            if m:
                terms.setdefault(normalise_term(m.group(1)), r["id"])
        elif kind == "table_row" and _DEFINITIONS_HEADING.search(r.get("section_title", "")):
            # "Term: Meaning." (two-column grid) or "Term: X; Meaning: Y." (headed)
            first = r["text_display"].split(";", 1)[0]
            if ":" in first:
                k_, v_ = first.split(":", 1)
                term = v_ if re.fullmatch(r"\s*(term|word|expression|definition\s+of)\s*", k_, re.I) else k_
                terms.setdefault(normalise_term(term), r["id"])
    return dict(sorted(terms.items()))


# ==========================================================================
# safety + validation
# ==========================================================================

_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]{2,}\b")
_PHONE = re.compile(r"(?<!\d)(?:\+\d{1,3}[\s.-]?)?(?:\(\d{3}\)|\d{3})[\s.-]\d{3}[\s.-]\d{4}(?!\d)")
_ACCOUNT_NEAR_NAME = re.compile(r"\b([A-Z][a-z]{2,}\s+[A-Z][a-z]{2,})\b[^.\n]{0,40}?\b(\d[\d-]{8,})\b")
_STREET = re.compile(
    r"\b\d{1,5}\s+(?:[A-Z][a-z]+\s+){1,3}(?:Street|St|Road|Rd|Avenue|Ave|Lane|Ln|Drive|Dr|Boulevard|Blvd|Court|Ct|Way)\b\.?",
    re.I)

# --- personal vs institutional ---------------------------------------------
# A public policy wording carries contact details by regulation: the insurer's
# customer-care mailbox, the IRDAI grievance address, a toll-free helpline. Those
# are not personal data, and refusing every document that has them refuses every
# Indian policy document. A person's own email, mobile or address still is.
_ROLE_LOCALS = (
    "info", "care", "customer", "customercare", "support", "grievance", "complaints",
    "claims", "service", "services", "helpdesk", "help", "contact", "enquiry",
    "enquiries", "feedback", "nodal", "ombudsman", "rgicl", "bima", "irdai", "noreply",
    "no-reply", "legal", "compliance", "sales",
)
_ROLE_SUFFIX = re.compile(r"^[a-z]+\.(care|support|grievance|claims|service)$")
_INSTITUTIONAL_DOMAINS = ("irdai.gov.in", "bimabharosa.irdai.gov.in", "cioins.co.in",
                          "rbi.org.in", "sebi.gov.in", "npci.org.in")
_INSTITUTIONAL_SUFFIXES = (".gov.in", ".nic.in")
_PRIVATE_DOMAINS = ("gmail.", "googlemail.", "yahoo.", "ymail.", "outlook.", "hotmail.",
                    "live.", "rediffmail.", "rediff.", "proton.", "protonmail.", "icloud.")
_HELPLINE = re.compile(r"helpline|toll[\s-]?free|customer\s+care|grievance|call\s+cent(?:re|er)|contact\s+us", re.I)
_TOLLFREE = re.compile(r"(?<!\d)(?:1800|1860)[\s.-]?\d{2,4}[\s.-]?\d{2,4}(?:[\s.-]?\d{1,4})?(?!\d)")
_SHORTCODE = re.compile(r"(?<!\d)1\d{4,5}(?!\d)")            # 155255-style helpline codes
_IN_MOBILE = re.compile(r"(?<!\d)(?:\+91[\s-]?|0)?[6-9]\d{9}(?!\d)")
_NAME = re.compile(r"\b([A-Z][a-z]{2,})\s+([A-Z][a-z]{2,})\b")
_YEAR_RANGE = re.compile(r"(?:19|20)\d{2}-(?:19|20)\d{2}")
# Capitalised words that look like a name and never are, in a policy document.
_NOT_NAMES = {
    "grievance", "redressal", "officer", "customer", "care", "insurance", "company",
    "limited", "policy", "toll", "free", "email", "contact", "please", "write", "call",
    "visit", "nodal", "ombudsman", "council", "executive", "insurers", "office", "head",
    "branch", "chief", "manager", "department", "life", "general", "health", "registered",
    "corporate", "address", "helpline", "centre", "center", "service", "services",
    "claims", "claim", "phone", "telephone", "mobile", "number", "regulatory",
    "development", "authority", "india", "bharosa", "bima", "lokpal", "website", "senior",
    "citizen", "citizens", "portal", "online", "assistance", "desk", "help", "support",
    "sales", "team", "unit", "cell", "escalation", "level", "matrix", "reach",
    "financial", "year", "calling", "calendar", "policy", "period", "effective", "date",
    "interest", "rate", "sum", "assured", "premium", "amount",
}
_PUBLIC_SUFFIXES = ("co.in", "gov.in", "org.in", "nic.in", "net.in", "ac.in", "co.uk",
                    "org.uk", "gov.uk", "com.au", "co.nz")


@dataclass
class PIIReport:
    """What the scan found, split by whether it is somebody's personal data.

    `personal` is what refuses the document. `institutional` is reported as a
    warning and recorded in the fixture's source block so the review trail
    keeps it, but it never blocks: a regulator's mailbox is not a person.
    """
    personal: list[tuple[str, str]] = field(default_factory=list)
    institutional: list[tuple[str, str]] = field(default_factory=list)

    def __bool__(self) -> bool:            # truthy == refuse
        return bool(self.personal)

    @property
    def all(self) -> list[tuple[str, str]]:
        return self.personal + self.institutional

    @property
    def unoverridable(self) -> bool:
        return any(label == "name_with_account_number" for label, _ in self.personal)

    def as_dict(self) -> dict:
        return {"personal": [{"label": l, "redacted": redact(v)} for l, v in self.personal],
                "institutional": [{"label": l, "redacted": redact(v)} for l, v in self.institutional]}


def _registrable(domain: str) -> str:
    labels = domain.lower().split(".")
    for suf in _PUBLIC_SUFFIXES:
        n = len(suf.split("."))
        if len(labels) > n and ".".join(labels[-n:]) == suf:
            return labels[-n - 1]
    return labels[-2] if len(labels) >= 2 else labels[0]


def _letters(s: str) -> str:
    return re.sub(r"[^a-z]", "", s.lower())


def _name_adjacent(text: str, start: int, end: int, window: int = 40) -> bool:
    around = text[max(0, start - window):start] + " " + text[end:end + window]
    for m in _NAME.finditer(around):
        if m.group(1).lower() in _NOT_NAMES or m.group(2).lower() in _NOT_NAMES:
            continue
        return True
    return False


def _near_helpline(text: str, start: int, end: int, window: int = 40) -> bool:
    return bool(_HELPLINE.search(text[max(0, start - window):end + window]))


def scan_pii(text: str, context: str = "") -> PIIReport:
    """`context` is the document's title, header and publisher: an address at
    the insurer's own domain is institutional when that name is on the cover."""
    rep = PIIReport()
    # The header context must not contain the addresses themselves, or every
    # domain would "appear on the cover" by virtue of the email being there.
    ctx = _letters(_EMAIL.sub(" ", context)) + _letters(_EMAIL.sub(" ", text[:1500]))

    emails = list(_EMAIL.finditer(text))
    counts: dict[str, int] = {}
    for m in emails:
        counts[m.group(0).lower()] = counts.get(m.group(0).lower(), 0) + 1
    seen: set[str] = set()
    for m in emails:
        addr = m.group(0)
        key = addr.lower()
        if key in seen:
            continue
        seen.add(key)
        local, _, domain = key.partition("@")
        private = any(domain.startswith(pd) for pd in _PRIVATE_DOMAINS)
        # A role anywhere in the local part: "bhflcustomerservice@", "rgicl.care@",
        # "bimalokpal@" are company-prefixed role mailboxes, not people.
        role = (local in _ROLE_LOCALS or bool(_ROLE_SUFFIX.match(local))
                or any(r in local and len(r) >= 4 for r in _ROLE_LOCALS))
        inst_domain = (domain in _INSTITUTIONAL_DOMAINS
                       or any(domain.endswith(suf) for suf in _INSTITUTIONAL_SUFFIXES)
                       or (len(_registrable(domain)) >= 4 and _registrable(domain) in ctx))
        boilerplate = counts[key] >= 3
        if not private and (role or inst_domain or boilerplate):
            rep.institutional.append(("email", addr))
        else:
            rep.personal.append(("email", addr))

    phones: list[tuple[int, int, str, str]] = []
    for m in _TOLLFREE.finditer(text):
        phones.append((m.start(), m.end(), m.group(0), "tollfree"))
    for m in _SHORTCODE.finditer(text):
        if _near_helpline(text, m.start(), m.end()):
            phones.append((m.start(), m.end(), m.group(0), "shortcode"))
    for m in _IN_MOBILE.finditer(text):
        phones.append((m.start(), m.end(), m.group(0), "mobile"))
    for m in _PHONE.finditer(text):
        phones.append((m.start(), m.end(), m.group(0), "generic"))
    taken: list[tuple[int, int]] = []
    for start, end, val, kind in sorted(phones):
        if any(a <= start < b or a < end <= b for a, b in taken):
            continue
        taken.append((start, end))
        if kind in ("tollfree", "shortcode") or _near_helpline(text, start, end):
            rep.institutional.append(("phone", val))
        elif kind == "mobile" or _name_adjacent(text, start, end):
            rep.personal.append(("phone", val))
        else:
            rep.institutional.append(("phone", val))

    for m in _STREET.finditer(text):
        if _name_adjacent(text, m.start(), m.end()):
            rep.personal.append(("street_address", m.group(0)))
        else:
            rep.institutional.append(("street_address", m.group(0)))

    for m in _ACCOUNT_NEAR_NAME.finditer(text):
        if m.group(1).split()[0].lower() in _NOT_NAMES or m.group(1).split()[1].lower() in _NOT_NAMES:
            continue
        num = m.group(2)
        if _YEAR_RANGE.fullmatch(num) or _TOLLFREE.fullmatch(num) or _TOLLFREE.match(num):
            continue                       # "Financial Year 2020-2021", "Toll Free 1800-4254-732"
        rep.personal.append(("name_with_account_number", f"{m.group(1)} … {num}"))
    return rep


def redact(s: str) -> str:
    s = s.strip()
    return (s[:4] + "…") if len(s) > 4 else "…"


class IngestError(RuntimeError):
    """An internal invariant failed. Never raised for the document's content."""


def validate(records: list[dict], opts, min_clauses: int = 0) -> None:
    """Internal invariants only: ids, index order, span coverage, the 1,000
    character Rime limit. A document's content never fails validation; what it
    is like goes in the ingest report."""
    n = len(records)
    if min_clauses and n < min_clauses:
        raise IngestError(f"only {n} clauses; need at least {min_clauses}")

    for r in records:
        if len(r["text_display"]) > MAX_CLAUSE_CHARS_HARD:
            raise IngestError(f"{r['id']}: {len(r['text_display'])} chars exceeds the hard limit of "
                              f"{MAX_CLAUSE_CHARS_HARD}; the splitter must break it at a sentence boundary")
    ids = [r["id"] for r in records]
    if len(ids) != len(set(ids)):
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        raise IngestError(f"duplicate clause ids after de-duplication: {dupes[:5]}")
    for i, r in enumerate(records):
        if r["index"] != i:
            raise IngestError(f"index not contiguous at {r['id']}: {r['index']} != {i}")

        text = r["text_display"]
        covered: set[int] = set()
        for a, b, _ in r["spoken_map"]:
            covered.update(range(a, b))
        for j, ch in enumerate(text):
            if not ch.isspace() and j not in covered:
                raise IngestError(f"{r['id']}: spoken_map does not cover char {j} {ch!r}")

        spans = r["sentences"]
        if not spans or spans[0][0] != 0 or spans[-1][1] != len(text):
            raise IngestError(f"{r['id']}: sentence spans do not cover text_display (got {spans[:2]}…{spans[-1:]}, len {len(text)})")
        for (a1, b1), (a2, _) in zip(spans, spans[1:]):
            if a2 < b1:
                raise IngestError(f"{r['id']}: sentence spans overlap at {b1}/{a2}")


# ==========================================================================
# main
# ==========================================================================

def preview(records: list[dict]) -> None:
    for r in records:
        head = r["text_display"][:100].replace("\n", " ")
        kind = f" <{r['kind']}>" if "kind" in r else ""
        print(f"[{r['id']}]{kind} {r['section_title']} | {head}...")
    print(f"\n{len(records)} clauses, "
          f"{len({r['section_title'] for r in records})} sections, "
          f"{sum(len(r['text_display']) for r in records)} display chars")


EXPECTED_RANGE = (150, 250)         # clauses for a ~20-page / ~12k-word wording
EXPECTED_RANGE_WORDS = 12000


@dataclass
class IngestResult:
    doc_id: str
    name: str
    title: str
    fixture_path: Optional[Path]
    report_path: Optional[Path]
    docling_path: Optional[Path]
    clause_count: int
    readable: bool
    entry: dict
    report: dict
    records: list


def _doc_id_for(data: bytes) -> str:
    """Content hash of the source bytes: the same PDF gives the same id on
    every machine and every run, and a re-upload is recognised as the same
    document."""
    return hashlib.sha256(data).hexdigest()[:16]


def _slug(s: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in s)[:60].strip("-") or "document"


def ingest_document(source, out_dir=None, *, out_path=None, name=None, title=None, id_prefix="sec",
                    min_clause_chars=None, max_clause_chars=None, fold_max_chars=600,
                    synthetic=False, referral=None, ocr=False, source_kind="auto",
                    progress=None, register=True, index_path=None, dry_run=False,
                    legacy_segmenter=False) -> IngestResult:
    """The one ingestion function. Build-time `scripts/ingest.py` and the
    runtime upload both call this and get the same bytes for the same source.

    Every stage always runs -- extract, structure, segment, normalize,
    pii_scan, validate, write -- and `progress(stage, status, elapsed_ms,
    detail)` is called as each ends. Nothing in the scan or the validation
    report blocks the document. The two silent invariants: no clause over
    the 1,000-character Rime limit (the splitter guarantees it; asserted
    after segmentation), and a document with zero body clauses is written
    with `readable: False` rather than raising.
    """
    timings: dict = {}
    notes: list[str] = []
    warnings: list[str] = []
    t_all = time.monotonic()

    def stage(name_, status="ok", detail="", t0=None, **extra):
        """`progress(stage, status, ms, detail, extra)`: `extra` carries what the
        companion may say aloud -- page count, top-level headings -- never text."""
        ms = round((time.monotonic() - (t0 if t0 is not None else t_all)) * 1000, 1)
        timings[name_] = ms
        if progress:
            progress(name_, status, ms, detail, extra or None)

    # ---- extract ---------------------------------------------------------
    t0 = time.monotonic()
    src = str(source)
    is_url = src.lower().startswith(("http://", "https://"))
    meta: dict = {}
    sblocks = None
    ddoc = None
    rep_s = None
    import ingest_structure as istr
    if is_url:
        blocks, raw, meta = extract_html(src, True)
        stype = "url"
        data = raw.encode("utf-8")
    else:
        p = Path(src)
        if not p.exists():
            raise IngestError(f"no such file: {p}")
        data = p.read_bytes()
        suf = p.suffix.lower()
        use_docling = (source_kind == "pdf" or (source_kind == "auto" and suf in (".pdf", ".docx"))) \
            and istr.docling_available()
        if suf == ".pdf" and not use_docling and source_kind != "text":
            if not istr.docling_available():
                raise IngestError("PDF ingestion needs docling: pip install -r requirements-build.txt")
        if use_docling:
            ddoc = istr.convert(p, ocr=ocr)
            stype = "pdf" if suf == ".pdf" else "docx"
            blocks, raw = [], ""
        elif suf == ".docx":
            blocks, raw = extract_docx(p)
            stype = "docx"
        elif suf in (".html", ".htm"):
            blocks, raw, meta = extract_html(str(p), False)
            stype = "html"
        elif suf in (".txt", ".md", ".markdown"):
            blocks, raw = extract_txt(p)
            stype = "text"
        else:
            raise IngestError(f"unsupported extension {suf!r}; use .pdf .docx .html .txt .md or a URL")
    doc_id = _doc_id_for(data)
    doc_key = name or (Path(out_path).stem if out_path else doc_id)
    stage("extract", detail=f"{stype} {len(data)} bytes", t0=t0,
          pages=(istr.num_pages(ddoc) if ddoc is not None else 0))

    # ---- structure -------------------------------------------------------
    t0 = time.monotonic()
    rep_s = istr.StructureReport()
    out_dir = Path(out_dir) if out_dir else (Path(out_path).parent if out_path else None)
    csv_dir = out_dir if (out_dir and not dry_run) else None
    if ddoc is not None:
        sblocks = istr.blocks_from_docling(ddoc, table_csv_dir=csv_dir, doc_key=doc_key, report=rep_s)
        n_pages = istr.num_pages(ddoc)
    else:
        sblocks = istr.blocks_from_legacy(blocks)
        n_pages = 0
    sblocks = istr.regex_boilerplate_pass(sblocks, n_pages, rep_s)
    sblocks, provenance = strip_fixture_preamble(sblocks)
    if provenance:
        notes.append(f"fixture preamble dropped: {provenance['blocks_dropped']} blocks, {len(provenance['rows'])} provenance rows")
    if ddoc is not None:
        raw = "\n".join(b.text for b in sblocks)
    if len(raw.strip()) < MIN_EXTRACT_CHARS:
        warnings.append(f"extracted only {len(raw.strip())} chars; scanned images, JS-rendered, or empty")
    _heads = [b.text for b in sblocks if b.kind == "heading" and b.level <= 1] or \
             [b.text for b in sblocks if b.kind == "heading"]
    stage("structure", detail=f"{len(sblocks)} blocks, {n_pages} pages", t0=t0,
          pages=n_pages, headings=_heads[:40], n_sections=len(_heads))

    # ---- segment (fold, merge, split; the 1,000-char assert) --------------
    t0 = time.monotonic()
    class Opts:
        pass
    opts = Opts()
    opts.id_prefix = id_prefix
    opts.max_clause_chars = max_clause_chars if max_clause_chars is not None else MAX_CLAUSE_CHARS_HARD
    opts.min_clause_chars = min_clause_chars if min_clause_chars is not None else 120
    if legacy_segmenter and ddoc is None:
        opts.max_clause_chars = max_clause_chars if max_clause_chars is not None else 600
        opts.min_clause_chars = min_clause_chars if min_clause_chars is not None else 40
        raws = segment(blocks, opts)
        raws = merge_and_split(raws, opts)
    else:
        raws, notes = segment_structured(sblocks, opts)
        if fold_max_chars:
            raws = fold_lists(raws, fold_max_chars, notes)
        if opts.min_clause_chars:
            raws = merge_runts(raws, opts.min_clause_chars, notes)
        raws = split_structured(raws, opts, notes)
    for r in raws:
        assert len(r.text) <= MAX_CLAUSE_CHARS_HARD, f"segmenter left {len(r.text)} chars in one clause"
    splits = [n for n in notes if n.startswith("split ") or n.startswith("hard split ")]
    stage("segment", detail=f"{len(raws)} clauses, {len(splits)} splits", t0=t0)

    # ---- normalize -------------------------------------------------------
    t0 = time.monotonic()
    records = to_records(raws, opts)
    stage("normalize", detail=f"{len(records)} spoken forms", t0=t0)

    # ---- pii_scan: informational, never blocks ---------------------------
    t0 = time.monotonic()
    pii = scan_pii(raw, context=" ".join([title or doc_key, str(meta.get("publisher", "")),
                                          str(meta.get("title", ""))]))

    def locate(value: str) -> Optional[str]:
        for r in records:
            if value in r["text_display"]:
                return r["id"]
        return None
    pii_report = {
        "personal": [{"label": l, "redacted": redact(v), "clause_id": locate(v)} for l, v in pii.personal],
        "institutional": [{"label": l, "redacted": redact(v), "clause_id": locate(v)} for l, v in pii.institutional],
        "note": "Informational. Nothing here removed or altered any text; the developer view "
                "shows it as the review trail.",
    }
    stage("pii_scan", detail=f"{len(pii.personal)} personal-looking, {len(pii.institutional)} institutional", t0=t0)

    # ---- validate: informational report + the internal invariants --------
    t0 = time.monotonic()
    validate(records, opts)
    by_kind: dict = {}
    for r in records:
        by_kind[r.get("kind", "body")] = by_kind.get(r.get("kind", "body"), 0) + 1
    body_n = by_kind.get("body", 0) + by_kind.get("definition", 0)
    readable = body_n > 0
    words = sum(len(r["text_display"].split()) for r in records)
    scaled = (EXPECTED_RANGE[0] * words / EXPECTED_RANGE_WORDS, EXPECTED_RANGE[1] * words / EXPECTED_RANGE_WORDS)
    in_range = scaled[0] <= len(records) <= scaled[1] * 1.5
    boiler = [{"id": r["id"], "text": r["text_display"][:120],
               "reason": r.get("demoted_by") or r.get("label") or "layout"}
              for r in records if r.get("kind") == "boilerplate"][:15]
    validate_report = {
        "clause_count": len(records),
        "by_kind": dict(sorted(by_kind.items())),
        "body_clauses": body_n,
        "readable": readable,
        "splits": splits,
        "folds_and_merges": [n for n in notes if n.startswith(("folded", "merged"))],
        "oversized_ok": all(len(r["text_display"]) <= MAX_CLAUSE_CHARS_HARD for r in records),
        "max_clause_chars": max((len(r["text_display"]) for r in records), default=0),
        "boilerplate_first_15": boiler,
        "expected_range": {
            "reference": f"{EXPECTED_RANGE[0]}-{EXPECTED_RANGE[1]} clauses for a ~{EXPECTED_RANGE_WORDS:,}-word wording",
            "words": words,
            "scaled_range": [round(scaled[0]), round(scaled[1])],
            "in_range": in_range,
        },
        "other_notes": [n for n in notes if not n.startswith(("split ", "hard split ", "folded", "merged"))],
        "unknown_labels": dict(rep_s.unknown_labels) if rep_s else {},
        "warnings": warnings,
    }
    stage("validate", detail=f"{len(records)} clauses, {len(by_kind)} kinds, readable={readable}", t0=t0)

    # ---- write -----------------------------------------------------------
    t0 = time.monotonic()
    source_block: dict = {
        "type": stype,
        "path_or_url": src if is_url else str(Path(src).name),
        "sha256": hashlib.sha256(data).hexdigest(),
    }
    if is_url:
        source_block["fetched_at"] = meta.get("fetched_at") or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    if pii.institutional:
        source_block["institutional_contacts"] = [{"label": l, "redacted": redact(v)} for l, v in pii.institutional]
    doc_title = title or (urlparse(src).netloc + urlparse(src).path if is_url else Path(src).stem)
    src_stem = Path(src).stem if not is_url else ""
    # A title given on purpose is what the voice says. The filename stem is
    # not one (the upload passes it, and its source is renamed .incoming_*_name).
    explicit = bool(title and title.strip()) and title.strip() != src_stem and not src_stem.endswith(title.strip())
    if explicit:
        from library import clean_title
        spoken_title = clean_title(title)
    else:
        spoken_title = spoken_title_for(sblocks, doc_title, None if is_url else Path(src))
    source_block["text_sha256"] = normalised_text_hash(raw) if is_url else text_fingerprint(Path(src))
    try:
        import docling
        dver = getattr(docling, "__version__", None) or __import__("importlib.metadata").metadata.version("docling")
    except Exception:
        dver = None
    doc = {
        "title": doc_title,
        "spoken_title": spoken_title,
        "provenance": provenance,
        "doc_id": doc_id,
        "synthetic": bool(synthetic),
        "readable": readable,
        "source": source_block,
        "clause_count": len(records),
        "clauses": records,
        "map": build_map(records),
        "terms": build_terms(records),
        "structure": {
            "tool": "docling" if ddoc is not None else "text",
            "version": dver if ddoc is not None else None,
            "docling_json": f"{doc_key}.docling.json" if ddoc is not None else None,
            "pages": n_pages,
            "labels": dict(sorted(rep_s.labels_seen.items())),
            "demoted": len(rep_s.demoted), "placeholders_stripped": rep_s.placeholders_stripped,
            "splits": len(splits),
            "tables": [{"key": k, "rows": n, "csv": Path(c).name if c else None} for k, n, c in rep_s.tables],
        },
    }
    report = {
        "doc_id": doc_id, "name": doc_key, "title": doc_title, "source": source_block,
        "stages": ["extract", "structure", "segment", "normalize", "pii_scan", "validate", "write"],
        "elapsed_ms": timings,
        "structure": doc["structure"],
        "pii_scan": pii_report,
        "validate": validate_report,
        "readable": readable,
        "provenance": provenance,
    }
    fixture_path = report_path = docling_path = None
    entry = {"doc_id": doc_id, "name": doc_key, "title": doc_title, "reviewed": False,
             "readable": readable, "clause_count": len(records)}
    if not dry_run:
        if out_path is None and out_dir is None:
            raise IngestError("out_dir or out_path is required unless dry_run")
        fixture_path = Path(out_path) if out_path else Path(out_dir) / f"{doc_key}.json"
        fixture_path.parent.mkdir(parents=True, exist_ok=True)
        if ddoc is not None:
            docling_path = fixture_path.parent / f"{doc_key}.docling.json"
            istr.save_docling_json(ddoc, docling_path)
        report_path = fixture_path.parent / f"{doc_key}.ingest_report.json"
        doc["report_file"] = report_path.name
        if fixture_path.exists():
            try:
                carry_enrichment(json.loads(fixture_path.read_text(encoding="utf-8")), doc, notes)
            except ValueError:
                pass
            report["validate"]["other_notes"] = [n for n in notes if not n.startswith(("split ", "hard split ", "folded", "merged"))]
        fixture_path.write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        report["elapsed_ms"]["write"] = round((time.monotonic() - t0) * 1000, 1)
        report["elapsed_ms"]["total"] = round((time.monotonic() - t_all) * 1000, 1)
        report_path.write_text(json.dumps(report, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        if register:
            idx = Path(index_path) if index_path else fixture_path.parent / INDEX_FILENAME
            entry_written = update_index(fixture_path, doc, doc_key, referral, index_path=idx)
            entry.update({k: entry_written[k] for k in ("reviewed", "readable", "report") if k in entry_written})
    stage("write", detail=str(fixture_path) if fixture_path else "dry run, nothing written", t0=t0)
    return IngestResult(doc_id=doc_id, name=doc_key, title=doc_title, fixture_path=fixture_path,
                        report_path=report_path, docling_path=docling_path, clause_count=len(records),
                        readable=readable, entry=entry, report=report, records=records)


def print_report(res: IngestResult, stream=sys.stderr) -> None:
    v = res.report["validate"]
    print(f"structure pass: {res.name}  (doc_id {res.doc_id})", file=stream)
    print("  clauses by kind: " + ", ".join(f"{k}={n}" for k, n in v["by_kind"].items()), file=stream)
    print(f"  splits: {len(v['splits'])}", file=stream)
    for n in v["splits"]:
        print(f"    {n}", file=stream)
    for n in v["folds_and_merges"] + v["other_notes"]:
        print(f"  {n}", file=stream)
    print(f"  elapsed: " + ", ".join(f"{k} {ms:.0f} ms" for k, ms in res.report["elapsed_ms"].items()), file=stream)
    e = v["expected_range"]
    print(f"  expected range: {e['reference']}; this document {e['words']} words -> "
          f"{e['scaled_range'][0]}-{e['scaled_range'][1]}; got {v['clause_count']} "
          f"({'in range' if e['in_range'] else 'OUTSIDE range'})", file=stream)
    if v["unknown_labels"]:
        print("  labels mapped to boilerplate by default (review): "
              + ", ".join(f"{k}={n}" for k, n in v["unknown_labels"].items()), file=stream)
    p = res.report["pii_scan"]
    print(f"  pii scan (informational): {len(p['personal'])} personal-looking, "
          f"{len(p['institutional'])} institutional", file=stream)
    print(f"  first {len(v['boilerplate_first_15'])} boilerplate items:", file=stream)
    for b in v["boilerplate_first_15"]:
        print(f"    [{b['id']}] ({b['reason']}) {b['text'][:90]}", file=stream)
    if not v["readable"]:
        print("  readable: false (no body text found)", file=stream)


def main() -> int:
    ap = argparse.ArgumentParser(description="Build-time document -> fixture ingestion (the same "
                                             "function the upload endpoint runs).")
    ap.add_argument("source_path", metavar="source",
                    help="path to .pdf/.docx/.html/.txt/.md, or an http(s) URL")
    ap.add_argument("--out", required=False, help="output fixture path (.json); default fixtures/<doc_id>.json")
    ap.add_argument("--out-dir", default=None, help="directory for <name>.json (default: examples/policy-reader/fixtures)")
    ap.add_argument("--title", default=None)
    ap.add_argument("--id-prefix", default="sec")
    ap.add_argument("--name", default=None,
                    help="registry name (default: --out stem, else the doc_id); must be unique")
    ap.add_argument("--referral", default=None,
                    help="who the listener should contact for a decision; shown on every answer card.")
    ap.add_argument("--min-clause-chars", type=int, default=None,
                    help="merge shorter clauses into the next (default 120 for the structure pass)")
    ap.add_argument("--max-clause-chars", type=int, default=None,
                    help="split threshold; default the 1000-char hard limit for the structure pass")
    ap.add_argument("--fold-lists", type=int, default=600, metavar="CHARS",
                    help="fold runs of list items into clauses up to CHARS (0 disables)")
    ap.add_argument("--synthetic", action="store_true", help="mark the fixture as synthetic")
    ap.add_argument("--dry-run", action="store_true", help="run every stage, print the report, write nothing")
    ap.add_argument("--ocr", action="store_true", help="enable Docling OCR (scanned PDFs; off by default)")
    ap.add_argument("--source", choices=("auto", "pdf", "text"), default="auto",
                    help="pdf: the Docling structure pass; text: the line-based extractors; "
                         "auto: Docling for .pdf/.docx when installed")
    ap.add_argument("--legacy-segmenter", action="store_true",
                    help="text sources only: the numbered/headed segmenters instead of the structure mapping")
    ap.add_argument("--no-register", action="store_true", help="write the fixture but not index.json")
    ap.add_argument("--force", action="store_true",
                    help="re-ingest even when the registered fixture was built from these same bytes")
    args = ap.parse_args()

    out_dir = Path(args.out_dir) if args.out_dir else (ROOT / "examples" / "policy-reader" / "fixtures")
    if not args.force and not args.dry_run and not str(args.source_path).lower().startswith(("http://", "https://")):
        sp = Path(args.source_path)
        idx = (Path(args.out).parent if args.out else out_dir) / INDEX_FILENAME
        if sp.exists() and idx.exists():
            try:
                sha = hashlib.sha256(sp.read_bytes()).hexdigest()
                same = [e for e in json.loads(idx.read_text(encoding="utf-8")).get("documents", [])
                        if (e.get("source") or {}).get("sha256") == sha and (not args.name or e.get("name") == args.name)]
            except ValueError:
                same = []
            if same:
                print(f"{sp.name}: already ingested as {same[0]['name']!r} from these same bytes; "
                      "pass --force to rebuild it", file=sys.stderr)
                return 0
    try:
        res = ingest_document(
            args.source_path, out_dir=None if args.out else out_dir, out_path=args.out, name=args.name,
            title=args.title, id_prefix=args.id_prefix, min_clause_chars=args.min_clause_chars,
            max_clause_chars=args.max_clause_chars, fold_max_chars=args.fold_lists,
            synthetic=args.synthetic, referral=args.referral, ocr=args.ocr, source_kind=args.source,
            progress=lambda st, status, ms, detail, extra=None: print(f"  {st:10s} {status:4s} {ms:8.0f} ms  {detail}", file=sys.stderr),
            register=not args.no_register, dry_run=args.dry_run, legacy_segmenter=args.legacy_segmenter)
    except IngestError as e:
        die(str(e))
    print_report(res)
    if args.dry_run:
        preview(res.records)
        print("\n--dry-run: nothing written.")
    else:
        print(f"wrote {res.fixture_path} — {res.clause_count} clauses; report {res.report_path.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
