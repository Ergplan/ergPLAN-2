"""Gujarat (GERC order 189), Madhya Pradesh (MPERC orders 41, 42) and Bihar (BERC orders 1, 3) from the
ARR Network, on trimmed copies of the real section pages: the shapes those schedules print (demand
slabs and energy bands as numbered clauses, rates in paise, 'All units – 798', a time-of-day table of
shares of the normal rate), what is deliberately left unmapped, the drafts with their golden cases,
and months worked by hand from the order pages."""

from __future__ import annotations

import calendar
from datetime import date
from decimal import Decimal as D
from typing import Any

import pytest
import yaml
from sqlalchemy import select

from ergplan.core.settings import get_settings
from ergplan.core.sourced import RefKind, SourcedValue, SourceRef
from ergplan.core.units import Unit
from ergplan.db.base import session_scope
from ergplan.db.models import RulebookVersion
from ergplan.engine import scope as escope
from ergplan.engine.tariff_eval import MonthQuantities, evaluate
from ergplan.regreader import arr_import, arr_rates
from ergplan.rulebook import sandbox, service

from .arr_helpers import mapped, offline_client

GJ = {189: date(2026, 4, 1)}  # the order, p. 158: 'shall come into force with effect from 1st April, 2026'
MP = {41: date(2026, 3, 26), 42: date(2025, 3, 29)}  # the orders' dates (p. 18 of each)
BR = {1: date(2026, 4, 1), 3: date(2025, 4, 1)}  # 'This Order shall be effective from 1st April …'
STATES: dict[str, tuple[dict[int, date], dict[int, str]]] = {
    "gj": (GJ, {189: "gj_discoms"}),
    "mp": (MP, {41: "mp_discoms", 42: "mp_discoms"}),
    "br": (BR, {}),
}


def _import(state: str) -> list[arr_import.OrderOutcome]:
    effective, jurisdictions = STATES[state]
    return arr_import.run(
        offline_client(), state, list(effective), effective=effective, jurisdictions=jurisdictions
    )


def _row(rows: list[dict[str, Any]], label: str) -> dict[str, Any]:
    [hit] = [r for r in rows if r["label"].startswith(label)]
    return hit


def _why(m: Any, needle: str) -> list[str]:
    return [u.reason for u in m.unmapped if needle in u.fact_id]


# ------------------------------------------------------------------ printed strings


def test_rates_these_orders_print() -> None:
    kvah = arr_rates.flat_rate("All units – 798", "Energy charges (Paise / kVAh)")
    assert kvah == arr_rates.Rate(D("798"), Unit.PAISE_PER_KVAH)  # the dash is not a sign
    assert kvah.in_rupees() == arr_rates.Rate(D("7.98"), Unit.INR_PER_KVAH)
    assert arr_rates.flat_rate("First 50 units at 742", "Energy charges (Paise / kVAh)") is None
    assert arr_rates.flat_rate("All units – 798", None) is None
    words, rate = arr_rates.trailing_rate("(a) Up to 500 kVA of billing demand 400 Paise per unit") or (
        "",
        None,
    )
    assert (words, rate) == (
        "(a) up to 500 kva of billing demand",
        arr_rates.Rate(D("400"), Unit.PAISE_PER_KWH),
    )
    assert arr_rates.trailing_rate("11 kV supply 377 674 609") is None
    assert arr_rates.rate("Rs. 150/- per kVA per month") == arr_rates.Rate(D("150"), Unit.INR_PER_KVA_MONTH)
    assert arr_rates.rate("Rs. 18/- per kVA per day") is None


def test_a_share_of_the_normal_rate_and_clock_times_with_am_and_pm() -> None:
    assert arr_rates.of_normal_pct("80% normal rate of energy charges") == D("-20")
    assert arr_rates.of_normal_pct("120% of normal rate of energy charges") == D("20")
    assert arr_rates.of_normal_pct("Normal rate of energy charges") == D("0")
    two = "I) 120% of normal rate of energy charges (NDS) II) 110% of normal rate of energy charges (others)"
    assert arr_rates.of_normal_pct(two) is None
    assert arr_rates.hours("(i) Off peak load period (9:00 a.m. to 5.00 p.m.)") == ("09:00", "17:00")
    assert arr_rates.hours("(iii) Normal period (11.00 p.m. to 9.00 a.m.)") == ("23:00", "09:00")


# ------------------------------------------------------------------ Gujarat: numbered clauses


def test_demand_slabs_and_energy_bands_printed_as_clauses_make_one_row() -> None:
    m = mapped(189, GJ[189])
    assert set(m.by_family) == {"tariff_schedule"} and len(m.by_family["tariff_schedule"]) == 2
    assert service.validate_rows("tariff_schedule", m.by_family["tariff_schedule"]) == {}
    r = _row(m.by_family["tariff_schedule"], "HTP-1")
    assert r["scope"] == {"category": ["HTP-1"]}  # the heading as the order prints it, without 'RATE:'
    assert r["body"] == {
        "category": "HTP-1",
        "billing_basis": "kwh",
        "demand_charge": {
            "value": "475",
            "basis": "inr_per_kva_month_billing_demand",
            "slabs": [
                {"value": "150", "up_to_kva": "500"},
                {"value": "260", "up_to_kva": "1000"},
                {"value": "475"},
            ],
        },
        "energy_charge": {
            "base": "4.00",  # 400 paise per unit
            "by_billing_demand": [
                {"above_kva": "500", "base": "4.20"},
                {"above_kva": "2500", "base": "4.30"},
            ],
        },
    }
    refs = r["field_refs"]
    assert refs["/demand_charge/slabs/1/value"]["page"] == 173
    assert (
        "For next 500 kVA of billing demand | Rs. 260/- per kVA per month"
        in refs["/demand_charge/slabs/1/value"]["quote"]
    )
    assert "above 2500 kVA 430 Paise per Unit" in refs["/energy_charge/by_billing_demand/1/base"]["quote"]
    two = _row(m.by_family["tariff_schedule"], "HTP-II")
    assert [s["value"] for s in two["body"]["demand_charge"]["slabs"]] == ["115", "225", "290"]
    assert two["body"]["energy_charge"]["base"] == "4.35"
    assert (two["field_refs"]["/demand_charge/value"]["page"], two["source_ref"]["page"]) == (176, 177)


def test_what_the_gujarat_schedule_prints_that_is_not_mapped() -> None:
    m = mapped(189, GJ[189])
    assert all("time-of-use charge printed as a clause" in w for w in _why(m, "HTP-1 PLUS"))
    assert len(_why(m, "HTP-1 PLUS")) == 1 and len(_why(m, "12.3. TIME OF USE")) == 1
    assert _why(m, "LTMD 5.1.") == ["billing-demand clauses in kW: the family's slabs are in kVA"]
    [per_day] = _why(m, "HTP-III 13.1.")
    assert per_day.startswith("a demand clause not read as rupees per kVA per month")
    # two tables of sections 16 and 17 filed under 'RATE: HTP-V' (section 15): neither is believed
    assert all("category of the table is not certain" in w for w in _why(m, "RATE: HTP-V"))
    assert len(_why(m, "RATE: HTP-V")) == 2
    # Table 9-3 works a surcharge of 2.52 out; the order's text then caps it at 1.33 (p. 152)
    assert _why(m, "Table 9-3") == ["not headed as approved by the commission (computed, or an input)"]
    assert _why(m, "Table 9-2") == ["not headed as approved by the commission (computed, or an input)"]


# ------------------------------------------------------------------ Madhya Pradesh


def test_a_serial_column_is_not_part_of_the_voltage_and_two_listed_voltages_are_a_band() -> None:
    m = mapped(41, MP[41])
    assert service.validate_rows("tariff_schedule", m.by_family["tariff_schedule"]) == {}
    rail = _row(m.by_family["tariff_schedule"], "HV-1")
    assert rail["scope"] == {
        "category": ["HV-1"],
        "voltage_kv": {"min": 132, "max": 220},
    }  # no block 'Tariff'
    assert rail["body"]["demand_charge"] == {"value": "354", "basis": "inr_per_kva_month"}
    assert rail["body"]["energy_charge"] == {"base": "6.26"} and rail["body"]["billing_basis"] == "kwh"
    assert rail["source_ref"]["quote"] == "1 | Railway Traction on 132 kV / 220 kV | 354 | 626"
    assert [r["label"].split(" · ")[0] for r in m.by_family["tariff_schedule"]] == ["HV-1", "HV-5", "HV-9"]
    # a fixed charge printed once for three voltages reaches the source on the first row only
    assert len(_why(m, "HV-5 Tariff")) == 2


def test_two_energy_rates_by_load_factor_are_not_one_rate() -> None:
    m = mapped(41, MP[41])
    for category in ("HV-2 Tariff", "HV-3 Tariff"):
        assert _why(m, category) == ["2 energy-charge columns and not exactly one headed approved"]


def test_a_charge_the_commission_determines_or_heads_as_approved() -> None:
    m = mapped(41, MP[41])
    assert service.validate_rows("oa_charge", m.by_family["oa_charge"]) == {}
    add = _row(m.by_family["oa_charge"], "Additional surcharge")
    assert add["body"] == {"component": "additional_surcharge", "basis": "inr_per_kwh", "value": "1.18"}
    assert add["source_ref"]["page"] == 139 and "Per Unit Additional Surcharge" in add["source_ref"]["quote"]
    green = _row(m.by_family["oa_charge"], "Approved Green Energy Charges")
    assert green["body"] == {"component": "green_tariff_premium", "basis": "inr_per_kwh", "value": "0.30"}
    assert green["scope"] == {} and green["source_ref"]["page"] == 142
    assert len(m.by_family["oa_charge"]) == 2
    claimed = "claimed, existing or proposed figures, not the commission's approved rate"
    assert (
        _why(m, "Table 97") == [claimed]
        and _why(m, "Table 99") == [claimed]
        and _why(m, "Table 81") == [claimed]
    )
    not_headed = ["not headed as approved by the commission (computed, or an input)"]
    assert _why(m, "Table 89") == not_headed and _why(m, "Table 96") == not_headed
    previous = mapped(42, MP[42])
    assert _row(previous.by_family["oa_charge"], "Additional surcharge")["body"]["value"] == "1.24"
    assert _row(previous.by_family["oa_charge"], "Approved Green")["body"]["value"] == "0.53"


# ------------------------------------------------------------------ Bihar


def test_one_table_per_voltage_with_the_rate_for_all_units() -> None:
    m = mapped(1, BR[1])
    assert service.validate_rows("tariff_schedule", m.by_family["tariff_schedule"]) == {}
    r = _row(m.by_family["tariff_schedule"], "HTIS (Industrial) – I (11 kV)")
    assert r["scope"] == {
        "category": ["HTIS (Industrial) – I (11 kV)"]
    }  # a sentence above a table is no block
    assert r["body"] == {
        "category": "HTIS (Industrial) – I (11 kV)",
        "billing_basis": "kvah",
        "demand_charge": {"value": "550", "basis": "inr_per_kva_month"},
        "energy_charge": {"base": "7.98"},
    }
    assert r["source_ref"]["page"] == 559 and r["source_ref"]["quote"] == "550 | All units – 798"
    assert _row(m.by_family["tariff_schedule"], "HTSS")["body"]["energy_charge"] == {"base": "4.94"}


def test_a_table_filed_under_a_category_of_another_voltage_is_not_believed() -> None:
    m = mapped(1, BR[1])
    # HTS-III (132 kV) and HTS-IV (220 kV) reach the source under the heading of HTS-II (33 kV)
    assert [w for w in _why(m, "3-phase at 132 kV")] == [
        "the heading names 132 kV but the table is filed under 'HTS (General) – II (33 kV)': "
        "the category of the table is not certain"
    ]
    assert any("names 132, 220 kV" in w for w in _why(m, "HTSS"))
    labels = [r["label"] for r in m.by_family["tariff_schedule"]]
    assert "HTS (General) – II (33 kV)" in labels  # the one table that names 33 kV
    # four HTIS tables (33, 132, 220, 400 kV) under one heading and no voltage in their titles
    assert not any(label.startswith("HTIS (Industrial)– II") for label in labels)
    assert len([u for u in m.unmapped if "4 tables print a rate for the same scope" in u.reason]) == 4


def test_time_of_day_as_shares_of_the_normal_rate_for_all_ht_consumers() -> None:
    m = mapped(1, BR[1])
    assert service.validate_rows("tod_slots", m.by_family["tod_slots"]) == {}
    [tod] = m.by_family["tod_slots"]
    assert tod["scope"] == {"consumer_kind": ["ht", "eht"]}
    assert [
        (s["start"], s["end"], s.get("rebate_pct"), s.get("surcharge_pct")) for s in tod["body"]["slots"]
    ] == [
        ("09:00", "17:00", "20", None),
        ("17:00", "23:00", None, "20"),
        ("23:00", "09:00", None, None),
    ]
    assert tod["source_ref"]["page"] == 567
    [lt] = _why(m, "LOW-TENSION")
    assert lt.startswith("adjustment not read")  # two shares in one cell


def test_a_time_of_day_table_headed_proposed_is_not_imported() -> None:
    m = mapped(3, BR[3])
    assert len(m.by_family["tod_slots"]) == 1 and m.by_family["tod_slots"][0]["source_ref"]["page"] == 736
    assert _why(m, "Prepaid Smart meters") == [
        "claimed, existing or proposed figures, not the commission's approved rate"
    ]
    # two approved wheeling tables, one per voltage, are not one charge for every consumer
    assert len([u for u in m.unmapped if "2 tables print a rate for the same scope" in u.reason]) == 2
    assert "oa_charge" not in m.by_family or not m.by_family["oa_charge"]


# ------------------------------------------------------------------ drafts and their golden cases


@pytest.mark.usefixtures("jurisdictions")
def test_the_three_states_become_drafts_with_green_golden_cases() -> None:
    out = {o.order.order_id: o for state in STATES for o in _import(state)}
    assert {i: o.jurisdiction_id for i, o in out.items()} == {
        189: "gj_discoms", 41: "mp_discoms", 42: "mp_discoms", 1: "br_discoms", 3: "br_discoms",
    }  # fmt: skip
    assert out[41].mapped == {"oa_charge": 2, "tariff_schedule": 3}
    assert out[42].mapped == {"oa_charge": 2, "tariff_schedule": 3}
    assert out[1].mapped == {
        "tariff_schedule": 8,
        "tod_slots": 1,
    }  # the fixture keeps the HT schedule and two LT tables
    assert out[3].mapped == {"tariff_schedule": 8, "tod_slots": 1}
    assert out[189].mapped == {"tariff_schedule": 2}
    assert (out[42].effective_from, out[42].effective_to) == (date(2025, 3, 29), date(2026, 3, 25))
    assert (out[3].effective_from, out[3].effective_to) == (date(2025, 4, 1), date(2026, 3, 31))
    for oid in (41, 42, 1, 3):
        for v in out[oid].versions:
            passed, total = (int(x) for x in v.golden.split("/"))
            assert v.action == "proposed" and passed == total >= 3 and not v.golden_failing, (oid, v)
    # Gujarat: the imported cases pass; the cases of the approved rulebook ask for 'HTP-I', the name the
    # order uses everywhere but in the heading of clause 11 ('RATE: HTP-1'), and for rules it states in words
    [gj] = out[189].versions
    assert gj.golden == "21/34" and len(gj.golden_failing) == 13
    assert not any("ARR" in name for name in gj.golden_failing)
    with session_scope() as s:
        drafts = list(s.scalars(select(RulebookVersion).where(RulebookVersion.origin == "arr_network")))
        assert len(drafts) == 9 and all(v.status == "draft" and v.approved_by is None for v in drafts)
    again = [v.action for state in STATES for o in _import(state) for v in o.versions]
    assert again == ["unchanged"] * 9


def _sv(value: Any, unit: Unit) -> SourcedValue:
    return SourcedValue(
        value=D(str(value)), unit=unit, source_refs=(SourceRef(kind=RefKind.ASSUMPTION, note="worked case"),)
    )


@pytest.mark.usefixtures("jurisdictions")
@pytest.mark.parametrize("state", ["gj", "mp", "br"])
def test_hand_worked_months_on_the_imported_drafts(state: str) -> None:
    _import(state)
    cases = yaml.safe_load(
        (get_settings().rulebooks_dir / "golden" / state / "tariff-months.yaml").read_text()
    )
    assert len(cases["cases"]) >= 2
    for c in cases["cases"]:
        y, m = (int(x) for x in str(c["month"]).split("-"))
        when = date(y, m, calendar.monthrange(y, m)[1])
        facts = escope.ConnectionFacts(
            discom=c["discom"], state=cases["state"], category=c["category"], voltage_kv=D(str(c["voltage_kv"])),
            connection_kind="ht", consumer_kind=c.get("consumer_kind"),
        )  # fmt: skip
        with session_scope() as s:
            chain = service.jurisdiction_chain(s, c["discom"])
            ctx = escope.context(facts, when)
            schedule = escope.first(
                sandbox.drafts_in_force(s, "tariff_schedule", chain, when, cases["origin"]), ctx, chain
            )
            tod = escope.first(
                sandbox.drafts_in_force(s, "tod_slots", chain, when, cases["origin"]), ctx, chain
            )
            assert schedule is not None, c["name"]
            assert (tod is not None) == bool(c["by_slot"]), c["name"]
            v = s.get(RulebookVersion, schedule.version_id)
            assert v is not None and v.status == "draft" and c["expect"]["tariff_year"] in (v.title or "")
        unit = Unit.KVAH if schedule.body["billing_basis"] == "kvah" else Unit.KWH
        q = MonthQuantities(
            month=when.replace(day=1),
            units=_sv(c["units"], unit),
            by_slot={k: _sv(u, unit) for k, u in c["by_slot"].items()},
            max_kva=_sv(c["max_kva"], Unit.KVA),
            contract_kva=_sv(c["contract_kva"], Unit.KVA),
        )
        bill = evaluate(schedule, tod, q)
        exp = c["expect"]
        assert bill.sum("demand_charge") == D(exp["demand_charge"]), c["name"]
        assert bill.sum("energy_charge") == D(exp["energy_charge"]), c["name"]
        assert bill.sum("tod_adjustment") == D(exp["tod_adjustment"]), c["name"]
        assert bill.total.value == D(exp["total"]), c["name"]
        # every line cites the order page of the imported row (invariant 1)
        refs = [r for ln in bill.lines for r in ln.amount.source_refs if r.kind == RefKind.ORDER]
        assert refs and all(r.page and r.quote and r.document_id for r in refs)
