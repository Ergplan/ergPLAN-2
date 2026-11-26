"""Generation profiles supplied for a site (Stage 15): upload, list, read, withdraw; a refused file
says why; a run generates with the supplied profile in place of the library curve, pins its id and
sha256, and replays to the paisa whatever is uploaded afterwards (invariant 5)."""

from __future__ import annotations

import hashlib
import json
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient

from ergplan.db.base import session_scope
from ergplan.db.models import Run
from ergplan.intake import bills as bill_store
from ergplan.intake import generation
from ergplan.report import whatif
from tests.engine.helpers import restore_committed
from tests.intake.gen_files import csv_of, cuf_pct, shares
from tests.intake.helpers import FakeBillx
from tests.platform.test_api_runs import _scenario, make_site, msedcl_5mw
from tests.platform.test_auth import prod, sign_in  # noqa: F401

from ergplan.platform.app import app  # isort: skip

client = TestClient(app)
AN = {"X-Ergplan-Actor": "analyst@joulewise.in", "X-Ergplan-Role": "analyst"}
CL = {"X-Ergplan-Actor": "client@acme.in", "X-Ergplan-Role": "client"}
APR = date(2025, 4, 1)
D = Decimal


@pytest.fixture
def mh_site(monkeypatch: pytest.MonkeyPatch) -> tuple[str, str]:
    return make_site(monkeypatch, msedcl_5mw(), {})


def _upload(site: str, data: bytes, name: str, **form: str) -> Any:
    return client.post(
        f"/sites/{site}/generation-profiles",
        files={"file": (name, data, "text/csv")},
        data=form,
        headers=CL,
    )


def test_upload_list_read_and_withdraw(mh_site: tuple[str, str]) -> None:
    site, _ = mh_site
    assert client.get(f"/sites/{site}/generation-profiles").json() == []
    rows = shares("solar", APR, 60)
    data = csv_of(rows, "kW", D(1000))
    r = _upload(site, data, "solar-plant.csv", technology="solar", provenance="measured", basis="per_mw")
    assert r.status_code == 201, r.text
    p = r.json()
    assert (p["technology"], p["applies_to"], p["provenance"]) == ("solar", "open_access", "measured")
    assert (p["filename"], p["resolution_min"], p["blocks"]) == ("solar-plant.csv", 60, 35040)
    assert p["sha256"] == hashlib.sha256(data).hexdigest()
    assert (p["unit_in_file"], p["basis"], p["capacity_mw"], p["first_day"]) == (
        "kW",
        "per_mw",
        None,
        "2025-04-01",
    )
    assert (p["version"], p["in_use"], p["withdrawn_at"], p["created_by"]) == (
        1,
        True,
        None,
        CL["X-Ergplan-Actor"],
    )
    # the CUF the file implies, worked from the rows here; a measurement, citing the file
    assert D(p["annual_cuf"]["value"]) == cuf_pct(rows).quantize(D("0.01"))
    assert p["annual_cuf"]["unit"] == "percent"
    [ref] = p["annual_cuf"]["source_refs"]
    assert ref["kind"] == "measured" and "solar-plant.csv" in ref["note"] and p["sha256"] in ref["note"]
    assert [m["month"] for m in p["monthly_cuf"]] == list(range(1, 13))
    may = [v for t, v in rows if t.month == 5]
    assert D(p["monthly_cuf"][4]["cuf"]["value"]) == (sum(may, D(0)) / len(may) * 100).quantize(D("0.01"))
    assert all(m["cuf"]["source_refs"][0]["kind"] == "measured" for m in p["monthly_cuf"])
    assert D(p["peak"]["value"]) == (max(v for _, v in rows) * 100).quantize(D("0.01"))
    # beside it, the library curve it replaces (an assumption)
    assert D(p["library_cuf"]["value"]) == generation.annual_cuf(generation.curve_for("solar", "MH"))
    assert p["library_cuf"]["source_refs"][0]["kind"] == "assumption"
    assert p["runs"] == []

    # a wind profile, 15-minute, stated by the developer for a 12.5 MW plant
    wind = shares("wind", APR, 15)
    w = _upload(
        site,
        csv_of(wind, "MW", D("12.5")),
        "wind.csv",
        technology="wind",
        provenance="stated",
        basis="capacity",
        capacity_mw="12.5",
    ).json()
    assert (w["version"], w["resolution_min"], w["capacity_mw"], w["in_use"]) == (2, 15, "12.5", True)
    assert w["annual_cuf"]["source_refs"][0]["kind"] == "client_stated"
    assert D(w["annual_cuf"]["value"]) == cuf_pct(wind).quantize(D("0.01"))

    listed = client.get(f"/sites/{site}/generation-profiles", headers=CL).json()
    assert [x["id"] for x in listed] == [w["id"], p["id"]]
    assert client.get(f"/generation-profiles/{p['id']}", headers=CL).json() == p
    assert client.get("/generation-profiles/missing").status_code == 404
    assert client.get("/sites/missing/generation-profiles").status_code == 404

    # a second solar file takes the first one's place; withdrawing it gives the place back
    p2 = _upload(
        site, csv_of(shares("solar", APR, 15), "cuf"), "solar-2.csv", technology="solar", provenance="stated"
    ).json()
    use = {x["id"]: x["in_use"] for x in client.get(f"/sites/{site}/generation-profiles").json()}
    assert use == {p["id"]: False, w["id"]: True, p2["id"]: True}
    gone = client.post(f"/generation-profiles/{p2['id']}/withdraw", headers=CL)
    assert gone.status_code == 200 and gone.json()["in_use"] is False and gone.json()["withdrawn_at"]
    use = {x["id"]: x["in_use"] for x in client.get(f"/sites/{site}/generation-profiles").json()}
    assert use == {p["id"]: True, w["id"]: True, p2["id"]: False}
    assert client.post("/generation-profiles/missing/withdraw", headers=CL).status_code == 404


def test_a_refused_file_says_why_and_nothing_is_stored(mh_site: tuple[str, str]) -> None:
    site, _ = mh_site
    rows = shares("solar", APR, 60)
    ok = {"technology": "solar", "provenance": "measured", "basis": "per_mw"}
    gap = [*rows[:500], *rows[501:]]
    r = _upload(site, csv_of(gap, "kW", D(1000)), "gap.csv", **ok)
    assert r.status_code == 422
    assert r.json()["detail"] == (
        "the file has a gap: row 501 is 2025-04-21 19:00 and row 502 is 2025-04-21 21:00, 1 period is missing"
    )
    r = _upload(site, csv_of(rows[:8000], "kW", D(1000)), "short.csv", **ok)
    assert r.status_code == 422 and "the year from that day needs 8,760" in r.json()["detail"]
    r = _upload(site, csv_of(rows, "kW", D(2500)), "big.csv", **ok)
    assert r.status_code == 422 and "a plant cannot generate above its capacity" in r.json()["detail"]
    r = _upload(site, csv_of(rows, "kW", D(100)), "low.csv", **ok)
    assert r.status_code == 422
    tenth = (cuf_pct(rows) / 10).quantize(D("0.01"))  # the file read as a tenth of what it is
    assert f"implies an annual CUF of {tenth}%; a solar plant is between 10% and 35%" in r.json()["detail"]
    r = _upload(
        site, csv_of(shares("wind", APR, 60), "cuf"), "wind.csv", technology="solar", provenance="stated"
    )
    assert r.status_code == 422 and "a solar plant is generating" in r.json()["detail"]
    # a rooftop profile is solar; the technology and the provenance are among those accepted
    r = _upload(
        site,
        csv_of(shares("wind", APR, 60), "cuf"),
        "w.csv",
        technology="wind",
        provenance="stated",
        applies_to="rooftop",
    )
    assert r.status_code == 422 and r.json()["detail"] == "a rooftop profile is a solar profile"
    assert _upload(site, b"x", "x.csv", technology="hydro", provenance="stated").status_code == 422
    assert _upload(site, b"x", "x.csv", technology="solar", provenance="synthesised").status_code == 422
    assert _upload("missing", csv_of(rows, "kW", D(1000)), "s.csv", **ok).status_code == 404
    assert client.get(f"/sites/{site}/generation-profiles").json() == []


SPEC = {
    "candidates": [
        {"kind": "oa_solar_intra", "ownership": "third_party", "max_mw": "8"},
        {"kind": "oa_wind_intra", "ownership": "third_party", "max_mw": "8"},
    ],
    "as_of": "2026-06-01",
    "resolution_min": 60,
}


def _run(sid: str) -> dict[str, Any]:
    r = client.post(f"/scenarios/{sid}/run", params={"wait": "true"}, headers=AN)
    assert r.status_code == 202, r.text
    run: dict[str, Any] = r.json()
    assert run["status"] == "done", run["error"]
    return run


def _replay(run_id: str) -> dict[str, Any]:
    r = client.post(f"/runs/{run_id}/replay", params={"wait": "true"}, headers=AN)
    assert r.status_code == 202, r.text
    out: dict[str, Any] = r.json()
    return out


def _cuf(run: dict[str, Any], kind: str) -> Any:
    size: dict[str, Any] = next(x for x in run["summary"]["sizes"] if x["kind"] == kind)
    return size["cuf"]


def test_a_run_uses_the_supplied_profiles_pins_them_and_replays(mh_site: tuple[str, str]) -> None:
    site, conn = mh_site
    sid = _scenario(site, conn, SPEC, "Solar and wind")
    # 1. no profile supplied: the library curves, and no generation pin
    lib = _run(sid)
    assert "generation_profiles" not in lib["pins"]
    assert _cuf(lib, "oa_solar_intra") is None and lib["summary"]["provenance"]["generation"] == []

    # 2. the developer's solar and wind profiles are supplied (hourly solar, 15-minute wind)
    solar_rows, wind_rows = shares("solar", APR, 60), shares("wind", APR, 15)
    solar_csv, wind_csv = csv_of(solar_rows, "kW", D(1000)), csv_of(wind_rows, "cuf")
    sp = _upload(
        site, solar_csv, "solar.csv", technology="solar", provenance="measured", basis="per_mw"
    ).json()
    wp = _upload(site, wind_csv, "wind.csv", technology="wind", provenance="stated").json()
    own = _run(sid)
    # the run differs from the library-curve run
    assert own["summary"]["annual"]["after"]["value"] != lib["summary"]["annual"]["after"]["value"]
    assert own["summary"]["sizes"] != lib["summary"]["sizes"]
    # it pins each profile's id and the file's sha256
    assert own["pins"]["generation_profiles"] == {
        "solar:open_access": {
            "id": sp["id"],
            "sha256": hashlib.sha256(solar_csv).hexdigest(),
            "file": "solar.csv",
            "provenance": "measured",
        },
        "wind:open_access": {
            "id": wp["id"],
            "sha256": hashlib.sha256(wind_csv).hexdigest(),
            "file": "wind.csv",
            "provenance": "stated",
        },
    }
    # the CUF on the plan is the file's, shown as measured or as stated (invariants 1 and 8)
    s_cuf, w_cuf = _cuf(own, "oa_solar_intra"), _cuf(own, "oa_wind_intra")
    assert D(s_cuf["value"]) == cuf_pct(solar_rows) and s_cuf["source_refs"][0]["kind"] == "measured"
    assert D(w_cuf["value"]) == cuf_pct(wind_rows) and w_cuf["source_refs"][0]["kind"] == "client_stated"
    assert sp["sha256"] in s_cuf["source_refs"][0]["note"]
    assert own["summary"]["provenance"]["generation"] == [
        f"solar (open access): solar.csv (measured), sha256 {sp['sha256']}",
        f"wind (open access): wind.csv (stated), sha256 {wp['sha256']}",
    ]
    notes = " ".join(own["summary"]["notes"])
    assert "the profile supplied (solar.csv, measured, " in notes
    assert "the profile supplied (wind.csv, stated, " in notes
    # the profile's screen names the runs that used it
    seen = client.get(f"/generation-profiles/{sp['id']}", headers=CL).json()
    assert [(x["run_id"], x["scenario_name"], x["status"]) for x in seen["runs"]] == [
        (own["id"], "Solar and wind", "done")
    ]

    # 3. a different solar file is uploaded afterwards, and the wind profile is withdrawn
    later = [(t, (v * D("0.9")).quantize(D("0.0001"))) for t, v in solar_rows]
    _upload(site, csv_of(later, "cuf"), "solar-later.csv", technology="solar", provenance="stated")
    assert client.post(f"/generation-profiles/{wp['id']}/withdraw", headers=CL).status_code == 200
    # the replay of the run still generates with what it pinned: identical to the paisa
    rp = _replay(own["id"])
    assert rp["status"] == "done" and rp["identical_to_original"] is True
    assert rp["summary"] == own["summary"]
    assert rp["pins"]["generation_profiles"] == own["pins"]["generation_profiles"]
    # a what-if on the run is worked with the run's own profiles too, not the later file
    with session_scope() as db:
        row = db.get(Run, own["id"])
        assert row is not None
        wi = whatif.evaluate(db, row, whatif.WhatIf(capacities_mw={"oa_solar_intra": D(3)}))
    wi_solar = next(x for x in wi["sizes"] if x["kind"] == "oa_solar_intra")
    assert D(wi_solar["cuf"]["value"]) == cuf_pct(solar_rows)
    assert wi_solar["cuf"]["source_refs"][0]["kind"] == "measured"
    wi_wind = next(x for x in wi["sizes"] if x["kind"] == "oa_wind_intra")
    assert wi_wind["cuf"]["source_refs"][0]["kind"] == "client_stated"
    # the replay of the library-curve run still takes the library curves
    rl = _replay(lib["id"])
    assert rl["status"] == "done" and rl["identical_to_original"] is True
    assert rl["summary"] == lib["summary"] and "generation_profiles" not in rl["pins"]
    # a new run takes the later solar file, and the library curve for wind again
    new = _run(sid)
    assert list(new["pins"]["generation_profiles"]) == ["solar:open_access"]
    assert new["pins"]["generation_profiles"]["solar:open_access"]["file"] == "solar-later.csv"
    assert new["summary"]["annual"]["after"]["value"] != own["summary"]["annual"]["after"]["value"]
    assert _cuf(new, "oa_wind_intra")["source_refs"][0]["kind"] == "assumption"
    both = client.get(f"/generation-profiles/{sp['id']}", headers=CL).json()
    assert [x["run_id"] for x in both["runs"]] == [rp["id"], own["id"]]
    assert both["runs"][0]["replay_of"] == own["id"]


def test_a_rooftop_profile_is_used_for_the_rooftop_only(mh_site: tuple[str, str]) -> None:
    site, conn = mh_site
    spec = {
        "candidates": [{"kind": "rooftop_pv", "max_mw": "1"}, {"kind": "oa_solar_intra", "max_mw": "4"}],
        "as_of": "2026-06-01",
        "resolution_min": 60,
    }
    sid = _scenario(site, conn, spec, "Roof and solar")
    lib = _run(sid)
    rows = shares("solar", APR, 60)
    data = csv_of(rows, "kWh", D(250))  # a 250 kW roof, kWh in each hour
    up = _upload(
        site,
        data,
        "roof.csv",
        technology="solar",
        provenance="measured",
        basis="capacity",
        capacity_mw="0.25",
        applies_to="rooftop",
    )
    assert up.status_code == 201, up.text
    assert (up.json()["applies_to"], up.json()["capacity_mw"]) == ("rooftop", "0.25")
    own = _run(sid)
    assert list(own["pins"]["generation_profiles"]) == ["solar:rooftop"]
    roof = _cuf(own, "rooftop_pv")
    assert D(roof["value"]) == cuf_pct(rows) and roof["source_refs"][0]["kind"] == "measured"
    # the open-access solar plant is another plant: it keeps the library curve
    assert _cuf(own, "oa_solar_intra")["source_refs"][0]["kind"] == "assumption"
    assert D(_cuf(own, "oa_solar_intra")["value"]) == generation.annual_cuf(
        generation.curve_for("solar", "MH")
    )
    assert own["summary"]["annual"]["after"]["value"] != lib["summary"]["annual"]["after"]["value"]
    rp = _replay(own["id"])
    assert rp["identical_to_original"] is True and rp["summary"] == own["summary"]


def test_settlement_and_landed_cost_take_the_supplied_profile(mh_site: tuple[str, str]) -> None:
    site, conn = mh_site
    body = {"sources": [{"id": "s1", "kind": "oa_solar_intra", "capacity_mw": "3"}], "as_of": "2026-06-01"}
    before = client.post(f"/connections/{conn}/settle", json=body, headers=AN)
    assert before.status_code == 200, before.text
    assert any("normative solar curve" in n for n in before.json()["notes"])
    rows = shares("solar", APR, 60)
    _upload(site, csv_of(rows, "cuf"), "solar.csv", technology="solar", provenance="stated")
    after = client.post(f"/connections/{conn}/settle", json=body, headers=AN)
    assert after.status_code == 200, after.text
    assert any("s1: solar: the profile supplied (solar.csv, stated, " in n for n in after.json()["notes"])
    assert not any("normative solar curve" in n for n in after.json()["notes"])
    assert after.json()["statement"] != before.json()["statement"]
    assert after.json()["months"] != before.json()["months"]


def _captive_solar_cuf(conn: str) -> dict[str, Any]:
    sc = json.dumps({"sources": {"oa_solar_intra": {"ownership": "captive"}}})
    r = client.get(f"/connections/{conn}/landed-cost", params={"scenario": sc})
    assert r.status_code == 200, r.text
    src = next(x for x in r.json()["sources"] if x["id"] == "oa_solar_intra")
    cuf: dict[str, Any] = next(x for x in src["inputs"] if x["label"] == "CUF")
    return cuf


def test_landed_cost_of_an_own_plant_is_worked_at_the_supplied_cuf(mh_site: tuple[str, str]) -> None:
    site, conn = mh_site
    lib = _captive_solar_cuf(conn)
    assert lib["source_refs"][0]["kind"] == "assumption"
    assert D(lib["value"]) == generation.annual_cuf(generation.curve_for("solar", "MH"))
    rows = shares("solar", APR, 60)
    _upload(site, csv_of(rows, "cuf"), "solar.csv", technology="solar", provenance="measured")
    own = _captive_solar_cuf(conn)
    assert D(own["value"]) == cuf_pct(rows) and own["source_refs"][0]["kind"] == "measured"


def test_in_production_a_profile_is_reached_only_by_its_client_and_analysts(
    prod: list[tuple[str, str]],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    restore_committed()
    fake = FakeBillx()
    monkeypatch.setattr(bill_store, "BillxClient", fake.client)
    owner, other = sign_in("owner@acme.in", prod), sign_in("someone@else.in", prod)
    analyst = sign_in("ana@joulewise.in", prod)
    pdf = fake.register("b", msedcl_5mw())
    [bill] = owner.post("/bills", files={"file": ("b.pdf", pdf, "application/pdf")}).json()
    si = owner.post("/intake/from-bills", json={"bill_ids": [bill["id"]]}).json()
    site = owner.post("/sites", json={"intake": si}).json()["site_id"]
    files = {"file": ("solar.csv", csv_of(shares("solar", APR, 60), "cuf"), "text/csv")}
    form = {"technology": "solar", "provenance": "stated"}
    path = f"/sites/{site}/generation-profiles"
    anon = TestClient(app, base_url="https://testserver")
    assert anon.post(path, files=files, data=form).status_code == 401
    assert other.post(path, files=files, data=form).status_code == 403
    up = owner.post(path, files=files, data=form)
    assert up.status_code == 201, up.text
    pid = up.json()["id"]
    assert up.json()["created_by"] == "owner@acme.in"
    for who, code in ((owner, 200), (analyst, 200), (other, 403), (anon, 401)):
        assert who.get(path).status_code == code
        assert who.get(f"/generation-profiles/{pid}").status_code == code
    assert other.post(f"/generation-profiles/{pid}/withdraw").status_code == 403
    assert owner.post(f"/generation-profiles/{pid}/withdraw").status_code == 200
