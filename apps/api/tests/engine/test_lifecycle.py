"""The 25-year view of a settled year: escalation, degradation, replacement, discounting, payback."""

from __future__ import annotations

from decimal import Decimal

from ergplan.core.sourced import RefKind, SourcedValue, SourceRef
from ergplan.core.units import Unit
from ergplan.engine import lifecycle
from ergplan.engine.lcoe import default_finance

D = Decimal


def sv(v: str, unit: Unit = Unit.PERCENT, label: str = "x") -> SourcedValue:
    return SourcedValue(
        value=D(v), unit=unit, label=label, source_refs=(SourceRef(kind=RefKind.ASSUMPTION, note=label),)
    )


def fin(**kw: str) -> lifecycle.Inputs:
    base = {
        "analysis_years": ("25", Unit.COUNT),
        "discount_pct": ("10", Unit.PERCENT),
        "tariff_escalation_pct": ("0", Unit.PERCENT),
        "ppa_escalation_pct": ("0", Unit.PERCENT),
        "om_escalation_pct": ("0", Unit.PERCENT),
    }
    for k, v in kw.items():
        base[k] = (v, base[k][1])
    return lifecycle.Inputs(**{k: sv(v, u, k) for k, (v, u) in base.items()})


def rooftop(
    capex: str, displaced: str, degradation: str = "0", life: str = "25", om: str = "0"
) -> lifecycle.Asset:
    return lifecycle.Asset(
        cid="rooftop_pv",
        label="Rooftop solar",
        capex=sv(capex, Unit.INR, "capex"),
        om_pct=sv(om),
        life_years=sv(life, Unit.COUNT),
        degradation_pct=sv(degradation),
        displaced_inr=D(displaced),
    )


def test_a_ppa_alone_is_the_year_repeated_and_discounted() -> None:
    before = {"grid_energy": D("1000"), "demand": D("200")}
    after = {"grid_energy": D("400"), "demand": D("200"), "energy:oa_solar_intra": D("300")}
    lc = lifecycle.project(before, after, [], fin(analysis_years="3", discount_pct="0"))
    assert [y.saving for y in lc.years] == [D("300.00")] * 3
    assert lc.capex0 == 0 and lc.npv == D("900.00") and lc.nominal_saving == D("900.00")
    assert lc.irr_pct is None and lc.simple_payback_years is None  # nothing invested
    assert lc.pv_bau == D("3600.00") and lc.pv_plan == D("2700.00")


def test_tariff_escalation_widens_the_saving_and_ppa_escalation_narrows_it() -> None:
    before = {"grid_energy": D("1000")}
    after = {"grid_energy": D("400"), "energy:ppa": D("300")}
    flat = lifecycle.project(before, after, [], fin(analysis_years="2", discount_pct="0"))
    esc = lifecycle.project(
        before, after, [], fin(analysis_years="2", discount_pct="0", tariff_escalation_pct="10")
    )
    # year 2: bau 1100, grid 440 → saving 1100 − 440 − 300 = 360
    assert esc.years[1].saving == D("360.00") > flat.years[1].saving == D("300.00")
    ppa = lifecycle.project(
        before, after, [], fin(analysis_years="2", discount_pct="0", ppa_escalation_pct="10")
    )
    assert ppa.years[1].saving == D("270.00")


def test_owned_plant_pays_back_degrades_and_is_replaced() -> None:
    before = {"grid_energy": D("1000")}
    after = {"grid_energy": D("600"), "fixed:rooftop_pv": D("150")}  # the annualised line is not a cash flow
    asset = rooftop(capex="1000", displaced="400", degradation="10", life="3", om="5")
    lc = lifecycle.project(before, after, [asset], fin(analysis_years="4", discount_pct="0"))
    y1, y2, y3, y4 = lc.years
    # year 1: grid 600, O&M 50 → saving 350; year 2: 10 % of the displaced 400 comes back → 310
    assert (y1.saving, y2.saving) == (D("350.00"), D("310.00"))
    assert y3.saving == D("274.00")  # 400 × (1 − 0.9²) = 76 back
    # year 4 is the first of the next life: replaced, output as new again
    assert y4.capex == D("1000.00") and y4.saving == D("350.00")
    assert y4.cash == D("-650.00")
    assert lc.capex0 == D("1000.00")
    assert lc.simple_payback_years == D("3.2")  # 350 + 310 + 274 = 934 after year 3, 66 of year 4's 350
    assert any("replaced in year 4" in n for n in lc.notes)
    assert lc.irr_pct is None  # four years with a replacement in the last: the capital never returns
    longer = lifecycle.project(before, after, [rooftop("1000", "400", life="25")], fin(analysis_years="10"))
    assert longer.irr_pct is not None and longer.irr_pct > 30  # 400 a year on 1,000


def test_discounting_and_the_json_carry_the_inputs() -> None:
    before = {"grid_energy": D("1000")}
    after = {"grid_energy": D("500")}
    lc = lifecycle.project(
        before, after, [rooftop("1000", "500")], fin(analysis_years="2", discount_pct="10")
    )
    assert lc.years[0].discount == D("0.909091") and lc.years[1].pv_cash == D("413.22")
    assert lc.discounted_payback_years is None  # 454.55 + 413.22 < 1000
    out = lifecycle.to_json(lc, [sv("1", Unit.KWH, "profile")])
    assert out["npv"]["source_refs"] and len(out["years"]) == 2  # type: ignore[index]
    row = out["years"][1]  # type: ignore[index]
    assert row["cumulative"]["value"] == str(lc.years[1].cumulative)  # type: ignore[index]
    assert row["cumulative"]["source_refs"][0]["kind"] == "derived"  # type: ignore[index]
    assert "year 2" in row["cumulative"]["source_refs"][0]["note"]  # type: ignore[index]
    assert set(out["assumptions"]) == {  # type: ignore[arg-type]
        "discount_pct",
        "tariff_escalation_pct",
        "ppa_escalation_pct",
        "om_escalation_pct",
    }


def test_the_default_assumptions_are_stated() -> None:
    d = default_finance()
    assert d["analysis_years"].value == 25 and d["discount_pct"].value == 10
    assert all(r.kind == RefKind.ASSUMPTION and r.note for v in d.values() for r in v.source_refs)


# ---------------------------------------------------------------- the owned capital (finding 2)


def plant(cid: str, capex: str, om: str = "0", displaced: str = "0") -> lifecycle.Asset:
    return lifecycle.Asset(
        cid=cid,
        label=cid,
        capex=sv(capex, Unit.INR, "capex"),
        om_pct=sv(om),
        life_years=sv("25", Unit.COUNT),
        degradation_pct=sv("0"),
        displaced_inr=D(displaced),
    )


def test_payback_and_irr_include_the_captive_plants_capital() -> None:
    # a rooftop (capital 400) and a captive open-access plant (capital 2,600, 2% O&M): the plant's
    # annualised `fixed:` line is not a cash flow; its capital at year 0 and its O&M are
    before = {"grid_energy": D("1000")}
    after = {
        "grid_energy": D("300"),
        "fixed:rooftop_pv": D("50"),
        "fixed:oa_solar_intra": D("340"),
        "energy:oa_solar_intra": D("100"),  # the open-access charges a captive plant still pays
    }
    roof, own = rooftop("400", "0"), plant("oa_solar_intra", "2600", om="2")
    lc = lifecycle.project(before, after, [roof, own], fin(analysis_years="10", discount_pct="0"))
    assert lc.capex0 == D("3000.00")  # not the rooftop's 400 alone
    # year's cash saving: 1000 − 300 − 100 − O&M 52 = 548 → 3000 / 548
    assert lc.years[0].saving == D("548.00") and lc.simple_payback_years == D("5.5")
    alone = lifecycle.project(before, after, [roof], fin(analysis_years="10", discount_pct="0"))
    assert alone.simple_payback_years == D("0.7")  # what the rooftop alone would have claimed
    assert lc.irr_pct is not None and alone.irr_pct is not None and lc.irr_pct < alone.irr_pct / 5
    out = lifecycle.to_json(lc, [sv("1", Unit.KWH, "profile")], owned=True)
    assert out["simple_payback_years"]["label"] == "Payback on the owned capital (simple)"  # type: ignore[index]
    assert out["irr_pct"]["label"] == "IRR on the owned capital"  # type: ignore[index]
    assert out["capex"]["label"] == "Capital of your own, year 0"  # type: ignore[index]
    assert out["years"][0]["owned_saving"]["value"] == "548.00"  # type: ignore[index]


def test_the_equity_outlay_is_capital_and_the_tariff_is_not_charged_twice() -> None:
    # 26% equity: an outlay of 700 at year 0 (no O&M, it is in the tariff) and the tariff per kWh;
    # the outlay's annualised line is not a cash flow beside the outlay itself
    before = {"grid_energy": D("1000")}
    after = {"grid_energy": D("400"), "energy:oa_solar_intra": D("350"), "fixed:oa_solar_intra": D("77")}
    lc = lifecycle.project(
        before, after, [plant("oa_solar_intra", "700")], fin(analysis_years="5", discount_pct="0")
    )
    assert lc.capex0 == D("700.00") and all(y.om == 0 for y in lc.years)
    assert lc.years[0].saving == D("250.00") and lc.simple_payback_years == D("2.8")
    assert lc.nominal_saving == D("550.00")  # 5 × 250 − 700: the outlay once, not also 77 a year
    out = lifecycle.to_json(lc, [sv("1", Unit.KWH, "profile")], owned=True)
    assert out["capex"]["label"] == "Capital of your own, year 0"  # type: ignore[index]
    assert out["capex"]["value"] == "700.00"  # type: ignore[index]
    assert [y["owned_saving"]["value"] for y in out["years"]] == ["250.00"] * 5  # type: ignore[index, attr-defined]


def test_capital_is_not_credited_with_the_saving_of_sources_bought_by_the_unit() -> None:
    # a rooftop (capital 1,000) beside a third-party PPA that avoids 500 of grid cost for 300
    before = {"grid_energy": D("1000")}
    after = {"grid_energy": D("300"), "energy:oa_solar_intra": D("300"), "fixed:rooftop_pv": D("110")}
    roof = rooftop("1000", "0")
    whole = lifecycle.project(before, after, [roof], fin(analysis_years="10", discount_pct="0"))
    assert whole.years[0].saving == D("400.00") and whole.simple_payback_years == D("2.5")
    lc = lifecycle.project(
        before, after, [roof], fin(analysis_years="10", discount_pct="0"), bought=(D("500"), D("300"))
    )
    # the plan's saving, NPV and cash flow are unchanged; payback and IRR are on the rooftop's 200
    assert [y.saving for y in lc.years] == [y.saving for y in whole.years] and lc.npv == whole.npv
    assert lc.years[0].owned_saving == D("200.00") and lc.simple_payback_years == D("5.0")
    assert lc.irr_pct is not None and whole.irr_pct is not None and lc.irr_pct < whole.irr_pct
    # the bought sources' saving moves with the tariff and their own price, as the plan's does
    esc = lifecycle.project(
        before,
        after,
        [roof],
        fin(analysis_years="2", discount_pct="0", tariff_escalation_pct="10", ppa_escalation_pct="5"),
        bought=(D("500"), D("300")),
    )
    # year 2: plan saving 1100 − 330 − 315 = 455; bought 550 − 315 = 235; owned 220
    assert esc.years[1].saving == D("455.00") and esc.years[1].owned_saving == D("220.00")


def test_no_payback_or_irr_without_capital_of_ones_own() -> None:
    before = {"grid_energy": D("1000")}
    after = {"grid_energy": D("400"), "energy:oa_solar_intra": D("300")}
    lc = lifecycle.project(
        before, after, [], fin(analysis_years="5", discount_pct="0"), bought=(D("600"), D("300"))
    )
    assert lc.capex0 == 0 and lc.npv == D("1500.00")  # the plan still saves
    assert lc.simple_payback_years is None and lc.discounted_payback_years is None and lc.irr_pct is None
    out = lifecycle.to_json(lc, [sv("1", Unit.KWH, "profile")], owned=True)
    assert out["simple_payback_years"] is None and out["irr_pct"] is None  # absent, never a zero
    assert all(y.owned_saving == 0 for y in lc.years)  # nothing of the saving is the capital's
