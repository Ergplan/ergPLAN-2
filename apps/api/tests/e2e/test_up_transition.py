"""Stage 15 end-to-end: the transition plan for the assumed Uttar Pradesh site of
tests/e2e/test_up_assumed.py (PVVNL HV-2, 5,000 kVA at 33 kV, twelve assumed bills), for a target of
50% renewable energy, through the API.

The plan lists its phases in order (rooftop, open access, storage) with every figure a SourcedValue;
the arithmetic between phases is checked by hand from the figures the page itself shows; the route
taken is one the state's approved settlement rule covers; a step is cited to a rule only where the
rule states it; every run of the plan replays to the paisa.

Slow: it sizes five hourly years (about two and a half minutes) and solves them again for the replay.
Set UP_OUT to keep the page as JSON."""

from __future__ import annotations

import json
import os
from decimal import Decimal as D
from pathlib import Path
from typing import Any

import pytest

from ergplan.core.sourced import SourcedValue
from tests.e2e import test_up_assumed as up

pytestmark = pytest.mark.slow
client = up.client
AN = up.AN
OUT = os.environ.get("UP_OUT")
# bare numbers a page may carry: counts and the page number of a citation, never a figure
COUNTS = ("order", "resolution_min", "pending_runs", "runs", "page", "curve_points")


def figures(node: Any, path: str = "", found: list[SourcedValue] | None = None) -> list[str]:
    """Paths of bare numbers on the page; every SourcedValue met is validated and collected."""
    out: list[str] = []
    if isinstance(node, dict):
        if "source_refs" in node and "value" in node:
            sv = SourcedValue.model_validate(node)
            if found is not None:
                found.append(sv)
            return out
        for k, v in node.items():
            out += figures(v, f"{path}.{k}", found)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            out += figures(v, f"{path}[{i}]", found)
    elif isinstance(node, int | float) and not isinstance(node, bool):
        out.append(path)
    return out


def val(x: dict[str, Any]) -> D:
    return D(x["value"])


def test_up_transition_plan_to_half_renewable(monkeypatch: pytest.MonkeyPatch) -> None:
    if not OUT:
        monkeypatch.setattr(up, "save", lambda *a, **k: None)  # the site's own notes are not kept
    site = up.make()
    body = {"kind": "share", "share_pct": "50", "as_of": up.AS_OF, "resolution_min": 60, "curve_points": 0}
    r = client.post(
        f"/connections/{site['conn']}/transition-plan", json=body, headers=AN, params={"wait": "true"}
    )
    assert r.status_code == 202, r.text
    d = r.json()
    if OUT:
        Path(OUT, "30_transition_plan.json").write_text(json.dumps(d, indent=1))
    assert d["status"] == "done" and d["pending_runs"] == 0 and d["not_worked_out"] == []
    assert d["state"] == "Uttar Pradesh" and d["discom"] == "pvvnl"

    # the phases in order, each a settled run of its own
    phases = d["phases"]
    assert [(p["order"], p["key"]) for p in phases] == [(1, "rooftop"), (2, "open_access"), (3, "storage")]
    assert len({p["run_id"] for p in phases}) == 3
    assert all(p["proven"] and p["proven_words"] is None for p in phases)

    # every figure is a SourcedValue: no bare number but counts, and every document citation opens
    # the stored page
    found: list[SourcedValue] = []
    bare = [p for p in figures(d, found=found) if p.split(".")[-1] not in COUNTS]
    assert bare == []
    assert len(found) > 100
    for sv in found:
        for ref in sv.source_refs:
            if ref.kind in ("order", "regulation"):
                assert ref.url == f"/documents/{ref.document_id}#page={ref.page}" and ref.quote

    # today: the twelve assumed bills' year, as tests/e2e/test_up_assumed.py's own run states it
    today = d["today"]
    assert today["annual_cost"]["value"] == "191476310.78"
    assert today["load_kwh"]["value"] == "22606225.914"
    before, load = val(today["annual_cost"]), val(today["load_kwh"])

    roof, oa, store = phases
    # phase 1, rooftop: as much as the default bound lets it (no roof area was stated: an assumption,
    # and the page says that is what holds it)
    [pv] = roof["sources"]
    assert pv["kind"] == "rooftop_pv" and pv["capacity"]["value"] == "5000.000"
    assert pv["added"]["value"] == "5000.000" and pv["capital"]["value"] == "200000000.00"
    assert pv["capital"]["source_refs"][0]["kind"] == "assumption" and pv["tariff"] is None
    assert roof["annual_cost"]["value"] == "164733922.20"
    assert roof["annual_saving"]["value"] == "26742388.58"
    assert any(b["name"] == "max_kw:rooftop_pv" and "assumption" in b["words"] for b in roof["binding"])
    assert roof["route"] is None and roof["status"] == "taken"

    # phase 2, open access: captive wind on top of the rooftop, which stays as built
    by = {s["kind"]: s for s in oa["sources"]}
    assert oa["route"] == "captive" and oa["status"] == "taken"
    assert (
        by["rooftop_pv"]["capacity"]["value"] == "5000.000" and by["rooftop_pv"]["added"]["value"] == "0.000"
    )
    assert by["oa_wind_intra"]["ownership"] == "captive"
    assert val(by["oa_wind_intra"]["capacity"]) == pytest.approx(D("4609.550"), abs=D("0.5"))
    assert val(by["oa_solar_intra"]["capacity"]) == 0  # wind lands in more of the factory's hours
    assert by["oa_wind_intra"]["capital"] is not None and by["oa_wind_intra"]["tariff"] is None
    # to the rupee (the last paise move with the solver's path; the replay below is to the paisa)
    assert val(oa["annual_cost"]) == pytest.approx(D("139609716"), abs=D("1"))

    # phase 3, storage: at today's battery cost the cheapest plan takes none, and the page says so
    assert store["status"] == "adds_nothing" and store["status_words"] and store["actions"] == []
    assert all(val(s["capacity"]) == 0 for s in store["sources"] if s["kind"] in ("btm_bess", "oa_bess"))
    assert abs(val(store["added_saving"])) < D("1")

    # the arithmetic of each phase, by hand from the figures shown
    prev_saving, prev_capital, prev_share = D(0), D(0), D(0)
    for p in phases:
        cost, saving = val(p["annual_cost"]), val(p["annual_saving"])
        assert saving == before - cost
        assert val(p["added_saving"]) == saving - prev_saving
        assert val(p["added_capital"]) == val(p["capital"]) - prev_capital
        grid = val(p["energy"]["grid_kwh"])
        assert val(p["share"]) == ((load - grid) / load * 100).quantize(D("0.01"))
        assert val(p["added_share"]) == val(p["share"]) - prev_share
        assert val(p["capital"]) == sum((val(s["capital"]) for s in p["sources"] if s["capital"]), D(0))
        # the energy balance of the settled year: what is set off as it arrives and what is drawn
        # from the bank and from the DISCOM make up the consumption (to the kWh)
        e = p["energy"]
        made = val(e["setoff_kwh"]) + val(e["withdrawn_kwh"]) + val(e["grid_kwh"])
        assert abs(made - load) < D("1")
        prev_saving, prev_capital, prev_share = saving, val(p["capital"]), val(p["share"])
    assert roof["share"]["value"] == "31.88"
    assert val(oa["share"]) == pytest.approx(D("80.16"), abs=D("0.01"))

    # the target: 50% is the client's own figure, and it is reached in phase 2
    t = d["target"]
    assert t["asked"]["value"] == "50" and t["asked"]["source_refs"][0]["kind"] == "client_stated"
    assert t["met"] is True and t["reached_in_phase"] == "open_access"
    assert t["words"] == "Your target of 50% is reached in phase 2 of the cheapest plan."

    # the state's rule decides the route: captive and group captive are covered by the approved
    # settlement rule (UPERC CRE Regulations 2024); third party is sized but not taken
    routes = {a["route"]: a for a in d["alternatives"]}
    assert routes["captive"]["chosen"] and routes["captive"]["settlement_rule_approved"]
    assert routes["group_captive"]["settlement_rule_approved"] and not routes["group_captive"]["chosen"]
    assert val(routes["group_captive"]["annual_cost"]) > val(routes["captive"]["annual_cost"])
    assert not routes["third_party"]["settlement_rule_approved"] and not routes["third_party"]["chosen"]
    assert "no approved settlement rule" in routes["third_party"]["words"]

    # the rule in words with its page: 15-minute set-off, banking at 8% in kind, monthly
    rules = " ".join(x["text"] for x in oa["rules"])
    assert "15-minute block" in rules and "8%" in rules and "month" in rules
    cited = [x for x in oa["rules"] if x["ref"]["kind"] == "regulation"]
    assert cited and all(x["ref"]["url"].startswith("/documents/") and x["ref"]["page"] for x in cited)

    # what must be signed or applied for: cited to the rule only where the rule states it
    ruled = [a for a in oa["actions"] if a["basis"] == "rule"]
    assert len(ruled) == 1 and "day ahead" in ruled[0]["what"]
    assert ruled[0]["ref"]["kind"] in ("regulation", "register") and ruled[0]["ref"]["rule_id"]
    assumed = [a for p in phases for a in p["actions"] if a["basis"] == "assumption"]
    assert len(assumed) >= 8
    assert all(
        a["ref"]["kind"] == "assumption" and "Uttar Pradesh rule book" in a["ref"]["note"] for a in assumed
    )
    assert any("open access" in a["what"] for a in assumed) and any("26%" in a["what"] for a in assumed)
    assert any("connectivity of the rooftop" in a["what"] for a in assumed)

    # the plan is read back as it was made, and asking again for the same target solves nothing
    again = client.get(f"/connections/{site['conn']}/transition-plan", headers=AN).json()
    assert again["id"] == d["id"] and again["phases"] == phases
    same = client.post(f"/connections/{site['conn']}/transition-plan", json=body, headers=AN).json()
    assert same["id"] == d["id"] and same["runs"] == d["runs"] == 5

    # every run of the plan is pinned and replays to the paisa (invariant 5)
    rep = client.post(f"/transition-plans/{d['id']}/replay", headers=AN, params={"wait": "true"})
    assert rep.status_code == 202, rep.text
    replayed = rep.json()
    assert len(replayed["runs"]) == 5 and replayed["identical"] is True
    assert all(x["identical"] for x in replayed["runs"])
    if OUT:
        Path(OUT, "31_transition_replay.json").write_text(json.dumps(replayed, indent=1))
