"""Stage 15, the consumption-profile questions: list, answer, change, withdraw; each answer is a
`client_stated` value that makes a new load-profile version; a measured file replaces the synthesis."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from ergplan.db.base import session_scope
from ergplan.db.models import Connection, SiteAnswer
from ergplan.intake import bills as bill_store
from ergplan.intake import questions
from ergplan.platform.app import app
from ergplan.platform.routers.cost import _kvah_factor
from tests.intake.helpers import FakeBillx, result, with_month

client = TestClient(app)
AN = {"X-Ergplan-Actor": "analyst@joulewise.in", "X-Ergplan-Role": "analyst"}
CL = {"X-Ergplan-Actor": "client@acme.in", "X-Ergplan-Role": "client"}
SYNTHESIS_ONLY = ["shifts", "weekly_off", "seasonal_shutdowns", "large_loads"]
ALL = [*SYNTHESIS_ONLY, "power_factor", "expansion", "roof_sqm", "land_acres", "contract_demand_kva"]


@pytest.fixture
def site(monkeypatch: pytest.MonkeyPatch) -> tuple[str, str]:
    """A site made from two accepted bills (June and July 2026), nothing typed beyond them."""
    fake = FakeBillx()
    monkeypatch.setattr(bill_store, "BillxClient", fake.client)
    ids = []
    for month in ("2026-06", "2026-07"):
        r = client.post(
            "/bills",
            files={
                "file": (
                    f"{month}.pdf",
                    fake.register(month, with_month(result("pvvnl_passed"), month)),
                    "application/pdf",
                )
            },
        )
        assert r.status_code == 201, r.text
        ids.append(r.json()[0]["id"])
    si = client.post("/intake/from-bills", json={"bill_ids": ids}).json()
    out = client.post("/sites", json={"intake": si})
    assert out.status_code == 201, out.text
    return str(out.json()["site_id"]), str(out.json()["connection_id"])


def _put(site_id: str, key: str, value: Any, headers: dict[str, str] = CL) -> dict[str, Any]:
    r = client.put(f"/sites/{site_id}/questions/{key}", json={"value": value}, headers=headers)
    assert r.status_code == 200, r.text
    return dict(r.json())


def _profile(conn: str, version: int | None = None) -> dict[str, Any]:
    r = client.get(f"/connections/{conn}/profile", params={"version": version} if version else None)
    assert r.status_code == 200, r.text
    return dict(r.json())


def _csv(conn: str, version: int) -> str:
    return client.get(f"/connections/{conn}/profile.csv", params={"version": version}).text


def test_the_questions_are_listed_in_plain_words(site: tuple[str, str]) -> None:
    sid, _ = site
    d = client.get(f"/sites/{sid}/questions").json()
    assert d["site_id"] == sid and d["measured"] is False
    # the bills print kWh and kVAh, so the power factor is known from them (Stage 17)
    assert [q["key"] for q in d["questions"]] == ALL and d["outstanding"] == [
        k for k in ALL if k != "power_factor"
    ]
    for q in d["questions"]:
        assert q["question"].endswith("?") and q["unit"] and q["why"] and q["used_for"]
        assert q["asked"] is True and q["answer"] is None and q["until_answered"]
        assert "_" not in q["label"] and "_" not in q["question"] and "_" not in q["until_answered"]
    by = {q["key"]: q for q in d["questions"]}
    assert (
        by["shifts"]["until_answered"]
        == "The operating pattern chosen with the inputs (Two-shift manufacturing)."
    )
    assert by["weekly_off"]["until_answered"] == "Sunday (from the inputs)."
    assert "held today (" in by["contract_demand_kva"]["until_answered"]
    assert by["land_acres"]["used_for"].endswith("it bounds nothing today.")
    assert by["power_factor"]["known_from_inputs"] == "worked from every bill's kWh and kVAh"
    assert client.get("/sites/missing/questions").status_code == 404


def test_answer_change_and_withdraw(site: tuple[str, str]) -> None:
    sid, conn = site
    v1 = _profile(conn)
    assert v1["version"] == 1 and v1["provenance"] == "synthesised"
    out = _put(sid, "shifts", [{"start": "08:00", "end": "16:00"}])
    assert out["changed"] is True and "shifts" not in out["outstanding"]
    a = out["question"]["answer"]
    assert a["words"] == "08:00 to 16:00" and a["stated_by"] == "client@acme.in" and a["stated_at"]
    assert a["source_ref"]["kind"] == "client_stated" and a["source_ref"]["url"] is None
    assert out["profiles"] == [
        {
            "connection_id": conn,
            "load_profile_id": out["profiles"][0]["load_profile_id"],
            "version": 2,
            "provenance": "synthesised",
        }
    ]
    v2 = _profile(conn)
    assert v2["version"] == 2 and v2["id"] == out["profiles"][0]["load_profile_id"]
    # the profile is redrawn, and still holds the bills' energy and recorded demand
    assert v2["week_kw"] != v1["week_kw"]
    billed = [m for m in v1["months"] if not m["extrapolated"]]
    assert [m["month"] for m in billed] == ["2026-06-01", "2026-07-01"]
    for m1, m2 in zip(v1["months"], v2["months"], strict=True):
        assert m2["energy_kwh"]["value"] == m1["energy_kwh"]["value"]
        if not m1["extrapolated"]:
            assert m2["max_kw"]["value"] == m1["max_kw"]["value"]
            kinds = [r["kind"] for r in m2["max_kw"]["source_refs"]]
            assert "client_stated" in kinds and "bill" in kinds
            assert "client_stated" not in [r["kind"] for r in m1["max_kw"]["source_refs"]]
    assert v2["method"]["answers"]["shifts"]["stated_by"] == "client@acme.in"
    # the same answer again changes nothing; a different one is a new version, by whoever states it
    same = _put(sid, "shifts", [{"start": "08:00", "end": "16:00"}])
    assert same["changed"] is False and same["profiles"] == [] and _profile(conn)["version"] == 2
    out3 = _put(sid, "shifts", [{"start": "06:00", "end": "14:00"}, {"start": "14:00", "end": "22:00"}], AN)
    assert out3["changed"] and out3["profiles"][0]["version"] == 3
    assert out3["question"]["answer"]["stated_by"] == "analyst@joulewise.in"
    assert out3["question"]["answer"]["words"] == "06:00 to 14:00, 14:00 to 22:00"
    listed = {q["key"]: q for q in client.get(f"/sites/{sid}/questions").json()["questions"]}
    assert listed["shifts"]["answer"]["words"] == "06:00 to 14:00, 14:00 to 22:00"
    # withdrawn: back to what ergPLAN used before, to the watt; every statement is kept in the table
    back = client.delete(f"/sites/{sid}/questions/shifts", headers=CL)
    assert back.status_code == 200, back.text
    assert back.json()["changed"] and back.json()["question"]["answer"] is None
    assert back.json()["profiles"][0]["version"] == 4 and "shifts" in back.json()["outstanding"]
    assert _csv(conn, 4) == _csv(conn, 1) and _csv(conn, 2) != _csv(conn, 1)
    assert "answers" not in _profile(conn)["method"]
    again = client.delete(f"/sites/{sid}/questions/shifts", headers=CL).json()
    assert again["changed"] is False and _profile(conn)["version"] == 4
    with session_scope() as s:
        rows = s.scalars(
            select(SiteAnswer).where(SiteAnswer.site_id == sid).order_by(SiteAnswer.stated_at)
        ).all()
        assert [(r.stated_by, r.superseded_at is not None, r.withdrawn_by) for r in rows] == [
            ("client@acme.in", True, None),
            ("analyst@joulewise.in", True, "client@acme.in"),
        ]


def test_refusals(site: tuple[str, str]) -> None:
    sid, conn = site
    bad = client.put(f"/sites/{sid}/questions/power_factor", json={"value": "1.4"}, headers=CL)
    assert bad.status_code == 422 and "less than or equal to 1" in bad.json()["detail"]
    bad = client.put(f"/sites/{sid}/questions/shifts", json={"value": [{"start": "8am", "end": "16:00"}]})
    assert bad.status_code == 422 and "HH:MM" in bad.json()["detail"]
    assert client.put(f"/sites/{sid}/questions/shifts", json={}).status_code == 422
    assert client.put(f"/sites/{sid}/questions/shifts", json={"value": [], "x": 1}).status_code == 422
    assert client.put(f"/sites/{sid}/questions/favourite", json={"value": 1}).status_code == 404
    assert client.delete(f"/sites/{sid}/questions/favourite").status_code == 404
    assert client.put("/sites/missing/questions/shifts", json={"value": []}).status_code == 404
    assert client.delete("/sites/missing/questions/shifts").status_code == 404
    assert _profile(conn)["version"] == 1  # nothing refused made a profile
    with session_scope() as s:
        assert s.scalars(select(SiteAnswer)).first() is None


def test_each_shaping_answer_makes_a_version_that_still_matches_the_bills(site: tuple[str, str]) -> None:
    sid, conn = site
    v1 = _profile(conn)
    answers: list[tuple[str, Any]] = [
        ("weekly_off", [4]),
        ("seasonal_shutdowns", [{"start": "06-10", "end": "06-14", "name": "overhaul"}]),
        ("large_loads", [{"name": "Furnace", "kw": "150", "start": "09:00", "end": "12:00"}]),
    ]
    seen = {_csv(conn, 1)}
    for n, (key, value) in enumerate(answers, start=2):
        out = _put(sid, key, value)
        assert out["profiles"][0]["version"] == n and not out["warnings"]
        p = _profile(conn)
        for m1, m2 in zip(v1["months"], p["months"], strict=True):
            if not m1["extrapolated"]:
                assert m2["energy_kwh"]["value"] == m1["energy_kwh"]["value"]
                assert m2["max_kw"]["value"] == m1["max_kw"]["value"]
        series = _csv(conn, n)
        assert series not in seen  # every answer changed the profile
        seen.add(series)
    # the loads named add up to more than the contract demand: accepted, and said
    big = _put(sid, "large_loads", [{"name": "Arc furnace", "kw": "90000", "start": "09:00", "end": "12:00"}])
    assert big["changed"] and "more than the contract demand" in big["warnings"][0]
    # what is not outstanding any more
    assert client.get(f"/sites/{sid}/questions").json()["outstanding"] == [
        "shifts",
        "expansion",
        "roof_sqm",
        "land_acres",
        "contract_demand_kva",
    ]
    # the service functions the MCP tools will call give the same
    with session_scope() as s:
        assert next(q.key for q in questions.outstanding(s, sid)) == "shifts"
        assert len(questions.listing(s, sid)) == 9


def _meter_csv() -> bytes:
    start = datetime(2026, 4, 1)
    rows = ["timestamp,kW"] + [
        f"{start + timedelta(minutes=15 * i):%Y-%m-%d %H:%M},{1000 + i % 7}" for i in range(365 * 96)
    ]
    return "\n".join(rows).encode()


def test_a_measured_file_replaces_the_synthesis(site: tuple[str, str]) -> None:
    sid, conn = site
    _put(sid, "shifts", [{"start": "08:00", "end": "16:00"}])  # synthesised v2
    up = client.post(
        f"/connections/{conn}/profile/measured", files={"file": ("meter.csv", _meter_csv(), "text/csv")}
    )
    assert up.status_code == 201 and up.json()["provenance"] == "measured" and up.json()["version"] == 3
    measured = _csv(conn, 3)
    # what only shapes the synthesis is no longer asked; the rest still is
    d = client.get(f"/sites/{sid}/questions").json()
    assert d["measured"] is True
    by = {q["key"]: q for q in d["questions"]}
    for key in SYNTHESIS_ONLY:
        assert by[key]["asked"] is False and "measured 15-minute file" in by[key]["not_asked"]
    assert d["outstanding"] == ["expansion", "roof_sqm", "land_acres", "contract_demand_kva"]
    # an answer makes a new version, and it is the measured series again, with the answer pinned
    out = _put(sid, "roof_sqm", "8000")
    assert out["profiles"][0]["version"] == 4 and out["profiles"][0]["provenance"] == "measured"
    p = _profile(conn)
    assert p["provenance"] == "measured" and p["method"]["answers"]["roof_sqm"]["value"] == "8000"
    assert p["method"]["file"] == "meter.csv" and p["method"]["carried_from_version"] == 3
    assert p["months"][0]["energy_kwh"]["source_refs"][0]["kind"] == "measured"
    assert _csv(conn, 4) == measured
    # an answer about shifts is kept but shapes nothing while the measured file is in use
    late = _put(sid, "weekly_off", [5, 6])
    assert late["question"]["asked"] is False and _csv(conn, late["profiles"][0]["version"]) == measured
    # the intake rebuilt (a typed correction): the synthesis runs, and the measured file still wins
    si = client.get(f"/connections/{conn}/intake").json()
    again = client.post("/sites", json={"intake": si, "connection_id": conn})
    assert again.status_code == 201, again.text
    now = _profile(conn)
    assert now["provenance"] == "measured" and now["id"] == again.json()["load_profile_id"]
    assert _csv(conn, now["version"]) == measured
    # the report says the profile is measured, and shows the answers as client-stated
    plan = client.get(f"/connections/{conn}/plan", headers=CL).json()
    assert plan["profile_provenance"] == "measured"
    assert plan["profile_words"] == "Measured: the 15-minute meter file meter.csv."
    shown = {a["key"]: a for a in plan["answers"]}
    assert set(shown) == {"shifts", "weekly_off", "roof_sqm"}
    assert shown["shifts"]["in_use"] is False and shown["roof_sqm"]["in_use"] is True
    assert shown["roof_sqm"]["figures"][0]["source_refs"][0]["kind"] == "client_stated"


def test_the_report_shows_answers_as_client_stated_never_as_sourced(site: tuple[str, str]) -> None:
    sid, conn = site
    stated: dict[str, Any] = {
        "shifts": [{"start": "06:00", "end": "14:00"}, {"start": "14:00", "end": "22:00"}],
        "weekly_off": [6],
        "seasonal_shutdowns": [{"start": "12-24", "end": "01-02", "name": "year end"}],
        "large_loads": [{"name": "Compressors", "kw": "120", "start": "10:00", "end": "15:00"}],
        "power_factor": "0.92",
        "expansion": {"growth_pct": "20", "note": "second line in 2027"},
        "roof_sqm": "8000",
        "land_acres": "12.5",
        "contract_demand_kva": "600",
    }
    for key, value in stated.items():
        _put(sid, key, value)
    assert client.get(f"/sites/{sid}/questions").json()["outstanding"] == []
    plan = client.get(f"/connections/{conn}/plan", headers=CL).json()
    assert plan["profile_provenance"] == "synthesised"
    assert plan["profile_words"] == (
        "Estimated from 2 months of bills and 9 answers the client gave about the plant, not measured. "
        "A 15-minute meter file replaces the estimate."
    )
    assert [a["key"] for a in plan["answers"]] == ALL
    words = {a["key"]: a["words"] for a in plan["answers"]}
    assert words == {
        "shifts": "06:00 to 14:00, 14:00 to 22:00",
        "weekly_off": "Sunday",
        "seasonal_shutdowns": "24 Dec to 2 Jan (year end)",
        "large_loads": "Compressors: 120 kW, 10:00 to 15:00",
        "power_factor": "0.92",
        "expansion": "20% more consumption than today (second line in 2027)",
        "roof_sqm": "8000 m²",
        "land_acres": "12.5 acres",
        "contract_demand_kva": "600 kVA",
    }
    for a in plan["answers"]:
        assert a["source_ref"]["kind"] == "client_stated" and a["source_ref"]["url"] is None
        assert a["stated_by"] == "client@acme.in" and a["stated_at"] and a["used_for"]
        assert a["in_use"] is True and a["in_this_plan"] is None  # no plan sized yet
        for f in a["figures"]:
            assert [r["kind"] for r in f["source_refs"]] == ["client_stated"]
            assert isinstance(f["value"], str)
    figures = {a["key"]: [(f["value"], f["unit"]) for f in a["figures"]] for a in plan["answers"]}
    assert figures["power_factor"] == [("0.92", "ratio")] and figures["expansion"] == [("20", "percent")]
    assert figures["large_loads"] == [("120", "kW")] and figures["contract_demand_kva"] == [("600", "kVA")]
    assert figures["shifts"] == [] and figures["roof_sqm"] == [("8000", "count")]


def test_values_typed_with_the_inputs_are_not_asked_again() -> None:
    si = {
        "client_name": {"value": "Acme"},
        "site_name": {"value": "P"},
        "state": {"value": "MH"},
        "discom": {"value": "msedcl"},
        "category": {"value": "HT I(A)"},
        "voltage_kv": {"value": "22"},
        "contract_demand_kva": {"value": "2200"},
        "industry_shape": {"value": "two_shift"},
        "weekly_off": {"value": [6]},
        "roof_sqm": {"value": "5000"},
        "land_acres": {"value": "3"},
        "months": [{"month": "2026-04-01", "energy": {"value": "1000000"}, "energy_unit": "kVAh"}],
    }
    built = client.post("/sites", json={"intake": si}).json()
    sid, conn = built["site_id"], built["connection_id"]
    d = client.get(f"/sites/{sid}/questions").json()
    by = {q["key"]: q for q in d["questions"]}
    assert (
        by["roof_sqm"]["known_from_inputs"] == "5000 m²"
        and by["land_acres"]["known_from_inputs"] == "3 acres"
    )
    assert by["weekly_off"]["known_from_inputs"] == "Sunday"
    assert d["outstanding"] == [
        "shifts",
        "seasonal_shutdowns",
        "large_loads",
        "power_factor",
        "expansion",
        "contract_demand_kva",
    ]
    # billed in kVAh with no power factor printed: 0.95 is assumed until the client states one
    apr = _profile(conn)["months"][0]
    assert apr["energy_kwh"]["value"] == "950000.000"
    with session_scope() as s:
        c = s.get(Connection, conn)
        assert c is not None
        factor, pf = _kvah_factor(s, c)
        assert pf.value == Decimal("0.95") and pf.source_refs[0].kind == "assumption"
    _put(sid, "power_factor", "0.9")
    apr = _profile(conn)["months"][0]
    assert apr["energy_kwh"]["value"] == "900000.000"
    notes = [r.get("note") or "" for r in apr["energy_kwh"]["source_refs"]]
    assert any("the power factor the client stated" in n for n in notes)
    assert not any("0.95" in n for n in notes)
    with session_scope() as s:
        c = s.get(Connection, conn)
        assert c is not None
        factor, pf = _kvah_factor(s, c)
        assert pf.value == Decimal("0.9") and pf.source_refs[0].kind == "client_stated"
        assert factor.value == 1 / Decimal("0.9") and factor.source_refs == pf.source_refs
