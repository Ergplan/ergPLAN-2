"""The tariff a bill shows (ergPLAN 2), the slot classes a block carries, and the schedule made of a
consumer's own tariff: small pure cases."""

from __future__ import annotations

from decimal import Decimal

from ergplan.core.sourced import RefKind, SourceRef
from ergplan.engine import scope as escope
from ergplan.engine.tariff_eval import slot_classes
from ergplan.intake.bills import BillValue
from ergplan.intake.site_intake import bill_tariff
from ergplan.platform.routers.cost import stated_schedule


def _v(path: str, value: str) -> BillValue:
    return BillValue(value=value, unit=None, ref=SourceRef(kind=RefKind.BILL, bill_id="b1", field_path=path))


def _bill(**over: str) -> dict[str, BillValue]:
    vals = {
        "charges.energy": "730000",
        "energy.billed_units": "100000",
        "energy.kwh": "100000",
        "charges.demand": "1215000",
        "demand.max_kva": "2700",
        "demand.contract_kva": "3000",
    }
    vals.update(over)
    return {k: _v(k, v) for k, v in vals.items()}


def test_energy_and_fixed_charge_from_the_bills_own_figures() -> None:
    t = bill_tariff(_bill())
    e, d = t["energy_charge_inr_per_unit"], t["fixed_charge_inr_per_kva_month"]
    assert (
        e.value == Decimal("7.3000")
        and e.provenance == "bill"
        and e.ref
        and e.ref.field_path == "charges.energy"
    )
    # billed on the higher of the recorded and the contract demand: 12,15,000 / 3,000 kVA
    assert d.value == Decimal("405.00") and d.ref and d.ref.field_path == "charges.demand"
    assert t["billing_basis"].value == "kwh"


def test_units_billed_in_kvah_are_recognised() -> None:
    t = bill_tariff(_bill(**{"energy.kvah": "104000", "energy.billed_units": "104000"}))
    assert t["billing_basis"].value == "kvah"


def test_a_credit_or_a_missing_line_gives_no_rate() -> None:
    assert "fixed_charge_inr_per_kva_month" not in bill_tariff(_bill(**{"charges.demand": "-5000"}))
    assert "energy_charge_inr_per_unit" not in bill_tariff(_bill(**{"charges.energy": "-1"}))
    assert bill_tariff({}) == {}


def test_slot_classes_keep_the_two_class_reading_and_add_the_category() -> None:
    # the chronological rules read `peak` / `off_peak` exactly as before; the category is an extra name
    assert slot_classes({"name": "evening", "surcharge_pct": "15"}) == {"peak", "cat:peak"}
    assert slot_classes({"name": "night", "rebate_pct": "10"}) == {"off_peak", "cat:off_peak"}
    assert slot_classes({"name": "day"}) == {"off_peak", "cat:normal"}
    assert slot_classes(None) == {"off_peak", "cat:normal"}
    # the regulation's own category wins over the sign of the adjustment
    assert slot_classes({"name": "solar hours", "category": "off_peak"}) == {"off_peak", "cat:off_peak"}
    assert slot_classes({"name": "x", "category": "peak"}) == {"off_peak", "cat:peak"}


def _facts(tariff: dict[str, object] | None) -> escope.ConnectionFacts:
    return escope.ConnectionFacts(
        discom="jdvvnl", state="RJ", category="HT-5", voltage_kv=Decimal(33), connection_kind="ht",
        contract_demand_kva=Decimal(3000), stated_tariff=tariff,
    )  # fmt: skip


def test_the_consumers_own_tariff_as_a_schedule_row() -> None:
    assert stated_schedule(_facts(None)) is None and stated_schedule(_facts({})) is None
    bill = {"kind": "bill", "bill_id": "b1", "field_path": "charges.energy", "url": "/bills/b1/file"}
    r = stated_schedule(
        _facts(
            {"energy_inr_per_unit": {"value": "7.30", "source_ref": bill}, "demand_inr_per_kva_month": None}
        )
    )
    assert r is not None and r.family == "tariff_schedule" and r.version_id == ""  # not a rulebook version
    assert r.body["energy_charge"]["base"] == "7.30" and r.body["demand_charge"]["value"] == "0"
    assert r.ref("/energy_charge/base").kind == RefKind.BILL  # cited to the bill, never to an order
    assert r.ref("/demand_charge/value").kind == RefKind.CLIENT_STATED
