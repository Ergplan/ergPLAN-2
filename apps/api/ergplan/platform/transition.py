"""The transition plan's service (Stage 15): it makes the plan's runs in the order
`report.transition.decide` gives, one ordinary scenario and run each (solved by the worker, settled
again by the judge, pinned), and reads the page from their results (`report.transition.build`).
Nothing is computed here.

`transition_plan` is the one call a screen, the worker or the MCP tool of the same name needs: it
starts a connection's plan for a target (or returns the one already made for that target), moves it
on, and returns the page as it stands."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.sourced import SourcedValue
from ..core.units import Unit
from ..db.base import session_scope
from ..db.models import Connection, Jurisdiction, Run, RunResult, Scenario, Site, TransitionPlan
from ..engine import scope as escope
from ..report import transition as tp
from ..report.plan import TRANSITION_PREFIX
from ..rulebook.models import RuleView
from ..rulebook.repository import Reader, approved_rules
from ..rulebook.scope import matches
from ..rulebook.service import jurisdiction_chain
from . import runs as svc
from .routers.cost import _facts

# scenarios of a transition plan carry this prefix: they are the plan's own and are kept out of the
# list of options the live plan compares (report.plan)
PREFIX = TRANSITION_PREFIX


class PlanNotFound(LookupError):
    pass


def _target(plan: TransitionPlan) -> tp.Target:
    return tp.Target.model_validate(plan.target)


def _routes(target: tp.Target) -> list[str]:
    return [target.ownership] if target.ownership else list(tp.ROUTES)


def _steps(s: Session, plan: TransitionPlan) -> list[tp.Step]:
    out = []
    for row in plan.steps or []:
        run = s.get(Run, row["run_id"])
        if run is None:
            continue
        rr = s.get(RunResult, run.id)
        out.append(
            tp.Step(
                key=row["key"],
                role=row["role"],
                route=row.get("route"),
                level_pct=Decimal(row["level_pct"]) if row.get("level_pct") is not None else None,
                status=run.status,
                summary=rr.summary if rr is not None and run.status == "done" else None,
                solver=run.solver or {},
                error=run.error,
                run_id=run.id,
                scenario_id=run.scenario_id,
                resolution_min=run.resolution_min,
            )
        )
    return out


def _pins(s: Session, plan: TransitionPlan) -> list[str] | None:
    """The rule versions the plan's latest finished run used, so the page restates the rules its own
    runs were settled under (None before any run has finished: the approved rules of the day)."""
    for row in reversed(plan.steps or []):
        run = s.get(Run, row["run_id"])
        if run is not None and run.status == "done" and (run.pins or {}).get("rulebook_versions"):
            return list(run.pins["rulebook_versions"])
    return None


def book(s: Session, conn: Connection, as_of: date, pins: list[str] | None = None) -> tp.Book:
    """What the state's approved rule book says for this connection that the plan restates: the
    settlement rule of each open-access route, the regulatory limits and the rooftop rows. Approved
    versions only (invariant 2)."""
    site = s.get(Site, conn.site_id)
    assert site is not None
    facts = _facts(s, conn)
    chain = jurisdiction_chain(s, facts.discom)
    ctx0 = escope.context(facts, date(as_of.year, as_of.month, 1))
    reader = Reader(pins)
    settlements = reader(s, "settlement", chain, as_of)

    def covers(route: str) -> RuleView | None:
        for source in ("solar", "wind"):
            ctx = {**ctx0, "connection": "intra_state", "source": source, "ownership": route}
            hit = next((r for r in settlements if matches(r.scope, ctx)), None)
            if hit is not None:
                return hit
        return None

    limits: list[tuple[str, SourcedValue]] = []
    for r in reader(s, "regulatory_limit", chain, as_of):
        if not matches(r.scope, ctx0):
            continue
        kind, v = str(r.body["limit"]), Decimal(str(r.body["value"]))
        unit = Unit.PERCENT if r.body.get("unit") == "percent" else Unit.COUNT
        limits.append(
            (
                svc._limit_words(kind, v, r),
                SourcedValue(value=v, unit=unit, source_refs=(r.ref("/value"),), label=r.label or kind),
            )
        )
    net = tuple(r for r in approved_rules(s, "net_metering", chain, as_of) if matches(r.scope, ctx0))
    state = s.get(Jurisdiction, site.state)
    return tp.Book(
        state=state.name if state is not None else site.state,
        discom=conn.discom_id,
        settlement={route: covers(route) for route in tp.ROUTES},
        limits=tuple(limits),
        net_metering=net,
    )


def _book_of(s: Session, plan: TransitionPlan) -> tp.Book:
    conn = s.get(Connection, plan.connection_id)
    assert conn is not None
    as_of = _target(plan).as_of or date.today()
    return book(s, conn, as_of, _pins(s, plan))


def advance(s: Session, plan_id: str) -> list[str]:
    """Make the runs the plan needs next (none while runs are being solved) and close the plan when
    it has them all. Safe to call from several workers at once: the plan's row is locked."""
    plan = s.get(TransitionPlan, plan_id, with_for_update=True)
    if plan is None:
        raise PlanNotFound(plan_id)
    if plan.status != "running":
        return []
    conn = s.get(Connection, plan.connection_id)
    assert conn is not None
    target = _target(plan)
    bk = _book_of(s, plan)
    steps = _steps(s, plan)
    new = tp.decide(target, _routes(target), steps, bk.settlement)
    if new is None:
        plan.status = "done" if tp.ladder(steps, bk.settlement) else "failed"
        plan.finished_at = datetime.now(UTC)
        return []
    queued = []
    rows = list(plan.steps or [])
    for n in new:
        sc = Scenario(
            site_id=conn.site_id,
            connection_id=conn.id,
            name=f"{PREFIX}{plan.id[:8]} · {n.key}",
            spec=n.spec.model_dump(mode="json"),
            created_by=plan.created_by,
        )
        s.add(sc)
        s.flush()
        run = svc.create_run(s, sc, plan.created_by)
        rows.append(
            {
                "key": n.key,
                "role": n.role,
                "route": n.route,
                "level_pct": str(n.level_pct) if n.level_pct is not None else None,
                "scenario_id": sc.id,
                "run_id": run.id,
            }
        )
        queued.append(run.id)
    plan.steps = rows
    return queued


def latest(s: Session, connection_id: str) -> TransitionPlan | None:
    return s.scalars(
        select(TransitionPlan)
        .where(TransitionPlan.connection_id == connection_id)
        .order_by(TransitionPlan.created_at.desc(), TransitionPlan.id)
    ).first()


def start(
    s: Session, connection_id: str, target: tp.Target, actor: str, fresh: bool = False
) -> TransitionPlan:
    """The connection's plan for this target: the latest one if it was made for the same target and
    did not fail (asking again does not solve again), else a new one."""
    conn = s.get(Connection, connection_id)
    if conn is None:
        raise PlanNotFound(connection_id)
    target = target.model_copy(update={"as_of": target.as_of or date.today()})
    asked = target.model_dump(mode="json")
    have = latest(s, connection_id)
    if have is not None and not fresh and have.status != "failed" and have.target == asked:
        return have
    plan = TransitionPlan(connection_id=conn.id, target=asked, status="running", steps=[], created_by=actor)
    s.add(plan)
    s.flush()
    return plan


def view(s: Session, plan: TransitionPlan) -> dict[str, Any]:
    """The plan's page as it stands (report.transition.build) with what identifies it."""
    out = tp.build(_target(plan), _steps(s, plan), _book_of(s, plan), plan.status)
    return {
        "id": plan.id,
        "connection_id": plan.connection_id,
        "created_at": plan.created_at,
        "finished_at": plan.finished_at,
        "asked": plan.target,
        **out,
    }


def run_to_end(plan_id: str, actor: str, role: str) -> None:
    """Solve the plan in this process, run by run (tests and `wait`); the worker does the same one
    run at a time through `on_run_finished`."""
    while True:
        with session_scope(actor=actor, role=role) as s:
            plan = s.get(TransitionPlan, plan_id)
            if plan is None or plan.status != "running":
                return
            queued = advance(s, plan_id)
            waiting = [
                row["run_id"]
                for row in (plan.steps or [])
                if (r := s.get(Run, row["run_id"])) is not None and r.status in ("queued", "running")
            ]
        for rid in queued or waiting:
            svc.execute(rid)


def on_run_finished(run_id: str) -> None:
    """The worker's hook: when a run of a transition plan finishes, make the plan's next runs."""
    with session_scope(actor=svc.WORKER) as s:
        ids = [
            p.id
            for p in s.scalars(select(TransitionPlan).where(TransitionPlan.status == "running"))
            if any(row.get("run_id") == run_id for row in (p.steps or []))
        ]
    for pid in ids:
        with session_scope(actor=svc.WORKER) as s:
            advance(s, pid)


def transition_plan(
    connection_id: str,
    target: tp.Target,
    actor: str,
    role: str = "client",
    wait: bool = False,
    fresh: bool = False,
) -> dict[str, Any]:
    """Start (or find) the connection's transition plan for a target, move it on, and return its page.
    `wait`: solve every run before returning. Raises PlanNotFound for an unknown connection."""
    with session_scope(actor=actor, role=role) as s:
        plan = start(s, connection_id, target, actor, fresh)
        plan_id = plan.id
        advance(s, plan_id)
    if wait:
        run_to_end(plan_id, actor, role)
    with session_scope() as s:
        made = s.get(TransitionPlan, plan_id)
        assert made is not None
        return view(s, made)


def replay(plan_id: str, actor: str, role: str, wait: bool = False) -> dict[str, Any]:
    """Recompute every finished run of the plan from its pins (invariant 5). Each is an ordinary
    replay run; `identical` says whether its result matches the original to the paisa."""
    made: list[tuple[str, str, str]] = []
    with session_scope(actor=actor, role=role) as s:
        plan = s.get(TransitionPlan, plan_id)
        if plan is None:
            raise PlanNotFound(plan_id)
        for row in plan.steps or []:
            orig = s.get(Run, row["run_id"])
            if orig is None or orig.status != "done" or not orig.pins:
                continue
            sc = s.get(Scenario, orig.scenario_id)
            assert sc is not None
            made.append((row["key"], orig.id, svc.create_run(s, sc, actor, replay_of=orig).id))
    if wait:
        for _, _, rid in made:
            svc.execute(rid)
    out = []
    with session_scope() as s:
        for key, orig_id, rid in made:
            run, a, b = s.get(Run, rid), s.get(RunResult, orig_id), s.get(RunResult, rid)
            assert run is not None
            out.append(
                {
                    "key": key,
                    "run_id": orig_id,
                    "replay_run_id": rid,
                    "status": run.status,
                    "identical": (a is not None and b is not None and a.digest == b.digest)
                    if run.status == "done"
                    else None,
                }
            )
    settled = [r["identical"] for r in out]
    return {
        "plan_id": plan_id,
        "runs": out,
        "identical": all(settled) if settled and all(x is not None for x in settled) else None,
    }
