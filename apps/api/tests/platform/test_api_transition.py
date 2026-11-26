"""The transition plan through the API (Stage 15): start it for a target, read it, replay it, read it
from a share link; the worker moving it on run by run; a target above what the rules and the client's
own limits allow (slow); the two settings of a run the plan rests on; every figure a SourcedValue;
drafts never reach it (invariant 2)."""

from __future__ import annotations

import copy
import itertools
from collections.abc import Iterator
from datetime import date
from decimal import Decimal as D
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from ergplan.core.sourced import SourcedValue
from ergplan.db.base import session_scope
from ergplan.db.models import Connection, Rule, RulebookVersion, Run, Scenario, TransitionPlan
from ergplan.platform import runs, worker
from ergplan.platform import transition as service
from ergplan.platform.app import app
from ergplan.rulebook import service as rulebook
from tests.intake.helpers import result
from tests.platform.test_api_plan import _truncate
from tests.platform.test_api_runs import AN, CL, make_site

client = TestClient(app)
AS_OF = "2026-11-01"
# the cheapest plan with the captive route only and no cost curve: three runs
BASE = {"kind": "lowest_cost", "ownership": "captive", "curve_points": 0, "as_of": AS_OF}
COUNTS = ("order", "resolution_min", "pending_runs", "runs", "page", "curve_points")


def bare_numbers(node: Any, path: str = "") -> list[str]:
    out: list[str] = []
    if isinstance(node, dict):
        if "source_refs" in node and "value" in node:
            SourcedValue.model_validate(node)
            return out
        for k, v in node.items():
            out += bare_numbers(v, f"{path}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            out += bare_numbers(v, f"{path}[{i}]")
    elif isinstance(node, int | float) and not isinstance(node, bool):
        out.append(path)
    return [p for p in out if p.split(".")[-1] not in COUNTS]


@pytest.fixture(scope="module")
def made() -> Iterator[dict[str, Any]]:
    """A PVVNL HV-2 site at 33 kV (one synthetic bill) and its cheapest transition plan, made the way
    the app makes it: the request queues the first run and the worker moves the plan on run by run."""
    _truncate()
    mp = pytest.MonkeyPatch()
    try:
        site, conn = make_site(mp, result("pvvnl_passed"), {"voltage_kv": "33"})
        r = client.post(f"/connections/{conn}/transition-plan", json=BASE, headers=CL)
        assert r.status_code == 202, r.text
        first = r.json()
        solved = worker.drain()
        plan = client.get(f"/connections/{conn}/transition-plan", headers=CL).json()
        yield {"site": site, "conn": conn, "first": first, "solved": solved, "plan": plan}
    finally:
        mp.undo()
        _truncate()


@pytest.fixture(autouse=True)
def clean_db() -> None:  # the module's site is shared by its tests
    return None


def test_unknown_things_and_bad_targets_are_refused(made: dict[str, Any]) -> None:
    conn = made["conn"]
    url = f"/connections/{conn}/transition-plan"
    assert client.post("/connections/missing/transition-plan", json=BASE, headers=CL).status_code == 404
    assert client.get("/connections/missing/transition-plan", headers=CL).status_code == 404
    assert client.get("/transition-plans/missing", headers=CL).status_code == 404
    assert client.post("/transition-plans/missing/replay", headers=AN).status_code == 404
    assert client.post(f"/transition-plans/{made['plan']['id']}/replay", headers=CL).status_code == 403
    assert client.get("/p/not-a-token/transition-plan").status_code == 404
    for bad in (
        {"kind": "share"},  # a share target with no share
        {"kind": "share", "share_pct": "120"},
        {"kind": "lowest_cost", "share_pct": "40"},
        {"kind": "cheapest"},
        {"sources": [{"kind": "iex_dam"}]},  # exchange purchases are not a phase of the plan
        {"sources": [{"kind": "rooftop_pv"}, {"kind": "rooftop_pv"}]},
        {"ownership": "ours"},
        {"unknown": 1},
    ):
        assert client.post(url, json=bad, headers=CL).status_code == 422, bad


def test_the_worker_moves_the_plan_on_run_by_run(made: dict[str, Any]) -> None:
    first, d = made["first"], made["plan"]
    # as asked: the first run is queued and nothing is shown yet
    assert first["status"] == "running" and first["runs"] == 1 and first["pending_runs"] == 1
    assert first["phases"] == [] and first["today"] is None and first["finished_at"] is None
    assert "being worked out" in first["target"]["words"]
    # rooftop, then open access, then storage: each queued when the one before it finished
    assert made["solved"] == 3
    assert d["id"] == first["id"] and d["status"] == "done" and d["finished_at"]
    with session_scope() as s:
        row = s.get(TransitionPlan, d["id"])
        assert row is not None and row.created_by == CL["X-Ergplan-Actor"]
        assert [x["key"] for x in row.steps] == ["rooftop", "open_access:captive", "storage"]


def test_the_plan_lists_its_phases_with_every_figure_sourced(made: dict[str, Any]) -> None:
    d = made["plan"]
    assert d["status"] == "done" and d["pending_runs"] == 0 and d["runs"] == 3
    assert d["state"] == "Uttar Pradesh" and d["discom"] == "pvvnl" and d["asked"]["as_of"] == AS_OF
    phases = d["phases"]
    assert [(p["order"], p["key"]) for p in phases] == [(1, "rooftop"), (2, "open_access"), (3, "storage")]
    assert bare_numbers(d) == []
    before = D(d["today"]["annual_cost"]["value"])
    load = D(d["today"]["load_kwh"]["value"])
    prev = D(0)
    for p in phases:
        # the figures of a phase agree with each other and with the phase before
        cost, saving = D(p["annual_cost"]["value"]), D(p["annual_saving"]["value"])
        assert saving == before - cost and D(p["added_saving"]["value"]) == saving - prev
        grid = D(p["energy"]["grid_kwh"]["value"])
        assert D(p["share"]["value"]) == ((load - grid) / load * 100).quantize(D("0.01"))
        assert p["proven"] is True and p["run_id"] and p["resolution_min"] == 60
        # the saving is derived: it carries the tariff order's page among its sources
        assert any(r["kind"] == "order" and r["url"] for r in p["annual_saving"]["source_refs"])
        prev = saving
    assert phases[0]["route"] is None and phases[1]["route"] == "captive"
    assert [s["kind"] for s in phases[0]["sources"]] == ["rooftop_pv"]
    assert {s["kind"] for s in phases[1]["sources"]} == {"rooftop_pv", "oa_solar_intra", "oa_wind_intra"}
    assert {"btm_bess", "oa_bess"} <= {s["kind"] for s in phases[2]["sources"]}
    # no later phase undoes an earlier one: each source is at least what the phase before built
    for a, b in itertools.pairwise(phases):
        was = {s["kind"]: D(s["capacity"]["value"]) for s in a["sources"]}
        now = {s["kind"]: D(s["capacity"]["value"]) for s in b["sources"]}
        assert all(now[k] >= v - D("0.002") for k, v in was.items())
    # the lowest cost: a target with no share asked, and the plan says what the cheapest plan reaches
    t = d["target"]
    assert t["kind"] == "lowest_cost" and t["asked"] is None and t["met"] is None
    assert t["reached"]["value"] == phases[-1]["share"]["value"] and "cheapest plan replaces" in t["words"]
    assert [a["route"] for a in d["alternatives"]] == ["captive"] and d["alternatives"][0]["chosen"]
    # the cost curve has the phases, and exactly one is marked the cheapest
    assert [c["role"] for c in d["curve"]] == ["phase"] * 3
    assert [c["cheapest"] for c in d["curve"]].count(True) == 1


def test_the_rule_and_the_steps_of_the_open_access_phase(made: dict[str, Any]) -> None:
    oa = made["plan"]["phases"][1]
    text = " ".join(r["text"] for r in oa["rules"])
    assert "15-minute block" in text and "8%" in text  # UPERC CRE Regulations 2024
    assert any(r["ref"]["kind"] == "regulation" and r["ref"]["url"] for r in oa["rules"])
    if oa["status"] == "taken":
        kinds = {a["basis"] for a in oa["actions"]}
        assert kinds == {"rule", "assumption"}
        for a in oa["actions"]:
            if a["basis"] == "assumption":  # general practice is never shown as a rule
                assert a["ref"]["kind"] == "assumption" and "no approved rule" in a["ref"]["note"]
            else:
                assert a["ref"]["rule_id"] and a["ref"]["kind"] != "assumption"
    roof = made["plan"]["phases"][0]
    assert all(a["basis"] == "assumption" for a in roof["actions"])


def test_the_plan_is_read_back_and_asking_again_solves_nothing(made: dict[str, Any]) -> None:
    conn, d = made["conn"], made["plan"]
    got = client.get(f"/connections/{conn}/transition-plan", headers=CL)
    assert got.status_code == 200 and got.json() == d
    assert client.get(f"/transition-plans/{d['id']}", headers=CL).json() == d
    with session_scope() as s:
        runs_before = len(s.scalars(select(Run)).all())
    same = client.post(f"/connections/{conn}/transition-plan", json=BASE, headers=CL)
    assert same.status_code == 202 and same.json()["id"] == d["id"]
    with session_scope() as s:
        assert len(s.scalars(select(Run)).all()) == runs_before


def test_each_phase_is_a_pinned_run_kept_out_of_the_plan_options(made: dict[str, Any]) -> None:
    conn, d = made["conn"], made["plan"]
    for p in d["phases"]:
        run = client.get(f"/runs/{p['run_id']}", headers=CL).json()
        assert run["status"] == "done" and run["pins"]["rulebook_versions"] and run["pins"]["load_profile_id"]
        assert run["pins"]["as_of"] == AS_OF
    ids = [p["scenario_id"] for p in d["phases"]]
    with session_scope() as s:
        names = [sc.name for sc in s.scalars(select(Scenario).where(Scenario.id.in_(ids)))]
        assert len(names) == 3 and all(n.startswith(service.PREFIX) for n in names)
    # the live plan compares options; the transition plan's phases are not options of it
    shown = client.get(f"/connections/{conn}/plan", headers=CL).json()["scenarios"]
    assert not any(x["scenario_id"] in ids or x["name"].startswith(service.PREFIX) for x in shown)


def test_a_share_link_shows_the_plan_logged_out(made: dict[str, Any]) -> None:
    tok = client.post(f"/connections/{made['conn']}/share", headers=CL).json()
    r = client.get(f"/p/{tok['token']}/transition-plan")  # no headers at all
    assert r.status_code == 200 and r.json() == made["plan"]


def test_a_draft_rule_never_reaches_the_plan(made: dict[str, Any]) -> None:
    """Invariant 2: a draft settlement rule with an absurd banking charge changes nothing — neither
    the page of a plan already made, nor what the rule book says to a plan about to be made."""
    conn = made["conn"]

    def read() -> dict[str, str | None]:
        with session_scope() as s:
            c = s.get(Connection, conn)
            assert c is not None
            bk = service.book(s, c, date.fromisoformat(AS_OF))
            return {k: (v.id if v else None) for k, v in bk.settlement.items()}

    before = read()
    assert before["captive"] and before["group_captive"] and before["third_party"] is None
    with session_scope(actor="analyst@joulewise.in", role="analyst") as s:
        v = s.scalars(
            select(RulebookVersion).where(
                RulebookVersion.family == "settlement",
                RulebookVersion.jurisdiction_id == "UP",
                RulebookVersion.status == "approved",
            )
        ).first()
        assert v is not None
        draft = rulebook.duplicate(s, v.id, actor="analyst@joulewise.in")
        rows = []
        for r in s.scalars(select(Rule).where(Rule.rulebook_version_id == draft.id).order_by(Rule.position)):
            body = copy.deepcopy(r.body)
            body["banking"]["charge"] = {"kind": "in_kind_percent", "value": "99"}
            rows.append({"scope": r.scope, "body": body, "source_ref": r.source_ref, "label": r.label})
        rulebook.revise(s, draft.id, rows)
    assert read() == before
    assert client.get(f"/connections/{conn}/transition-plan", headers=CL).json() == made["plan"]


def _run(made: dict[str, Any], name: str, spec: dict[str, Any]) -> dict[str, Any]:
    body = {
        "connection_id": made["conn"],
        "name": name,
        "spec": {"as_of": AS_OF, "resolution_min": 60, **spec},
    }
    sc = client.post(f"/sites/{made['site']}/scenarios", json=body, headers=AN)
    assert sc.status_code == 201, sc.text
    r = client.post(f"/scenarios/{sc.json()['id']}/run", params={"wait": "true"}, headers=AN)
    assert r.status_code == 202, r.text
    out: dict[str, Any] = r.json()
    return out


def _share(summary: dict[str, Any]) -> D:
    load = sum((D(m["load_kwh"]["value"]) for m in summary["monthly"]), D(0))
    return (load - D(summary["settlement"]["grid_kwh"]["value"])) / load * 100


# a rooftop plant too dear to be worth building: the cheapest plan takes none of it
DEAR_ROOF = {"kind": "rooftop_pv", "max_mw": "1", "capex_inr_per_kw": "400000"}


def test_a_run_meets_a_target_share_at_least_cost(made: dict[str, Any]) -> None:
    """The two settings of a run the plan uses (ScenarioSpec.min_share_pct and most_renewable),
    through the runs API on rooftop solar alone (a second each)."""
    free = _run(made, "dear roof", {"candidates": [DEAR_ROOF]})
    assert free["status"] == "done" and D(free["summary"]["sizes"][0]["kw"]["value"]) == 0
    held = _run(made, "dear roof to 2%", {"candidates": [DEAR_ROOF], "min_share_pct": "2"})
    assert held["status"] == "done", held["error"]
    s = held["summary"]
    assert D(s["sizes"][0]["kw"]["value"]) > 0
    # sized to reach the share and no further: it costs more than not building
    assert abs(_share(s) - D(2)) < D("0.01")
    assert D(s["annual"]["after"]["value"]) > D(free["summary"]["annual"]["after"]["value"])
    [b] = [b for b in s["binding"] if b["name"].startswith("limit:min_share")]
    assert "The target you set: at least 2%" in b["words"]
    assert b["refs"][0]["value"] == "2" and b["refs"][0]["source_refs"][0]["kind"] == "client_stated"
    # the target is pinned with the run's spec, so it replays to the paisa
    again = client.post(f"/runs/{held['id']}/replay", params={"wait": "true"}, headers=AN).json()
    assert again["identical_to_original"] is True


def test_a_run_sized_for_the_most_the_limits_allow(made: dict[str, Any]) -> None:
    most = _run(made, "dear roof, most", {"candidates": [DEAR_ROOF], "most_renewable": True})
    assert most["status"] == "done", most["error"]
    s = most["summary"]
    # all the client allowed, although it does not pay; the cost shown is at the real rates
    assert s["sizes"][0]["kw"]["value"] == "1000.000"
    assert any(
        b["name"] == "max_kw:rooftop_pv" and "the most you allowed" in b["words"] for b in s["binding"]
    )
    assert any("sized for the most the rules allow" in n for n in s["notes"])
    lines = most["analyst"]["lines_after"]
    assert D(s["annual"]["after"]["value"]) == sum((D(v) for v in lines.values()), D(0))
    assert D(s["annual"]["after"]["value"]) < D(s["annual"]["before"]["value"]) * 2  # no penalty in it
    # a share that 1 MW of rooftop cannot reach: no plan, and a client never sees the failed run
    none = _run(made, "dear roof to 99%", {"candidates": [DEAR_ROOF], "min_share_pct": "99"})
    assert none["status"] == "failed" and "nfeasible" in none["error"] and none["summary"] is None
    assert client.get(f"/runs/{none['id']}", headers=CL).status_code == 404


def test_a_plan_part_made_replays_what_has_finished(made: dict[str, Any]) -> None:
    """Replay recomputes the plan's finished runs from their pins. Here on a new plan whose first
    run alone has finished (the whole plan's replay is in tests/e2e/test_up_transition.py)."""
    conn = made["conn"]
    r = client.post(f"/connections/{conn}/transition-plan", json=BASE, headers=CL, params={"fresh": "true"})
    assert r.status_code == 202, r.text
    d = r.json()
    assert d["id"] != made["plan"]["id"] and d["status"] == "running" and d["runs"] == 1
    with session_scope() as s:
        row = s.get(TransitionPlan, d["id"])
        assert row is not None
        [first] = [x["run_id"] for x in row.steps]
    assert runs.execute(first) == "done"
    out = client.post(f"/transition-plans/{d['id']}/replay", headers=AN, params={"wait": "true"})
    assert out.status_code == 202, out.text
    rep = out.json()
    assert rep["plan_id"] == d["id"] and rep["identical"] is True
    [one] = rep["runs"]
    assert one["key"] == "rooftop" and one["run_id"] == first and one["status"] == "done" and one["identical"]
    # the replay is a run of its own: the plan still shows its original run, and goes on from it
    part = client.get(f"/transition-plans/{d['id']}", headers=CL).json()
    assert [p["run_id"] for p in part["phases"]] == [first] and part["status"] == "running"
    with session_scope(actor="test") as s:
        row = s.get(TransitionPlan, d["id"])
        assert row is not None
        s.delete(row)  # leave the module's first plan as the connection's latest


@pytest.mark.slow
def test_a_target_above_what_can_be_reached_says_the_most_and_what_holds_it(made: dict[str, Any]) -> None:
    """The client allows only small plants and asks for 95%: the plan is built to the most those
    limits allow, says the target cannot be reached, and names what holds it with its source."""
    caps = [
        {"kind": "rooftop_pv", "max_mw": "0.2"},
        {"kind": "oa_solar_intra", "max_mw": "0.3"},
        {"kind": "oa_wind_intra", "max_mw": "0.3"},
        {"kind": "btm_bess", "max_mw": "0.1"},
        {"kind": "oa_bess", "max_mw": "0.1"},
    ]
    body = {**BASE, "kind": "share", "share_pct": "95", "sources": caps}
    r = client.post(
        f"/connections/{made['conn']}/transition-plan", json=body, headers=CL, params={"wait": "true"}
    )
    assert r.status_code == 202, r.text
    d = r.json()
    assert d["status"] == "done" and bare_numbers(d) == []
    # the cheapest plan already takes all the client allows: there is no further phase to show
    assert [p["key"] for p in d["phases"]] == ["rooftop", "open_access", "storage"]
    t = d["target"]
    most, reached = D(t["most"]["value"]), D(t["reached"]["value"])
    assert t["asked"]["value"] == "95" and t["met"] is False and reached == most < D(95)
    assert "cannot be reached" in t["words"] and "already reaches it" in t["words"]
    assert t["reached_in_phase"] is None and t["most_proven"] is True
    # what holds it: the limits the client set, each cited as the client's own statement
    names = {b["name"] for b in t["most_binding"]}
    assert {"max_kw:rooftop_pv", "max_kw:oa_solar_intra", "max_kw:oa_wind_intra"} <= names
    for b in t["most_binding"]:
        if b["name"].startswith("max_kw:"):
            assert "the most you allowed" in b["words"]
            assert b["refs"][0]["source_refs"][0]["kind"] == "client_stated"
    by = {s["kind"]: D(s["capacity"]["value"]) for s in d["phases"][-1]["sources"]}
    assert by["rooftop_pv"] == D("200.000") and by["oa_wind_intra"] == D("300.000")
    assert d["curve"][-1]["role"] == "most" and d["curve"][-1]["run_id"] == t["most_run_id"]


def test_every_figure_of_the_response_has_an_owner_in_the_field_trace() -> None:
    schemas = app.openapi()["components"]["schemas"]
    mine = [
        "TransitionOut",
        "TransitionPhaseOut",
        "TransitionPhaseSourceOut",
        "TransitionPhaseEnergyOut",
        "TransitionTodayOut",
        "TransitionTargetOut",
        "TransitionCurvePointOut",
        "TransitionCurveSizeOut",
        "TransitionRouteOut",
        "TransitionRuleLineOut",
    ]
    seen = 0
    for name in mine:
        for field, prop in schemas[name]["properties"].items():
            refs = [prop.get("$ref"), *(x.get("$ref") for x in prop.get("anyOf", []))]
            if any(r and r.endswith("/SourcedValue") for r in refs):
                assert prop.get("owner") and prop.get("ref_kinds"), f"{name}.{field}"
                seen += 1
    assert seen >= 40
