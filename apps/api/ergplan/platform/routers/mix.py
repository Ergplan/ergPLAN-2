"""The client's own mix (journey step 5): the sources a consumer can choose (open-access solar, wind
and hybrid, intra-state or ISTS; rooftop solar and a battery behind the meter; purchases on the
exchange, segment by segment), and a scenario of their choice sized at least cost over a year of
15-minute blocks. The consumer only buys on the exchange; nothing is sold."""

from __future__ import annotations

from datetime import date
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select

from ...db.base import session_scope
from ...db.models import Connection, PriceVintage, Run, Scenario
from ...engine import lcoe
from ...engine.landed_cost import SOURCE_BY_ID
from ...optimizer.scenarios import SIZEABLE, CandidateSpec, FinanceSpec, ScenarioSpec
from .. import runs as svc
from ..auth import Caller, caller, owned_connection
from .landed import DEFAULT_PRICE

router = APIRouter(tags=["mix"])
MIX_NAME = "Your mix"

GROUP = {
    "solar": "Solar",
    "wind": "Wind",
    "hybrid": "Solar-wind hybrid",
    "iex": "Power exchange (buy only)",
    "behind_meter": "At your site",
    "bess": "Storage at the open-access plant",
}
# What the consumer may state for each kind beside its target, with ergPLAN's assumption shown as the
# placeholder (engine.lcoe defaults): key → the default as text
_ROOF, _BESS = lcoe.default_rooftop(), lcoe.default_bess()
_STORAGE_INPUTS = {
    "capex_inr_per_kwh": str(_BESS.capex_inr_per_kwh.value),
    "om_pct_of_capex": str(_BESS.om_pct_of_capex.value),
    "life_years": str(_BESS.life_years.value),
    "round_trip_pct": str(_BESS.round_trip_pct.value),
    "min_hours": "1",
    "max_hours": "4",
}
INPUTS: dict[str, dict[str, str]] = {
    "rooftop_pv": {
        "capex_inr_per_kw": str(_ROOF.capex_inr_per_kw.value),
        "om_pct_of_capex": str(_ROOF.om_pct_of_capex.value),
        "cuf_pct": str(_ROOF.cuf_pct.value),
        "life_years": str(_ROOF.life_years.value),
        "degradation_pct": str(_ROOF.degradation_pct.value),
    },
    "btm_bess": _STORAGE_INPUTS,
    "oa_bess": _STORAGE_INPUTS,
}


def _plant_inputs(source: str) -> dict[str, str]:
    """An open-access plant: its CUF always (blank: the state's curve); and, when it is taken as
    captive (the consumer's own) or with 26% equity, the plant's benchmark cost (engine.lcoe)."""
    if source not in lcoe.UTILITY:
        return {"cuf_pct": ""}
    g = lcoe.default_utility(source)
    return {
        "cuf_pct": "",
        "capex_inr_per_kw": str(g.capex_inr_per_kw.value),
        "om_pct_of_capex": str(g.om_pct_of_capex.value),
        "life_years": str(g.life_years.value),
        "degradation_pct": str(g.degradation_pct.value),
        "equity_share_pct": str(lcoe.default_equity_share().value),
    }


class SourceOption(BaseModel):
    kind: str
    label: str
    group: str
    connection: str  # intra_state | ists | behind_meter
    price_kind: str  # ppa | iex | lcoe | lcos
    default_price_inr_per_kwh: str | None
    price_note: str | None
    ownerships: list[str]
    available: bool
    reason: str | None = None
    # cost and yield inputs the consumer may state for this kind → ergPLAN's default (an assumption)
    inputs: dict[str, str] = Field(default_factory=dict)


class MixIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(default=MIX_NAME, min_length=1, max_length=80)
    sources: list[CandidateSpec] = Field(min_length=1, max_length=8)
    resolution_min: Literal[15, 60] = 60
    as_of: date | None = None
    finance: FinanceSpec | None = None

    @model_validator(mode="after")
    def one_each_on_site(self) -> MixIn:
        kinds = [c.kind for c in self.sources]
        for k in ("btm_bess", "oa_bess", "rooftop_pv"):
            if kinds.count(k) > 1:
                raise ValueError(f"at most one {SOURCE_BY_ID[k].label.lower()}")
        if (
            "oa_bess" in kinds
            and not any(k.startswith("oa_") and k != "oa_bess" for k in kinds)
            and ("group_captive" not in kinds)
        ):
            raise ValueError(
                "a battery at the open-access plant needs an open-access solar, wind or hybrid plant"
            )
        if all(k == "btm_bess" for k in kinds):
            raise ValueError("a battery needs a source of energy beside it (or the grid's off-peak hours)")
        if len(set(kinds)) != len(kinds):
            raise ValueError("each source once; set its largest size with max_mw")
        return self


class MixOut(BaseModel):
    scenario_id: str
    name: str
    run_id: str | None
    status: str


def _segments_loaded() -> set[str]:
    with session_scope() as s:
        return {
            v.segment for v in s.scalars(select(PriceVintage).where(PriceVintage.superseded_by.is_(None)))
        }


@router.get("/mix/options", response_model=list[SourceOption])
def mix_options_any() -> list[SourceOption]:
    """The sources any client may choose (the Evaluate page, before a connection exists)."""
    return _options()


@router.get("/connections/{connection_id}/mix/options", response_model=list[SourceOption])
def mix_options(connection_id: str, who: Annotated[Caller, Depends(caller)]) -> list[SourceOption]:
    with session_scope(actor=who.actor) as s:
        owned_connection(s, who, connection_id)
        if s.get(Connection, connection_id) is None:
            raise HTTPException(404, "connection not found")
    return _options()


def _options() -> list[SourceOption]:
    loaded = _segments_loaded()
    out = []
    for kind in SIZEABLE:
        src = SOURCE_BY_ID[kind]
        group = "behind_meter" if src.connection == "behind_meter" else src.source
        price = DEFAULT_PRICE.get(kind)
        available, reason = True, None
        if src.price_kind == "iex" and src.iex_segment not in loaded:
            available = False
            reason = f"no {str(src.iex_segment).upper()} clearing prices uploaded yet"
        out.append(
            SourceOption(
                kind=kind,
                label=src.label,
                group=GROUP.get(group, group),
                connection=src.connection,
                price_kind=src.price_kind,
                default_price_inr_per_kwh=price[0] if price else None,
                price_note=price[1] if price else None,
                ownerships=(["captive", "group_captive", "third_party"] if src.price_kind == "ppa" else []),
                available=available,
                reason=reason,
                inputs=INPUTS.get(kind, _plant_inputs(src.source) if src.price_kind == "ppa" else {}),
            )
        )
    return out


@router.post("/connections/{connection_id}/mix", response_model=MixOut, status_code=202)
def size_mix(
    connection_id: str,
    body: MixIn,
    who: Annotated[Caller, Depends(caller)],
    wait: Annotated[bool, Query(description="solve in the request (tests)")] = False,
) -> MixOut:
    """Size the client's chosen sources at least cost over a year (one scenario per name; asking
    again with other sources replaces its spec, and every earlier run keeps its own)."""
    loaded = _segments_loaded()
    for c in body.sources:
        src = SOURCE_BY_ID[c.kind]
        if src.price_kind == "iex" and src.iex_segment not in loaded:
            raise HTTPException(422, f"no {str(src.iex_segment).upper()} clearing prices uploaded yet")
    spec = ScenarioSpec(
        candidates=body.sources,
        as_of=body.as_of or date.today(),
        resolution_min=body.resolution_min,
        finance=body.finance,
    )
    with session_scope(actor=who.actor, role=who.role) as s:
        owned_connection(s, who, connection_id)
        conn = s.get(Connection, connection_id)
        if conn is None:
            raise HTTPException(404, "connection not found")
        sc = s.scalars(
            select(Scenario).where(Scenario.connection_id == conn.id, Scenario.name == body.name)
        ).first()
        if sc is None:
            sc = Scenario(
                site_id=conn.site_id,
                connection_id=conn.id,
                name=body.name,
                spec=spec.model_dump(mode="json"),
                created_by=who.actor,
            )
            s.add(sc)
            s.flush()
        else:
            sc.spec = spec.model_dump(mode="json")
        busy = s.scalars(
            select(Run).where(Run.scenario_id == sc.id, Run.status.in_(("queued", "running")))
        ).first()
        run_id = busy.id if busy is not None and busy.spec == sc.spec else svc.create_run(s, sc, who.actor).id
        sc_id, name = sc.id, sc.name
    if wait:
        svc.execute(run_id)
    with session_scope() as s:
        run = s.get(Run, run_id)
        status = run.status if run else "queued"
    return MixOut(scenario_id=sc_id, name=name, run_id=run_id, status=status)
