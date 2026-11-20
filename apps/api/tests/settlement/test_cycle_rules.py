"""The approved UP and Maharashtra settlement rules (cycle settlement, ergPLAN 2) on hand-worked
months, read from the committed rulebook files and settled by the judge (`settlement.simulate`)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal as D
from typing import Any

import yaml

from ergplan.core.settings import REPO_ROOT
from ergplan.rulebook.models import RuleView
from ergplan.settlement.simulate import Block, Result, SourceIn, simulate

STATES = REPO_ROOT / "packages" / "rulebooks" / "states"
APR = date(2026, 4, 1)


def _rule(folder: str, jurisdiction: str) -> RuleView:
    [path] = sorted((STATES / folder / "settlement").glob(f"{jurisdiction}-v*.yaml"))
    d = yaml.safe_load(path.read_text())
    assert d["status"] == "approved"
    row = d["rows"][0]
    assert row["body"]["method"] == "cycle_pooled"
    return RuleView(
        id="r", family="settlement", version_id="v", jurisdiction_id=jurisdiction, scope=row["scope"],
        body=row["body"], source_ref=row["source_ref"], field_refs=row.get("field_refs") or {},
    )  # fmt: skip


def _blocks(rows: list[tuple[str, Any, Any]]) -> list[Block]:
    return [
        Block(month=APR, slot=cat, classes=frozenset({f"cat:{cat}"}), load_kwh=D(str(load)), supply_kwh={"re": D(str(gen))})
        for cat, load, gen in rows
    ]  # fmt: skip


def _totals(res: Result) -> dict[str, D]:
    return {
        "grid": sum(res.grid_kwh, D(0)),
        "setoff": sum(res.setoff_kwh["re"], D(0)),
        "withdrawn": sum(res.withdrawn_kwh, D(0)),
        "banked": sum(res.deposit_kwh["re"], D(0)),
        "charge": sum(res.charge_kwh["re"], D(0)),
        "unbanked": sum(res.unbanked_kwh["re"], D(0)),
        "lapsed": sum((x["kwh"] for x in res.lapsed), D(0)),
    }


def test_up_monthly_pool_limit_charge_and_higher_to_lower() -> None:
    """100 kWh surplus in a normal block; 50 short at peak, 40 off-peak. The drawal left after
    real-time settlement is 90, so the limit is the higher of 30% of it (27) and 25% of generation
    (25): 27 banked, 8% charged in kind (2.16), 24.84 in the bank. Banked at normal it may be drawn at
    normal or off-peak, not at peak: all 24.84 goes off-peak. 73 was over the limit."""
    rule = _rule("up", "UP")
    res = simulate(
        _blocks([("normal", 0, 100), ("peak", 50, 0), ("off_peak", 40, 0)]), [SourceIn("re")], rule
    )
    t = _totals(res)
    assert t == {
        "grid": D("65.16"), "setoff": D(0), "withdrawn": D("24.84"), "banked": D("24.84"),
        "charge": D("2.16"), "unbanked": D(73), "lapsed": D(0),
    }  # fmt: skip
    assert res.grid_kwh[1] == D(50) and res.grid_kwh[2] == D("15.16")  # nothing reaches the peak block
    # the method and the limit are cited to the register, the charge to UPERC's page
    assert (
        rule.ref("/banking/limit").kind == "register"
        and rule.ref("/banking/charge/value").kind == "regulation"
    )


def test_up_peak_banked_energy_serves_the_peak_first() -> None:
    """Banked at peak, energy may be drawn anywhere; the peak shortfall is served first."""
    rule = _rule("up", "UP")
    res = simulate(_blocks([("peak", 0, 40), ("peak", 30, 0), ("normal", 30, 0)]), [SourceIn("re")], rule)
    # drawal 60 → limit max(18, 10) = 18 → 16.56 after 8%: all of it to the peak block
    assert res.withdrawn_kwh[1] == D("16.56") and res.withdrawn_kwh[2] == D(0)
    assert _totals(res)["grid"] == D("43.44")


def test_maharashtra_slot_netting_and_the_drawal_matrix() -> None:
    """Maharashtra nets each month's ToD slot as a whole, then banks. Off-peak (solar-hours) surplus may
    be drawn only off-peak, so with no off-peak shortfall it lapses; peak surplus may be drawn anywhere."""
    rule = _rule("mh", "msedcl")
    charge = D(str(rule.body["banking"]["charge"]["value"]))
    assert charge == D(8) and rule.ref("/banking/charge/value").kind == "register"
    # off-peak: 100 generated in one block, 20 consumed in another → the slot sets off 20, 80 surplus
    res = simulate(
        _blocks([("off_peak", 0, 100), ("off_peak", 20, 0), ("peak", 50, 0), ("normal", 30, 0)]),
        [SourceIn("re")],
        rule,
    )
    t = _totals(res)
    # drawal 80 → limit max(24, 25) = 25 banked, 23 after the charge, none of it drawable: it lapses
    assert (t["setoff"], t["grid"], t["withdrawn"], t["lapsed"], t["unbanked"]) == (
        D(20),
        D(80),
        D(0),
        D(23),
        D(55),
    )
    res = simulate(
        _blocks([("peak", 0, 100), ("peak", 20, 0), ("off_peak", 50, 0), ("normal", 30, 0)]),
        [SourceIn("re")],
        rule,
    )
    t = _totals(res)
    # the same 23 kWh, banked at peak: drawn at normal first (the next category in value), 57 from the grid
    assert (t["setoff"], t["withdrawn"], t["grid"], t["lapsed"]) == (D(20), D(23), D(57), D(0))
    assert res.withdrawn_kwh[3] == D(23)
