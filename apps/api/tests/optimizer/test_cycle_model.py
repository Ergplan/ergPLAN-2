"""The cycle settlement (`method: cycle_pooled`, the erg_plan 2.0 engine) inside the optimizer: tiny
models with optima worked by hand, and on each the judge (settlement.cycle through simulate) agrees
with the solver's own settlement (invariant 6)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from ergplan.optimizer.highs_solve import Solution, solve
from ergplan.optimizer.model import BlockMeta, Candidate, Grid, Problem
from ergplan.optimizer.resettle import resettle
from ergplan.rulebook.models import RuleView

APR, MAY = date(2026, 4, 1), date(2026, 5, 1)


def rule(**banking: Any) -> dict[str, Any]:
    b = {"allowed": True, "period": "monthly", "charge": {"kind": "in_kind_percent", "value": "10"}}
    b.update(banking)
    gran = b.pop("granularity", "block_15min")
    return {"method": "cycle_pooled", "granularity": gran, "banking": b}


def P(
    load: list[float],
    shape: list[float],
    body: dict[str, Any],
    cats: list[str],
    rate: list[float],
    months: list[date] | None = None,
    max_kw: float = 1000,
    price: float = 3.0,
    fixed_kw: float | None = None,
    share: float = 1.0,
) -> Problem:
    n = len(load)
    months = months or [APR] * n
    blocks = [BlockMeta(months[i], cats[i], frozenset({f"cat:{cats[i]}"}), i) for i in range(n)]
    c = Candidate(
        "pv",
        shape=shape,
        energy_inr_per_kwh=[price] * n,
        min_kw=fixed_kw or 0.0,
        max_kw=fixed_kw or max_kw,
        may_bank=True,
        delivered_share=share,
    )
    r = RuleView(
        id="r", family="settlement", version_id="v", jurisdiction_id="X", scope={}, body=body, source_ref={}
    )
    return Problem(
        blocks=blocks, hours=1.0, load_kwh=load, grid=Grid(rate=rate, demand_rate={}), candidates=[c], rule=r
    )


def check(p: Problem) -> tuple[Solution, Any]:
    s = solve(p)
    rs = resettle(p, s, Decimal("0.01"))
    assert rs.agrees, (rs.max_diff_kwh, rs.by_month)
    return s, rs


def total(rs: Any, q: str) -> float:
    return float(sum(r[q]["settled"] for r in rs.by_month.values()))


def test_pool_is_settled_as_a_whole_within_the_month() -> None:
    # generation in the LAST block serves the first two through the bank: a cycle is a pool, not a queue
    p = P([10, 10, 0], [0, 0, 1], rule(), ["normal"] * 3, [8.0] * 3)
    s, rs = check(p)
    assert s.cap_kw["pv"] == pytest.approx(20 / 0.9)
    assert total(rs, "withdrawn") == pytest.approx(20, abs=1e-2) and total(rs, "grid") == pytest.approx(
        0, abs=1e-2
    )


def test_nothing_crosses_a_monthly_cycle() -> None:
    p = P([0, 10], [1, 0], rule(), ["normal"] * 2, [8.0] * 2, months=[APR, MAY])
    s, rs = check(p)
    assert s.cap_kw["pv"] == pytest.approx(0, abs=1e-6)  # April's surplus lapses at the month's end
    assert total(rs, "grid") == pytest.approx(10, abs=1e-2)


def test_an_annual_cycle_pools_the_year() -> None:
    p = P([0, 10], [1, 0], rule(period="financial_year"), ["normal"] * 2, [8.0] * 2, months=[APR, MAY])
    s, rs = check(p)
    assert s.cap_kw["pv"] == pytest.approx(10 / 0.9)
    assert total(rs, "withdrawn") == pytest.approx(10, abs=1e-2)


def test_higher_to_lower_and_same_tod() -> None:
    # energy banked off-peak may not be drawn at peak; banked at peak it may be drawn anywhere
    load, cats, rate = [0, 10], ["off_peak", "peak"], [6.0, 10.0]
    s, _ = check(P(load, [1, 0], rule(drawal_rule="higher_to_lower"), cats, rate))
    assert s.cap_kw["pv"] == pytest.approx(0, abs=1e-6)
    s, _ = check(P([0, 10], [1, 0], rule(drawal_rule="higher_to_lower"), ["peak", "off_peak"], [10.0, 6.0]))
    assert s.cap_kw["pv"] == pytest.approx(10 / 0.9)
    s, _ = check(P([0, 10], [1, 0], rule(drawal_rule="same_tod"), ["peak", "off_peak"], [10.0, 6.0]))
    assert s.cap_kw["pv"] == pytest.approx(0, abs=1e-6)
    custom = rule(drawal_rule="custom", matrix={"off_peak": ["peak", "off_peak"]})
    s, _ = check(P(load, [1, 0], custom, cats, rate))
    assert s.cap_kw["pv"] == pytest.approx(10 / 0.9)


def test_banked_energy_goes_to_the_highest_rate_first() -> None:
    # 9 kWh in the bank (10 banked less 10%), 10 kWh short at peak and at normal: all of it to peak
    p = P([0, 10, 10], [1, 0, 0], rule(), ["peak", "normal", "peak"], [10.0, 8.0, 10.0], fixed_kw=10)
    _, rs = check(p)
    cyc = rs.result.cycle
    drawn = {(f, t): float(k) for _g, f, t, k in cyc.drawals}
    assert drawn == {("peak", "peak"): pytest.approx(9.0)}
    assert total(rs, "grid") == pytest.approx(11, abs=1e-2)


def test_banking_limit_on_drawal_only() -> None:
    # 100 kWh generated in an empty block; 40 kWh short elsewhere; limit: 30% of the drawal (12 kWh)
    body = rule(limit={"drawal_pct": "30", "combine": "drawal"})
    p = P([0, 40], [1, 0], body, ["normal"] * 2, [8.0] * 2, fixed_kw=100)
    _, rs = check(p)
    assert total(rs, "banked") + total(rs, "charge") == pytest.approx(12, abs=1e-2)
    assert total(rs, "withdrawn") == pytest.approx(10.8, abs=1e-2)
    assert total(rs, "grid") == pytest.approx(29.2, abs=1e-2)


def test_banking_limit_higher_of_drawal_and_generation_at_injection() -> None:
    # delivered 100 kWh of 125 injected (share 0.8): 25% of generation = 31.25 > 30% of drawal = 12
    body = rule(limit={"drawal_pct": "30", "generation_pct": "25", "combine": "max"})
    p = P([0, 40], [1, 0], body, ["normal"] * 2, [8.0] * 2, fixed_kw=100, share=0.8)
    _, rs = check(p)
    assert total(rs, "banked") + total(rs, "charge") == pytest.approx(31.25, abs=1e-2)
    assert total(rs, "withdrawn") == pytest.approx(28.125, abs=1e-2)
    body = rule(limit={"drawal_pct": "30", "generation_pct": "25", "combine": "min"})
    p = P([0, 40], [1, 0], body, ["normal"] * 2, [8.0] * 2, fixed_kw=100, share=0.8)
    _, rs = check(p)
    assert total(rs, "banked") + total(rs, "charge") == pytest.approx(12, abs=1e-2)


def test_tod_basis_nets_within_the_slot() -> None:
    # the slot's totals net: 10 generated in one normal block serves the other normal block directly
    body = rule(granularity="tod_slot_monthly", allowed=False)
    p = P([0, 10], [1, 0], body, ["normal"] * 2, [8.0] * 2)
    s, rs = check(p)
    assert s.cap_kw["pv"] == pytest.approx(10)
    assert total(rs, "setoff") == pytest.approx(10, abs=1e-2) and total(rs, "withdrawn") == 0


def test_sizing_stops_at_the_banking_limit() -> None:
    # sized freely: each kW gives 1 kWh into an empty block; the drawal left after real-time settlement
    # is the 40 kWh of the other block, so at most 30% of it (12 kWh) may be banked: the 13th kW is unpaid
    body = rule(limit={"drawal_pct": "30", "combine": "drawal"})
    s, rs = check(P([0, 40], [1, 0], body, ["normal"] * 2, [8.0] * 2))
    assert s.cap_kw["pv"] == pytest.approx(12, rel=1e-4)
    assert total(rs, "grid") == pytest.approx(40 - 10.8, abs=1e-2)
    assert "banking_limit" in s.binding
