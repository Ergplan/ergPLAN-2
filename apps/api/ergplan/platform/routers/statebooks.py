"""State rule books: the month's issue per DISCOM, for a reference industrial consumer or a client's
connection — landed cost by source and route, the saving on the grid tariff, how open-access energy
and exchange purchases are settled. An analyst generates, reviews and approves; a client sees only
approved issues, for their own connection or the DISCOM's reference consumer.

The book's `content` is returned as stored (report.statebook.build): its figures are SourcedValues
in JSON, each with its source_refs."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from ...db.base import session_scope
from ...db.models import Connection, StateRulebook
from ...report import statebook
from ...rulebook.service import UnknownJurisdiction
from ..auth import Caller, analyst, caller, owned_connection

router = APIRouter(tags=["state rule books"])
Analyst = Annotated[Caller, Depends(analyst)]
Anyone = Annotated[Caller, Depends(caller)]


class BookOut(BaseModel):
    id: str
    state: str
    discom_id: str
    connection_id: str | None
    month: date
    issue: int
    status: str
    title: str
    consumer: dict[str, Any]
    changes: int  # figures that differ from the last approved issue
    gaps: list[str]
    created_by: str
    created_at: datetime
    reviewed_by: str | None
    reviewed_at: datetime | None
    review_note: str | None

    @classmethod
    def of(cls, b: StateRulebook) -> BookOut:
        return cls(
            id=b.id,
            state=b.state,
            discom_id=b.discom_id,
            connection_id=b.connection_id,
            month=b.month,
            issue=b.issue,
            status=b.status,
            title=b.title,
            consumer=b.content.get("consumer") or {},
            changes=len(b.content.get("changes") or []),
            gaps=sorted((b.content.get("gaps") or {}).keys()),
            created_by=b.created_by,
            created_at=b.created_at,
            reviewed_by=b.reviewed_by,
            reviewed_at=b.reviewed_at,
            review_note=b.review_note,
        )


class BookDetail(BookOut):
    inputs: dict[str, Any]
    content: dict[str, Any]
    content_sha256: str

    @classmethod
    def of(cls, b: StateRulebook) -> BookDetail:
        return cls(
            **BookOut.of(b).model_dump(), inputs=b.inputs, content=b.content, content_sha256=b.content_sha256
        )


class GenerateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    discom: str = Field(max_length=32)
    month: date
    connection_id: str | None = None
    # the reference consumer, when no connection is named
    category: str | None = Field(default=None, max_length=64)
    voltage_kv: Decimal | None = Field(default=None, gt=0, le=765)
    contract_demand_kva: Decimal | None = Field(default=None, gt=0)
    # the PPA tariff to quote per source (INR/kWh); a source left out takes ergPLAN's benchmark
    prices: dict[str, Decimal] = Field(default_factory=dict)


class MonthIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    month: date
    clients: bool = True


class ReviewIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note: str | None = Field(default=None, max_length=2000)


@router.get("/statebooks", response_model=list[BookOut])
def books(
    who: Analyst,
    state: str | None = None,
    month: date | None = None,
    status: str | None = None,
) -> list[BookOut]:
    with session_scope() as s:
        q = select(StateRulebook)
        if state:
            q = q.where(StateRulebook.state == state)
        if month:
            q = q.where(StateRulebook.month == month.replace(day=1))
        if status:
            q = q.where(StateRulebook.status == status)
        q = q.order_by(
            StateRulebook.month.desc(),
            StateRulebook.state,
            StateRulebook.discom_id,
            StateRulebook.issue.desc(),
        )
        return [BookOut.of(b) for b in s.scalars(q.limit(500))]


@router.post("/statebooks/generate", response_model=BookDetail, status_code=201)
def generate(body: GenerateIn, who: Analyst) -> BookDetail:
    bad = [k for k in body.prices if k not in statebook.RE_SOURCES]
    if bad or any(v <= 0 for v in body.prices.values()):
        raise HTTPException(422, f"prices: a positive tariff for any of {', '.join(statebook.RE_SOURCES)}")
    with session_scope(actor=who.actor, role=who.role) as s:
        try:
            b = statebook.generate(
                s,
                actor=who.actor,
                discom=body.discom,
                month=body.month,
                connection_id=body.connection_id,
                category=body.category,
                voltage_kv=body.voltage_kv,
                contract_demand_kva=body.contract_demand_kva,
                prices=body.prices or None,
            )
        except (statebook.BookError, UnknownJurisdiction) as e:
            raise HTTPException(422, str(e)) from e
        return BookDetail.of(b)


@router.post("/statebooks/generate-month", response_model=list[BookOut], status_code=201)
def generate_month(body: MonthIn, who: Analyst) -> list[BookOut]:
    """The month's drafts for every DISCOM with open-access rules and every client connection."""
    with session_scope(actor=who.actor, role=who.role) as s:
        made = statebook.generate_month(s, month=body.month, actor=who.actor, clients=body.clients)
        return [BookOut.of(b) for b in made]


def _visible(s: Any, who: Caller, b: StateRulebook | None) -> StateRulebook:
    if b is None:
        raise HTTPException(404, "rule book not found")
    if who.role == "analyst" and who.signed_in:
        return b
    if b.status != "approved":
        raise HTTPException(404, "rule book not found")
    if b.connection_id is not None:
        owned_connection(s, who, b.connection_id)
    return b


@router.get("/statebooks/{book_id}", response_model=BookDetail)
def book(book_id: str, who: Anyone) -> BookDetail:
    with session_scope() as s:
        return BookDetail.of(_visible(s, who, s.get(StateRulebook, book_id)))


@router.get("/statebooks/{book_id}/file")
def book_file(book_id: str, who: Anyone) -> Response:
    """The rule book as a file (Markdown), every figure footnoted to its source."""
    with session_scope() as s:
        b = _visible(s, who, s.get(StateRulebook, book_id))
        name = f"rule-book-{b.state}-{b.discom_id}-{b.month:%Y-%m}-issue-{b.issue}.md"
        return Response(
            statebook.to_markdown(b),
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{name}"'},
        )


def _status(e: statebook.BookError) -> int:
    """Not found, a state that does not allow the action, or a request that cannot be carried out."""
    if isinstance(e, statebook.BookNotFound):
        return 404
    return 409 if isinstance(e, statebook.BookConflict) else 422


@router.post("/statebooks/{book_id}/approve", response_model=BookDetail)
def approve(book_id: str, body: ReviewIn, who: Analyst) -> BookDetail:
    with session_scope(actor=who.actor, role=who.role) as s:
        try:
            return BookDetail.of(
                statebook.approve(s, book_id, actor=who.actor, role=who.role, note=body.note)
            )
        except statebook.BookError as e:
            raise HTTPException(_status(e), str(e)) from e


@router.post("/statebooks/{book_id}/reject", response_model=BookDetail)
def reject(book_id: str, body: ReviewIn, who: Analyst) -> BookDetail:
    with session_scope(actor=who.actor, role=who.role) as s:
        try:
            return BookDetail.of(
                statebook.reject(s, book_id, actor=who.actor, role=who.role, note=body.note or "")
            )
        except statebook.BookError as e:
            raise HTTPException(_status(e), str(e)) from e


@router.get("/connections/{connection_id}/statebook", response_model=BookDetail)
def connection_book(connection_id: str, who: Anyone) -> BookDetail:
    """The rule book in force for a connection: its own latest approved issue, else the latest
    approved issue for its DISCOM's reference consumer."""
    with session_scope() as s:
        owned_connection(s, who, connection_id)
        conn = s.get(Connection, connection_id)
        if conn is None:
            raise HTTPException(404, "connection not found")
        b = statebook.last_approved(s, conn.discom_id, connection_id) or statebook.last_approved(
            s, conn.discom_id, None
        )
        if b is None:
            raise HTTPException(404, "no approved rule book for this connection or its DISCOM yet")
        return BookDetail.of(b)
