"""The path into HiGHS (the matrix handed over directly, the search of few-integer models here, the
sizing settlement's pools and its banking-limit rows) gives the optimum the earlier path gave: Pyomo's
own HiGHS interface and HiGHS's own MIP search, on a small site with every feature that matters to
it (two sources that may bank, a monthly banking limit that is the higher of two, a battery). And a
search that ends on its time limit says so."""

from __future__ import annotations

import dataclasses
import math
from datetime import date
from decimal import Decimal
from typing import Any

import pyomo.environ as pyo
import pytest
from pyomo.contrib.solver.common.factory import SolverFactory

from ergplan.optimizer import highs_solve as hs
from ergplan.optimizer.model import Battery, BlockMeta, Candidate, Grid, Problem, build
from ergplan.optimizer.resettle import resettle
from ergplan.optimizer.scenarios import unproven_note
from ergplan.rulebook.models import RuleView

MONTHS = [date(2026, 4, 1), date(2026, 7, 1), date(2026, 10, 1), date(2027, 1, 1)]
DAY = 24
# the Uttar Pradesh rule's shape: a monthly pool, 8% in kind, banking up to the HIGHER of 30% of
# the drawal and 25% of the injection (a binary per month)
RULE = {
    "method": "cycle_pooled",
    "granularity": "block_15min",
    "banking": {
        "allowed": True,
        "period": "monthly",
        "charge": {"kind": "in_kind_percent", "value": "8"},
        "limit": {"combine": "max", "drawal_pct": "30", "generation_pct": "25"},
    },
}


def site(wind_price: float = 4.4, battery: bool = True, split: bool = False) -> Problem:
    """Four months of one day each, hourly: a two-shift load, solar, wind and a battery."""
    blocks, load, rate, sun, wind = [], [], [], [], []
    for i, mo in enumerate(MONTHS):
        for hr in range(DAY):
            slot = "peak" if 18 <= hr < 23 else ("off_peak" if hr < 6 else "normal")
            blocks.append(BlockMeta(mo, slot, frozenset({f"cat:{slot}"}), hr))
            load.append(900.0 + 320.0 * math.sin((hr + 2 * i) / 3.7) ** 2 + 45.0 * i)
            rate.append({"peak": 8.6, "off_peak": 6.1, "normal": 7.2}[slot])
            sun.append(
                max(0.0, math.sin((hr - 6) / 12 * math.pi)) * (0.55 + 0.1 * i) if 6 <= hr <= 18 else 0.0
            )
            wind.append(0.18 + 0.22 * math.cos((hr + 5 * i) / 5.3) ** 2)
    n = len(blocks)

    def source(cid: str, shape: list[float], price: float, max_kw: float) -> Candidate:
        return Candidate(cid, shape=shape, energy_inr_per_kwh=[price] * n, max_kw=max_kw, may_bank=True)

    cands = [source("wind", wind, wind_price, 3000.0)]
    if split:  # the same solar plant offered as two halves
        cands += [source("pv_a", sun, 3.9, 2000.0), source("pv_b", sun, 3.9, 2000.0)]
    else:
        cands += [source("pv", sun, 3.9, 4000.0)]
    rule = RuleView(
        id="r", family="settlement", version_id="v", jurisdiction_id="X", scope={}, body=RULE, source_ref={}
    )
    return Problem(
        blocks=blocks,
        hours=1.0,
        load_kwh=load,
        grid=Grid(rate=rate, demand_rate=dict.fromkeys(MONTHS, 290.0), cd_kva=2000.0, floor_pct=75.0),
        candidates=cands,
        batteries=[Battery("bess", max_kw=600.0, inr_per_kwh_year=14.0)] if battery else [],
        rule=rule,
    )


def by_pyomo(m: Any) -> float:
    """The optimum by the earlier path: Pyomo's HiGHS interface and HiGHS's own MIP search."""
    res = SolverFactory("highs").solve(
        m, solver_options={"threads": 1, "random_seed": 0, "output_flag": False, "mip_rel_gap": 1e-9}
    )
    return float(res.incumbent_objective)


def sizes(m: Any) -> dict[str, float]:
    out = {k: float(pyo.value(v)) for k, v in m.cap.items()}
    out.update({f"{k} kWh": float(pyo.value(v)) for k, v in m.bkwh.items()})
    return out


# wind at 4.4: the first LP's solution already holds with whole guards (no branching); at 5.6 and
# 9.0 the LP banks more than either limit allows and the search has to branch (at 9.0 the months
# end on different legs)
@pytest.mark.parametrize("wind_price", [4.4, 5.6, 9.0])
def test_the_search_here_finds_what_highs_found_through_pyomo(wind_price: float) -> None:
    p = site(wind_price)
    old = build(p)
    expected = by_pyomo(old)
    new = build(p)
    done = hs._run(new, 60, 1e-9, "sizing", hs.storage_sizes(new))
    assert done.search == "tree" and done.integers == len(MONTHS) and done.optimal
    assert float(pyo.value(new.obj)) == pytest.approx(expected, rel=1e-7)
    assert sizes(new) == pytest.approx(sizes(old), rel=1e-5, abs=1e-3)
    assert all(float(pyo.value(z)) in (0.0, 1.0) for z in new.limit_z.values())
    assert (done.nodes > 6) == (wind_price > 5)


def test_highs_own_search_on_the_same_matrix_agrees(monkeypatch: pytest.MonkeyPatch) -> None:
    p = site(9.0)
    here = build(p)
    hs._run(here, 60, 1e-9, "sizing", hs.storage_sizes(here))
    monkeypatch.setattr(hs, "TREE_MAX_INTEGERS", 0)  # as for a thermal fleet: HiGHS's MIP search
    there = build(p)
    done = hs._run(there, 60, 1e-9)
    assert done.search == "highs" and done.optimal
    assert float(pyo.value(there.obj)) == pytest.approx(float(pyo.value(here.obj)), rel=1e-7)


def test_holding_storage_at_zero_first_changes_no_optimum() -> None:
    # a battery that pays (cheap) and one that does not: with and without the first search
    for cost in (14.0, 4000.0):
        p = site(5.6)
        p = dataclasses.replace(p, batteries=[dataclasses.replace(p.batteries[0], inr_per_kwh_year=cost)])
        held, plain = build(p), build(p)
        a = hs._run(held, 60, 1e-9, "sizing", hs.storage_sizes(held))
        b = hs._run(plain, 60, 1e-9, "sizing")
        assert a.held_nodes > 0 and b.held_nodes == 0
        assert float(pyo.value(held.obj)) == pytest.approx(float(pyo.value(plain.obj)), rel=1e-7)
        assert (float(pyo.value(held.bkwh["bess"])) > 1.0) == (cost < 100)


def test_the_banking_limit_rows_cut_off_no_solution() -> None:
    # one loose relaxation for both rows and no rows without a binary (the earlier formulation,
    # which `big_m_kwh` still gives) against the tight ones: the same optimum
    p = site(9.0)
    tight, loose = build(p), build(dataclasses.replace(p, big_m_kwh=1e7))
    assert len(tight.cons) == len(loose.cons) + 2 * len(MONTHS)
    hs._run(tight, 60, 1e-9)
    assert float(pyo.value(tight.obj)) == pytest.approx(by_pyomo(loose), rel=1e-7)


def test_sources_that_settle_alike_are_one_pool_in_sizing() -> None:
    # a plant offered as two halves is the same plant: the pool has one set-off, one deposit and
    # one surplus per block whatever the number of sources, and the same optimum
    one, two = build(site(5.6, battery=False)), build(site(5.6, battery=False, split=True))
    assert len(one.so) == len(two.so) and len(one.dep) == len(two.dep)
    hs._run(one, 60, 1e-9)
    hs._run(two, 60, 1e-9)
    assert float(pyo.value(two.obj)) == pytest.approx(float(pyo.value(one.obj)), rel=1e-7)
    halves = float(pyo.value(two.cap["pv_a"])) + float(pyo.value(two.cap["pv_b"]))
    assert halves == pytest.approx(float(pyo.value(one.cap["pv"])), rel=1e-5)


def test_the_whole_solve_agrees_with_the_judge() -> None:
    p = site()
    s = hs.solve(p, 60)
    assert s.status == "optimal" and s.gap is not None and s.gap <= 0.001
    assert [q.name.split()[0] for q in s.passes] == ["sizing", *["settlement"] * len(MONTHS)]
    assert resettle(p, s, Decimal("0.01")).agrees
    assert unproven_note(s.status, s.gap) is None


class Clock:
    """A clock on which, while `late`, every look is a hundred seconds later: any time limit has
    passed."""

    def __init__(self) -> None:
        self.now, self.late = 0.0, True

    def perf_counter(self) -> float:
        self.now += 100.0 if self.late else 0.001
        return self.now


def test_a_search_that_ends_on_its_time_limit_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    p = site(5.6)  # a search that has to branch
    proven = hs.solve(p, 60, 1e-9)
    clock = Clock()
    monkeypatch.setattr(hs, "time", clock)
    settle = hs.build_settlement

    def in_time(*a: Any, **k: Any) -> Any:  # the settlement passes after the sizing are given time
        clock.late = False
        return settle(*a, **k)

    monkeypatch.setattr(hs, "build_settlement", in_time)
    s = hs.solve(p, 60, 1e-9)
    # it has a plan (the first one its search reached) and the judge settles it, but it is not
    # called optimal, and the distance to the bound it could not close is reported
    assert s.status == "time_limit" and not s.passes[0].optimal
    assert s.gap is not None and s.gap > 1e-9
    assert s.objective >= proven.objective * (1 - 1e-9)
    assert s.objective * (1 - s.gap) <= proven.objective * (1 + 1e-9)  # the bound is a true one
    assert resettle(p, s, Decimal("0.01")).agrees
    note = unproven_note(s.status, s.gap)
    assert note is not None and "time limit" in note and f"{s.gap * 100:.2f}%" in note
    assert unproven_note("time_limit", None) is not None


def test_a_time_limited_run_tells_the_analyst_and_the_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """Through the API: the run record keeps `time_limit` and the gap, and the plan's notes (shown
    on the results page and on the client's plan page) say in words that it was not proven."""
    from fastapi.testclient import TestClient

    from ergplan.platform import runs as svc
    from ergplan.platform.app import app
    from tests.intake.helpers import result
    from tests.platform.test_api_runs import AN, CL, make_site

    api = TestClient(app)
    site_id, conn = make_site(monkeypatch, result("pvvnl_passed"), {"voltage_kv": "33"})
    real = svc.solve

    def out_of_time(*a: Any, **k: Any) -> hs.Solution:
        sol = real(*a, **k)
        sol.status, sol.gap = "time_limit", 0.0146  # as the five-component run of 25 Nov 2026 ended
        return sol

    monkeypatch.setattr(svc, "solve", out_of_time)
    spec = {
        "candidates": [{"kind": "oa_solar_intra", "ownership": "captive", "max_mw": "6"}],
        "as_of": "2026-09-01",
        "resolution_min": 60,
    }
    sid = api.post(
        f"/sites/{site_id}/scenarios", json={"connection_id": conn, "name": "s", "spec": spec}, headers=AN
    ).json()["id"]
    run = api.post(f"/scenarios/{sid}/run", params={"wait": "true"}, headers=AN).json()
    assert run["status"] == "done", run["error"]
    assert run["solver"]["status"] == "time_limit" and run["solver"]["gap"] == 0.0146
    [said] = [n for n in run["summary"]["notes"] if "time limit" in n]
    assert "1.46%" in said and "cheapest" in said
    plan = api.get(f"/connections/{conn}/plan", headers=CL).json()
    [shown] = [sc for sc in plan["scenarios"] if sc["run_id"] == run["id"]]
    assert said in shown["summary"]["notes"]
