"""report.tariff_book: each figure restates a rule field, with the unit the row's basis names and the
citation of that field (or the row); nothing is computed."""

from __future__ import annotations

from decimal import Decimal

from ergplan.core.units import Unit
from ergplan.report import tariff_book as tb
from ergplan.rulebook.models import RuleView

ROW_REF = {"kind": "order", "document_id": "doc-1", "page": 10, "quote": "the schedule"}
FIELD_REF = {"kind": "order", "document_id": "doc-2", "page": 77, "quote": "peak +25%"}


def view(family: str, body: dict[str, object], **kw: object) -> RuleView:
    return RuleView(
        id="r1",
        family=family,
        version_id="v1",
        jurisdiction_id="msedcl",
        scope=dict(kw.pop("scope", {})),  # type: ignore[call-overload]
        body=body,
        source_ref=ROW_REF,
        field_refs=dict(kw.pop("field_refs", {})),  # type: ignore[call-overload]
        label="HT I",
    )


def by_key(figs: list[tb.Figure]) -> dict[str, tb.Figure]:
    return {f.key: f for f in figs}


def test_schedule_units_follow_the_billing_basis() -> None:
    body = {
        "category": "HT I (A)",
        "billing_basis": "kvah",
        "demand_charge": {
            "value": "650",
            "basis": "inr_per_kva_month",
            "billing_demand_rule": {"min_pct_of_contract_demand": "75"},
        },
        "energy_charge": {"base": "8.44", "by_slot": {"peak": "9.10"}},
        "retail_wheeling_charge": "0.81",
        "minimum_charge": {"inr_per_kva_month": "100", "min_units_per_kva_month": "75"},
    }
    f = by_key(tb.schedule_figures(view("tariff_schedule", body)))
    assert f["/energy_charge/base"].value.unit == Unit.INR_PER_KVAH
    assert f["/energy_charge/base"].value.value == Decimal("8.44")
    assert f["/energy_charge/by_slot/peak"].value.unit == Unit.INR_PER_KVAH
    assert f["/retail_wheeling_charge"].value.unit == Unit.INR_PER_KVAH
    assert f["/demand_charge/value"].value.unit == Unit.INR_PER_KVA_MONTH
    assert f["/demand_charge/billing_demand_rule/min_pct_of_contract_demand"].value.unit == Unit.PERCENT
    assert f["/minimum_charge/inr_per_kva_month"].value.value == Decimal("100")
    assert f["/minimum_charge/min_units_per_kva_month"].value.unit == Unit.KWH_PER_KVA_MONTH
    # every figure cites the row (no field refs here), with the rule id
    for x in f.values():
        (r,) = x.value.source_refs
        assert (r.document_id, r.page, r.rule_id) == ("doc-1", 10, "r1")
    kwh = tb.schedule_figures(view("tariff_schedule", {**body, "billing_basis": "kwh"}))
    assert by_key(kwh)["/energy_charge/base"].value.unit == Unit.INR_PER_KWH


def test_demand_slabs_and_fixed_charge_words() -> None:
    body = {
        "category": "HTP-I",
        "billing_basis": "kwh",
        "demand_charge": {
            "value": "475",
            "basis": "inr_per_kva_month_billing_demand",
            "slabs": [{"up_to_kva": "500", "value": "150"}, {"value": "475"}],
        },
        "energy_charge": {"base": "4.25"},
    }
    figs = tb.schedule_figures(view("tariff_schedule", body))
    labels = [f.label for f in figs]
    assert labels[:2] == [
        "Demand charge on billing demand, 0 to 500 kVA",
        "Demand charge on billing demand, above 500 kVA",
    ]
    assert "/demand_charge/value" not in by_key(figs)  # the slabs state it
    fixed = {**body, "demand_charge": {"value": "200", "basis": "inr_per_kw_month"}}
    (first, *_) = tb.schedule_figures(view("tariff_schedule", fixed))
    assert first.label == "Fixed charge" and first.value.unit == Unit.INR_PER_KW_MONTH


def test_tod_slots_cite_the_field_ref_and_key_by_slot_name() -> None:
    body = {
        "slots": [
            {"name": "normal", "start": "00:00", "end": "09:00"},
            {"name": "peak", "start": "17:00", "end": "24:00", "surcharge_pct": "25", "months": [4, 5]},
        ]
    }
    slots = tb.tod_slots(view("tod_slots", body, field_refs={"/slots/1/surcharge_pct": FIELD_REF}))
    assert [s.name for s in slots] == ["normal", "peak"] and slots[0].figures == []
    (peak,) = slots[1].figures
    assert peak.key == "peak:surcharge_pct" and peak.value.unit == Unit.PERCENT
    assert peak.value.source_refs[0].document_id == "doc-2"  # the field's own citation wins
    assert slots[1].months == [4, 5] and slots[1].ref.document_id == "doc-1"


def test_open_access_basis_and_exemptions() -> None:
    body = {
        "component": "wheeling_losses",
        "basis": "percent_in_kind",
        "value_by_voltage": {"33": "3", "11": "8", "132": "1"},
        "exemptions": [
            {"condition": "captive_26_51", "waived_pct": "100"},
            {"condition": "other", "waived_pct": "50", "note": "plants commissioned before 2020"},
        ],
    }
    figs = tb.oa_figures(view("oa_charge", body))
    assert [f.label for f in figs[:3]] == [
        "Charge (in kind) at 11 kV",
        "Charge (in kind) at 33 kV",
        "Charge (in kind) at 132 kV",
    ]
    assert all(f.value.unit == Unit.PERCENT for f in figs)
    assert figs[3].label == "Share waived for a captive user (26% of the equity, 51% of the output)"
    assert "commissioned before 2020" in figs[4].label
    one = tb.oa_figures(
        view("oa_charge", {"component": "wheeling", "basis": "inr_per_kvah", "value": "0.93"})
    )
    assert one[0].value.unit == Unit.INR_PER_KVAH and one[0].label == "Charge"


def test_banking_figures_and_sentences() -> None:
    body = {
        "granularity": "tod_slot_monthly",
        "banking": {
            "allowed": True,
            "period": "monthly",
            "charge": {"kind": "in_kind_percent", "value": "8"},
        },
    }
    row = tb.row_of(view("settlement", body), "approved")
    (charge,) = row.figures
    assert charge.label == "Banking charge (in kind)" and charge.value.unit == Unit.PERCENT
    assert row.points and all("ref" in p for p in row.points)


def test_a_missing_or_non_numeric_value_is_not_a_figure() -> None:
    body = {
        "category": "X",
        "billing_basis": "kwh",
        "demand_charge": {"value": "", "basis": "inr_per_kva_month"},
        "energy_charge": {"base": "n/a"},
    }
    assert tb.schedule_figures(view("tariff_schedule", body)) == []


def test_match_key_ignores_values_and_versions() -> None:
    a = view(
        "oa_charge",
        {"component": "wheeling", "basis": "inr_per_kwh", "value": "1"},
        scope={"voltage_kv": {"max": 33}},
    )
    b = view(
        "oa_charge",
        {"component": "wheeling", "basis": "inr_per_kwh", "value": "9"},
        scope={"voltage_kv": {"max": 33}},
    )
    c = view(
        "oa_charge",
        {"component": "wheeling", "basis": "inr_per_kwh", "value": "1"},
        scope={"voltage_kv": {"min": 66}},
    )
    assert tb.match_key(a) == tb.match_key(b) != tb.match_key(c)
