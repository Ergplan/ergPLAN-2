"""Himachal Pradesh (HPERC orders 9, 10), Jharkhand (JSERC 111, 37), Assam (AERC 176, 177), Goa and
Puducherry (JERC 23, 28, 30) from the ARR Network, on trimmed copies of the real section pages: the
shapes these orders print (a category column with the unit and the rate in columns of their own, a
fixed and a variable charge for each year of a control period, a schedule carried over pages under
the commission's running head, 'Existing and Approved Tariff for … Category', a load band as a
caption, ToD tables with a serial column and shares of the 'Normal Energy Charges'), what is left
unmapped and why, the drafts with their golden cases, and months worked by hand from the order pages
(packages/rulebooks/golden/{hp,jh,as,ga,py}/tariff-months.yaml)."""

from __future__ import annotations

import calendar
from dataclasses import replace
from datetime import date
from decimal import Decimal as D
from typing import Any

import pytest
import yaml
from sqlalchemy import select

from ergplan.core.ids import document_id
from ergplan.core.settings import get_settings
from ergplan.core.sourced import RefKind, SourcedValue, SourceRef
from ergplan.core.units import Unit
from ergplan.db.base import session_scope
from ergplan.db.models import RulebookVersion
from ergplan.engine import scope as escope
from ergplan.engine.tariff_eval import MonthQuantities, evaluate
from ergplan.regreader import arr_import, arr_map, arr_rates
from ergplan.regreader.tariff_oder import Mapped
from ergplan.rulebook import sandbox, service

from .arr_helpers import offline_client, read

# where each date comes from: HPERC 'This Schedule of Tariff shall come into force with effect from 1st April 2026'
# (order 9, p. 491) / '1st April 2025' (order 10, p. 414); JSERC 'TARIFF SCHEDULE APPLICABLE FROM 01.04.2026' (111,
# p. 326) / '01.05.2025' (37, p. 256); AERC 'effective from April 1, 2026' (176, p. 18) / 'April 1, 2023' (177, p. 19,
# the next order of 27 June 2024 is not at the source); JERC 'This Order shall come into force with effect from
# 1st October, 2025' (23, p. 15; 28, p. 20), the FY 2026-27 rates of the control period from 1 April 2026 to 31 March 2027
STATES: dict[str, dict[str, Any]] = {
    "hp": {"orders": [9, 10], "from": {9: date(2026, 4, 1), 10: date(2025, 4, 1)}},
    "jh": {"orders": [111, 37], "from": {111: date(2026, 4, 1), 37: date(2025, 5, 1)}},
    "as": {
        "orders": [176, 177],
        "from": {176: date(2026, 4, 1), 177: date(2023, 4, 1)},
        "to": {177: date(2024, 6, 26)},
    },
    "ga": {
        "orders": ["23@FY2025-26", "23@FY2026-27"],
        "from": {"23@FY2025-26": date(2025, 10, 1), "23@FY2026-27": date(2026, 4, 1)},
        "to": {"23@FY2026-27": date(2027, 3, 31)},
        "jurisdiction": {23: "goa_ed"},
    },
    "py": {
        "orders": ["28@FY2025-26", "28@FY2026-27"],
        "from": {"28@FY2025-26": date(2025, 10, 1), "28@FY2026-27": date(2026, 4, 1)},
        "to": {"28@FY2026-27": date(2027, 3, 31)},
        "jurisdiction": {28: "ped_py"},
    },
}


def _import(state: str) -> list[arr_import.OrderOutcome]:
    s = STATES[state]
    return arr_import.run(
        offline_client(),
        state,
        s["orders"],
        effective=s["from"],
        until=s.get("to"),
        jurisdictions=s.get("jurisdiction"),
    )


def _mapped(order_id: int, year: str | None = None) -> Mapped:
    order, sections = read(order_id)
    if year:
        order = replace(order, arr_year=year)
    return arr_map.map_order(arr_map.Source(order, document_id(order.sha256), date(2026, 4, 1)), sections)


def _one(rows: list[dict[str, Any]], **scope: Any) -> dict[str, Any]:
    [hit] = [r for r in rows if all(r["scope"].get(k) == v for k, v in scope.items())]
    return hit


def _why(m: Mapped, needle: str) -> list[str]:
    return [u.reason for u in m.unmapped if needle in u.fact_id]


def _rates(rows: list[dict[str, Any]]) -> dict[str, tuple[str, str, str]]:
    return {
        r["body"]["category"]: (
            r["body"]["demand_charge"]["value"],
            r["body"]["energy_charge"]["base"],
            r["body"]["billing_basis"],
        )
        for r in rows
    }


# ------------------------------------------------------------------ printed strings


def test_units_and_shares_these_orders_print() -> None:
    assert arr_rates.rate("400", heading="Rs/kVA/Month") == arr_rates.Rate(D("400"), Unit.INR_PER_KVA_MONTH)
    assert arr_rates.rate("6.40", heading="Rs/kVAh") == arr_rates.Rate(D("6.40"), Unit.INR_PER_KVAH)
    assert arr_rates.rate("75", heading="Rs/Conn./Month") is None  # per connection: not a demand charge
    assert arr_rates.rate("50", heading="Rs/hp/Month") is None
    assert arr_rates.unit_of("Approved Demand Charge (Rs/kVA/mth)") == Unit.INR_PER_KVA_MONTH  # HPERC's 'mth'
    assert arr_rates.unit_of("Rs. / kVA or part thereof / month") == Unit.INR_PER_KVA_MONTH
    assert arr_rates.of_normal_pct("80% of Normal Energy Charges") == D(-20)
    assert arr_rates.of_normal_pct("120% of Normal Energy Charges") == D(20)
    assert arr_rates.of_normal_pct("Normal Energy Charges") == D(0)
    assert arr_rates.of_normal_pct("120% of normal rate of energy charges") == D(20)  # as before


# ------------------------------------------------------------------ a category column, the unit in a column of its own


def test_jsec_rows_name_their_category_and_print_the_unit_beside_the_rate() -> None:
    m = _mapped(111)
    rows = m.by_family["tariff_schedule"]
    assert _rates(rows) == {
        "DS-HT": ("150", "7.20", "kvah"),
        "CS-HT": ("400", "8.00", "kvah"),
        "LTIS": ("150", "6.60", "kvah"),
        "HTS": ("400", "6.40", "kvah"),
        "HTSS": ("400", "5.80", "kvah"),
        "HTIS": ("400", "6.50", "kvah"),
    }
    hts = _one(rows, category=["HTS"])
    assert hts["body"]["demand_charge"]["basis"] == "inr_per_kva_month"
    assert (
        hts["source_ref"]["page"] == 336
        and hts["source_ref"]["quote"] == "HTS | Rs/kVA/Month | 400 | Rs/kVAh | 6.40"
    )
    # 'HTS' is printed under the heading 'High Tension Special Service (HTSS)': the row's own code is its category
    assert "rate_block" not in hts["scope"]
    # a table whose rows name no code ('Rural', 'Urban': sub-categories of a heading the site did not keep) is
    # read the old way, and not mapped
    assert _why(m, "Tariff · p. 327") == [
        "not printed under a tariff category (a summary or billing-rate table)"
    ]
    assert _why(m, "IAS Tariff · p. 332") == ["2 fixed-charge columns and not exactly one headed approved"]


def test_jerc_control_period_columns_are_read_for_the_order_year_only() -> None:
    first, second = _mapped(23, "FY2025-26"), _mapped(23, "FY2026-27")
    a, b = _rates(first.by_family["tariff_schedule"]), _rates(second.by_family["tariff_schedule"])
    assert a["HTS-IV"] == ("280.00", "5.30", "kvah") and b["HTS-IV"] == ("280.00", "5.60", "kvah")
    assert a["EHTS-II"] == ("275.00", "5.20", "kvah") and b["EHTS-II"] == ("275.00", "5.50", "kvah")
    assert a["HTS-I"] == ("120.00", "5.50", "kvah") and b["HTS-I"] == ("120.00", "5.85", "kvah")
    assert set(a) == set(b) and "LTIS-I" not in a  # LTIS-I is printed in consumption slabs
    hts = _one(first.by_family["tariff_schedule"], category=["HTS-IV"])
    assert hts["source_ref"]["page"] == 229
    assert hts["source_ref"]["quote"].startswith(
        "Approved Tariff FY 2025 -26 FC | Approved Tariff FY 2025 -26 VC"
    )
    # the table on p. 230 is filed under 'HTS-IV' (the last row of p. 229): its rows keep their own codes
    assert _one(first.by_family["tariff_schedule"], category=["HTS-V"])["source_ref"]["page"] == 230
    assert any("consumption slab" in w for w in _why(first, "row '0-500"))


def test_a_schedule_carried_over_pages_under_the_running_head_keeps_its_year() -> None:
    first, second = _mapped(28, "FY2025-26"), _mapped(28, "FY2026-27")
    a, b = _rates(first.by_family["tariff_schedule"]), _rates(second.by_family["tariff_schedule"])
    # Table 9-1 (FY 2025-26) starts on p. 241; its HT rows are on p. 242 under the commission's running head
    assert a["HTS-IV"] == ("450.00", "6.25", "kvah") and b["HTS-IV"] == ("450.00", "6.25", "kvah")
    assert a["LTAS-III"] == ("30.00", "5.95", "kvah") and b["LTAS-III"] == ("30.00", "6.20", "kvah")
    assert _one(first.by_family["tariff_schedule"], category=["HTS-IV"])["source_ref"]["page"] == 242
    assert _one(second.by_family["tariff_schedule"], category=["HTS-IV"])["source_ref"]["page"] == 244
    # the years after the order's own are not read into it (Table 9-5 is for FY 2029-30)
    assert any("FY2029-30" in w for w in _why(second, "Table 9-5"))
    # the FY 2026-27 schedule is printed twice (Table 6-4, p. 200, and Table 9-2, pp. 242-243): one row each
    assert len([r for r in second.by_family["tariff_schedule"] if r["body"]["category"] == "LTAS-III"]) == 1


def test_a_table_under_the_running_head_is_not_filed_under_the_heading_before_it() -> None:
    m = _mapped(30)
    assert not m.by_family["tariff_schedule"]
    assert any("running head" in w for w in _why(m, "LT Industries"))


# ------------------------------------------------------------------ HPERC: the category in the caption


def test_existing_and_approved_tariff_tables_give_the_approved_demand_charge() -> None:
    m = _mapped(9)
    rows = m.by_family["tariff_schedule"]
    lips = [r for r in rows if r["body"]["category"] == "Large Industrial Power Supply"]
    got = {
        (str(r["scope"].get("voltage_kv")), r["scope"].get("rate_block")): (
            r["body"]["demand_charge"]["value"],
            r["body"]["energy_charge"]["base"],
        )
        for r in lips
    }
    assert got == {
        ("{'min': 220}", None): ("425.00", "5.45"),
        ("{'min': 132, 'max': 132}", None): ("425.00", "5.50"),
        ("{'min': 66, 'max': 66}", None): ("425.00", "5.55"),
        ("None", "HT-1 (up to 1 MVA)"): ("250.00", "5.85"),
        ("None", "HT-2 (More than 1 MVA)"): ("400.00", "5.60"),
    }
    assert all(r["source_ref"]["page"] == 424 and r["body"]["billing_basis"] == "kvah" for r in lips)
    # beside the existing columns the approved ones; of two approved fixed columns, the one per kVA
    bulk = _one(rows, category=["Bulk Supply"], rate_block="EHT")
    assert (bulk["body"]["demand_charge"]["value"], bulk["body"]["energy_charge"]["base"]) == (
        "350.00",
        "5.47",
    )
    # a 'Slab' column of loads printed as '50kVA' is still taken for consumption slabs (as before): not mapped
    assert any("consumption slabs" in w for w in _why(m, "Table 279"))


# ------------------------------------------------------------------ AERC


def test_assam_schedule_rows_and_what_is_left_out() -> None:
    m = _mapped(176)
    rows = m.by_family["tariff_schedule"]
    ht1 = _one(rows, category=["HT Category V (B)-HT-I Industries", "HT I"])
    # the caption 'Load above 50 kVA and up to 150 kVA.' is the row's band of contract demand
    assert ht1["scope"]["contract_demand_kva"] == {"min": 50, "min_exclusive": True, "max": 150}
    assert "rate_block" not in ht1["scope"]
    assert (ht1["body"]["demand_charge"]["value"], ht1["body"]["energy_charge"]["base"]) == ("200", "7.69")
    opt = {
        r["scope"]["rate_block"]: r["body"]["energy_charge"]["base"] for r in rows if "HT-II" in r["label"]
    }
    assert opt == {"Option -1": "8.09", "Option -2": "7.69"}
    # a line of the applicability clause is no rate block; 'For all consumption' adds nothing
    pww = _one(rows, category=["HT Category - III: Public Water Works"])
    assert "rate_block" not in pww["scope"]
    # 'First 120 kWh per month' is a consumption slab
    assert any("consumption slab" in w for w in _why(m, "Domestic A"))
    # headings the site took from the applicability text where every other heading names a category
    assert any("names no category" in w for w in _why(m, "Secondary Education Council"))
    assert not [r for r in rows if "AHSEC" in r["label"] or "CBSE" in r["label"]]


def test_assam_tod_rows_only_where_the_order_makes_them_apply() -> None:
    m = _mapped(176)
    tod = m.by_family["tod_slots"]
    cats = {r["scope"]["category"][0] for r in tod}
    assert cats == {
        "HT Category V (B)-HT-I Industries",
        "HT Category VII: Oil and Coal",
        "LT Category X- Electric Vehicles Charging Station",
    }
    ht1 = _one(tod, category=["HT Category V (B)-HT-I Industries"])
    assert [
        (s["start"], s["end"], s.get("rebate_pct"), s.get("surcharge_pct")) for s in ht1["body"]["slots"]
    ] == [
        ("22:00", "09:00", None, None),
        ("09:00", "17:00", "20", None),
        ("17:00", "22:00", None, "20"),
    ]
    # 'shall be optionally applicable'; a table without a caption of its own (the sentence that says whether it
    # applies is on the page before: HT-III 'optionally', HT-II 'shall be applicable' to Option-1 only)
    assert any("optional for the consumer" in w for w in _why(m, "HT Category I: HT Domestic"))
    assert any("no caption of its own" in w for w in _why(m, "HT Category - III"))
    assert any("no caption of its own" in w for w in _why(m, "HT Category V (C): HT-II Industries"))


# ------------------------------------------------------------------ open-access charges


def test_charges_by_voltage_a_loss_column_and_a_css_column() -> None:
    rows = _mapped(111).by_family["oa_charge"]
    wheeling = {r["label"]: r["body"]["value"] for r in rows if r["body"]["component"] == "wheeling"}
    assert wheeling == {
        "Wheeling charge · LT": "2.06",
        "Wheeling charge · 11 kV": "0.65",
        "Wheeling charge · 33 kV and above": "0.25",
    }
    assert _one(rows, consumer_kind=["lt"])["body"]["value"] == "2.06"
    losses = {r["label"]: r["body"]["value"] for r in rows if r["body"]["component"] == "wheeling_losses"}
    assert losses == {"33 kV": "3.00", "11kV": "8.00", "LT": "15.05"}  # the 'Dist. loss level' column
    css = [r["body"]["value"] for r in rows if r["body"]["component"] == "cross_subsidy_surcharge"]
    assert css == ["1.95", "1.95"]
    # the serial column is not the row's name ('1 | At 33 kV level | 5%')
    assam = {
        r["label"]: r["body"]["value"]
        for r in _mapped(176).by_family["oa_charge"]
        if r["body"]["component"] == "wheeling_losses"
    }
    assert assam == {"At 33 kV level": "5", "At 11 kV level": "11"}


def test_the_unit_of_a_single_rate_in_its_row_name() -> None:
    [row] = _mapped(23, "FY2025-26").by_family["oa_charge"]
    assert row["body"] == {"component": "additional_surcharge", "basis": "inr_per_kwh", "value": "0.74"}
    # a finding, not a fix: the ARR Network files the Electricity Department's own Table 165 under the caption
    # of the Commission's Table 166 (0.82): the golden case reads 0.82 from the page and fails on this draft


# ------------------------------------------------------------------ the drafts


@pytest.mark.usefixtures("jurisdictions")
def test_the_five_states_become_drafts_with_their_golden_cases() -> None:
    out = {f"{o.order.order_id}@{o.order.arr_year}": o for state in STATES for o in _import(state)}
    assert {k: o.jurisdiction_id for k, o in out.items()} == {
        "9@FY2026-27": "hpsebl", "10@FY2025-26": "hpsebl", "111@FY2026-27": "jbvnl", "37@FY2025-26": "jbvnl",
        "176@FY2026-27": "apdcl", "177@FY2023-24": "apdcl", "23@FY2025-26": "goa_ed", "23@FY2026-27": "goa_ed",
        "28@FY2025-26": "ped_py", "28@FY2026-27": "ped_py",
    }  # fmt: skip
    windows = {k: (o.effective_from, o.effective_to) for k, o in out.items()}
    assert windows["10@FY2025-26"] == (date(2025, 4, 1), date(2026, 3, 31))
    assert windows["37@FY2025-26"] == (date(2025, 5, 1), date(2026, 3, 31))
    assert windows["177@FY2023-24"] == (date(2023, 4, 1), date(2024, 6, 26))
    assert windows["23@FY2025-26"] == (date(2025, 10, 1), date(2026, 3, 31))
    assert windows["28@FY2026-27"] == (date(2026, 4, 1), date(2027, 3, 31))
    assert all("ASSUMED" not in o.effective_basis for o in out.values())
    golden = {(k, v.family): (v.golden, v.golden_failing) for k, o in out.items() for v in o.versions}
    for (k, family), (score, failing) in golden.items():
        passed, total = (int(x) for x in score.split("/"))
        if (k, family) in {("23@FY2025-26", "oa_charge"), ("23@FY2026-27", "oa_charge")}:
            # the additional surcharge the ARR Network gives is the petitioner's (see the golden file)
            assert passed == 0 and total == 1 and "additional surcharge (Table 166, approved)" in failing[0]
            continue
        assert passed == total >= 1 and not failing, (k, family, score, failing)
    with session_scope() as s:
        drafts = list(s.scalars(select(RulebookVersion).where(RulebookVersion.origin == "arr_network")))
        assert len(drafts) == len(golden) and all(
            v.status == "draft" and v.approved_by is None for v in drafts
        )
    again = [v.action for state in STATES for o in _import(state) for v in o.versions]
    assert again == ["unchanged"] * len(golden)


def _sv(value: Any, unit: Unit) -> SourcedValue:
    return SourcedValue(
        value=D(str(value)), unit=unit, source_refs=(SourceRef(kind=RefKind.ASSUMPTION, note="worked case"),)
    )


@pytest.mark.usefixtures("jurisdictions")
@pytest.mark.parametrize("state", list(STATES))
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
            connection_kind="ht", rate_block=c.get("rate_block"), contract_demand_kva=D(str(c["contract_kva"])),
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
        exp = c["expect"]
        assert schedule.body["billing_basis"] == exp["billing_basis"], c["name"]
        unit = Unit.KVAH if schedule.body["billing_basis"] == "kvah" else Unit.KWH
        q = MonthQuantities(
            month=when.replace(day=1),
            units=_sv(c["units"], unit),
            by_slot={k: _sv(u, unit) for k, u in c["by_slot"].items()},
            max_kva=_sv(c["max_kva"], Unit.KVA),
            contract_kva=_sv(c["contract_kva"], Unit.KVA),
        )
        bill = evaluate(schedule, tod, q)
        assert bill.sum("demand_charge") == D(exp["demand_charge"]), c["name"]
        assert bill.sum("energy_charge") == D(exp["energy_charge"]), c["name"]
        assert bill.sum("tod_adjustment") == D(exp["tod_adjustment"]), c["name"]
        assert bill.total.value == D(exp["total"]), c["name"]
        refs = [r for ln in bill.lines for r in ln.amount.source_refs if r.kind == RefKind.ORDER]
        assert refs and all(r.page and r.quote and r.document_id for r in refs)
