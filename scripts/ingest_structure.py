"""Structure pass for scripts/ingest.py: a Docling layout parse -> typed blocks.

BUILD TIME ONLY, on the developer's machine. Docling (and its layout models)
are a build dependency, never imported by the agent path, never run in CI or
at demo time. The judged flow reads committed fixtures.

One-time setup (models are downloaded to ~/.cache/docling, ~500 MB):

    pip install -r requirements-build.txt
    docling-tools models download

What this module does, deterministically (no LLM anywhere):

  1. `convert()` runs Docling and returns the lossless DoclingDocument. The
     caller saves it next to the fixture as `<doc_key>.docling.json`; that
     file is the reproducibility artifact, and the mapping below can be re-run
     from it without the converter.
  2. `blocks_from_docling()` walks `iterate_items()` and maps layout labels to
     the clause `kind` taxonomy:

       section_header            -> heading      (spoken, the clause-id anchor; signposted)
       text / paragraph          -> body
       list_item                 -> body         (marker kept in display text)
       table                     -> table_stub + one table_row per row (rows spoken on request)
       page_header / page_footer -> boilerplate  (never sent)
       caption / footnote        -> body
       anything else             -> boilerplate  (never sent; label logged for review)

  3. `regex_boilerplate_pass()` demotes what the layout model leaves inside body
     text: page numbers, standalone UIN codes, registered-office / CIN / IRDAI
     registration lines, `<<...>>` placeholder fields (stripped, clause flagged),
     and any line that recurs on >= 30 % of pages.
"""
from __future__ import annotations

import csv
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

# --------------------------------------------------------------------------
# label taxonomy
# --------------------------------------------------------------------------

LABEL_TO_KIND = {
    "section_header": "heading",
    "title": "heading",
    "text": "body",
    "paragraph": "body",
    "list_item": "body",
    "caption": "body",
    "footnote": "body",
    "table": "table",
    "page_header": "boilerplate",
    "page_footer": "boilerplate",
}
DEFAULT_KIND = "boilerplate"          # anything else is never sent; the label is logged

HEADER_FOOTER_PAGE_FRACTION = 0.30    # a line on >= 30 % of pages is furniture
MIN_PAGES_FOR_FRACTION = 3

_PAGE_NO = re.compile(r"^\s*(?:page\s+)?\d{1,4}\s*(?:of\s+\d{1,4})?\s*$", re.I)
_BARE_INT = re.compile(r"^\s*\d{1,4}\s*$")
_UIN = re.compile(r"^\s*UIN[:\s]*[A-Z0-9]{10,}\s*\.?\s*$", re.I)
_REG_LINE = re.compile(
    r"^\s*(?:registered\s+(?:office|address)|regd\.?\s+office|corporate\s+(?:identity|office)"
    r"|CIN\s*[:\-]|IRDAI?\s+reg(?:istration|n)?\.?\s*(?:no|number)?|insurance\s+is\s+the\s+subject"
    r"\s+matter\s+of\s+solicitation)", re.I)
_PLACEHOLDER = re.compile(r"<<[^<>]*>>")
_WS = re.compile(r"\s+")
_MD_EMPHASIS = re.compile(r"\*\*|__|(?<!\w)\*(?=\w)|(?<=\w)\*(?!\w)")   # DOCX bold/italic runs


def _clean(text: str) -> str:
    return _WS.sub(" ", _MD_EMPHASIS.sub("", str(text))).strip()


@dataclass
class SBlock:
    """A structured block: what the layout model said, plus provenance."""
    kind: str                       # heading | body | table_stub | table_row | boilerplate
    text: str
    label: str = ""                 # the Docling label, verbatim
    level: int = 0                  # heading level (1 = top)
    page: int = 0                   # 1-based page of the first provenance, 0 if unknown
    marker: str = ""                # list item marker, e.g. "(a)", "1."
    table_key: str = ""             # table_stub: its key; table_row: the stub it belongs to
    has_placeholder: bool = False
    demoted_by: str = ""            # regex rule that demoted a body block, if any
    extra: dict = field(default_factory=dict)


@dataclass
class StructureReport:
    labels_seen: Counter = field(default_factory=Counter)
    unknown_labels: Counter = field(default_factory=Counter)
    demoted: list = field(default_factory=list)        # (rule, text)
    placeholders_stripped: int = 0
    tables: list = field(default_factory=list)          # (key, rows, csv path)


# --------------------------------------------------------------------------
# conversion
# --------------------------------------------------------------------------

_CONVERTERS: dict = {}


def docling_available() -> bool:
    try:
        import docling  # noqa: F401
        return True
    except Exception:
        return False


def get_converter(ocr: bool = False):
    """One DocumentConverter per OCR setting, built once and reused.

    Building it loads the layout models (seconds), so the server warms it at
    start and every upload after that is not cold. OCR is off by default: the
    fixtures are digital PDFs; `ocr=True` is there for a scanned one.
    """
    key = bool(ocr)
    if key not in _CONVERTERS:
        from docling.document_converter import DocumentConverter, PdfFormatOption
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import PdfPipelineOptions
        opts = PdfPipelineOptions()
        opts.do_ocr = bool(ocr)
        _CONVERTERS[key] = DocumentConverter(
            format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=opts)})
    return _CONVERTERS[key]


def warm(ocr: bool = False) -> bool:
    """Build the converter now (server start). Returns False if docling is absent."""
    if not docling_available():
        return False
    get_converter(ocr)
    return True


def convert(path: Path, ocr: bool = False):
    """Run Docling. Import is local so nothing else in ingest.py needs it."""
    result = get_converter(ocr).convert(str(path))
    return result.document


def blocks_from_legacy(blocks) -> list:
    """The text / HTML / python-docx extractors' Blocks, mapped onto the same
    typed blocks the layout model produces, so every source goes through one
    downstream mapping. A run of legacy table rows becomes a stub plus rows."""
    out: list[SBlock] = []
    table_n = 0
    in_table = False
    row_i = 0
    for b in blocks:
        if b.kind == "table_row":
            if not in_table:
                table_n += 1
                in_table, row_i = True, 0
                out.append(SBlock("table_stub", f"There is a table here: table {table_n}. Ask me for any row.",
                                  label="table", table_key=f"t{table_n}"))
            row_i += 1
            text = b.text[5:].strip() if b.text.startswith("Row: ") else b.text
            out.append(SBlock("table_row", text, label="table_row", table_key=f"t{table_n}",
                              extra={"row": row_i}))
            continue
        in_table = False
        if b.kind == "heading":
            out.append(SBlock("heading", b.text, label="section_header", level=b.level or 1))
        elif b.kind in ("para", "list_item"):
            out.append(SBlock("body", b.text, label="list_item" if b.kind == "list_item" else "text"))
        else:
            out.append(SBlock("boilerplate", b.text, label=b.kind))
    return out


def load_docling_json(path: Path):
    from docling_core.types.doc import DoclingDocument
    return DoclingDocument.model_validate_json(Path(path).read_text(encoding="utf-8"))


def save_docling_json(doc, path: Path) -> None:
    """Lossless, key-sorted, so a re-run on the same input is byte-identical."""
    import json
    data = doc.export_to_dict()
    Path(path).write_text(json.dumps(data, indent=1, ensure_ascii=False, sort_keys=True) + "\n",
                          encoding="utf-8")


# --------------------------------------------------------------------------
# element -> block
# --------------------------------------------------------------------------

def _label_of(item) -> str:
    lab = getattr(item, "label", "")
    return str(getattr(lab, "value", lab) or "").lower()


def _page_of(item) -> int:
    prov = getattr(item, "prov", None) or []
    for p in prov:
        n = getattr(p, "page_no", None)
        if n:
            return int(n)
    return 0


def _text_of(item) -> str:
    t = getattr(item, "text", None)
    if t is None:
        return ""
    return _clean(t)


def _table_rows(item, doc) -> tuple[list[str], list[list[str]]]:
    """Header row and body rows as strings, from the table's cell grid."""
    try:
        df = item.export_to_dataframe(doc)
    except TypeError:
        df = item.export_to_dataframe()
    except Exception:
        df = None
    if df is not None:
        header = [_clean(c) for c in list(df.columns)]
        rows = [[_clean(v) for v in r] for r in df.astype(str).values.tolist()]
        if len(header) == 2:
            # A two-column table is a key/value list (a definitions grid, a
            # metadata block). Its first row is a pair like the others, not a
            # header, so every row is spoken "key: value".
            return [], [header] + rows
        return header, rows
    # fallback: the raw cell grid
    data = getattr(item, "data", None)
    grid = getattr(data, "grid", None) or []
    cells = [[_clean(getattr(c, "text", "")) for c in row] for row in grid]
    if not cells:
        return [], []
    if len(cells[0]) == 2:
        return [], cells
    return cells[0], cells[1:]


def _caption_of(item, doc) -> str:
    try:
        return _WS.sub(" ", str(item.caption_text(doc) or "")).strip()
    except Exception:
        return ""


def blocks_from_docling(doc, table_csv_dir: Optional[Path] = None, doc_key: str = "doc",
                        report: Optional[StructureReport] = None) -> list[SBlock]:
    """Reading-order blocks with kinds. Tables become a stub plus rows."""
    rep = report if report is not None else StructureReport()
    out: list[SBlock] = []
    table_n = 0
    for item, _level in doc.iterate_items():
        label = _label_of(item)
        if not label or label in ("list", "ordered_list", "inline", "group", "key_value_region", "form"):
            continue                                    # containers: their children follow
        rep.labels_seen[label] += 1
        page = _page_of(item)
        if label == "table":
            table_n += 1
            key = f"t{table_n}"
            header, rows = _table_rows(item, doc)
            caption = _caption_of(item, doc)
            head_desc = caption or ", ".join(h for h in header if h) or \
                (f"{len(rows)} entries, {rows[0][0]} to {rows[-1][0]}" if rows and not header and rows[0] and rows[-1]
                 else f"table {table_n}")
            stub_text = f"There is a table here: {head_desc}. Ask me for any row."
            out.append(SBlock("table_stub", stub_text, label=label, page=page, table_key=key,
                              extra={"rows": len(rows), "caption": caption, "header": header}))
            csv_path = None
            if table_csv_dir is not None:
                table_csv_dir.mkdir(parents=True, exist_ok=True)
                csv_path = table_csv_dir / f"{doc_key}.{key}.csv"
                with csv_path.open("w", newline="", encoding="utf-8") as fh:
                    w = csv.writer(fh)
                    if header:
                        w.writerow(header)
                    w.writerows(rows)
            rep.tables.append((key, len(rows), str(csv_path) if csv_path else ""))
            for i, row in enumerate(rows, start=1):
                pairs = []
                if not header and len(row) == 2:
                    k_, v_ = row
                    if k_ and v_ and v_.lower() not in ("nan", "none"):
                        pairs.append(f"{k_}: {v_}")
                else:
                    for j, v in enumerate(row):
                        if not v or v.lower() in ("nan", "none"):
                            continue
                        h = header[j] if j < len(header) and header[j] else f"column {j + 1}"
                        pairs.append(f"{h}: {v}")
                if not pairs:
                    continue
                out.append(SBlock("table_row", "; ".join(pairs) + ".", label="table_row", page=page,
                                  table_key=key, extra={"row": i}))
            continue
        text = _text_of(item)
        if not text:
            continue
        kind = LABEL_TO_KIND.get(label, DEFAULT_KIND)
        if label not in LABEL_TO_KIND:
            rep.unknown_labels[label] += 1
        b = SBlock(kind, text, label=label, page=page)
        if kind == "heading":
            b.level = int(getattr(item, "level", 1) or 1)
        if label == "list_item":
            marker = str(getattr(item, "marker", "") or "").strip()
            if marker and not text.startswith(marker):
                b.text = f"{marker} {text}"
            b.marker = marker
        out.append(b)
    return out


# --------------------------------------------------------------------------
# regex boilerplate pass
# --------------------------------------------------------------------------

def _norm_line(s: str) -> str:
    return _WS.sub(" ", re.sub(r"\d+", "#", s.lower())).strip()


def recurring_lines(blocks: list[SBlock], n_pages: int) -> set[str]:
    """Normalised texts that appear on >= 30 % of pages (page furniture)."""
    if n_pages < MIN_PAGES_FOR_FRACTION:
        return set()
    pages_of: dict[str, set[int]] = {}
    for b in blocks:
        if b.page and len(b.text) < 160:
            pages_of.setdefault(_norm_line(b.text), set()).add(b.page)
    cutoff = max(2, int(round(n_pages * HEADER_FOOTER_PAGE_FRACTION)))
    return {t for t, pages in pages_of.items() if len(pages) >= cutoff}


def regex_boilerplate_pass(blocks: list[SBlock], n_pages: int,
                           report: Optional[StructureReport] = None) -> list[SBlock]:
    rep = report if report is not None else StructureReport()
    recurring = recurring_lines(blocks, n_pages)
    out: list[SBlock] = []
    for b in blocks:
        if b.kind in ("table_stub", "table_row"):
            out.append(b)
            continue
        text = b.text
        if _PLACEHOLDER.search(text):
            stripped = _WS.sub(" ", _PLACEHOLDER.sub(" ", text)).strip(" ,;:")
            rep.placeholders_stripped += len(_PLACEHOLDER.findall(text))
            b.has_placeholder = True
            text = stripped
            b.text = text
            if not text:
                b.kind = "boilerplate"
                b.demoted_by = "placeholder_only"
                rep.demoted.append(("placeholder_only", b.text))
                out.append(b)
                continue
        rule = None
        if b.kind != "boilerplate":
            if _PAGE_NO.match(text) or _BARE_INT.match(text):
                rule = "page_number"
            elif _UIN.match(text):
                rule = "uin"
            elif _REG_LINE.match(text) and len(text) <= 160:
                # Furniture is short. A clause that merely opens with the
                # regulatory phrase ("Insurance is the subject matter of
                # solicitation, and the Borrower ...") is body text.
                rule = "registration_line"
            elif _norm_line(text) in recurring:
                rule = "recurring_on_pages"
        if rule:
            b.kind = "boilerplate"
            b.demoted_by = rule
            rep.demoted.append((rule, text))
        out.append(b)
    return out


def num_pages(doc) -> int:
    try:
        n = doc.num_pages()
        if n:
            return int(n)
    except Exception:
        pass
    pages = getattr(doc, "pages", None) or {}
    return len(pages)
