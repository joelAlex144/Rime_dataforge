"""Document library: pick a document at runtime, keep a position in each.

Selection is a runtime operation; ingestion is not. `fixtures/index.json` is the
only source of available documents, and nothing here reads a file that is not
listed in it. A document the registry does not name cannot be opened, which is
what keeps "choose a document" from becoming "load arbitrary text at runtime".

Each document owns a `Session` -- read cursor, last heard unit, delivery
boundary, ledger, question history. Switching documents saves the outgoing
session and restores the incoming one; nothing is recomputed and nothing is
shared, so a read in one document cannot move the cursor in another.

Retrieval stays per-document on purpose. A question asked while document B is
open is answered from B, and `not_found` is the right answer for something that
only appears in A. There is no cross-document retrieval here and there should
not be: answering from a document the listener is not in is a position leak of
exactly the kind the spoiler gate exists to prevent.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from grounding import Grounding


class LibraryError(LookupError):
    """Base for document-selection failures."""


class UnknownDocument(LibraryError):
    pass


class AmbiguousDocument(LibraryError):
    pass


@dataclass
class Session:
    """Where a listener is inside one document. Saved and restored on switch."""
    read_cursor: int = 0                       # index of the NEXT clause to read
    last_heard_unit_id: Optional[str] = None
    boundary_char: int = 0                     # chars of last_heard actually heard
    ledger: dict = field(default_factory=dict)  # unit_id -> "heard" | "truncated@N"
    history: list = field(default_factory=list)  # (question, kind, unit_id)

    # Not in the minimal contract, but a switch that loses these loses the
    # meaning of `stop` and `resume` on return: `current_unit_id` is the clause
    # an interruption would cut, `read_index` is the retrieval scope.
    current_unit_id: Optional[str] = None
    read_index: int = -1

    def status_of(self, unit_id: str) -> str:
        return self.ledger.get(unit_id, "never_sent")

    def to_dict(self) -> dict:
        return {
            "read_cursor": self.read_cursor,
            "last_heard_unit_id": self.last_heard_unit_id,
            "boundary_char": self.boundary_char,
            "ledger": dict(self.ledger),
            "history": [list(h) for h in self.history],
            "current_unit_id": self.current_unit_id,
            "read_index": self.read_index,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Session":
        return cls(
            read_cursor=int(d.get("read_cursor", 0)),
            last_heard_unit_id=d.get("last_heard_unit_id"),
            boundary_char=int(d.get("boundary_char", 0)),
            ledger=dict(d.get("ledger") or {}),
            history=[tuple(h) for h in (d.get("history") or [])],
            current_unit_id=d.get("current_unit_id"),
            read_index=int(d.get("read_index", -1)),
        )


class Document:
    """One registry entry. The fixture and its Grounding load on first use."""

    def __init__(self, name: str, title: str, path: Path, clause_count: int = 0,
                 source: Optional[dict] = None) -> None:
        self.name = name
        self.title = title
        self.path = Path(path)
        self.clause_count = clause_count
        self.source = source or {}
        self.session = Session()
        self._fixture: Optional[dict] = None
        self._grounding: Optional[Grounding] = None

    @property
    def fixture(self) -> dict:
        if self._fixture is None:
            self._fixture = json.loads(self.path.read_text(encoding="utf-8"))
        return self._fixture

    @property
    def grounding(self) -> Grounding:
        """Built lazily and cached: a 213-clause BM25 index is not free, and a
        library with six documents should not pay for five of them."""
        if self._grounding is None:
            self._grounding = Grounding(self.path)
        return self._grounding

    @property
    def loaded(self) -> bool:
        return self._grounding is not None

    def __repr__(self) -> str:
        return f"<Document {self.name!r} {self.clause_count} clauses>"


class Library:
    def __init__(self, index_path, events) -> None:
        self.index_path = Path(index_path)
        self.events = events
        self.root = self.index_path.parent
        self._docs: dict[str, Document] = {}
        self._current: Optional[str] = None
        if self.index_path.exists():
            self._read_index()

    # ------------------------------------------------------------- registry
    def _read_index(self) -> None:
        data = json.loads(self.index_path.read_text(encoding="utf-8"))
        for e in data.get("documents", []):
            name = e["name"]
            self._docs[name] = Document(
                name=name,
                title=e.get("title") or name,
                path=(self.root / e["path"]).resolve(),
                clause_count=int(e.get("clause_count") or 0),
                source=e.get("source") or {},
            )

    @classmethod
    def single(cls, fixture_path, events) -> "Library":
        """A one-entry library around a fixture given directly on the command
        line. `--fixture` stays supported; it just becomes a library of one."""
        p = Path(fixture_path).resolve()
        lib = cls(p.parent / "__none__.json", events)
        try:
            title = json.loads(p.read_text(encoding="utf-8")).get("title", p.stem)
        except (OSError, ValueError):
            title = p.stem
        lib._docs = {p.stem: Document(p.stem, title, p)}
        return lib

    # ---------------------------------------------------------------- query
    def list(self) -> list:
        return [{"name": d.name, "title": d.title, "clause_count": d.clause_count}
                for d in self._docs.values()]

    @property
    def current(self) -> Optional[Document]:
        return self._docs.get(self._current) if self._current else None

    def _resolve(self, name_or_query: str) -> Document:
        q = (name_or_query or "").strip()
        if not q:
            raise UnknownDocument("no document named")
        if q in self._docs:
            return self._docs[q]
        lowered = {n.lower(): n for n in self._docs}
        if q.lower() in lowered:
            return self._docs[lowered[q.lower()]]
        hits = [d for d in self._docs.values()
                if q.lower() in d.title.lower() or q.lower() in d.name.lower()]
        if len(hits) == 1:
            return hits[0]
        if not hits:
            raise UnknownDocument(
                f"no document matching {q!r}; available: {', '.join(sorted(self._docs)) or '(none)'}")
        raise AmbiguousDocument(
            f"{q!r} matches {len(hits)} documents: {', '.join(sorted(d.name for d in hits))}")

    # ----------------------------------------------------------------- open
    def open(self, name_or_query: str) -> Document:
        doc = self._resolve(name_or_query)
        leaving = self.current
        self.events.emit("document_opened", name=doc.name)
        if leaving is not None and leaving.name != doc.name:
            s = leaving.session
            self.events.emit("position_saved", document=leaving.name,
                             unit_id=s.current_unit_id, cursor=s.read_cursor,
                             last_heard_unit_id=s.last_heard_unit_id,
                             boundary_char=s.boundary_char)
        s = doc.session
        self.events.emit("position_restored", document=doc.name,
                         unit_id=s.current_unit_id, cursor=s.read_cursor,
                         last_heard_unit_id=s.last_heard_unit_id,
                         boundary_char=s.boundary_char)
        self._current = doc.name
        return doc

    # -------------------------------------------------------- session state
    def save(self, path) -> Path:
        """Sessions only. Fixtures are large, committed, and immutable; a saved
        session that embedded one would go stale the moment it was re-ingested."""
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({
            "current": self._current,
            "sessions": {n: d.session.to_dict() for n, d in self._docs.items()},
        }, indent=1), encoding="utf-8")
        return p

    def load(self, path) -> None:
        d = json.loads(Path(path).read_text(encoding="utf-8"))
        for name, sd in (d.get("sessions") or {}).items():
            if name in self._docs:
                self._docs[name].session = Session.from_dict(sd)
        cur = d.get("current")
        if cur in self._docs:
            self._current = cur
