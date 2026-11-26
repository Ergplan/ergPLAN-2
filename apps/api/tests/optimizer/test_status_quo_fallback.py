"""A sizing pass that ends on its time limit with no plan at all does not fail the run: today's supply
(nothing built, the contract demand as it is) stands in, settled by the judge, labelled in words as
not a plan and not the cheapest, never as the optimum."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from ergplan.optimizer import highs_solve as hs
from ergplan.optimizer.resettle import resettle
from ergplan.optimizer.scenarios import unproven_note
from tests.optimizer.test_highs_solve import Clock, site


def starve_sizing(monkeypatch: pytest.MonkeyPatch) -> None:
    """The sizing pass's every LP ends on its time limit (and its clock has run out); the passes
    after it are given time."""
    clock = Clock()
    monkeypatch.setattr(hs, "time", clock)
    real_lp, real_run = hs._lp, hs._run

    def lp(h: Any, deadline: float) -> Any:
        return hs._TIME if clock.late else real_lp(h, deadline)

    def run(m: Any, limit: float, gap: float, name: str = "", hold: Any = ()) -> hs.Pass:
        clock.late = name == "sizing"
        return real_run(m, limit, gap, name, hold)

    monkeypatch.setattr(hs, "_lp", lp)
    monkeypatch.setattr(hs, "_run", run)


def test_no_plan_in_time_falls_back_to_todays_supply(monkeypatch: pytest.MonkeyPatch) -> None:
    p = site(5.6)
    starve_sizing(monkeypatch)
    s = hs.solve(p, 1)
    assert s.status == hs.STATUS_QUO and s.gap is None
    assert [q.name.split()[0] for q in s.passes][:2] == ["sizing", "status"]
    assert all(v == 0 for v in [*s.cap_kw.values(), *s.bess_kw.values(), *s.bess_kwh.values()])
    assert s.cd_kva == pytest.approx(p.grid.cd_kva)
    assert resettle(p, s, Decimal("0.01")).agrees
    note = unproven_note(s.status, s.gap)
    assert note is not None and "today's supply" in note and "not the cheapest" in note


def test_a_run_with_no_plan_in_time_completes_labelled(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi.testclient import TestClient

    from ergplan.platform.app import app
    from tests.intake.helpers import result
    from tests.platform.test_api_runs import AN, CL, make_site

    api = TestClient(app)
    site_id, conn = make_site(monkeypatch, result("pvvnl_passed"), {"voltage_kv": "33"})
    starve_sizing(monkeypatch)
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
    assert run["solver"]["status"] == hs.STATUS_QUO
    annual = run["summary"]["annual"]
    # nothing built: the year costs what it costs today (to the rounding of the separate sums)
    assert abs(Decimal(annual["saving"]["value"])) < 1
    assert all(Decimal(z["kw"]["value"]) == 0 for z in run["summary"]["sizes"])
    [said] = [n for n in run["summary"]["notes"] if "time limit" in n]
    assert "today's supply" in said
    plan = api.get(f"/connections/{conn}/plan", headers=CL).json()
    [shown] = [sc for sc in plan["scenarios"] if sc["run_id"] == run["id"]]
    assert said in shown["summary"]["notes"]
    assert plan["recommended"] is None  # today's supply is never the recommended plan
