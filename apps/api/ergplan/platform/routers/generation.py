"""Generation profiles supplied for a site (Stage 15): upload, list, read, withdraw.

The CUF a profile implies is a SourcedValue whose ref names the file: `measured` when the profile
comes from the plant's meter, `client_stated` when the developer stated it. A refused file answers
422 with the reason in plain words (`intake.generation_profiles`).
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field, JsonValue
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...core.sourced import RefKind, SourcedValue, SourceRef
from ...core.units import Unit, convert
from ...db.base import session_scope
from ...db.models import GenerationProfile, Site
from ...intake import generation, generation_profiles
from ..auth import Caller, caller, may_reach, production
from ..ratelimit import upload_limit

router = APIRouter(tags=["generation"])

MAX_BYTES = 20 * 1024 * 1024
OWNER = "generation_profiles.series_per_kw"
KINDS: list[JsonValue] = ["measured", "client_stated"]


def _cuf() -> Any:
    return Field(json_schema_extra={"owner": OWNER, "ref_kinds": KINDS})


class MonthCuf(BaseModel):
    month: int  # 1 = January
    cuf: SourcedValue = _cuf()


class RunUse(BaseModel):
    run_id: str
    scenario_id: str
    scenario_name: str
    status: str
    created_at: datetime
    replay_of: str | None


class GenerationProfileOut(BaseModel):
    id: str
    site_id: str
    version: int
    technology: str
    applies_to: str
    provenance: str
    filename: str
    sha256: str
    resolution_min: int
    first_day: date
    blocks: int
    unit_in_file: str
    basis: str
    capacity_mw: str | None
    in_use: bool  # new runs for the site generate with it
    withdrawn_at: datetime | None
    created_by: str
    created_at: datetime
    annual_cuf: SourcedValue = _cuf()
    peak: SourcedValue = _cuf()  # the highest block, percent of capacity
    monthly_cuf: list[MonthCuf]
    # the annual CUF of the library curve this profile replaces, for comparison (an assumption)
    library_cuf: SourcedValue | None = Field(
        default=None, json_schema_extra={"owner": "packages/profiles/cuf", "ref_kinds": ["assumption"]}
    )
    runs: list[RunUse]


def _out(s: Session, row: GenerationProfile, in_use_ids: set[str], state: str) -> GenerationProfileOut:
    ref = (generation_profiles.source_ref(row),)

    def pct(v: Any, label: str) -> SourcedValue:
        return SourcedValue(value=Decimal(str(v)), unit=Unit.PERCENT, source_refs=ref, label=label)

    library = SourcedValue(
        value=generation.annual_cuf(generation.curve_for(row.technology, state)),
        unit=Unit.PERCENT,
        source_refs=(
            SourceRef(
                kind=RefKind.ASSUMPTION,
                note=f"normative {row.technology} curve for {state} (packages/profiles/cuf)",
            ),
        ),
        label="CUF of the library curve",
    )
    return GenerationProfileOut(
        id=row.id,
        site_id=row.site_id,
        version=row.version,
        technology=row.technology,
        applies_to=row.applies_to,
        provenance=row.provenance,
        filename=row.filename,
        sha256=row.sha256,
        resolution_min=row.resolution_min,
        first_day=row.first_day,
        blocks=row.blocks,
        unit_in_file=row.unit_in_file,
        basis=row.basis,
        capacity_mw=(
            format(convert(Decimal(row.capacity_kw), Unit.KW, Unit.MW).normalize(), "f")
            if row.capacity_kw is not None
            else None
        ),
        in_use=row.id in in_use_ids,
        withdrawn_at=row.withdrawn_at,
        created_by=row.created_by,
        created_at=row.created_at,
        annual_cuf=pct(Decimal(row.annual_cuf_pct).quantize(Decimal("0.01")), "Annual CUF"),
        peak=pct((row.method or {}).get("peak_pct", "0"), "Highest block, share of capacity"),
        monthly_cuf=[
            MonthCuf(month=int(m), cuf=pct(v, "CUF of the month"))
            for m, v in sorted((row.monthly_cuf_pct or {}).items(), key=lambda kv: int(kv[0]))
        ],
        library_cuf=library,
        runs=[
            RunUse(
                run_id=run.id,
                scenario_id=sc.id,
                scenario_name=sc.name,
                status=run.status,
                created_at=run.created_at,
                replay_of=run.replay_of,
            )
            for run, sc in generation_profiles.runs_using(s, row)
        ],
    )


def _in_use_ids(s: Session, site_id: str) -> set[str]:
    return {r.id for r in generation_profiles.in_use(s, site_id).values()}


@router.post(
    "/sites/{site_id}/generation-profiles",
    response_model=GenerationProfileOut,
    status_code=201,
    dependencies=[Depends(upload_limit)],
)
async def upload_generation_profile(
    site_id: str,
    who: Annotated[Caller, Depends(caller)],
    file: Annotated[UploadFile, File()],
    technology: Annotated[Literal["solar", "wind"], Form()],
    provenance: Annotated[Literal["measured", "stated"], Form()],
    applies_to: Annotated[Literal["open_access", "rooftop"], Form()] = "open_access",
    basis: Annotated[Literal["per_mw", "capacity"] | None, Form()] = None,
    capacity_mw: Annotated[Decimal | None, Form(gt=0)] = None,
    first_day: Annotated[date | None, Form()] = None,
) -> GenerationProfileOut:
    """A year of solar or wind generation as CSV. Refused (422) with the reason when it is not a
    whole year, has a gap, does not say its unit, or is not physically possible."""
    data = await file.read()
    if len(data) > MAX_BYTES:
        raise HTTPException(422, "the file is larger than 20 MB; a year at 15 minutes is about 1 MB")
    with session_scope(actor=who.actor, role=who.role) as s:
        site = s.get(Site, site_id)
        if site is None:
            raise HTTPException(404, "site not found")
        try:
            parsed = generation_profiles.parse(data, technology, basis, capacity_mw, first_day)
            row = generation_profiles.store(
                s,
                site_id,
                parsed,
                applies_to=applies_to,
                provenance=provenance,
                filename=file.filename or "generation.csv",
                sha256=generation_profiles.sha256_of(data),
                actor=who.actor,
            )
        except generation_profiles.GenerationFileError as e:
            raise HTTPException(422, str(e)) from e
        return _out(s, row, _in_use_ids(s, site_id), site.state)


@router.get("/sites/{site_id}/generation-profiles", response_model=list[GenerationProfileOut])
def list_generation_profiles(site_id: str) -> list[GenerationProfileOut]:
    with session_scope() as s:
        site = s.get(Site, site_id)
        if site is None:
            raise HTTPException(404, "site not found")
        rows = s.scalars(
            select(GenerationProfile)
            .where(GenerationProfile.site_id == site_id)
            .order_by(GenerationProfile.version.desc())
        ).all()
        ids = _in_use_ids(s, site_id)
        return [_out(s, r, ids, site.state) for r in rows]


def _owned(s: Session, who: Caller, profile_id: str) -> tuple[GenerationProfile, Site]:
    row = s.get(GenerationProfile, profile_id)
    if row is None:
        raise HTTPException(404, "generation profile not found")
    if production() and not may_reach(s, who, "site_id", row.site_id):
        raise HTTPException(403, "this belongs to another client")
    site = s.get(Site, row.site_id)
    assert site is not None
    return row, site


@router.get("/generation-profiles/{profile_id}", response_model=GenerationProfileOut)
def get_generation_profile(profile_id: str, who: Annotated[Caller, Depends(caller)]) -> GenerationProfileOut:
    with session_scope() as s:
        row, site = _owned(s, who, profile_id)
        return _out(s, row, _in_use_ids(s, row.site_id), site.state)


@router.post("/generation-profiles/{profile_id}/withdraw", response_model=GenerationProfileOut)
def withdraw_generation_profile(
    profile_id: str, who: Annotated[Caller, Depends(caller)]
) -> GenerationProfileOut:
    """Stop using the profile in new runs (the library curve, or an earlier profile still in use,
    takes its place). Runs that used it keep their pin and still replay."""
    with session_scope(actor=who.actor, role=who.role) as s:
        row, site = _owned(s, who, profile_id)
        generation_profiles.withdraw(s, row, who.actor)
        return _out(s, row, _in_use_ids(s, row.site_id), site.state)
