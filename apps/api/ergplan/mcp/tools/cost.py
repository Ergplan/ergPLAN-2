"""What power costs today and what each source would land at: the evaluate screen's summary
(`GET /connections/{id}/today`) and the landed-cost screen (`GET /connections/{id}/landed-cost`).
Every figure is the SourcedValue the screen shows, with its source_refs."""

from __future__ import annotations

from datetime import date

from ...platform.routers import evaluate, landed
from ..registry import call, signed_in, tool


@tool(title="Today's cost of power", read_only=True)
async def todays_cost(connection_id: str, as_of: date | None = None) -> evaluate.TodayOut:
    """What the connection's year of power costs today: the approved tariff's energy and demand
    charges on its load profile, with FPPCA and duty at the rates its own bills print where the
    tariff order does not state them. as_of: the tariff in force on that date (default today)."""
    who = signed_in()
    return await call(lambda: evaluate.today(connection_id, who, as_of))


@tool(title="Landed cost by source", read_only=True)
async def landed_cost(
    connection_id: str,
    scenario: landed.ScenarioIn | None = None,
    sources: list[str] | None = None,
) -> landed.LandedOut:
    """The landed cost at the meter of each renewable source, per ToD slot, line by line (energy,
    wheeling, losses, cross-subsidy and additional surcharge, banking …), beside the grid rate it
    would avoid, and the regulatory limits that apply. scenario: as_of, month, GEOA, and the person's
    own price, ownership, capacity or CUF per source. sources: only these source ids in the answer
    (the screen's figures for them, unchanged)."""
    signed_in()
    body = None if scenario is None else scenario.model_dump_json(exclude_unset=True)
    out = await call(lambda: landed.connection_landed_cost(connection_id, body))
    if sources:
        out = out.model_copy(update={"sources": [x for x in out.sources if x.id in set(sources)]})
    return out
