"""GET /tariffs/{discom_id}: a DISCOM's tariff as the rule books hold it (the "DISCOM tariffs" screen).

Read only. Approved versions in force on the date by default (invariant 2); `drafts=true` adds the
newest draft per jurisdiction, read through rulebook.sandbox, for an analyst only. Every figure is a
SourcedValue citing the rule field it restates; nothing is computed (report/tariff_book.py)."""

from __future__ import annotations

from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, JsonValue

from ...core.sourced import SourcedValue, SourceRef
from ...db.base import session_scope
from ...report import tariff_book
from ..auth import Caller, caller

router = APIRouter(prefix="/tariffs", tags=["tariffs"])

RULE_KINDS: list[JsonValue] = ["order", "regulation", "register", "assumption"]


class TariffFigureOut(BaseModel):
    key: str
    label: str
    value: SourcedValue = Field(json_schema_extra={"owner": "rules.body", "ref_kinds": RULE_KINDS})


class TariffSlotOut(BaseModel):
    name: str
    start: str
    end: str
    months: list[int]
    category: str | None
    ref: SourceRef
    figures: list[TariffFigureOut]


class TariffRowOut(BaseModel):
    family: str
    rule_id: str
    version_id: str
    status: str
    jurisdiction_id: str
    label: str | None
    scope: dict[str, Any]
    heading: str | None
    basis: str | None
    match_key: str
    figures: list[TariffFigureOut]
    slots: list[TariffSlotOut]
    points: list[dict[str, Any]]
    notes: list[str]
    nearer: str | None


class TariffVersionOut(BaseModel):
    id: str
    family: str
    jurisdiction_id: str
    jurisdiction_name: str
    version: int
    status: str
    origin: str
    title: str | None
    effective_from: date
    effective_to: date | None
    approved_by: str | None
    golden_passed: int
    golden_total: int


class TariffChainLink(BaseModel):
    id: str
    kind: str
    name: str


class TariffDocument(BaseModel):
    title: str | None
    url_public: str | None
    stored: bool


class TariffBookOut(BaseModel):
    discom_id: str
    discom_name: str
    state_code: str | None
    chain: list[TariffChainLink]
    on: date
    drafts: bool
    rows: list[TariffRowOut]
    versions: list[TariffVersionOut]
    documents: dict[str, TariffDocument]
    not_sourced: list[str]


def _fig(f: tariff_book.Figure) -> TariffFigureOut:
    return TariffFigureOut(key=f.key, label=f.label, value=f.value)


def _out(b: tariff_book.Book) -> TariffBookOut:
    return TariffBookOut(
        discom_id=b.discom_id,
        discom_name=b.discom_name,
        state_code=b.state_code,
        chain=[TariffChainLink(id=i, kind=k, name=n) for i, k, n in b.chain],
        on=b.on,
        drafts=b.drafts,
        rows=[
            TariffRowOut(
                family=r.family,
                rule_id=r.rule_id,
                version_id=r.version_id,
                status=r.status,
                jurisdiction_id=r.jurisdiction_id,
                label=r.label,
                scope=r.scope,
                heading=r.heading,
                basis=r.basis,
                match_key=r.match_key,
                figures=[_fig(f) for f in r.figures],
                slots=[
                    TariffSlotOut(
                        name=s.name,
                        start=s.start,
                        end=s.end,
                        months=s.months,
                        category=s.category,
                        ref=s.ref,
                        figures=[_fig(f) for f in s.figures],
                    )
                    for s in r.slots
                ],
                points=r.points,
                notes=r.notes,
                nearer=r.nearer,
            )
            for r in b.rows
        ],
        versions=[TariffVersionOut(**vars(v)) for v in b.versions],
        documents={k: TariffDocument(**v) for k, v in b.documents.items()},
        not_sourced=b.not_sourced,
    )


@router.get("/{discom_id}", response_model=TariffBookOut)
def tariff(
    discom_id: str, who: Annotated[Caller, Depends(caller)], on: date | None = None, drafts: bool = False
) -> TariffBookOut:
    """The DISCOM's tariff on a date (default today). Drafts only for an analyst (invariant 2)."""
    if drafts and not (who.role == "analyst" and who.signed_in):
        raise HTTPException(403, "drafts are shown to jouleWise analysts only")
    with session_scope() as s:
        try:
            book = tariff_book.build(s, discom_id, on or date.today(), drafts=drafts)
        except tariff_book.NotADiscom as e:
            raise HTTPException(404, f"no DISCOM {e}") from e
        return _out(book)
