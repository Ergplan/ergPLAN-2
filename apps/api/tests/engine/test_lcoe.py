"""The cost of plant the consumer owns: the utility-scale benchmarks (assumptions, beside the PPA
benchmarks), the yearly cost of a kW of a captive plant, and the equity outlay of a plant taken with
26% equity with its per-kWh carrying cost (finding 2 of the Uttar Pradesh full test)."""

from __future__ import annotations

import dataclasses
from decimal import Decimal

import pytest

from ergplan.core.sourced import RefKind
from ergplan.engine import lcoe
from ergplan.platform.routers.landed import DEFAULT_PRICE

D = Decimal


@pytest.mark.parametrize(
    ("source", "ppa_kind"),
    [("solar", "oa_solar_intra"), ("wind", "oa_wind_intra"), ("hybrid", "oa_hybrid_intra")],
)
def test_the_utility_benchmarks_are_assumptions_that_level_near_the_ppa_benchmark(
    source: str, ppa_kind: str
) -> None:
    g = lcoe.default_utility(source)
    for f in dataclasses.fields(g):
        v = getattr(g, f.name)
        assert [r.kind for r in v.source_refs] == [RefKind.ASSUMPTION] and v.source_refs[0].note, f.name
    for name in ("capex_inr_per_kw", "om_pct_of_capex", "cuf_pct", "degradation_pct", "life_years"):
        assert "to be replaced by your own figure" in getattr(g, name).source_refs[0].note
    # a PPA tariff is, to first order, the plant's levelised cost: within 5% of the benchmark tariff
    ppa = D(DEFAULT_PRICE[ppa_kind][0])
    assert abs(lcoe.lcoe(g).value - ppa) / ppa < D("0.05")


def test_no_benchmark_plant_for_a_source_that_is_not_one() -> None:
    with pytest.raises(KeyError):
        lcoe.default_utility("bess")


def test_a_captive_plants_yearly_cost_is_capital_recovery_plus_o_and_m() -> None:
    g = lcoe.default_utility("solar")
    f = lcoe.crf(g.discount_pct, g.life_years)
    assert f.value.quantize(D("0.000001")) == D("0.110168")  # 10% over 25 years
    yearly = lcoe.annual_cost_per_kw(g)
    assert yearly.value == D(42000) * (f.value + D("0.015"))
    assert yearly.value.quantize(D("0.01")) == D("5257.06")  # ₹42,000 × (0.110168 + 1.5%)
    assert {r.kind for r in yearly.source_refs} == {RefKind.ASSUMPTION}  # never a sourced fact
    # the same plant as a cost per kWh: the yearly cost over the average year's energy of a kW
    cuf20 = dataclasses.replace(g, cuf_pct=g.cuf_pct.model_copy(update={"value": D(20)}))
    energy = D(8760) * D("0.20") * sum((D("0.995") ** y for y in range(25)), D(0)) / 25
    assert lcoe.lcoe(cuf20).value == yearly.value / energy
    assert lcoe.lcoe(cuf20).value.quantize(D("0.01")) == D("3.18")


def test_the_equity_outlay_and_its_per_kwh_view_are_one_figure() -> None:
    g = lcoe.default_utility("solar")
    share = lcoe.default_equity_share()
    assert share.value == 26 and share.source_refs[0].kind == RefKind.ASSUMPTION
    assert "debt and equity split is not modelled" in str(share.source_refs[0].note)
    outlay = lcoe.equity_outlay_per_kw(g, share)
    assert outlay.value == D("10920")  # 26% × ₹42,000 per kW
    per_kwh = lcoe.equity_cost_per_kwh(g, share)
    f = lcoe.crf(g.discount_pct, g.life_years)
    energy = D(8760) * D("0.21") * sum((D("0.995") ** y for y in range(25)), D(0)) / 25
    # per kWh × the kW's yearly energy = the outlay's yearly carrying cost: the same money, once
    assert abs(per_kwh.value * energy - outlay.value * f.value) < D("0.000001")
    assert {r.kind for r in per_kwh.source_refs} == {RefKind.ASSUMPTION}
    half = lcoe.equity_outlay_per_kw(g, share.model_copy(update={"value": D(13)}))
    assert half.value == outlay.value / 2
