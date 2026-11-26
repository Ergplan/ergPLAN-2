"""Stage 15: the two entries of `Problem.limits` the transition plan uses, on models worked by hand.
A target share is met at least cost; a share no size can reach is infeasible; the search for the
most the rules allow replaces every unit it can and the judge prices the result at the real rates."""

from __future__ import annotations

from decimal import Decimal

import pytest

from ergplan.optimizer.highs_solve import SolveFailed, solve
from ergplan.optimizer.model import GRID_PENALTY, MIN_SHARE, Candidate, Limit
from ergplan.optimizer.resettle import resettle
from tests.optimizer.test_model import P, check


def dear_pv() -> Candidate:
    """Open-access solar that delivers 1 kWh per kW in the first block only, at ₹9 against a grid at ₹8."""
    return Candidate("pv", shape=[1.0, 0.0], energy_inr_per_kwh=[9.0, 9.0], max_kw=100)


def test_least_cost_leaves_a_dearer_source_out() -> None:
    s = check(P([10, 10], [dear_pv()]))
    assert s.cap_kw["pv"] == pytest.approx(0)
    assert s.objective == pytest.approx(160)


def test_a_target_share_is_met_at_least_cost() -> None:
    # 30% of 20 kWh not from the grid: 6 kWh of solar in the first block, so 6 kW and no more
    p = P([10, 10], [dear_pv()], limits=[Limit(MIN_SHARE, 30.0, "target")])
    s = check(p)
    assert s.cap_kw["pv"] == pytest.approx(6)
    assert s.objective == pytest.approx(6 * 9 + 14 * 8)
    assert f"limit:{MIN_SHARE}:" in s.binding


def test_a_share_no_size_can_reach_is_infeasible() -> None:
    # the source only delivers in the first block: half the consumption is the most it can replace
    p = P([10, 10], [dear_pv()], limits=[Limit(MIN_SHARE, 60.0, "target")])
    with pytest.raises(SolveFailed):
        solve(p)


def test_the_most_the_rules_allow_and_its_cost_at_the_real_rates() -> None:
    p = P([10, 10], [dear_pv()], limits=[Limit(GRID_PENALTY, 1000.0, "most")])
    s = solve(p)
    # every unit of the first block is replaced and not a kW more (a kW more only adds cost)
    assert s.cap_kw["pv"] == pytest.approx(10)
    rs = resettle(p, s, Decimal("0.01"))
    assert rs.agrees
    # the judge's cost is at the real rates: 10 kWh of grid at ₹8 and 10 kWh of solar at ₹9
    assert sum(rs.costs.values(), Decimal(0)) == Decimal("170.00")
    assert sum(rs.result.grid_kwh, Decimal(0)) == Decimal("10.000")


def test_a_cap_binds_the_most_the_rules_allow() -> None:
    p = P(
        [10, 10],
        [dear_pv()],
        limits=[Limit(GRID_PENALTY, 1000.0, "most"), Limit("max_kw", 4.0, "cap", {}, "pv")],
    )
    s = solve(p)
    assert s.cap_kw["pv"] == pytest.approx(4)
    assert "limit:max_kw:pv" in s.binding
