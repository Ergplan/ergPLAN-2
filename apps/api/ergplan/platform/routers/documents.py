"""Documents: list, upload, open the PDF inline (so `#page=N` works), page images and text."""

from __future__ import annotations

from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel
from sqlalchemy import select

from ...db.base import session_scope
from ...db.models import SourceDocument
from ...documents import missing, service
from ..auth import Caller, analyst, caller, production

router = APIRouter(prefix="/documents", tags=["documents"])


class DocumentOut(BaseModel):
    id: str
    title: str
    regulator: str | None
    jurisdiction: str | None
    document_kind: str
    utilities: list[str]
    file_sha256: str
    pages: int
    order_date: date | None
    effective_from: date | None
    url_public: str | None
    page_hints: dict[str, Any]
    notes: list[str]
    url: str

    @classmethod
    def of(cls, d: SourceDocument) -> DocumentOut:
        return cls(
            id=d.id,
            title=d.title,
            regulator=d.regulator,
            jurisdiction=d.jurisdiction,
            document_kind=d.document_kind,
            utilities=list(d.utilities or []),
            file_sha256=d.file_sha256,
            pages=d.pages,
            order_date=d.order_date,
            effective_from=d.effective_from,
            url_public=d.url_public,
            page_hints=dict(d.page_hints or {}),
            notes=list(d.notes or []),
            url=f"/documents/{d.id}",
        )


@router.get("", response_model=list[DocumentOut])
def list_documents(jurisdiction: str | None = None) -> list[DocumentOut]:
    with session_scope() as s:
        q = select(SourceDocument).order_by(SourceDocument.jurisdiction, SourceDocument.title)
        if jurisdiction:
            q = q.where(SourceDocument.jurisdiction == jurisdiction)
        return [DocumentOut.of(d) for d in s.scalars(q)]


@router.post("", response_model=DocumentOut, status_code=201)
async def upload_document(
    file: Annotated[UploadFile, File()],
    title: Annotated[str, Form()],
    regulator: Annotated[str | None, Form()] = None,
    jurisdiction: Annotated[str | None, Form()] = None,
    document_kind: Annotated[str, Form()] = "order",
    url_public: Annotated[str | None, Form()] = None,
    uploaded_by: Annotated[str, Form()] = "analyst",
    who: Annotated[Caller, Depends(caller)] = Caller(actor="analyst", role="analyst"),
) -> DocumentOut:
    """The library is the analysts': in production only an analyst adds to it, under their own name."""
    if production():
        if who.role != "analyst" or not who.signed_in:
            raise HTTPException(403, "analyst role required")
        uploaded_by = who.actor
    data = await file.read()
    meta = service.DocumentMeta(
        title=title,
        regulator=regulator,
        jurisdiction=jurisdiction,
        document_kind=document_kind,
        url_public=url_public,
        original_filename=file.filename,
    )
    try:
        with session_scope(actor=uploaded_by) as s:
            doc, _ = service.register(s, data, meta, uploaded_by=uploaded_by)
            return DocumentOut.of(doc)
    except service.NotAPdf as e:
        raise HTTPException(422, str(e)) from e


class MissingOut(BaseModel):
    """An order or regulation that approved rules cite and this server does not hold."""

    document_id: str
    jurisdictions: list[str]
    families: list[str]
    cited_in: list[str]
    pages: list[int]
    rules: int
    quote: str | None


@router.get("/missing", response_model=list[MissingOut])
def missing_documents(who: Annotated[Caller, Depends(analyst)]) -> list[MissingOut]:
    """What the approved rules cite that is not in the store (a server not yet sent the library)."""
    with session_scope() as s:
        return [
            MissingOut(
                document_id=m.document_id,
                jurisdictions=sorted(m.jurisdictions),
                families=sorted(m.families),
                cited_in=sorted(m.cited_in),
                pages=sorted(m.pages),
                rules=m.rules,
                quote=m.quote,
            )
            for m in missing.cited_missing(s)
        ]


NOT_STORED = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Not yet stored · ergPLAN</title>
<style>body{font:16px/1.5 system-ui,sans-serif;max-width:40rem;margin:15vh auto;padding:0 1.25rem}
code{font-size:.85em;color:#57606a}</style></head><body>
<h1>This page is not yet stored here</h1>
<p>The number you clicked cites an order or regulation whose PDF has not been added to this server's
regulation library, so its page cannot be opened yet. The citation itself (document, page and the
quoted words) is shown beside the number.</p>
<p>A jouleWise analyst can add the PDF under Documents; the link then opens the cited page.</p>
<p><code>document __ID__</code></p></body></html>"""


def _not_stored(document_id: str) -> HTMLResponse:
    """Said in words: a citation to a PDF this server does not hold never opens a raw error."""
    safe = "".join(ch for ch in document_id if ch.isalnum() or ch == "-")[:64]
    return HTMLResponse(NOT_STORED.replace("__ID__", safe), status_code=404)


@router.get("/{document_id}/meta", response_model=DocumentOut)
def document_meta(document_id: str) -> DocumentOut:
    with session_scope() as s:
        try:
            return DocumentOut.of(service.get(s, document_id))
        except service.DocumentNotFound as e:
            raise HTTPException(404, "document not found") from e


@router.get("/{document_id}", response_model=None)
def open_document(document_id: str) -> Response:
    """The PDF, inline, so a browser honours `#page=N`; a page saying so when it is not stored here."""
    with session_scope() as s:
        try:
            doc = service.get(s, document_id)
            path = service.file_path(doc)
        except service.DocumentNotFound:
            return _not_stored(document_id)
        name = (doc.original_filename or f"{doc.id}.pdf").replace('"', "")
    return FileResponse(
        path, media_type="application/pdf", headers={"Content-Disposition": f'inline; filename="{name}"'}
    )


@router.get("/{document_id}/pages/{page}.png")
def page_image(document_id: str, page: int) -> Response:
    with session_scope() as s:
        try:
            doc = service.get(s, document_id)
            png = service.render_page_png(doc, page)
        except service.DocumentNotFound as e:
            raise HTTPException(404, "document not found") from e
        except service.PageOutOfRange as e:
            raise HTTPException(404, str(e)) from e
    return Response(png, media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})


@router.get("/{document_id}/pages/{page}/text")
def page_text(document_id: str, page: int) -> dict[str, Any]:
    with session_scope() as s:
        try:
            doc = service.get(s, document_id)
            return {"document_id": doc.id, "page": page, "text": service.page_text(doc, page)}
        except service.DocumentNotFound as e:
            raise HTTPException(404, "document not found") from e
        except service.PageOutOfRange as e:
            raise HTTPException(404, str(e)) from e
