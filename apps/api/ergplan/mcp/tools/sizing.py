"""The sizing and the plan: the plan screen's `POST /connections/{id}/plan` (the standard
scenarios) or the analyst's `POST /sites/{id}/scenarios` and `POST /scenarios/{id}/run` (a mix the
person names), `GET /runs/{id}` and `GET /connections/{id}/plan`. A solve is queued for the worker
and never held in the request: run_sizing answers "running" with run ids when it takes more than
a few seconds, and sizing_status reads each run."""

from __future__ import annotations

from datetime import date
from typing import Literal

import anyio
from pydantic import BaseModel

from ...optimizer.scenarios import ScenarioSpec
from ...platform.routers import intake as intake_screen
from ...platform.routers import plan as plan_screen
from ...platform.routers import runs as runs_screen
from ..registry import call, signed_in, tool

# how long run_sizing waits for the worker before answering "running" (the tests set it to 0)
QUICK_WAIT_S = 8.0
BUSY = ("queued", "running")

SizingStatus = Literal["running", "done", "failed"]


class SizingOut(BaseModel):
    status: SizingStatus
    run_ids: list[str]
    runs: list[runs_screen.RunOut]
    next_step: str


def _status(runs: list[runs_screen.RunOut]) -> SizingStatus:
    if any(r.status in BUSY for r in runs):
        return "running"
    return "done" if runs and all(r.status == "done" for r in runs) else "failed"


def _words(status: SizingStatus) -> str:
    if status == "running":
        return (
            "The runs are being solved on the worker (about a minute each at 60 minutes, several at 15). "
            "Call sizing_status with each run id until it is done; meanwhile ask any open question."
        )
    if status == "failed":
        return "A run failed: say so with its error; a run that failed re-settlement is never shown."
    return (
        "Done. Read the figures from each run's summary only, with their source kinds; say which are "
        "assumed. If a run's notes say it ended on its time limit, it is the best plan found, not "
        "proven cheapest. Then get_plan or transition_plan."
    )


async def _runs(run_ids: list[str]) -> list[runs_screen.RunOut]:
    who = signed_in()
    return [await call(lambda rid=rid: runs_screen.get_run(rid, who)) for rid in run_ids]  # type: ignore[misc]


@tool(title="Size the plan")
async def run_sizing(
    connection_id: str,
    spec: ScenarioSpec | None = None,
    name: str | None = None,
    as_of: date | None = None,
    resolution_min: Literal[15, 60] = 60,
) -> SizingOut:
    """Size renewable sources for the connection: the least-cost grid replacement the state's rules
    allow, re-settled by ergPLAN's settlement on the solver's dispatch. With no spec, the plan
    screen's standard scenarios; with spec, the mix the person names (candidates such as
    {"kind": "oa_solar_intra", "ownership": "captive", "max_mw": "8"}, as_of, resolution_min).
    Ask the open questions first. Answers "running" with run ids when the solve takes more than a few
    seconds; then call sizing_status."""
    who = signed_in()
    if spec is None:
        built = await call(lambda: plan_screen.build_plan(connection_id, who, as_of, resolution_min, False))
        run_ids = list(built.queued)
        if not run_ids:  # the standard scenarios were sized already: their latest runs
            plan = await call(lambda: plan_screen.plan_for_connection(connection_id, who))
            run_ids = [sc.run_id for sc in plan.scenarios]
    else:
        conn = await call(lambda: intake_screen.get_connection(connection_id))
        body = runs_screen.ScenarioIn(
            connection_id=connection_id, name=(name or "Sized in Claude")[:80], spec=spec
        )
        sc = await call(lambda: runs_screen.create_scenario(str(conn.site["id"]), body, who))
        run = await call(lambda: runs_screen.run_scenario(sc.id, who, False))
        run_ids = [run.id]
    waited = 0.0
    runs = await _runs(run_ids)
    while _status(runs) == "running" and waited < QUICK_WAIT_S:
        await anyio.sleep(1.0)
        waited += 1.0
        runs = await _runs(run_ids)
    status = _status(runs)
    return SizingOut(status=status, run_ids=run_ids, runs=runs, next_step=_words(status))


@tool(title="Status of a sizing run", read_only=True)
async def sizing_status(run_id: str) -> runs_screen.RunOut:
    """One run of run_sizing: its status (queued, running, done, failed, failed_resettle) and, once
    done, its summary: sizes, the year's cost before and after, the saving, the share of grid energy
    replaced, the binding rules and notes, every figure with its sources."""
    who = signed_in()
    return await call(lambda: runs_screen.get_run(run_id, who))


@tool(title="The plan", read_only=True)
async def get_plan(connection_id: str) -> plan_screen.PlanOut:
    """The connection's plan as the plan page shows it: each sized scenario's settled summary, the
    recommended one, the assumptions, what the client stated, the provenance of the load profile and
    the documents cited. pending_runs counts runs still being solved."""
    who = signed_in()
    return await call(lambda: plan_screen.plan_for_connection(connection_id, who))
