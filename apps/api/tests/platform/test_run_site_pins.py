"""A run pins what it reads from the site and the connection (invariant 5): the roof typed with the
inputs, the connection's facts its rules were matched on (with the supply level), the captive thermal
units. Changing the roof after a run leaves its replay identical; a run made before the pin existed
replays as it did, reading them live."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import update

from ergplan.db.base import session_scope
from ergplan.db.models import Run, Site
from tests.platform.test_api_runs import AN, _scenario, client, make_site, msedcl_5mw

SPEC: dict[str, Any] = {
    "candidates": [
        {"kind": "rooftop_pv"},
        {"kind": "oa_solar_intra", "ownership": "third_party", "max_mw": "4"},
    ],
    "as_of": "2026-06-01",
    "resolution_min": 60,
}


def _roof(site: str, sqm: str) -> None:
    with session_scope(actor="analyst@joulewise.in") as db:
        db.execute(update(Site).where(Site.id == site).values(roof_sqm=Decimal(sqm)))


def _rooftop_kw(run: dict[str, Any]) -> Decimal:
    sizes = {x["kind"]: x for x in run["summary"]["sizes"]}
    return Decimal(sizes["rooftop_pv"]["kw"]["value"]) if "rooftop_pv" in sizes else Decimal(0)


@pytest.fixture
def mh(monkeypatch: pytest.MonkeyPatch) -> tuple[str, str]:
    return make_site(monkeypatch, msedcl_5mw(), {})


def test_a_replay_reads_the_roof_the_run_read(mh: tuple[str, str]) -> None:
    site, conn = mh
    _roof(site, "3000")
    sid = _scenario(site, conn, SPEC, "Rooftop and solar")
    run = client.post(f"/scenarios/{sid}/run", params={"wait": "true"}, headers=AN).json()
    assert run["status"] == "done", run["error"]
    pin = run["pins"]["site"]
    assert Decimal(pin["roof_sqm"]) == Decimal("3000")
    assert pin["facts"]["consumer_kind"] in ("ht", "eht") and pin["thermals"] == []
    # the roof bounds the rooftop plant: 3,000 m² at 10 m² per kWp
    assert Decimal(0) < _rooftop_kw(run) <= Decimal("300")

    _roof(site, "500")  # the roof typed again, smaller
    rp = client.post(f"/runs/{run['id']}/replay", params={"wait": "true"}, headers=AN).json()
    assert rp["status"] == "done" and rp["identical_to_original"] is True
    assert rp["summary"] == run["summary"] and rp["pins"]["site"] == pin
    # a new run reads the roof as it stands now
    fresh = client.post(f"/scenarios/{sid}/run", params={"wait": "true"}, headers=AN).json()
    assert fresh["status"] == "done" and _rooftop_kw(fresh) <= Decimal("50")
    assert fresh["summary"] != run["summary"]


def test_a_run_made_before_the_pin_replays_as_it_did(mh: tuple[str, str]) -> None:
    site, conn = mh
    _roof(site, "3000")
    sid = _scenario(site, conn, SPEC, "Rooftop and solar")
    run = client.post(f"/scenarios/{sid}/run", params={"wait": "true"}, headers=AN).json()
    assert run["status"] == "done", run["error"]
    with session_scope(actor="analyst@joulewise.in") as db:
        old = db.get(Run, run["id"])
        assert old is not None
        old.pins = {k: v for k, v in old.pins.items() if k != "site"}  # as a run made before 26 Nov
    rp = client.post(f"/runs/{run['id']}/replay", params={"wait": "true"}, headers=AN).json()
    assert rp["status"] == "done" and rp["identical_to_original"] is True
    assert "site" not in rp["pins"]
