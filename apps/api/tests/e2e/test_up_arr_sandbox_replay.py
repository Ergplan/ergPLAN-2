"""Stage 14 end to end for Uttar Pradesh, offline: the four UPERC orders of the ARR Network (state
DISCOMs and NPCL, FY 2025-26 and FY 2026-27) imported as drafts from trimmed copies of the real
section pages, and the twelve assumed PVVNL bills of test_up_assumed.py (November 2025 to October
2026: two tariff years) rebuilt from those drafts through `rulebook.sandbox`. Nothing here is
approved and no client endpoint is involved: the approved rulebook still answers eight of the
twelve months with 'no tariff in force'."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal as D
from typing import Any

from ergplan.core.sourced import RefKind, SourcedValue, SourceRef
from ergplan.core.units import Unit
from ergplan.db.base import session_scope
from ergplan.engine import replay
from ergplan.engine import scope as escope
from ergplan.regreader.arr_import import ORIGIN
from ergplan.rulebook import repository, sandbox, service
from tests.e2e.test_up_assumed import CD, MONTHS, assumed_bill, hand_bill
from tests.engine.helpers import restore_committed
from tests.regreader.arr_helpers import import_up

FACTS = escope.ConnectionFacts(
    discom="pvvnl", state="UP", category="HV-2", voltage_kv=D("33"), connection_kind="ht", rate_block="urban"
)
CONTRACT = SourcedValue(
    value=CD, unit=Unit.KVA, source_refs=(SourceRef(kind=RefKind.CLIENT_STATED, note="assumed bill"),)
)


def _period_end(first: date) -> date:
    return (first.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)


def sandbox_replay(first: date, factor: str) -> tuple[replay.ReplayReport, str | None]:
    """One assumed bill rebuilt from the imported drafts in force at the end of its supply period
    (the date the product replays a bill on), and the title of the draft schedule used."""
    when = _period_end(first)
    bill = assumed_bill(first, factor)["bills"][0]
    with session_scope() as s:
        chain = service.jurisdiction_chain(s, FACTS.discom)
        ctx = escope.context(FACTS, when)
        schedule = escope.first(
            sandbox.drafts_in_force(s, "tariff_schedule", chain, when, ORIGIN), ctx, chain
        )
        tod = escope.first(sandbox.drafts_in_force(s, "tod_slots", chain, when, ORIGIN), ctx, chain)
        title = None
        if schedule is not None:
            from ergplan.db.models import RulebookVersion

            v = s.get(RulebookVersion, schedule.version_id)
            assert v is not None and v.status == "draft"
            title = v.title
    return replay.replay(f"assumed-{first:%Y-%m}", bill, {}, when, schedule, tod, CONTRACT), title


def _line(r: replay.ReplayReport, group: str) -> Any:
    [ln] = [x for x in r.lines if x.group == group]
    return ln


def test_twelve_assumed_bills_replay_across_both_tariff_years_in_the_sandbox() -> None:
    restore_committed()
    with session_scope() as s:
        chain = service.jurisdiction_chain(s, "pvvnl")
        approved_before = {
            m: [r.id for r in repository.approved_rules(s, "tariff_schedule", chain, _period_end(m))]
            for m, _ in MONTHS
        }
    # today, on approved rules only: the eight months before the FY 2026-27 order have no tariff
    assert [bool(v) for v in approved_before.values()] == [False] * 8 + [True] * 4

    import_up()

    used: dict[str, int] = {}
    for first, factor in MONTHS:
        r, title = sandbox_replay(first, factor)
        hand = hand_bill(first, factor)
        assert title is not None, f"{first:%b %Y}: no imported draft in force"
        year = "FY2025-26" if _period_end(first) < date(2026, 7, 2) else "FY2026-27"
        assert year in title and "ARR Network order" in title
        used[year] = used.get(year, 0) + 1
        assert r.status != replay.NO_TARIFF
        # energy and time-of-day charges are rebuilt from the imported tables to within the replay's
        # ₹1 (the bill's ToD units are whole kWh, so under a rupee of rounding remains on ₹11–15 million)
        energy = _line(r, "energy")
        hand_energy = hand["energy_charge"] + hand["tod_surcharge"] + hand["tod_rebate"]
        assert energy.status == "match" and energy.billed == hand_energy
        assert abs(energy.difference) <= replay.TOLERANCE
        # the demand charge is not: the bill is on 75% of contract demand (3,750 kVA), a rule the order
        # states in words (general provisions), which no table holds; the draft bills the recorded
        # 3,250 kVA and the replay says which rule is missing instead of calling the bill wrong
        demand = _line(r, "demand")
        assert demand.status == "explained" and demand.gap == "billing_demand_rule"
        assert demand.computed.value == D("290") * D("3250") and demand.billed == D("290") * D("3750")
        assert demand.difference == D("-145000.00")
        # every value rebuilt cites the order page it was read from
        refs = [x for x in energy.computed.source_refs if x.kind == RefKind.ORDER]
        assert refs and all(x.document_id and x.page and x.quote for x in refs)
        assert not [ln for ln in r.lines if ln.status == "unexplained"]
        print(
            f"{first:%b %Y} | {year} | {title.split(' · ')[-1]} | energy+ToD billed {energy.billed} computed "
            f"{energy.computed.value} diff {energy.difference} {energy.status} | demand billed {demand.billed} "
            f"computed {demand.computed.value} diff {demand.difference} {demand.status} ({demand.gap}) | "
            + "; ".join(
                f"{ln.group}: {ln.status}" + (f" ({ln.gap})" if ln.gap else "")
                for ln in r.lines
                if ln.group not in ("energy", "demand")
            )
            + f" | overall {r.status}"
        )
    assert used == {"FY2025-26": 8, "FY2026-27": 4}

    # nothing imported reaches a client: the approved rules of every month are what they were
    with session_scope() as s:
        for m, _ in MONTHS:
            now = [r.id for r in repository.approved_rules(s, "tariff_schedule", chain, _period_end(m))]
            assert now == approved_before[m]
