"""The rule book and the landed-cost screen agree: beside the landed cost before banking and the banking
charge on a banked unit, the book states the screen's figure, the landed cost if every unit is banked
(the upper bound), equal to the screen's total slot by slot."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal as D

import pytest
from fastapi.testclient import TestClient

from ergplan.platform.app import app
from tests.platform.test_api_runs import AN, make_site, msedcl_5mw

client = TestClient(app)


def test_the_book_states_the_screens_landed_cost_with_banking(monkeypatch: pytest.MonkeyPatch) -> None:
    _site, conn = make_site(monkeypatch, msedcl_5mw(), {})
    month = date.today().replace(day=1)
    r = client.post(
        "/statebooks/generate",
        json={"discom": "msedcl", "month": month.isoformat(), "connection_id": conn},
        headers=AN,
    )
    assert r.status_code == 201, r.text[:400]
    src = next(x for x in r.json()["content"]["renewable"] if x["source"] == "oa_solar_intra")
    route = next(x for x in src["routes"] if x["route"] == "third_party")
    scenario = {
        "month": month.month,
        "as_of": date.today().isoformat(),
        "sources": {"oa_solar_intra": {"ownership": "third_party"}},
    }
    screen = client.get(
        f"/connections/{conn}/landed-cost", params={"scenario": json.dumps(scenario)}, headers=AN
    ).json()
    on_screen = {
        w["slot"]: w["total"]
        for w in next(x for x in screen["sources"] if x["id"] == "oa_solar_intra")["by_slot"]
    }
    assert route["slots"]
    for sl in route["slots"]:
        assert sl["banking"] is not None  # Maharashtra's approved banking charge
        upper = sl["landed_upper"]
        assert D(upper["value"]) == D(sl["landed"]["value"]) + D(sl["banking"]["value"])
        assert D(upper["value"]) == D(on_screen[sl["slot"]]["value"])  # the screen's figure
        assert upper["source_refs"]
    md = client.get(f"/statebooks/{r.json()['id']}/file", headers=AN)
    assert md.status_code == 200 and "| Landed cost if every unit is banked (upper bound) |" in md.text
