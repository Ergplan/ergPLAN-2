"""Source PDFs: stored once by SHA-256 under data/documents/<sha>.pdf, one row each, pages rendered
on demand. The only writer of source_documents."""

from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import pypdfium2 as pdfium
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.ids import document_id
from ..core.settings import get_settings
from ..db.models import SourceDocument
from . import ocr


class NotAPdf(ValueError):
    pass


class DocumentNotFound(LookupError):
    pass


class PageOutOfRange(LookupError):
    pass


@dataclass(frozen=True)
class DocumentMeta:
    title: str
    regulator: str | None = None
    jurisdiction: str | None = None
    document_kind: str = "order"
    utilities: tuple[str, ...] = ()
    order_date: date | None = None
    effective_from: date | None = None
    effective_to: date | None = None
    url_public: str | None = None
    notes: tuple[str, ...] = ()
    page_hints: dict[str, Any] | None = None
    original_filename: str | None = None


def documents_dir() -> Path:
    d = get_settings().documents_dir
    d.mkdir(parents=True, exist_ok=True)
    return d


def path_for(sha256: str) -> Path:
    return documents_dir() / f"{sha256}.pdf"


def page_count(data: bytes) -> int:
    try:
        pdf = pdfium.PdfDocument(io.BytesIO(data))
    except pdfium.PdfiumError as e:
        raise NotAPdf("the file is not a readable PDF") from e
    try:
        return len(pdf)
    finally:
        pdf.close()


def register(
    session: Session, data: bytes, meta: DocumentMeta, uploaded_by: str
) -> tuple[SourceDocument, bool]:
    """Store the PDF and its row. Returns (document, created). Same bytes → the existing row."""
    if not data.startswith(b"%PDF"):
        raise NotAPdf("the file does not start with %PDF")
    sha = hashlib.sha256(data).hexdigest()
    existing = session.scalar(select(SourceDocument).where(SourceDocument.file_sha256 == sha))
    target = path_for(sha)
    if existing is not None and existing.file_bytes and target.exists():
        return existing, False
    pages = page_count(data)
    if not target.exists():
        tmp = target.with_suffix(".part")
        tmp.write_bytes(data)
        tmp.replace(target)
    if existing is not None:
        # the row was a reference (register_reference), or its file was not on this machine: the
        # PDF has now arrived, so its pages open
        existing.file_bytes, existing.pages = len(data), pages
        session.flush()
        return existing, False
    doc = SourceDocument(
        id=document_id(sha),
        title=meta.title,
        regulator=meta.regulator,
        jurisdiction=meta.jurisdiction,
        document_kind=meta.document_kind,
        utilities=list(meta.utilities),
        file_sha256=sha,
        file_bytes=len(data),
        pages=pages,
        order_date=meta.order_date,
        effective_from=meta.effective_from,
        effective_to=meta.effective_to,
        url_public=meta.url_public,
        notes=list(meta.notes),
        page_hints=meta.page_hints or {},
        original_filename=meta.original_filename,
        uploaded_by=uploaded_by,
    )
    session.add(doc)
    session.flush()
    return doc, True


def register_reference(
    session: Session, sha256: str, pages: int, meta: DocumentMeta, uploaded_by: str
) -> tuple[SourceDocument, bool]:
    """A row for a PDF this machine does not hold: its SHA-256 (so its id is the one the PDF will
    have), its page count and the regulator's link, as another reader reported them. `file_bytes`
    is 0 and no file is written, so `stored_ids` leaves it out and every citation to it says the
    page is not stored, beside the regulator's link. `register` with the PDF itself completes it."""
    sha = sha256.lower()
    if len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
        raise ValueError("not a SHA-256")
    existing = session.scalar(select(SourceDocument).where(SourceDocument.file_sha256 == sha))
    if existing is not None:
        return existing, False
    doc = SourceDocument(
        id=document_id(sha),
        title=meta.title,
        regulator=meta.regulator,
        jurisdiction=meta.jurisdiction,
        document_kind=meta.document_kind,
        utilities=list(meta.utilities),
        file_sha256=sha,
        file_bytes=0,
        pages=pages,
        order_date=meta.order_date,
        effective_from=meta.effective_from,
        effective_to=meta.effective_to,
        url_public=meta.url_public,
        notes=list(meta.notes),
        page_hints=meta.page_hints or {},
        original_filename=meta.original_filename,
        uploaded_by=uploaded_by,
    )
    session.add(doc)
    session.flush()
    return doc, True


def is_stored(doc: SourceDocument) -> bool:
    return path_for(doc.file_sha256).exists()


def get(session: Session, document_id: str) -> SourceDocument:
    doc = session.get(SourceDocument, document_id)
    if doc is None:
        raise DocumentNotFound(document_id)
    return doc


def stored_ids(session: Session) -> set[str]:
    """Ids of the documents whose PDF is in the store: a row and its file. A citation to any other
    id opens nothing on this machine (a server that has not been sent the library yet)."""
    return {
        d.id
        for d in session.scalars(select(SourceDocument))
        if (get_settings().documents_dir / f"{d.file_sha256}.pdf").exists()
    }


def file_path(doc: SourceDocument) -> Path:
    p = path_for(doc.file_sha256)
    if not p.exists():
        raise DocumentNotFound(f"file for {doc.id} is missing from the store")
    return p


def render_page_png(doc: SourceDocument, page: int, scale: float = 1.5) -> bytes:
    """Render a 1-based page to PNG bytes."""
    if page < 1 or page > doc.pages:
        raise PageOutOfRange(f"page {page} is outside 1..{doc.pages}")
    pdf = pdfium.PdfDocument(str(file_path(doc)))
    try:
        bitmap = pdf[page - 1].render(scale=scale)
        img = bitmap.to_pil()
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    finally:
        pdf.close()


def page_text(doc: SourceDocument, page: int) -> str:
    """The text layer of a 1-based page ('' when the page is a scan)."""
    if page < 1 or page > doc.pages:
        raise PageOutOfRange(f"page {page} is outside 1..{doc.pages}")
    pdf = pdfium.PdfDocument(str(file_path(doc)))
    try:
        text = str(pdf[page - 1].get_textpage().get_text_range())
    finally:
        pdf.close()
    if len(text.strip()) < ocr.MIN_TEXT_CHARS:
        scanned = ocr.ocr_text(doc.file_sha256, page)
        if scanned is not None:
            return scanned
    return text


def page_is_ocr(doc: SourceDocument, page: int) -> bool:
    """True when page_text for this page comes from OCR rather than the PDF's text layer."""
    return ocr.ocr_text(doc.file_sha256, page) is not None
