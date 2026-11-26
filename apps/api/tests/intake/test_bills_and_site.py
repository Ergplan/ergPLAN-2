"""Bills through (fake) billx, the confirmation round trip, SiteIntake validation and defaults, and
build() from bills to a connection with a synthesised profile."""

from __future__ import annotations

import copy
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from ergplan.core.sourced import RefKind, SourceRef
from ergplan.db.base import session_scope
from ergplan.db.models import Bill, Connection, LoadProfile
from ergplan.intake import bills as bill_store
from ergplan.intake import defaults, site_intake
from ergplan.intake.profile_synthesis import decode

from .helpers import FakeBillx, result, with_month


@pytest.fixture
def billx() -> FakeBillx:
    return FakeBillx()


def _ingest(fake: FakeBillx, tag: str, res: dict[str, Any]) -> list[Bill]:
    data = fake.register(tag, res)
    with session_scope() as s:
        rows = bill_store.ingest(s, data, f"{tag}.pdf", client=fake.client())
        s.commit()
        return rows


def test_passed_bill_is_usable_with_bill_refs(billx: FakeBillx) -> None:
    [b] = _ingest(billx, "a", result("pvvnl_passed"))
    assert b.accepted and b.accepted_by == "engine" and b.bill_month == date(2026, 9, 1)
    assert billx.options[0]["kind"] == "bill" and billx.options[0]["product"] == "ergplan"
    with session_scope() as s:
        bb = s.get(Bill, b.id)
        assert bb is not None
        assert bill_store.outstanding(s, bb) == []
        v = bill_store.values(s, bb)
        assert v["demand.max_kva"].dec == Decimal("3250")
        ref = v["demand.max_kva"].ref
        assert ref.kind == RefKind.BILL and ref.field_path == "demand.max_kva" and ref.page == 1
        assert ref.url == f"/bills/{b.id}/file#page=1"


def test_same_file_is_not_read_twice(billx: FakeBillx) -> None:
    _ingest(billx, "a", result("pvvnl_passed"))
    _ingest(billx, "a", result("pvvnl_passed"))
    assert billx.calls == 1


def test_an_unreadable_reading_is_read_again(billx: FakeBillx) -> None:
    bad = copy.deepcopy(result("pvvnl_passed"))
    bad["bills"][0]["status"] = "unreadable"
    bad["bills"][0]["passed"] = False
    bad["bills"][0]["accepted"] = False
    [b] = _ingest(billx, "u", bad)
    assert b.status == "unreadable" and billx.calls == 1
    [b2] = _ingest(billx, "u", result("pvvnl_passed"))  # billx can read it now (say, a model key added)
    assert billx.calls == 2 and b2.accepted and b2.id != b.id
    with session_scope() as s:
        assert s.get(Bill, b.id) is None
    _ingest(billx, "u", result("pvvnl_passed"))
    assert billx.calls == 2  # a readable reading is kept


def test_needs_review_round_trips_through_confirmation(billx: FakeBillx) -> None:
    [b] = _ingest(billx, "r", result("pvvnl_needs_review"))
    assert not b.accepted and b.status == "needs_review"
    with session_scope() as s:
        bb = s.get(Bill, b.id)
        assert bb is not None
        assert bill_store.outstanding(s, bb) == ["energy.kwh", "demand.max_kva", "energy.tod.0.kwh"]
        with pytest.raises(ValueError):
            bill_store.values(s, bb)
        bill_store.confirm(s, b.id, "energy.kwh", "1972620", "client@x")
        bill_store.confirm(s, b.id, "demand.max_kva", "3300", "client@x")  # a correction
        assert not bb.accepted
        bill_store.confirm(s, b.id, "energy.tod.0.kwh", "725220", "client@x")
        assert bb.accepted and bb.accepted_by == "bill_confirmations"
        v = bill_store.values(s, bb)
        assert v["demand.max_kva"].dec == Decimal("3300")
        assert v["demand.max_kva"].ref.note == "confirmed by client@x"
        with pytest.raises(bill_store.UnknownField):
            bill_store.confirm(s, b.id, "nonsense.path", "1", "client@x")
        with pytest.raises(bill_store.BillNotFound):
            bill_store.confirm(s, "missing", "energy.kwh", "1", "client@x")


def test_fact_provenance_rules() -> None:
    assert site_intake.Fact[Decimal](value=Decimal("1")).ref == SourceRef(kind=RefKind.CLIENT_STATED)
    with pytest.raises(ValidationError, match="needs its source_ref"):
        site_intake.Fact[Decimal](value=Decimal("1"), provenance="assumption")
    with pytest.raises(ValidationError, match="provenance bill"):
        site_intake.Fact[Decimal](
            value=Decimal("1"), provenance="bill", ref=SourceRef(kind=RefKind.ASSUMPTION)
        )


def _typed(**kw: Any) -> dict[str, Any]:
    d: dict[str, Any] = {
        "client_name": {"value": "Acme"},
        "site_name": {"value": "Plant 1"},
        "state": {"value": "UP"},
        "discom": {"value": "npcl"},
        "category": {"value": "HV-2"},
        "voltage_kv": {"value": "11"},
        "contract_demand_kva": {"value": "3000"},
        "industry_shape": {"value": "two_shift"},
        "weekly_off": {"value": [6]},
        "months": [
            {
                "month": "2026-05-01",
                "energy": {"value": "800000"},
                "max_kva": {"value": "2500"},
                "power_factor": {"value": "0.98"},
                "tod": [{"start": "19:00", "end": "03:00", "energy": {"value": "200000"}}],
            }
        ],
    }
    d.update(kw)
    return d


def test_typed_intake_is_valid() -> None:
    si = site_intake.SiteIntake.model_validate(_typed())
    assert si.months[0].energy.value == Decimal("800000")
    assert si.months[0].energy.provenance == "client_stated"
    assert "1 of 12 months given" in si.warnings()[0]


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"state": {"value": "MH"}}, "npcl is in UP"),
        ({"discom": {"value": "xyz"}}, "unknown DISCOM"),
        ({"industry_shape": {"value": "bakery"}}, "unknown industry shape"),
        ({"weekly_off": {"value": [7]}}, "weekly off"),
        ({"voltage_kv": {"value": "0"}}, "positive"),
        ({"months": []}, "at least 1"),
        ({"months": [{"month": "2026-05-01", "energy": {"value": "0"}}]}, "positive"),
        (
            {
                "months": [
                    {"month": "2026-05-01", "energy": {"value": "10"}, "power_factor": {"value": "1.2"}}
                ]
            },
            "power factor",
        ),
        ({"months": [{"month": "2026-05-01", "energy": {"value": "10"}, "shutdown_days": [32]}]}, "shutdown"),
        (
            {
                "months": [
                    {
                        "month": "2026-05-01",
                        "energy": {"value": "10"},
                        "tod": [{"start": "19:00", "end": "03:00", "energy": {"value": "20"}}],
                    }
                ]
            },
            "more than the month",
        ),
        (
            {
                "months": [
                    {
                        "month": "2026-05-01",
                        "energy": {"value": "10"},
                        "tod": [{"start": "19:10", "end": "03:00", "energy": {"value": "2"}}],
                    }
                ]
            },
            "quarter hour",
        ),
        (
            {
                "months": [
                    {"month": "2026-05-01", "energy": {"value": "10"}},
                    {"month": "2026-05-09", "energy": {"value": "10"}},
                ]
            },
            "each month",
        ),
    ],
)
def test_intake_validation(change: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        site_intake.SiteIntake.model_validate(_typed(**change))


def test_assets_validated_per_kind() -> None:
    ok = site_intake.AssetIn(
        kind="captive_thermal",
        params={
            "fuel": "coal",
            "capacity_mw": "10",
            "technical_minimum_pct": "55",
            "variable_cost_inr_per_kwh": "4.2",
        },
    )
    assert ok.params["aux_consumption_pct"] == "8"
    with pytest.raises(ValidationError, match="variable_cost"):
        site_intake.AssetIn(
            kind="captive_thermal",
            params={"fuel": "coal", "capacity_mw": "10", "technical_minimum_pct": "55"},
        )
    with pytest.raises(ValidationError):
        site_intake.AssetIn(kind="bess", params={"power_mw": "1"})
    with pytest.raises(ValidationError):
        site_intake.AssetIn(kind="rooftop_pv", params={"capacity_kwp": "100", "colour": "blue"})


def test_defaults_are_assumptions() -> None:
    v = defaults.voltage_kv("HV-2")
    assert v.value == Decimal("11") and v.ref.kind == RefKind.ASSUMPTION and "HV-2" in (v.ref.note or "")
    assert defaults.voltage_kv(None).value == Decimal("11")
    assert defaults.weekly_off("two_shift").value == [6]
    assert defaults.weekly_off("continuous_process").value == []
    assert defaults.connection_kind(Decimal("132")) == "eht"
    assert defaults.connection_kind(Decimal("0.415")) == "lt"
    assert defaults.discom_from_profile("PVVNL") == "pvvnl" and defaults.discom_from_profile("zz") is None
    assert defaults.power_factor().ref.kind == RefKind.ASSUMPTION


# billx's DISCOM profiles (billx/profiles/, Nov 2026) and the state each must land in
BILLX_PROFILES = {
    "apcpdcl": "AP", "avvnl": "RJ", "dvvnl": "UP", "gescom": "KA", "msedcl": "MH", "mvvnl": "UP",
    "npcl": "UP", "pspcl": "PB", "pvvnl": "UP", "tangedco": "TN", "tgspdcl": "TS",
}  # fmt: skip


def test_every_billx_profile_maps_to_a_discom_and_state() -> None:
    for profile, state in BILLX_PROFILES.items():
        discom = defaults.discom_from_profile(profile)
        assert discom and defaults.state_of(discom) == state, profile


@pytest.mark.parametrize(
    ("state", "printed", "code"),
    [
        ("UP", "HV-2(L & H POWER)", "HV-2"),
        ("UP", "HV 2", "HV-2"),
        ("MH", "105 HT-I A", "HT I(A)"),
        ("MH", "HT I (A) Industry", "HT I(A)"),
        ("MH", "HT-II", "HT II"),
        ("UP", "LMV-6", "LMV-6"),
        ("UP", "XYZ", None),
    ],
)
def test_category_mapping(state: str, printed: str, code: str | None) -> None:
    got = defaults.category_from_printed(state, printed)
    assert (got.value if got else None) == code


@pytest.mark.parametrize(
    ("slot", "window"),
    [
        ("19:00-03:00", ("19:00", "03:00")),
        ("05.00 to 10.00 hrs", ("05:00", "10:00")),
        ("Tod-1", None),
        (None, None),
    ],
)
def test_window_of(slot: str | None, window: tuple[str, str] | None) -> None:
    assert site_intake.window_of(slot) == window


def test_from_bills_then_build(billx: FakeBillx) -> None:
    rows = [
        _ingest(billx, f"m{m}", with_month(result("pvvnl_passed"), f"2026-{m:02d}", str(1900000 + m * 1000)))[
            0
        ]
        for m in (7, 8, 9)
    ]
    with session_scope() as s:
        si = site_intake.from_bills(s, [b.id for b in rows], {"industry_shape": "continuous_process"})
    assert si.discom.value == "pvvnl" and si.discom.provenance == "assumption"
    assert si.state.value == "UP" and si.category.value == "HV-2" and si.category.provenance == "assumption"
    assert si.voltage_kv.provenance == "assumption"
    assert si.contract_demand_kva.provenance == "bill" and si.contract_demand_kva.value == Decimal("5000")
    assert si.industry_shape.provenance == "client_stated" and si.weekly_off.value == []
    assert [m.month for m in si.months] == [date(2026, 7, 1), date(2026, 8, 1), date(2026, 9, 1)]
    assert si.months[0].unmapped_tod_slots == ["Tod-1", "Tod-2", "Tod-3"] and si.months[0].tod == []
    assert si.months[2].energy.ref is not None and si.months[2].energy.ref.kind == RefKind.BILL
    with session_scope() as s:
        out = site_intake.build(s, si, "client@x")
        s.commit()
    assert any("do not name their hours" in w for w in out["warnings"])
    with session_scope() as s:
        conn = s.get(Connection, out["connection_id"])
        assert conn is not None and conn.discom_id == "pvvnl" and conn.connection_kind == "ht"
        assert conn.provenance["voltage_kv"]["provenance"] == "assumption"
        lp = s.get(LoadProfile, out["load_profile_id"])
        assert lp is not None and lp.provenance == "synthesised" and lp.version == 1
        kw = decode(lp.series_kw)
        assert len(kw) == 365 * 96 == lp.blocks
        assert all(s.get(Bill, b.id).connection_id == conn.id for b in rows)  # type: ignore[union-attr]
        rep = {r["month"]: r for r in lp.method["months"]}
        # the peak in kW is the recorded 3,250 kVA at the bill's kWh/kVAh (1,909,000 / 2,059,020),
        # not 3,250 kW (Stage 17: the bill prints no power factor, but it prints both registers)
        assert rep["2026-09-01"]["kwh"] == 1909000.0 and rep["2026-09-01"]["max_kw_target"] == 3013.205
    with session_scope() as s:
        again = site_intake.build(s, si, "client@x", connection_id=out["connection_id"])
        s.commit()
    assert again["load_profile_version"] == 2 and again["connection_id"] == out["connection_id"]
    with session_scope() as s, pytest.raises(LookupError):
        site_intake.build(s, si, "client@x", connection_id="missing")


def test_power_factor_from_the_bills_registers_when_none_is_printed(billx: FakeBillx) -> None:
    """Stage 17: a bill printing kWh and kVAh but no power factor gave the month no power factor, so
    the profile took the recorded kVA as kW and today's demand charge (kVA = kW × kVAh/kWh) came out
    above the bill's."""
    [b] = _ingest(billx, "aug", result("pvvnl_passed"))  # 1,972,620 kWh and 2,059,020 kVAh, no PF
    with session_scope() as s:
        [m] = site_intake.from_bills(s, [b.id]).months
    assert m.power_factor is not None and m.power_factor.value == Decimal("0.958038")
    assert m.power_factor.provenance == "bill" and m.power_factor.ref is not None
    assert m.power_factor.ref.field_path == "energy.kvah"
    printed = copy.deepcopy(result("pvvnl_passed"))
    printed["bills"][0]["fields"]["energy"]["power_factor"] = copy.deepcopy(
        printed["bills"][0]["fields"]["energy"]["kvah"]
    ) | {"value": "0.97", "printed": "0.97"}
    printed["file"]["sha256"] = "another-file"
    [b2] = _ingest(BillxWithPf := FakeBillx(), "aug-pf", printed)
    assert BillxWithPf.calls == 1
    with session_scope() as s:
        [m2] = site_intake.from_bills(s, [b2.id]).months
    assert m2.power_factor is not None and m2.power_factor.value == Decimal("0.97")  # as printed


def test_from_bills_refuses_unaccepted(billx: FakeBillx) -> None:
    [b] = _ingest(billx, "r", result("pvvnl_needs_review"))
    with session_scope() as s, pytest.raises(ValueError, match="not accepted"):
        site_intake.from_bills(s, [b.id])
    with session_scope() as s, pytest.raises(bill_store.BillNotFound):
        site_intake.from_bills(s, ["missing"])


def test_typed_tod_hours_shape_the_profile() -> None:
    si = site_intake.SiteIntake.model_validate(_typed())
    with session_scope() as s:
        out = site_intake.build(s, si, "client@x")
        s.commit()
        lp = s.get(LoadProfile, out["load_profile_id"])
        assert lp is not None
        may = lp.method["months"][1]
        assert may["tod"][0]["kwh"] == pytest.approx(200000, rel=1e-3)
        assert may["max_kw"] == pytest.approx(2500 * 0.98)


def test_shift_days_and_times_validated() -> None:
    assert site_intake.Shift(start="06:00", end="14:00", days=[0, 6]).days == [0, 6]
    with pytest.raises(ValidationError, match="0 \\(Mon\\)"):
        site_intake.Shift(start="06:00", end="14:00", days=[7])
    with pytest.raises(ValidationError, match="HH:MM"):
        site_intake.Shift(start="6am", end="14:00")


@pytest.mark.parametrize(
    ("start", "end", "month"),
    [
        (date(2026, 6, 1), date(2026, 6, 30), date(2026, 6, 1)),
        (date(2026, 1, 16), date(2026, 2, 15), date(2026, 1, 1)),  # 16 days of January, 15 of February
        (date(2026, 5, 26), date(2026, 6, 25), date(2026, 6, 1)),
        (date(2026, 4, 16), date(2026, 5, 15), date(2026, 5, 1)),  # 15 days each: the closing month
        (None, date(2026, 7, 1), date(2026, 6, 1)),  # the thirty days up to the reading
        (date(2026, 3, 1), None, date(2026, 3, 1)),
        (None, None, None),
        (date(2026, 6, 30), date(2026, 6, 1), None),  # ends before it starts
        (date(2025, 6, 1), date(2026, 6, 30), None),  # a misread year, not a period
    ],
)
def test_supply_month(start: date | None, end: date | None, month: date | None) -> None:
    assert site_intake.supply_month(start, end) == month


def test_a_bill_is_filed_under_its_supply_month_not_its_bill_month(billx: FakeBillx) -> None:
    # the fixture is supplied 1–31 August 2026 and billed on 3 September (bill month 09/2026)
    [b] = _ingest(billx, "aug", result("pvvnl_passed"))
    assert b.bill_month == date(2026, 9, 1)
    with session_scope() as s:
        si = site_intake.from_bills(s, [b.id])
    [m] = si.months
    assert m.month == date(2026, 8, 1) and m.month_basis == "supply_period"
    assert not any("no supply period" in w for w in si.warnings())


def _period(res: dict[str, Any], change: str) -> dict[str, Any]:
    r = copy.deepcopy(res)
    for end in ("from", "to"):
        node = r["bills"][0]["fields"]["period"][end]
        if change == "absent":
            node["value"] = None
        else:  # read, but not verified by billx and not confirmed by anyone
            node["verification"] = "unverified"
    return r


@pytest.mark.parametrize("change", ["absent", "unverified"])
def test_a_bill_without_a_usable_supply_period_takes_its_bill_month_and_says_so(
    billx: FakeBillx, change: str
) -> None:
    [b] = _ingest(billx, f"np-{change}", _period(result("pvvnl_passed"), change))
    with session_scope() as s:
        si = site_intake.from_bills(s, [b.id])
        out = site_intake.build(s, si, "client@x")
        s.commit()
    [m] = si.months
    assert m.month == date(2026, 9, 1) and m.month_basis == "bill_month"
    assert any(w.startswith("Sep 2026: the bill states no supply period") for w in out["warnings"])


def test_a_confirmed_supply_period_moves_the_month(billx: FakeBillx) -> None:
    [b] = _ingest(billx, "conf", _period(result("pvvnl_passed"), "unverified"))
    with session_scope() as s:
        bill_store.confirm(s, b.id, "period.from", "2026-07-01", "client@x")
        bill_store.confirm(s, b.id, "period.to", "2026-07-31", "client@x")
        s.commit()
    with session_scope() as s:
        [m] = site_intake.from_bills(s, [b.id]).months
    assert m.month == date(2026, 7, 1) and m.month_basis == "supply_period"


def test_twelve_bills_billed_the_month_after_cover_their_supply_year(billx: FakeBillx) -> None:
    rows = []
    for i in range(12):  # supplied November 2025 to October 2026, each billed on the 3rd of the next month
        y, mo = 2025 + (10 + i) // 12, (10 + i) % 12 + 1
        res = with_month(result("pvvnl_passed"), f"{y}-{mo:02d}")
        ny, nmo = y + mo // 12, mo % 12 + 1
        res["bills"][0]["fields"]["period"]["bill_month"]["value"] = f"{ny}-{nmo:02d}"
        rows.append(_ingest(billx, f"y{i}", res)[0])
    with session_scope() as s:
        si = site_intake.from_bills(s, [b.id for b in rows])
        out = site_intake.build(s, si, "client@x")
        s.commit()
        lp = s.get(LoadProfile, out["load_profile_id"])
        assert lp is not None
        assert si.months[0].month == date(2025, 11, 1) and si.months[-1].month == date(2026, 10, 1)
        assert lp.year_start == date(2025, 11, 1) and lp.blocks == 365 * 96
        assert not any(r.get("extrapolated") for r in lp.method["months"])
        assert [r["from_bill_month"] for r in lp.method["months"]] == [
            r["month"] for r in lp.method["months"]
        ]


def test_tod_energy_above_the_recorded_demand_is_a_warning_on_the_build() -> None:
    # 911,350 kWh in the 270 hours of 10:00–19:00 is 3,375 kW on average: above 3,250 kVA at any power factor
    month = {
        "month": "2026-06-01",
        "energy": {"value": "2169882"},
        "max_kva": {"value": "3250"},
        "tod": [
            {"start": "05:00", "end": "10:00", "energy": {"value": "477374"}},
            {"start": "10:00", "end": "19:00", "energy": {"value": "911350"}},
            {"start": "19:00", "end": "03:00", "energy": {"value": "650965"}},
            {"start": "03:00", "end": "05:00", "energy": {"value": "130193"}},
        ],
    }
    si = site_intake.SiteIntake.model_validate(_typed(contract_demand_kva={"value": "5000"}, months=[month]))
    with session_scope() as s:
        out = site_intake.build(s, si, "client@x")
        s.commit()
    [w] = [w for w in out["warnings"] if "above the recorded demand" in w]
    assert w.startswith("Jun 2026: the energy billed for 10:00-19:00 averages more than")
    feasible = dict(month, max_kva={"value": "3600"})
    si = site_intake.SiteIntake.model_validate(
        _typed(contract_demand_kva={"value": "5000"}, months=[feasible])
    )
    with session_scope() as s:
        out = site_intake.build(s, si, "client@x")
        s.commit()
    assert not any("above the recorded demand" in w for w in out["warnings"])
