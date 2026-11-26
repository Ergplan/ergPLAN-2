"""Exchange-purchase rules read from the stored documents, proposed as DRAFTS (Stage 18).

The proposals are files a person wrote from the stored tariff orders
(packages/rulebooks/proposals/exchange_purchase/<STATE>.yaml): every field is either stated, with
the document, page and verbatim quote it was read from, or `not_stated` with the regulation that
would have to be obtained (operating rule 18). No model reads or decides anything here.

Before a file becomes a draft, each citation passes the same grounding check as every other
proposed rule (`regreader.grounding`): the document is stored on this server, the quote is on the
cited page of the stored PDF, and every number of the field is printed on that page. A file with a
citation that fails is not proposed at all, and the failures are listed. The draft is written
through `rulebook.service.propose` only (invariant 3); nothing here approves.

    python -m ergplan.regreader.exchange_drafts                 # propose what is not there yet
    python -m ergplan.regreader.exchange_drafts --pdf-dir DIR   # first store the cited PDFs found in
                                                                # DIR as <sha256>.pdf (hash checked)
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.settings import get_settings
from ..db.models import RulebookVersion, SourceDocument
from ..documents import service as documents
from ..engine.exchange import FAMILY, FIELDS
from ..rulebook import golden, service
from . import grounding

ORIGIN = "exchange_reader"
ACTOR = "exchange-reader@ergplan"
DOCUMENT_KINDS = ("regulation", "order")


@dataclass
class Outcome:
    jurisdiction: str
    status: str  # proposed | present | refused
    version_id: str | None = None
    stated: list[str] = field(default_factory=list)
    not_stated: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    golden: str | None = None


def proposals_dir() -> Path:
    return get_settings().rulebooks_dir / "proposals" / FAMILY


def cited_fields(row: dict[str, Any]) -> list[tuple[str, dict[str, Any] | list[Any], dict[str, Any] | None]]:
    """(pointer, the field's body, its citation or None) for every stated field and every barrier."""
    body, refs = row["body"], row.get("field_refs") or {}
    out: list[tuple[str, dict[str, Any] | list[Any], dict[str, Any] | None]] = []
    for name in FIELDS:
        f = body.get(name) or {}
        if f.get("status") == "stated":
            out.append((f"/{name}", f, refs.get(f"/{name}")))
    for i, b in enumerate(body.get("barriers") or []):
        out.append((f"/barriers/{i}", b, refs.get(f"/barriers/{i}")))
    return out


def structure_errors(row: dict[str, Any]) -> list[str]:
    """What the schema cannot say: a stated field is cited to a document page of its own, and a
    citation points at a field that is stated."""
    errors = []
    pointers = set()
    for pointer, _, ref in cited_fields(row):
        pointers.add(pointer)
        if ref is None:
            errors.append(f"{pointer}: stated but not cited (field_refs has no {pointer})")
        elif ref.get("kind") not in DOCUMENT_KINDS:
            errors.append(
                f"{pointer}: cited to a {ref.get('kind')}, not to a stored regulation or order page"
            )
    for pointer in (row.get("field_refs") or {}).keys() - pointers:
        errors.append(f"{pointer}: a citation for a field that is not stated")
    return errors


def _store_from(
    session: Session, pdf_dir: Path, document_id: str, meta: dict[str, Any], jurisdiction: str
) -> None:
    sha = str(meta.get("sha256") or "")
    path = pdf_dir / f"{sha}.pdf"
    if not sha or not path.is_file():
        return
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != sha:
        return
    documents.register(
        session,
        data,
        documents.DocumentMeta(
            title=str(meta.get("title") or document_id),
            regulator=meta.get("regulator"),
            jurisdiction=jurisdiction,
            url_public=meta.get("url_public"),
        ),
        ACTOR,
    )


def grounding_failures(session: Session, row: dict[str, Any]) -> list[str]:
    """Every citation of the row against the stored page: the document is stored, the quote is on
    the page, and the field's numbers are printed there."""
    failures = []
    for pointer, body, ref in cited_fields(row):
        if ref is None or ref.get("kind") not in DOCUMENT_KINDS:
            continue  # reported by structure_errors
        doc = session.get(SourceDocument, ref["document_id"])
        if doc is None or not documents.is_stored(doc):
            failures.append(f"{pointer}: document {ref['document_id']} is not stored on this server")
            continue
        try:
            text = documents.page_text(doc, int(ref["page"]))
        except documents.PageOutOfRange as e:
            failures.append(f"{pointer}: {e}")
            continue
        if not grounding.quote_on_page(str(ref["quote"]), text, documents.page_is_ocr(doc, int(ref["page"]))):
            failures.append(f"{pointer}: the quote is not on page {ref['page']} of {doc.title}")
        missing = grounding.ungrounded_numbers(body if isinstance(body, dict) else {"v": body}, [text])
        if missing:
            failures.append(
                f"{pointer}: {', '.join(missing)} not printed on page {ref['page']} of {doc.title}"
            )
    return failures


def propose_file(session: Session, path: Path, pdf_dir: Path | None = None) -> Outcome:
    d = yaml.safe_load(path.read_text())
    if d.get("family") != FAMILY:
        raise ValueError(f"{path.name}: not an {FAMILY} proposal")
    jid, rows, title = str(d["jurisdiction"]), list(d["rows"]), str(d["title"])
    out = Outcome(jid, "refused")
    for row in rows:
        for name in FIELDS:
            stated = (row["body"].get(name) or {}).get("status") == "stated"
            (out.stated if stated else out.not_stated).append(name)
    existing = session.scalar(
        select(RulebookVersion).where(
            RulebookVersion.family == FAMILY,
            RulebookVersion.jurisdiction_id == jid,
            RulebookVersion.title == title,
            RulebookVersion.status.in_(("draft", "approved", "superseded")),
        )
    )
    if existing is not None:
        out.status, out.version_id, out.golden = "present", existing.id, existing.golden_status
        return out
    for i, errs in service.validate_rows(FAMILY, rows).items():
        out.failures += [f"row {i}: {e}" for e in errs]
    for i, row in enumerate(rows):
        out.failures += [f"row {i} {e}" for e in structure_errors(row)]
    if out.failures:
        return out
    if pdf_dir is not None:
        for doc_id, meta in (d.get("documents") or {}).items():
            _store_from(session, pdf_dir, str(doc_id), meta, jid)
    for i, row in enumerate(rows):
        out.failures += [f"row {i} {e}" for e in grounding_failures(session, row)]
    if out.failures:
        return out
    v = service.propose(
        session,
        family=FAMILY,
        jurisdiction_id=jid,
        rows=rows,
        actor=ACTOR,
        effective_from=date.fromisoformat(str(d["effective_from"])),
        title=title,
        origin=ORIGIN,
        notes="Read from the stored documents; fields no stored regulation states are 'not stated'. "
        "Proposed as a draft: an analyst completes it from the open access regulations and approves.",
    )
    result = service.run_golden(session, v.id)
    out.status, out.version_id = "proposed", v.id
    out.golden = f"{result.passed} of {result.total}"
    return out


def propose_all(session: Session, folder: Path | None = None, pdf_dir: Path | None = None) -> list[Outcome]:
    service.sync_jurisdictions(session)
    golden.sync_tests(session)
    return [propose_file(session, p, pdf_dir) for p in sorted((folder or proposals_dir()).glob("*.yaml"))]


def main(argv: list[str] | None = None) -> int:
    import argparse

    from ..db.base import session_scope

    ap = argparse.ArgumentParser(prog="ergplan.regreader.exchange_drafts")
    ap.add_argument("--pdf-dir", type=Path, default=None)
    a = ap.parse_args(argv)
    with session_scope(actor=ACTOR) as s:
        outcomes = propose_all(s, pdf_dir=a.pdf_dir)
    for o in outcomes:
        print(
            f"{o.jurisdiction}: {o.status}"
            + (f" (version {o.version_id}, golden {o.golden})" if o.version_id else "")
            + f"; stated: {', '.join(o.stated) or 'nothing'}; not stated: {len(o.not_stated)} fields"
        )
        for f in o.failures:
            print(f"   refused: {f}")
    return 1 if any(o.status == "refused" for o in outcomes) else 0


if __name__ == "__main__":
    raise SystemExit(main())
