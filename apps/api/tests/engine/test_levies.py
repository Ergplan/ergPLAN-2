"""FPPCA and electricity duty at the bills' own rates (engine/levies.py), their place in the avoided
cost of a grid unit, and the stated treatment on units not bought from the DISCOM. Pure: no database."""

from __future__ import annotations

from datetime import date
from decimal import Decimal as D
from typing import Any

from ergplan.core.sourced import RefKind, SourcedValue
from ergplan.core.units import Unit
from ergplan.engine import avoided_cost, landed_cost
from ergplan.engine import levies as L
from ergplan.rulebook.models import RuleView

from .helpers import sv

ORDER = {"kind": "order", "document_id": "d1", "page": 7, "quote": "as printed"}


def _bill(i: int, kvah: str, kwh: str, fppca: str | None, duty: str | None) -> L.BillCharges:
    amounts = {}
    if fppca is not None:
        amounts[L.FPPCA] = (D(fppca), (4,))
    if duty is not None:
        amounts[L.DUTY] = (D(duty), (5,))
    return L.BillCharges(f"bill-{i}", amounts, D(kvah), D(kwh))


def _schedule(**body: Any) -> RuleView:
    return RuleView(
        id="r1",
        family="tariff_schedule",
        version_id="v1",
        jurisdiction_id="pvvnl",
        scope={},
        body={"billing_basis": "kvah", "energy_charge": {"base": "6.80"}, **body},
        source_ref=ORDER,
    )


def _oa(component: str, value: str) -> RuleView:
    return RuleView(
        id=f"oa-{component}",
        family="oa_charge",
        version_id="v2",
        jurisdiction_id="pvvnl",
        scope={},
        body={"component": component, "basis": "inr_per_kwh", "value": value},
        source_ref=ORDER,
        label=component.replace("_", " "),
    )


def test_levies_are_the_bills_average_rate_and_cite_the_bill_lines() -> None:
    bills = [_bill(1, "1000", "950", "180", "500"), _bill(2, "3000", "2900", "600", "1500")]
    fppca, duty = L.from_bills(bills)
    assert (fppca.code, duty.code) == ("fppca", "electricity_duty") and fppca.bills == 2
    r = fppca.rate(Unit.INR_PER_KVAH)
    assert r is not None and r.value == D("0.1950")  # 780 on 4,000 kVAh, not the mean of the two rates
    assert duty.rate(Unit.INR_PER_KVAH).value == D("0.5000")  # type: ignore[union-attr]
    assert duty.rate(Unit.INR_PER_KWH).value == (D(2000) / D(3850)).quantize(D("0.0001"))  # type: ignore[union-attr]
    # the client's own bills, never an order or a regulation, and a sourced value (not an assumption)
    assert {x.kind for x in r.source_refs} == {RefKind.BILL} and r.is_sourced
    assert [(x.bill_id, x.field_path) for x in r.source_refs] == [
        ("bill-1", "elements.4.amount"),
        ("bill-2", "elements.4.amount"),
    ]
    assert "2 bills" in str(r.label)


def test_a_bill_without_the_line_counts_its_units_and_no_bill_means_no_levy() -> None:
    [fppca] = L.from_bills([_bill(1, "1000", "950", "180", None), _bill(2, "1000", "950", None, None)])
    assert fppca.rate(Unit.INR_PER_KVAH).value == D("0.0900") and fppca.bills == 1  # type: ignore[union-attr]
    assert L.from_bills([_bill(1, "1000", "950", None, None)]) == ()  # nothing assumed about a rate
    assert L.from_bills([]) == ()


def test_the_treatment_on_other_units_is_an_assumption_named_in_words() -> None:
    fppca, duty = L.from_bills([_bill(1, "1000", "950", "180", "500")])
    factor = sv("1.05", Unit.RATIO)
    for lv in (fppca, duty):
        assert [x.kind for x in lv.treatment.source_refs] == [RefKind.ASSUMPTION]
        assert not lv.treatment.is_sourced and "Not sourced to a regulation" in lv.words
    assert fppca.treatment.value == 0 and "assumed not charged" in fppca.words
    assert L.on_other_units(fppca, "kvah", factor) is None
    assert duty.treatment.value == 100 and "cautious side" in duty.words
    own = L.on_other_units(duty, "kvah", factor)
    grid = L.per_kwh(duty, "kvah", factor)
    assert own is not None and grid is not None and own.value == grid.value == D("0.5250")
    assert RefKind.ASSUMPTION in {x.kind for x in own.source_refs} and not own.is_sourced
    assert "not sourced to a regulation" in str(own.label)
    assert L.Levy.from_json(duty.to_json()) == duty  # what a run pins comes back as it was


def test_rate_card_takes_the_bills_levies_only_where_the_rule_is_silent() -> None:
    levies = L.from_bills([_bill(1, "1000", "950", "180", "500")])
    factor = sv("1.05", Unit.RATIO)
    month = date(2026, 11, 1)
    plain = avoided_cost.rate_card(month, avoided_cost.MonthRules(_schedule(), None), None, factor)
    card = avoided_cost.rate_card(month, avoided_cost.MonthRules(_schedule(), None, levies), None, factor)
    assert plain is not None and card is not None
    assert plain.rate_inr_per_kwh.value == D("6.80") * D("1.05") and plain.levies == {}
    assert card.rate_inr_per_kwh.value == (D("6.80") + D("0.18") + D("0.50")) * D("1.05")
    assert set(card.levies) == {"fppca", "electricity_duty"}
    assert card.levies["fppca"].value == D("0.18") * D("1.05")
    assert {x.kind for x in card.rate_inr_per_kwh.source_refs} >= {RefKind.ORDER, RefKind.BILL}
    # an approved row that states the levy wins over the bills: the bill's rate is not added to it
    stated = _schedule(fppca_inr_per_unit="0.30", electricity_duty={"inr_per_unit": "0.40"})
    ruled = avoided_cost.rate_card(month, avoided_cost.MonthRules(stated, None, levies), None, factor)
    assert ruled is not None and ruled.levies == {}
    assert ruled.rate_inr_per_kwh.value == (D("6.80") + D("0.30") + D("0.40")) * D("1.05")
    assert avoided_cost.levies_in_force(avoided_cost.MonthRules(stated, None, levies)) == ()
    assert avoided_cost.levies_in_force(avoided_cost.MonthRules(None, None, levies)) == ()


def _duty() -> SourcedValue:
    [_, duty] = L.from_bills([_bill(1, "1000", "950", "180", "500")])
    own = L.on_other_units(duty, "kvah", sv("1.05", Unit.RATIO))
    assert own is not None
    return own


def test_landed_cost_adds_the_assumed_duty_unless_an_approved_row_states_it() -> None:
    own = _duty()
    sc = landed_cost.Scenario(price=sv("3.10", Unit.INR_PER_KWH), duty_on_own_units=own)
    solar = landed_cost.SOURCE_BY_ID["oa_solar_intra"]
    wf = landed_cost.evaluate(solar, {}, sc, [_oa("wheeling", "0.40")])
    assert own in wf.lines and wf.total.value == D("3.10") + D("0.40") + D("0.5250")
    assert landed_cost.ASSUMED_DUTY_NOTE in wf.notes
    [step] = [s for s in wf.steps if s.component == "electricity_duty_assumed"]
    # an `extra`, not a regulatory `drawal` link: there is no rule row behind it to correct
    assert step.kind == "extra" and step.rule_id is None and step.per_kwh == own
    assert not [s for s in wf.steps if s.component == "electricity_duty_oa"]
    # with an approved electricity_duty_oa row the row is the duty: no assumed line beside it
    ruled = landed_cost.evaluate(solar, {}, sc, [_oa("wheeling", "0.40"), _oa("electricity_duty_oa", "0.20")])
    assert own not in ruled.lines and ruled.total.value == D("3.10") + D("0.40") + D("0.20")
    assert landed_cost.ASSUMED_DUTY_NOTE not in ruled.notes
    [step] = [s for s in ruled.steps if s.component == "electricity_duty_oa"]
    assert step.rule_id == "oa-electricity_duty_oa"
    assert not [s for s in ruled.steps if s.component == "electricity_duty_assumed"]
    # without the bills' duty nothing is added and the gap stands
    bare = landed_cost.evaluate(solar, {}, landed_cost.Scenario(price=sv("3.10", Unit.INR_PER_KWH)), [])
    assert bare.total.value == D("3.10") and "electricity_duty" in bare.gaps


def test_assumed_duty_falls_on_own_generation_but_not_on_discom_or_grid_charged_units() -> None:
    own = _duty()
    sc = landed_cost.Scenario(price=sv("4.00", Unit.INR_PER_KWH), duty_on_own_units=own)
    for sid, charged in (
        ("rooftop_pv", True),
        ("captive_thermal", True),
        ("discom_grid", False),  # the duty is in the grid rate already
        ("discom_green_tariff", False),
        ("btm_bess", False),  # charged from the grid: those units paid the duty when bought
        ("oa_bess", False),  # in the landed cost of the plant's energy it stores
    ):
        wf = landed_cost.evaluate(landed_cost.SOURCE_BY_ID[sid], {}, sc, [])
        assert (own in wf.lines) is charged, sid
        assert wf.total.value == D("4.00") + (own.value if charged else 0), sid
