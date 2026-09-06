#!/usr/bin/env python3
"""Build-time document ingestion: a file or URL in, a committed fixture out.

BUILD TIME ONLY. Nothing in the agent path calls this. A developer runs it,
eyeballs the segmentation with --review, and commits the resulting JSON.
There is no runtime upload and no runtime URL fetching anywhere in the
delivery layer; this script is the only place a network fetch happens, and
its product is a file in version control.

  python scripts/ingest.py <path-or-url> --out examples/policy-reader/fixtures/<name>.json \
      [--title "..."] [--id-prefix sec] [--min-clause-chars 40] [--max-clause-chars 600] \
      [--synthetic] [--review] [--dry-run] [--allow-pii "reason"]

Output schema is exactly fixtures/policy.json's, plus two optional per-clause
fields (`kind`, `path`). grounding.py / wordmap.py / resume.py / read_demo.py
all read that schema, so it does not change.

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
    AMBIGUOUS_TOKENS,
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

def extract_txt(path: Path) -> tuple[list[Block], str]:
    raw = path.read_text(encoding="utf-8", errors="replace")
    return blocks_from_lines(raw.splitlines(), markdown=path.suffix.lower() in (".md", ".markdown")), raw


def extract_pdf(path: Path) -> tuple[list[Block], str]:
    try:
        from pypdf import PdfReader
    except ImportError:
        try:
            from PyPDF2 import PdfReader  # type: ignore
        except ImportError:
            die("PDF input needs pypdf:  pip install pypdf")
    reader = PdfReader(str(path))
    pages = [(p.extract_text() or "") for p in reader.pages]
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
        # An ALL-CAPS or short unpunctuated standalone line reads as a heading.
        if (not markdown or True) and len(s) < 90 and not s.endswith((".", ";", ":", ",")) \
                and (s.isupper() or (s.istitle() and len(s.split()) <= 10)) and not parse_marker(s):
            flush()
            blocks.append(Block("heading", s, 2))
            continue
        buf.append(s)
    flush()
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
            new = f"{cid}-{seen[cid]}"
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
        records.append(rec)
    return records


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

    def stage(name_, status="ok", detail="", t0=None):
        ms = round((time.monotonic() - (t0 if t0 is not None else t_all)) * 1000, 1)
        timings[name_] = ms
        if progress:
            progress(name_, status, ms, detail)

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
            die(f"unsupported extension {suf!r}; use .pdf .docx .html .txt .md or a URL")

    if len(raw.strip()) < MIN_EXTRACT_CHARS:
        die(f"extracted only {len(raw.strip())} chars (need >= {MIN_EXTRACT_CHARS}). "
            "The document is probably scanned images, JS-rendered, or behind a login.")

    title_hint = args.title or (urlparse(src).netloc if is_url else Path(src).stem)
    pii = scan_pii(raw, context=" ".join([title_hint, str(meta.get("publisher", "")),
                                          str(meta.get("title", ""))]))
    if args.pii_report:
        Path(args.pii_report).write_text(json.dumps(pii.as_dict(), indent=1), encoding="utf-8")
    for label, val in pii.institutional:
        print(f"warning: institutional contact detail kept: {label} {redact(val)}", file=sys.stderr)
    if pii.personal:
        print(f"PII scan found {len(pii.personal)} personal-data hit(s):", file=sys.stderr)
        for label, val in pii.personal[:20]:
            print(f"  {label}: {redact(val)}", file=sys.stderr)
        if pii.unoverridable:
            die("refusing: a person's name next to an account or policy number is personal "
                "data whatever the reason. --allow-pii does not apply.", code=2)
        if not args.allow_pii:
            die("refusing to ingest a document that looks like it contains real personal data. "
                "Use a synthetic or public document, or pass --allow-pii \"reason\" if these are "
                "false positives (the reason is recorded in the fixture).", code=2)
        print(f"--allow-pii given: {args.allow_pii}", file=sys.stderr)

    raws = segment(blocks, args)
    raws = merge_and_split(raws, args)
    records = to_records(raws, args)
    validate(records, args)

    if args.dry_run or args.review:
        preview(records)
    if args.dry_run:
        print("\n--dry-run: nothing written.")
        return 0
    if args.review:
        try:
            input("\nReview the clauses above. Enter to write, Ctrl-C to abort: ")
        except (EOFError, KeyboardInterrupt):
            print("\naborted; nothing written.")
            return 1

    source: dict = {
        "type": stype,
        "path_or_url": src if is_url else str(Path(src).name),
        "fetched_at": meta.get("fetched_at") or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
    }
    if args.allow_pii and pii.personal:
        source["pii_override_reason"] = args.allow_pii
    if pii.institutional:
        # Redacted on purpose: the review trail needs to know the document
        # carries a helpline and a grievance mailbox, not what they are.
        source["institutional_contacts"] = [
            {"label": l, "redacted": redact(v)} for l, v in pii.institutional]

    title = args.title or (urlparse(src).netloc + urlparse(src).path if is_url else Path(src).stem)
    doc = {
        "title": title,
        "synthetic": bool(args.synthetic),
        "source": source,
        "clause_count": len(records),
        "clauses": records,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, indent=1, ensure_ascii=False))
    print(f"wrote {out} — {len(records)} clauses, "
          f"{len({r['section_title'] for r in records})} sections")
    update_index(out, doc, args.name or out.stem, args.referral)
    print("Add an entry to examples/policy-reader/fixtures/README.md before committing.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
