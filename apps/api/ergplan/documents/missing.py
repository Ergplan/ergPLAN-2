"""The orders and regulations the approved rules cite whose PDF is not in this machine's store.

    python -m ergplan.documents.missing

A document's id is derived from its bytes (core/ids.py), so a citation committed with a rulebook
resolves on any machine once the same PDF is registered there: by `python -m ergplan.documents.corpus`
over a folder of the library, or by an upload (POST /documents). Until then a number that cites it
says the page is not stored instead of linking to it (report.statebook, GET /documents/{id}).
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.base import session_scope
from ..db.models import Rule, RulebookVersion
from . import service

DOCUMENT_KINDS = ("order", "regulation")


def document_refs(node: Any) -> Iterator[dict[str, Any]]:
    """Every order or regulation ref (a dict with kind and document_id) anywhere inside a JSON value."""
    if isinstance(node, dict):
        if node.get("kind") in DOCUMENT_KINDS and node.get("document_id"):
            yield node
        for v in node.values():
            yield from document_refs(v)
    elif isinstance(node, list):
        for v in node:
            yield from document_refs(v)


@dataclass
class MissingDocument:
    document_id: str
    jurisdictions: set[str] = field(default_factory=set)
    families: set[str] = field(default_factory=set)
    cited_in: set[str] = field(default_factory=set)  # the titles of the versions that cite it
    pages: set[int] = field(default_factory=set)
    rules: int = 0
    quote: str | None = None  # one quoted line, to recognise the document by


def cited_missing(session: Session) -> list[MissingDocument]:
    """Documents cited by an approved rule (its row ref or a field's ref) and not stored here."""
    stored = service.stored_ids(session)
    out: dict[str, MissingDocument] = {}
    q = (
        select(Rule, RulebookVersion)
        .join(RulebookVersion, Rule.rulebook_version_id == RulebookVersion.id)
        .where(RulebookVersion.status == "approved")
        .order_by(RulebookVersion.jurisdiction_id, RulebookVersion.family, Rule.position)
    )
    for rule, version in session.execute(q):
        seen: set[str] = set()
        for ref in document_refs([rule.source_ref, rule.field_refs]):
            doc = str(ref["document_id"])
            if doc in stored:
                continue
            m = out.setdefault(doc, MissingDocument(document_id=doc))
            m.jurisdictions.add(version.jurisdiction_id)
            m.families.add(version.family)
            if version.title:
                m.cited_in.add(version.title)
            if isinstance(ref.get("page"), int):
                m.pages.add(ref["page"])
            m.quote = m.quote or (str(ref["quote"]) if ref.get("quote") else None)
            if doc not in seen:
                m.rules += 1
                seen.add(doc)
    return sorted(out.values(), key=lambda m: (sorted(m.jurisdictions), m.document_id))


def main() -> int:
    with session_scope() as s:
        missing = cited_missing(s)
        stored = len(service.stored_ids(s))
    print(f"{stored} documents are stored; {len(missing)} cited by approved rules are missing")
    for m in missing:
        pages = sorted(m.pages)
        where = ", ".join(sorted(m.jurisdictions))
        print(f"\n{m.document_id}  {where}  ({m.rules} rules, pages {pages[0]}–{pages[-1]})")
        for title in sorted(m.cited_in):
            print(f"    cited in: {title}")
        if m.quote:
            print(f"    quoted:   “{m.quote[:150]}”")
    if missing:
        print(
            "\nSend each PDF exactly as it is on the Mac mini (the id is its SHA-256): copy the library to "
            "data/corpus/ and run `python -m ergplan.documents.corpus --root /data/corpus`, or upload it "
            "in the app (Documents)."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
