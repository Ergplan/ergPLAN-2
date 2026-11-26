"""The transition plan's pure part (report.transition): the ask, the order of the runs, and the page
built from finished runs — on made-up run results with figures worked by hand."""

from __future__ import annotations

from decimal import Decimal as D
from typing import Any

import pytest
from pydantic import ValidationError

from ergplan.core.sourced import SourcedValue
from ergplan.optimizer.scenarios import CandidateSpec
from ergplan.report import transition as tp
from ergplan.rulebook.models import RuleView

REG = {
    "kind": "regulation",
    "document_id": "doc-1",
    "page": 16,
    "quote": "Banking of energy subject to a maximum ceiling",
}


def j(value: str, unit: str, kind: str = "assumption", label: str | None = None) -> dict[str, Any]:
    ref = REG if kind == "regulation" else {"kind": kind, "note": "made up for the test"}
    return {"value": value, "unit": unit, "label": label, "source_refs": [ref]}


def size(kind: str, kw: str, **kw_: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": kind,
        "kind": kind,
        "label": {"rooftop_pv": "Rooftop solar", "oa_wind_intra": "Open-access wind"}.get(kind, kind),
        "kw": j(kw, "kW"),
        "waterfall": {},
        "gaps": [],
        "ownership": kw_.pop("ownership", None),
    }
    if kind in tp.BATTERIES:
        row["kwh"] = j(kw_.pop("kwh", "0"), "kWh")
    else:
        row["delivered_kwh"] = j(kw_.pop("delivered", "0"), "kWh")
        row["setoff_kwh"] = j(kw_.pop("setoff", "0"), "kWh")
        row["energy_cost"] = j("0", "INR")
    row.update(kw_)
    return row


def summary(
    sizes: list[dict[str, Any]], after: str, grid: str, capex: str = "0", before: str = "1000.00"
) -> dict[str, Any]:
    """A run's result: a year of 1,000 kWh at ₹1,000 today."""
    return {
        "sizes": sizes,
        "annual": {
            "before": j(before, "INR", "regulation"),
            "after": j(after, "INR", "regulation"),
            "saving": j(str(D(before) - D(after)), "INR", "regulation"),
            "capex": j(capex, "INR"),
        },
        "owned_capital": {
            "assets": [
                {
                    "id": z["id"],
                    "label": z["label"],
                    "kind": "plant",
                    "capex": j(capex, "INR"),
                    "words": "own",
                }
                for z in sizes
                if z["kind"] == "rooftop_pv" and D(z["kw"]["value"]) > 0
            ]
        },
        "settlement": {
            "load_kwh": j("1000.000", "kWh", "measured"),
            "grid_kwh": j(grid, "kWh", "regulation"),
            "lapsed_kwh": j("0", "kWh", "regulation"),
            "withdrawn_kwh": j("0", "kWh", "regulation"),
        },
        "binding": [],
        "notes": [],
        "payback_years": None,
        "irr_pct": None,
        "provenance": None,
    }


def step(key: str, role: str, s: dict[str, Any] | None, **kw: Any) -> tp.Step:
    status = kw.pop("status", "done" if s is not None else "failed")
    return tp.Step(
        key=key,
        role=role,
        summary=s,
        status=status,
        run_id=f"run-{key}",
        solver=kw.pop("solver", {"status": "optimal", "gap": 0.0}),
        **kw,
    )


def rule(day_ahead: bool = True) -> RuleView:
    return RuleView(
        id="rule-1",
        family="settlement",
        version_id="v1",
        jurisdiction_id="UP",
        scope={},
        body={
            "granularity": "block_15min",
            "banking": {
                "allowed": True,
                "period": "monthly",
                "charge": {"kind": "in_kind_percent", "value": "8"},
                "requires_day_ahead_schedule": day_ahead,
            },
        },
        source_ref=REG,
    )


BOOK = tp.Book(
    state="Uttar Pradesh",
    discom="pvvnl",
    settlement={"captive": rule(), "group_captive": rule(), "third_party": None},
)
ROOF = summary(
    [size("rooftop_pv", "100.000", delivered="200.000")], after="900.00", grid="800.000", capex="4000"
)
OA = summary(
    [
        size("rooftop_pv", "100.000", delivered="200.000"),
        size("oa_solar_intra", "0.000", ownership="captive"),
        size("oa_wind_intra", "150.000", delivered="400.000", setoff="350.000", ownership="captive"),
    ],
    after="700.00",
    grid="450.000",
    capex="9000",
)
STORE = summary(
    [*OA["sizes"], size("btm_bess", "0.000", kwh="0.000"), size("oa_bess", "0.000", kwh="0.000")],
    after="700.00",
    grid="450.000",
    capex="9000",
)


def test_the_ask_is_validated() -> None:
    assert tp.Target().kind == "lowest_cost"
    assert tp.Target(kind="share", share_pct=D(50)).share_pct == D(50)
    for bad in (
        {"kind": "share"},
        {"kind": "lowest_cost", "share_pct": "50"},
        {"kind": "share", "share_pct": "0"},
        {"kind": "share", "share_pct": "101"},
        {"sources": [{"kind": "rooftop_pv"}, {"kind": "rooftop_pv"}]},
        {"sources": [{"kind": "iex_dam"}]},  # the exchange is not a phase of the plan (Stage 18)
        {"curve_points": 7},
    ):
        with pytest.raises(ValidationError):
            tp.Target.model_validate(bad)


def test_what_is_built_stays_as_a_minimum_just_below_the_figure_shown() -> None:
    assert tp.floor_mw(D("5000.000")) == D("4.999999")
    assert tp.floor_mw(D("4609.550")) == D("4.609549")
    assert tp.floor_mw(D("0.000")) == D("0")
    assert tp.floor_mw(D("0.0004")) == D("0")


def test_share_is_the_consumption_not_drawn_from_the_discom() -> None:
    s = tp.share_of(ROOF)
    assert s.value == D("20.00") and s.unit == "percent"
    # derived: it carries the refs of the load and of the grid drawal
    assert {r.kind for r in s.source_refs} == {"measured", "regulation"}
    assert tp.share_of(OA).value == D("55.00")


def test_each_phase_sizes_its_sources_and_those_before_it() -> None:
    def kinds(upto: str, route: str | None) -> list[str]:
        return [c.kind for c in tp.candidates(upto, route, {}, [])]

    assert kinds("rooftop", None) == ["rooftop_pv"]
    assert kinds("open_access", "captive") == ["rooftop_pv", "oa_solar_intra", "oa_wind_intra"]
    assert kinds("storage", "captive") == [
        "rooftop_pv",
        "oa_solar_intra",
        "oa_wind_intra",
        "btm_bess",
        "oa_bess",
    ]
    # no open-access route worked: storage is the battery at the site only
    assert kinds("storage", None) == ["rooftop_pv", "btm_bess"]
    # no exchange source in any phase (Stage 18 adds it)
    assert not any(k.startswith("iex_") for k in kinds("storage", "third_party"))
    with pytest.raises(NotImplementedError, match="Stage 18"):
        tp.exchange_phase()


def test_candidates_carry_the_route_what_was_built_and_what_the_client_states() -> None:
    stated = [
        CandidateSpec(kind="rooftop_pv", max_mw=D("1"), capex_inr_per_kw=D("38000")),
        CandidateSpec(kind="oa_wind_intra", price_inr_per_kwh=D("3.9"), min_mw=D("2")),
    ]
    built = {"rooftop_pv": D("1000.000"), "oa_wind_intra": D("150.000")}
    roof, solar, wind = tp.candidates("open_access", "third_party", built, stated)
    assert roof.max_mw == D("1") and roof.capex_inr_per_kw == D("38000")
    assert roof.min_mw == D("0.999999")  # what phase 1 built stays
    assert solar.ownership == wind.ownership == "third_party" and solar.min_mw == 0
    assert wind.price_inr_per_kwh == D("3.9")
    assert wind.min_mw == D("2")  # the client's own minimum is higher than what was built


def test_curve_levels_lie_strictly_between_the_ends() -> None:
    assert tp.curve_levels(D("55"), D("85"), 2, None) == [D("65.0"), D("75.0")]
    assert tp.curve_levels(D("55"), D("85"), 2, D("65")) == [
        D("75.0")
    ]  # the target's own level is a run already
    assert tp.curve_levels(D("55"), D("85"), 0, None) == []
    assert tp.curve_levels(D("80"), D("80.8"), 3, None) == []  # no room for a point


def _keys(new: list[tp.NewStep] | None) -> list[str] | None:
    return None if new is None else [n.key for n in new]


def test_the_order_of_the_runs_for_the_lowest_cost() -> None:
    t = tp.Target(curve_points=0)
    routes = list(tp.ROUTES)
    bk = BOOK.settlement
    assert _keys(tp.decide(t, routes, [], bk)) == ["rooftop"]
    roof = step("rooftop", "rooftop", ROOF)
    # a run still being solved: wait
    assert tp.decide(t, routes, [step("rooftop", "rooftop", None, status="running")], bk) == []
    new = tp.decide(t, routes, [roof], bk)
    assert _keys(new) == ["open_access:captive", "open_access:group_captive", "open_access:third_party"]
    assert new is not None and new[0].spec.candidates[0].min_mw == D("0.099999")
    assert [c.ownership for c in new[2].spec.candidates[1:]] == ["third_party", "third_party"]
    oas = [
        step("open_access:captive", "open_access", OA, route="captive"),
        step("open_access:group_captive", "open_access", None, route="group_captive"),
        step("open_access:third_party", "open_access", OA, route="third_party"),
    ]
    new = tp.decide(t, routes, [roof, *oas], bk)
    assert _keys(new) == ["storage"]
    assert new is not None
    by = {c.kind: c for c in new[0].spec.candidates}
    assert by["oa_wind_intra"].ownership == "captive" and by["oa_wind_intra"].min_mw == D("0.149999")
    assert {"btm_bess", "oa_bess"} <= set(by)
    store = step("storage", "storage", STORE)
    # the lowest cost with no curve asked for: nothing more to run
    assert tp.decide(t, routes, [roof, *oas, store], bk) is None


def test_the_cost_curve_adds_the_most_the_rules_allow_and_points_between() -> None:
    t = tp.Target(curve_points=2)
    steps = [
        step("rooftop", "rooftop", ROOF),
        step("open_access:captive", "open_access", OA, route="captive"),
        step("storage", "storage", STORE),
    ]
    new = tp.decide(t, ["captive"], steps, BOOK.settlement)
    assert _keys(new) == ["most"] and new is not None and new[0].spec.most_renewable
    most = step("most", "most", summary(STORE["sizes"], after="950.00", grid="150.000"))
    new = tp.decide(t, ["captive"], [*steps, most], BOOK.settlement)
    assert _keys(new) == ["curve:65.0", "curve:75.0"]
    assert new is not None and [n.spec.min_share_pct for n in new] == [D("65.0"), D("75.0")]
    done = [step(n.key, "curve", STORE, level_pct=n.level_pct) for n in new]
    assert tp.decide(t, ["captive"], [*steps, most, *done], BOOK.settlement) is None


def test_a_share_target_runs_only_what_it_needs() -> None:
    steps = [
        step("rooftop", "rooftop", ROOF),
        step("open_access:captive", "open_access", OA, route="captive"),
        step("storage", "storage", STORE),
    ]
    bk = BOOK.settlement
    # reached by the cheapest plan (55%): nothing more
    assert tp.decide(tp.Target(kind="share", share_pct=D(50), curve_points=0), ["captive"], steps, bk) is None
    # above the cheapest plan: first how far the rules allow
    t = tp.Target(kind="share", share_pct=D(70), curve_points=0)
    assert _keys(tp.decide(t, ["captive"], steps, bk)) == ["most"]
    most = step("most", "most", summary(STORE["sizes"], after="950.00", grid="150.000"))
    new = tp.decide(t, ["captive"], [*steps, most], bk)
    assert _keys(new) == ["target"] and new is not None and new[0].spec.min_share_pct == D(70)
    # above the most the rules allow (85%): no run can reach it, the most is the last phase
    t = tp.Target(kind="share", share_pct=D(95), curve_points=0)
    assert tp.decide(t, ["captive"], [*steps, most], bk) is None
    # the most could not be worked out: the target is tried all the same
    failed = step("most", "most", None)
    assert _keys(tp.decide(t, ["captive"], [*steps, failed], bk)) == ["target"]


def test_the_route_taken_is_the_cheapest_one_an_approved_rule_covers() -> None:
    dear = step("open_access:captive", "open_access", OA, route="captive")
    cheap = step(
        "open_access:third_party",
        "open_access",
        summary(OA["sizes"], after="650.00", grid="500.000"),
        route="third_party",
    )
    got = tp.chosen_route([dear, cheap], BOOK.settlement)
    assert got is not None and got.route == "captive"  # third party is cheaper but has no approved rule
    none_covered = {"captive": None, "third_party": None}
    got = tp.chosen_route([dear, cheap], none_covered)
    assert got is not None and got.route == "third_party"
    assert (
        tp.chosen_route([step("open_access:captive", "open_access", None, route="captive")], none_covered)
        is None
    )


def _figures(node: Any, path: str = "") -> list[tuple[str, Any]]:
    """Every number-like leaf that is not inside a SourcedValue (there must be none but counts)."""
    out: list[tuple[str, Any]] = []
    if isinstance(node, dict):
        if "source_refs" in node and "value" in node:
            SourcedValue.model_validate(node)  # a figure: it must be a valid SourcedValue
            return out
        for k, v in node.items():
            out += _figures(v, f"{path}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            out += _figures(v, f"{path}[{i}]")
    elif isinstance(node, int | float | D) and not isinstance(node, bool):
        out.append((path, node))
    return out


# bare numbers a page may carry: counts and the page number of a citation, never a figure
COUNTS = ("order", "resolution_min", "pending_runs", "runs", "page")


def plan(target: tp.Target, extra: list[tp.Step] | None = None, status: str = "done") -> dict[str, Any]:
    steps = [
        step("rooftop", "rooftop", ROOF),
        step("open_access:captive", "open_access", OA, route="captive"),
        step(
            "open_access:third_party",
            "open_access",
            summary(OA["sizes"], after="650.00", grid="500.000"),
            route="third_party",
        ),
        step(
            "open_access:group_captive",
            "open_access",
            None,
            route="group_captive",
            error="solver ended infeasible",
        ),
        step("storage", "storage", STORE),
        *(extra or []),
    ]
    return tp.build(target, steps, BOOK, status)


def test_the_page_lists_the_phases_with_every_figure_sourced() -> None:
    d = plan(tp.Target(kind="share", share_pct=D(50), curve_points=0))
    assert [p["key"] for p in d["phases"]] == ["rooftop", "open_access", "storage"]
    assert [p["order"] for p in d["phases"]] == [1, 2, 3]
    roof, oa, store = d["phases"]
    # phase 1: against today's bill
    assert roof["annual_cost"]["value"] == "900.00" and roof["annual_saving"]["value"] == "100.00"
    assert roof["added_saving"]["value"] == "100.00" and roof["share"]["value"] == "20.00"
    assert roof["capital"]["value"] == "4000" and roof["added_capital"]["value"] == "4000.00"
    assert (
        roof["sources"][0]["capacity"]["value"] == "100.000"
        and roof["sources"][0]["added"]["value"] == "100.000"
    )
    assert roof["sources"][0]["capital"]["value"] == "4000" and roof["route"] is None
    # phase 2: what it adds to phase 1
    assert oa["annual_saving"]["value"] == "300.00" and oa["added_saving"]["value"] == "200.00"
    assert oa["share"]["value"] == "55.00" and oa["added_share"]["value"] == "35.00"
    assert oa["added_capital"]["value"] == "5000.00" and oa["route"] == "captive"
    by = {s["kind"]: s for s in oa["sources"]}
    assert (
        by["rooftop_pv"]["added"]["value"] == "0.000" and by["oa_wind_intra"]["added"]["value"] == "150.000"
    )
    assert (
        oa["energy"]["delivered_kwh"]["value"] == "600.000"
        and oa["energy"]["setoff_kwh"]["value"] == "350.000"
    )
    # phase 3 takes no storage: it says so and gives no steps to take
    assert store["status"] == "adds_nothing" and store["status_words"] and store["actions"] == []
    assert store["added_saving"]["value"] == "0.00" and store["added_share"]["value"] == "0.00"
    assert roof["status"] == oa["status"] == "taken"
    # every figure on the page is a SourcedValue (the only bare numbers are counts)
    bare = [p for p, _ in _figures(d)]
    assert all(p.split(".")[-1] in COUNTS for p in bare), [p for p in bare if p.split(".")[-1] not in COUNTS]
    assert d["today"]["annual_cost"]["value"] == "1000.00" and d["today"]["share"]["value"] == "0.00"
    # the target, in words and figures
    t = d["target"]
    assert t["asked"]["value"] == "50" and t["asked"]["source_refs"][0]["kind"] == "client_stated"
    assert t["met"] is True and t["reached_in_phase"] == "open_access" and "phase 2" in t["words"]
    assert t["reached"]["value"] == "55.00" and t["most"] is None


def test_steps_are_cited_to_a_rule_only_where_a_rule_states_them() -> None:
    d = plan(tp.Target(curve_points=0))
    roof, oa, _ = d["phases"]
    assert roof["actions"] and all(a["basis"] == "assumption" for a in roof["actions"])
    assert all(
        a["ref"]["kind"] == "assumption" and "Uttar Pradesh" in a["ref"]["note"] for a in roof["actions"]
    )
    ruled = [a for a in oa["actions"] if a["basis"] == "rule"]
    assert len(ruled) == 1 and "day ahead" in ruled[0]["what"]
    assert ruled[0]["ref"]["kind"] == "regulation" and ruled[0]["ref"]["rule_id"] == "rule-1"
    assumed = [a for a in oa["actions"] if a["basis"] == "assumption"]
    assert any("open access" in a["what"] for a in assumed) and any("26%" in a["what"] for a in assumed)
    assert all(a["ref"]["kind"] == "assumption" for a in assumed)  # never shown as a rule
    # the state's settlement rule in words, each sentence with the rule's own citation
    texts = " ".join(r["text"] for r in oa["rules"])
    assert "15-minute block" in texts and "8%" in texts
    assert all(r["ref"]["kind"] == "regulation" for r in oa["rules"])
    # a rule that asks for no schedule: no such step
    no_schedule = tp.Book("Uttar Pradesh", "pvvnl", {"captive": rule(day_ahead=False)})
    acts = tp.actions("open_access", "captive", {"oa_wind_intra"}, no_schedule)
    assert all(a["basis"] == "assumption" for a in acts)
    # third party: a power purchase agreement, no equity
    acts = tp.actions("open_access", "third_party", {"oa_solar_intra"}, BOOK)
    assert any("power purchase agreement" in a["what"] for a in acts)
    assert not any("26%" in a["what"] for a in acts)


def test_the_routes_compared_and_what_could_not_be_worked_out() -> None:
    d = plan(tp.Target(curve_points=0))
    by = {a["route"]: a for a in d["alternatives"]}
    assert by["captive"]["chosen"] and by["captive"]["settlement_rule_approved"]
    assert not by["third_party"]["chosen"] and "no approved settlement rule" in by["third_party"]["words"]
    assert by["third_party"]["annual_cost"]["value"] == "650.00"
    assert by["group_captive"]["annual_cost"] is None and "Not shown" in by["group_captive"]["words"]
    [gone] = d["not_worked_out"]
    assert gone["key"] == "open_access:group_captive" and "no size of these sources" in gone["words"]
    assert "infeasible" not in gone["words"]  # plain words, not the solver's


def test_a_target_above_the_cheapest_plan_adds_the_step_that_reaches_it() -> None:
    reach = step("target", "target", summary(STORE["sizes"], after="780.00", grid="300.000"), level_pct=D(70))
    most = step("most", "most", summary(STORE["sizes"], after="950.00", grid="150.000"))
    d = plan(tp.Target(kind="share", share_pct=D(70), curve_points=0), [most, reach])
    assert [p["key"] for p in d["phases"]] == ["rooftop", "open_access", "storage", "target"]
    last = d["phases"][-1]
    assert last["share"]["value"] == "70.00" and last["added_saving"]["value"] == "-80.00"
    t = d["target"]
    assert t["met"] is True and t["reached_in_phase"] == "target" and t["most"]["value"] == "85.00"
    assert "cheapest plan stops at 55%" in t["words"]
    # the curve: the phases, the target and the most the rules allow, in order of share
    assert [(c["role"], c["share"]["value"]) for c in d["curve"]] == [
        ("phase", "20.00"),
        ("phase", "55.00"),
        ("phase", "55.00"),
        ("target", "70.00"),
        ("most", "85.00"),
    ]
    assert [c["cheapest"] for c in d["curve"]].count(True) == 1 and d["curve"][1]["cheapest"]


def test_a_target_above_what_the_rules_allow_says_the_most_and_what_binds() -> None:
    top = summary(STORE["sizes"], after="950.00", grid="150.000")
    top["binding"] = [
        {
            "name": "banking_limit",
            "words": "The most that may be banked is reached",
            "refs": [j("0", "count", "regulation")],
        }
    ]
    d = plan(tp.Target(kind="share", share_pct=D(95), curve_points=0), [step("most", "most", top)])
    assert [p["key"] for p in d["phases"]][-1] == "most"
    t = d["target"]
    assert t["met"] is False and t["most"]["value"] == "85.00" and t["reached"]["value"] == "85.00"
    assert "cannot be reached" in t["words"] and "85%" in t["words"]
    [b] = t["most_binding"]
    assert (
        b["words"].startswith("The most that may be banked")
        and b["refs"][0]["source_refs"][0]["kind"] == "regulation"
    )


def test_a_time_limited_run_is_never_shown_as_the_optimum() -> None:
    late = step(
        "target",
        "target",
        summary(STORE["sizes"], after="780.00", grid="300.000"),
        level_pct=D(70),
        solver={"status": "time_limit", "gap": 0.0123},
    )
    most = step("most", "most", summary(STORE["sizes"], after="950.00", grid="150.000"))
    d = plan(tp.Target(kind="share", share_pct=D(70), curve_points=0), [most, late])
    last = d["phases"][-1]
    assert (
        last["proven"] is False and "time limit" in last["proven_words"] and "1.23%" in last["proven_words"]
    )
    point = next(c for c in d["curve"] if c["role"] == "target")
    assert point["proven"] is False and "1.23%" in point["proven_words"]
    assert all(p["proven"] and p["proven_words"] is None for p in d["phases"][:-1])


def test_a_plan_still_running_shows_what_has_finished() -> None:
    steps = [
        step("rooftop", "rooftop", ROOF),
        step("open_access:captive", "open_access", None, status="queued", route="captive"),
    ]
    d = tp.build(tp.Target(), steps, BOOK, "running")
    assert d["status"] == "running" and d["pending_runs"] == 1
    assert [p["key"] for p in d["phases"]] == ["rooftop"]
    assert d["alternatives"][0]["words"] == "Being worked out." and d["not_worked_out"] == []
    empty = tp.build(tp.Target(), [], BOOK, "running")
    assert empty["phases"] == [] and empty["today"] is None and "being worked out" in empty["target"]["words"]


def test_a_run_the_judge_did_not_agree_with_is_never_shown() -> None:
    bad = step("rooftop", "rooftop", ROOF, status="failed_resettle")
    d = tp.build(tp.Target(), [bad], BOOK, "failed")
    assert d["phases"] == [] and d["today"] is None
    assert "did not agree with the judge" in d["not_worked_out"][0]["words"]


def test_a_target_above_the_most_when_the_cheapest_plan_is_already_there() -> None:
    # the limits hold the cheapest plan and the most the rules allow at the same share: no further phase
    most = step("most", "most", summary(STORE["sizes"], after="700.00", grid="450.000"))
    d = plan(tp.Target(kind="share", share_pct=D(95), curve_points=0), [most])
    assert [p["key"] for p in d["phases"]] == ["rooftop", "open_access", "storage"]
    t = d["target"]
    assert t["met"] is False and t["most"]["value"] == "55.00" and t["most_proven"] is True
    assert "cannot be reached" in t["words"] and "already reaches it" in t["words"]


def test_a_search_for_the_most_that_ran_out_of_time_is_not_taken_as_the_most() -> None:
    steps = [
        step("rooftop", "rooftop", ROOF),
        step("open_access:captive", "open_access", OA, route="captive"),
        step("storage", "storage", STORE),
    ]
    late = step(
        "most",
        "most",
        summary(STORE["sizes"], after="950.00", grid="150.000"),
        solver={"status": "time_limit", "gap": None},
    )
    t = tp.Target(kind="share", share_pct=D(95), curve_points=0)
    # 95% is above the 85% the search reached, but the search was cut short: the target is still tried
    new = tp.decide(t, ["captive"], [*steps, late], BOOK.settlement)
    assert _keys(new) == ["target"]
    failed = step("target", "target", None, level_pct=D(95), error="solver ended with Infeasible")
    d = tp.build(t, [*steps, late, failed], BOOK, "done")
    tt = d["target"]
    assert tt["most_proven"] is False and tt["met"] is False
    assert (
        "not shown that 95% cannot be reached" in tt["words"] and "cannot be reached under" not in tt["words"]
    )
    assert d["phases"][-1]["key"] == "most" and d["phases"][-1]["proven"] is False
    # without a target the page says "at least"
    free = tp.build(tp.Target(curve_points=1), [*steps, late], BOOK, "done")
    assert "at least 85%" in free["target"]["words"] and free["target"]["most_proven"] is False
