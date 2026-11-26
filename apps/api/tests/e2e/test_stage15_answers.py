"""Stage 15 end to end, the consumption-profile answers: a bill, the questions, the answers, a sizing
that uses them, the report that shows them as client-stated, and replay to the paisa of the runs made
before each answer changed (invariant 5: a run pins its load profile, and the profile its answers)."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient

from ergplan.db.base import session_scope
from ergplan.db.models import RunResult
from ergplan.platform.app import app
from tests.platform.test_api_runs import AN, CL, make_site, msedcl_5mw

client = TestClient(app)

SPEC = {
    "candidates": [
        {"kind": "rooftop_pv"},
        {"kind": "oa_hybrid_intra", "ownership": "third_party", "max_mw": "8"},
    ],
    "as_of": "2026-06-01",
    "resolution_min": 60,
}


def _run(scenario: str) -> dict[str, Any]:
    r = client.post(f"/scenarios/{scenario}/run", params={"wait": "true"}, headers=AN)
    assert r.status_code == 202, r.text
    run = dict(r.json())
    assert run["status"] == "done", run["error"]
    return run


def _answer(site: str, key: str, value: Any) -> dict[str, Any]:
    r = client.put(f"/sites/{site}/questions/{key}", json={"value": value}, headers=CL)
    assert r.status_code == 200, r.text
    return dict(r.json())


def _replays_identically(run: dict[str, Any]) -> None:
    rp = client.post(f"/runs/{run['id']}/replay", params={"wait": "true"}, headers=AN).json()
    assert rp["status"] == "done" and rp["identical_to_original"] is True
    assert rp["summary"] == run["summary"]
    assert rp["pins"]["load_profile_id"] == run["pins"]["load_profile_id"]


def _load_kwh(run: dict[str, Any]) -> Decimal:
    """The year's consumption the run was sized for, from its stored result."""
    with session_scope() as s:
        rr = s.get(RunResult, run["id"])
        assert rr is not None
        return Decimal(rr.summary["settlement"]["load_kwh"]["value"])


def _rooftop_kw(run: dict[str, Any]) -> Decimal:
    return next(Decimal(z["kw"]["value"]) for z in run["summary"]["sizes"] if z["kind"] == "rooftop_pv")


def test_answers_reach_the_sizing_and_old_runs_replay(monkeypatch: pytest.MonkeyPatch) -> None:
    site, conn = make_site(monkeypatch, msedcl_5mw(), {})
    asked = client.get(f"/sites/{site}/questions").json()
    # the bill prints its power factor, so that is not asked; the other eight questions are open
    assert len(asked["outstanding"]) == 8 and "power_factor" not in asked["outstanding"]
    known = {q["key"]: q["known_from_inputs"] for q in asked["questions"]}
    assert known["power_factor"] == "printed on every bill" and known["shifts"] is None
    sc = client.post(
        f"/sites/{site}/scenarios",
        json={"connection_id": conn, "name": "Rooftop and hybrid", "spec": SPEC},
        headers=AN,
    ).json()["id"]

    # 1. nothing answered: the plan is sized on the bill alone
    first = _run(sc)
    assert first["pins"]["load_profile_version"] == 1
    load0 = _load_kwh(first)
    assert Decimal(first["summary"]["contract_demand_kva"]["value"]) == 5000
    roof0 = _rooftop_kw(first)
    assert roof0 > 400  # with no roof stated the rooftop plant is offered up to the contract demand

    # 2. the roof bounds the rooftop plant
    v = _answer(site, "roof_sqm", "4000")["profiles"][0]
    assert v["version"] == 2
    second = _run(sc)
    assert second["pins"]["load_profile_version"] == 2
    assert second["pins"]["load_profile_id"] == v["load_profile_id"] != first["pins"]["load_profile_id"]
    assert _rooftop_kw(second) == Decimal("400")  # 4000 m² at 10 m² per kWp
    assert abs(_load_kwh(second) - load0) < 1
    binding = [b for b in second["summary"]["binding"] if "roof" in b["words"].lower()]
    assert binding and "4000 m²" in binding[0]["words"]

    # 3. the planned expansion scales the year the plan is sized for; the contract demand intended
    #    stands in for the bill's; a shift pattern reshapes the day
    _answer(site, "expansion", {"growth_pct": "20"})
    _answer(site, "contract_demand_kva", "6000")
    v = _answer(site, "shifts", [{"start": "06:00", "end": "18:00"}])["profiles"][0]
    assert v["version"] == 5
    third = _run(sc)
    assert third["pins"]["load_profile_version"] == 5
    load = _load_kwh(third)
    assert abs(load - load0 * Decimal("1.2")) < 1  # every block × 1.2, each rounded to the watt
    notes = " ".join(third["summary"]["notes"])
    assert "planned expansion: the load profile scaled by +20%" in notes
    assert "contract demand the client intends to hold, 6000 kVA (the bill shows 5000 kVA)" in notes
    assert Decimal(third["summary"]["contract_demand_kva"]["value"]) == 6000
    assert Decimal(third["summary"]["annual"]["before"]["value"]) > Decimal(
        first["summary"]["annual"]["before"]["value"]
    )
    assert _rooftop_kw(third) == Decimal("400")
    # the stored profile stays the year the bills cover (today's cost screens read it)
    prof = client.get(f"/connections/{conn}/profile").json()
    assert abs(sum(Decimal(m["energy_kwh"]["value"]) for m in prof["months"]) - load0) < 1

    # 4. the report: every answer a client-stated chip, and whether the plan was sized with it
    plan = client.get(f"/connections/{conn}/plan", headers=CL).json()
    shown = {a["key"]: a for a in plan["answers"]}
    assert set(shown) == {"roof_sqm", "expansion", "contract_demand_kva", "shifts"}
    assert all(a["in_this_plan"] is True for a in shown.values())
    assert all(a["source_ref"]["kind"] == "client_stated" for a in shown.values())
    assert plan["profile_provenance"] == "synthesised" and "4 answers" in plan["profile_words"]

    # 5. an answer changes after the plan was sized: the report says the plan does not hold it yet
    _answer(site, "roof_sqm", "2000")
    assert client.delete(f"/sites/{site}/questions/expansion", headers=CL).status_code == 200
    plan = client.get(f"/connections/{conn}/plan", headers=CL).json()
    shown = {a["key"]: a for a in plan["answers"]}
    assert shown["roof_sqm"]["in_this_plan"] is False and shown["shifts"]["in_this_plan"] is True
    assert "expansion" not in shown

    # 6. every run made before replays to the paisa on the profile, and so the answers, it pinned
    for run in (first, second, third):
        _replays_identically(run)

    # 7. and a new run reads the answers in force now
    fourth = _run(sc)
    assert fourth["pins"]["load_profile_version"] == 7
    assert _rooftop_kw(fourth) == Decimal("200")
    assert abs(_load_kwh(fourth) - load0) < 1
    assert Decimal(fourth["summary"]["contract_demand_kva"]["value"]) == 6000
