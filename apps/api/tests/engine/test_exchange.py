"""Exchange purchase, feasibility before price (Stage 18): the verdict from a rule row, the schedule
the rule requires, the deviation risk priced, and one month of real prices worked by hand for the
Rajasthan-shaped fixture and two fixtures where the exchange is workable.

The rules are fixtures (tests/engine/exchange_fixture.py): no state's open access regulations are
stored yet. The prices are the real ones, read here from the copied file
packages/prices/iex/tenderos2/iex_15min_prices.json (June 2026, every block)."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal as D

import pytest

from ergplan.core.units import Unit, convert
from ergplan.engine import exchange
from ergplan.engine import scope as escope
from ergplan.intake import iex_vintage
from tests.engine import exchange_fixture as fx

SERIES = json.loads((iex_vintage.FOLDER / "iex_15min_prices.json").read_text())
JUNE0 = (31 + 28 + 31 + 30 + 31) * 96  # the first block of 1 June in the file's calendar year
H = D("0.25")


def june(market: str) -> list[D]:
    """June's 2,880 clearing prices in Rs/MWh, as the file prints them."""
    return [D(str(x)) for x in SERIES["markets"][market]["blocksRsPerMwh"][JUNE0 : JUNE0 + 30 * 96]]


def kwh_price(rs_per_mwh: list[D]) -> list[D]:
    return [convert(p, Unit.INR_PER_MWH, Unit.INR_PER_KWH) for p in rs_per_mwh]


# ---------------------------------------------------------------- the verdict


def test_no_rule_is_not_yet_sourced_and_offers_nothing() -> None:
    v = exchange.verdict("Rajasthan", None)
    assert v.verdict == exchange.NOT_YET_SOURCED and not v.offers and v.label == "Not yet sourced"
    assert "no approved rule" in v.words


def test_a_rule_that_does_not_state_permission_or_schedule_is_not_yet_sourced() -> None:
    b = fx.body(cross_subsidy_surcharge={"payable": "yes"})  # as the committed drafts are
    v = exchange.verdict("Bihar", fx.view(b, "BR"))
    assert v.verdict == exchange.NOT_YET_SOURCED and not v.offers and v.schedule is None
    assert [r.field for r in v.reasons] == ["permitted", "schedule"]
    assert "To obtain: the fixture's schedule" in v.reasons[1].text
    assert len(v.not_stated) == 12
    only_schedule = exchange.verdict("Bihar", fx.view(fx.body(permitted={"value": "yes"}), "BR"))
    assert only_schedule.verdict == exchange.NOT_YET_SOURCED
    assert [r.field for r in only_schedule.reasons] == ["schedule"]


def test_rajasthan_fixture_is_not_practical_and_says_why_with_the_citation() -> None:
    v = exchange.verdict("Rajasthan", fx.view(fx.UNIFORM_WITH_BARRIER), D(5000), D(33))
    assert v.verdict == exchange.NOT_PRACTICAL and not v.offers
    [reason] = v.reasons
    assert reason.field == "barriers" and "before the exchange's results are known" in reason.text
    assert reason.refs[0].document_id == fx.DOC and reason.refs[0].page == 40 and reason.refs[0].url
    assert reason.text in v.words
    # the rule is still read out in full: the schedule it requires and what would be applied for
    assert v.schedule is not None and v.schedule.uniform and v.schedule.uniform_hours == 24
    assert [c.field for c in v.apply_for] == ["sldc_clearance", "contract_demand"]
    assert "valid for 30 days" in v.apply_for[0].text


def test_a_workable_state_is_feasible_with_conditions_each_with_its_source() -> None:
    v = exchange.verdict("Maharashtra", fx.view(fx.BY_BLOCK, "MH"), D(5000), D(33))
    assert v.verdict == exchange.WITH_CONDITIONS and v.offers
    assert v.schedule is not None and not v.schedule.uniform
    by = {c.field: c for c in v.conditions}
    assert "cannot be banked" in by["banking"].text
    assert "Cross-subsidy surcharge is paid" in by["cross_subsidy_surcharge"].text
    assert "Additional surcharge is not paid" in by["additional_surcharge"].text
    assert "not paid back" in by["deviation_under"].text and "ordinary tariff" in by["deviation_over"].text
    assert all(c.refs and c.refs[0].document_id == fx.DOC for c in v.conditions + v.apply_for)
    assert "standing clearance" in v.apply_for[0].text.lower() and "90 days" in v.apply_for[0].text
    # what the stored regulation does not state is said, with what to obtain: never a guess
    assert {c.field for c in v.not_stated} == {"notice", "standby", "lock_in"}
    assert v.deviation is not None
    assert v.deviation.under_compensated_inr_per_kwh == 0 and v.deviation.over_multiple_of_grid_rate == 1


def test_a_consumer_below_the_rules_minimum_is_not_eligible() -> None:
    small = exchange.verdict("Maharashtra", fx.view(fx.BY_BLOCK, "MH"), D(750), D(33))
    assert small.verdict == exchange.NOT_PRACTICAL
    assert "750 kVA is below the 1000 kVA the rule requires" in small.reasons[0].text
    b = fx.body(
        permitted={"value": "yes"},
        schedule={"shape": "by_block"},
        eligibility={"min_voltage_kv": "33", "condition": "Supply at 33 kV and above."},
    )
    low = exchange.verdict("Maharashtra", fx.view(b, "MH"), D(5000), D(11))
    assert low.verdict == exchange.NOT_PRACTICAL and "11 kV is below the 33 kV" in low.reasons[0].text
    assert exchange.verdict("Maharashtra", fx.view(b, "MH"), D(5000), D(33)).offers


def test_a_rule_that_forbids_it_is_not_practical_and_a_rule_with_nothing_conditional_is_feasible() -> None:
    no = fx.body(
        permitted={"value": "no", "condition": "Only licensees may buy."}, schedule={"shape": "by_block"}
    )
    v = exchange.verdict("Punjab", fx.view(no, "PB"))
    assert v.verdict == exchange.NOT_PRACTICAL and "Only licensees may buy." in v.words
    every = {
        "permitted": {"value": "yes"},
        "eligibility": {"condition": "Any consumer."},
        "sldc_clearance": {"required": "no"},
        "schedule": {"shape": "by_block"},
        "contract_demand": {"treatment": "stays"},
    }
    partial = exchange.verdict("Punjab", fx.view(fx.body(**every), "PB"))
    assert partial.verdict == exchange.WITH_CONDITIONS  # fields not stated are conditions to confirm


def test_engine_scope_gives_the_states_verdict_from_the_row_that_applies() -> None:
    facts = escope.ConnectionFacts(
        discom="msedcl",
        state="MH",
        category="HT-I",
        voltage_kv=D(33),
        connection_kind="ht",
        contract_demand_kva=D(5000),
    )
    rows = [fx.view(fx.BY_BLOCK, "MH"), fx.view(fx.UNIFORM, "CERC")]
    v = escope.exchange_verdict(rows, facts, date(2026, 6, 1), ["msedcl", "MH", "CERC"], "dam")
    assert v.verdict == exchange.WITH_CONDITIONS and v.rule_id == "rule-MH" and v.state == "MH"
    assert escope.exchange_verdict([], facts, date(2026, 6, 1), ["msedcl", "MH"], "dam").verdict == (
        exchange.NOT_YET_SOURCED
    )
    # a row scoped to the green market does not answer for the day-ahead market
    green = [fx.view(fx.BY_BLOCK, "MH", {"source": ["iex"], "iex_segment": ["gdam"]})]
    assert not escope.exchange_verdict(green, facts, date(2026, 6, 1), ["MH"], "dam").offers
    assert escope.exchange_verdict(green, facts, date(2026, 6, 1), ["MH"], "gdam").offers


# ---------------------------------------------------------------- the schedule the rule requires


def test_a_non_uniform_schedule_is_refused_where_the_rule_requires_a_uniform_one() -> None:
    uniform = exchange.schedule_of(fx.view(fx.UNIFORM_WITH_BARRIER))
    by_block = exchange.schedule_of(fx.view(fx.BY_BLOCK, "MH"))
    assert uniform is not None and by_block is not None
    flat = [D(1000)] * 96 * 2
    day_shaped = [D(2000) if 36 <= t % 96 < 68 else D(0) for t in range(96 * 2)]
    exchange.check_schedule(uniform, flat, H)  # accepted
    exchange.check_schedule(uniform, [D(1000)] * 96 + [D(400)] * 96, H)  # another quantity the next day
    with pytest.raises(exchange.ScheduleRefused, match="uniform schedule over each day; day 1 varies"):
        exchange.check_schedule(uniform, day_shaped, H)
    with pytest.raises(exchange.ScheduleRefused, match=r"day 2 varies between 0\.00 kW and 1000\.00 kW"):
        exchange.check_schedule(uniform, flat[:96] + [D(0)] * 4 + flat[:92], H)
    exchange.check_schedule(by_block, day_shaped, H)  # the same schedule where the rule lets it vary
    # the month's cost is not worked for a schedule the rule refuses
    with pytest.raises(exchange.ScheduleRefused):
        exchange.month_cost(
            [D(5)] * 192,
            day_shaped,
            H,
            loss_pct=D(5),
            charges_inr_per_kwh=D("2.5"),
            schedule=uniform,
            terms=exchange.DeviationTerms(),
            deviation_pct=D(5),
            grid_inr_per_kwh=[D(7)] * 192,
        )
    # a rule that holds the schedule uniform for 8 hours at a time
    eight = exchange.Schedule(D(8))
    exchange.check_schedule(eight, [D(1)] * 32 + [D(2)] * 32 + [D(0)] * 32, H)
    with pytest.raises(exchange.ScheduleRefused, match="8-hour window 2"):
        exchange.check_schedule(eight, [D(1)] * 40 + [D(2)] * 24 + [D(0)] * 32, H)


# ---------------------------------------------------------------- the deviation risk


def test_the_deviation_risk_is_priced_from_what_the_rule_states() -> None:
    lost = exchange.DeviationTerms(D(0), D("1.25"))
    # 5% short: paid for at the landed cost of 8 and lost → 0.40; 5% over at 1.25 × 7 instead of 7 → 0.0875
    assert exchange.deviation_adder(lost, D(8), D(7), D(5)) == D("0.4875")
    paid_back = exchange.DeviationTerms(D(3), D(1))
    assert exchange.deviation_adder(paid_back, D(8), D(7), D(5)) == D("0.25")  # 5% × (8 − 3); over at tariff
    assert exchange.deviation_adder(paid_back, D(2), D(7), D(5)) == 0  # never a gain from deviating
    assert exchange.deviation_adder(exchange.DeviationTerms(), D(8), D(7), D(5)) == 0  # nothing stated
    assert not exchange.DeviationTerms().priced and lost.priced


# ---------------------------------------------------------------- one month of real prices, by hand

SUM_DAM_JUNE = D("14884463.7")  # Σ of the 2,880 DAM prices of June 2026, Rs/MWh (mean 5,168.2165625)
SUM_DAM_JUNE_DAY = D("1670076.8")  # Σ of DAM, 09:00–17:00 (blocks 37–68) of the 30 days: 960 prices
SUM_GDAM_JUNE_NIGHT = D("8515279.5")  # Σ of GDAM, 22:00–06:00 (blocks 89–96 and 1–24): 960 prices


def test_the_sums_worked_from_are_the_files() -> None:
    dam, gdam = june("DAM"), june("GDAM")
    assert len(dam) == 2880 and sum(dam, D(0)) == SUM_DAM_JUNE
    assert sum((dam[d * 96 + b] for d in range(30) for b in range(36, 68)), D(0)) == SUM_DAM_JUNE_DAY
    night = [*range(0, 24), *range(88, 96)]
    assert sum((gdam[d * 96 + b] for d in range(30) for b in night), D(0)) == SUM_GDAM_JUNE_NIGHT


def test_rajasthan_fixture_a_uniform_megawatt_for_june_worked_by_hand() -> None:
    """Rajasthan-shaped fixture, DAM, June 2026: 1,000 kW in every block (the rule's uniform schedule).
    Fixture figures: losses 5%, open-access charges ₹2.50/kWh delivered, drawal short of the schedule
    lost, drawal over it at 1.25 × the energy charge of ₹7.00, deviation 5% each way.

        scheduled   1,000 kW × 0.25 h × 2,880 blocks              =   720,000 kWh
        delivered   720,000 × 0.95                                =   684,000 kWh
        price       250 kWh × Σ price ÷ 1000 = 250 × 14,884.4637  = ₹3,721,115.925
        charges     684,000 × 2.50                                = ₹1,710,000
        deviation   5% × Σ delivered × (price ÷ 0.95 + 2.50)      = 5% × (3,721,115.925 + 1,710,000)
                  + 5% × (1.25 − 1) × 7.00 × 684,000              = 271,555.79625 + 59,850
                                                                  = ₹331,405.79625
        total                                                     = ₹5,762,521.72125
        landed      5,762,521.72125 ÷ 684,000                     = ₹8.4247 per kWh
    """
    rule = fx.view(fx.UNIFORM_WITH_BARRIER)
    schedule = exchange.schedule_of(rule)
    assert schedule is not None
    got = exchange.month_cost(
        kwh_price(june("DAM")),
        [D(1000)] * 2880,
        H,
        loss_pct=D(5),
        charges_inr_per_kwh=D("2.50"),
        schedule=schedule,
        terms=exchange.deviation_of(rule),
        deviation_pct=D(5),
        grid_inr_per_kwh=[D("7.00")] * 2880,
    )
    assert got.scheduled_kwh == D(720000) and got.delivered_kwh == D(684000)
    assert got.price_inr == D("3721115.925") and got.charges_inr == D(1710000)
    assert abs(got.deviation_inr - D("331405.79625")) < D("0.000001")
    assert abs(got.total_inr - D("5762521.72125")) < D("0.000001")
    assert got.landed_inr_per_kwh.quantize(D("0.0001")) == D("8.4247")
    # the verdict for the same rule: not practical, so the figure is an analyst's working only
    assert not exchange.verdict("Rajasthan", rule, D(5000), D(33)).offers


def test_a_workable_state_buying_the_day_hours_of_june_worked_by_hand() -> None:
    """Workable fixture (schedule by block), DAM, June 2026: 2,000 kW from 09:00 to 17:00 every day.
    Fixture figures: losses 3%, charges ₹1.80/kWh delivered, drawal short of the schedule lost, drawal
    over it at the ordinary tariff, deviation 5%.

        scheduled   2,000 kW × 0.25 h × 960 blocks                =   480,000 kWh
        delivered   480,000 × 0.97                                =   465,600 kWh
        price       500 kWh × 1,670,076.8 ÷ 1000                  = ₹835,038.40
        charges     465,600 × 1.80                                = ₹838,080
        deviation   5% × (835,038.40 + 838,080)                   = ₹83,655.92
        total                                                     = ₹1,756,774.32
        landed      1,756,774.32 ÷ 465,600                        = ₹3.7731 per kWh
    """
    rule = fx.view(fx.BY_BLOCK, "MH")
    schedule = exchange.schedule_of(rule)
    assert schedule is not None
    got = exchange.month_cost(
        kwh_price(june("DAM")),
        [D(2000) if 36 <= t % 96 < 68 else D(0) for t in range(2880)],
        H,
        loss_pct=D(3),
        charges_inr_per_kwh=D("1.80"),
        schedule=schedule,
        terms=exchange.deviation_of(rule),
        deviation_pct=D(5),
        grid_inr_per_kwh=[D("8.00")] * 2880,
    )
    assert got.scheduled_kwh == D(480000) and got.delivered_kwh == D(465600)
    assert got.price_inr == D("835038.4") and got.charges_inr == D(838080)
    assert abs(got.deviation_inr - D("83655.92")) < D("0.000001")
    assert abs(got.total_inr - D("1756774.32")) < D("0.000001")
    assert got.landed_inr_per_kwh.quantize(D("0.0001")) == D("3.7731")
    assert exchange.verdict("Maharashtra", rule, D(5000), D(33)).offers


def test_a_second_workable_state_buying_green_power_at_night_worked_by_hand() -> None:
    """Second workable fixture (by block; drawal over the schedule at 1.5 × the energy charge, the
    settlement of drawal short of it not stated), GDAM, June 2026: 1,500 kW from 22:00 to 06:00.
    Fixture figures: losses 4.5%, charges ₹0.90/kWh delivered, energy charge ₹6.50, deviation 5%.

        scheduled   1,500 kW × 0.25 h × 960 blocks                =   360,000 kWh
        delivered   360,000 × 0.955                               =   343,800 kWh
        price       375 kWh × 8,515,279.5 ÷ 1000                  = ₹3,193,229.8125
        charges     343,800 × 0.90                                = ₹309,420
        deviation   5% × (1.5 − 1) × 6.50 × 343,800               = ₹55,867.50
        total                                                     = ₹3,558,517.3125
        landed      3,558,517.3125 ÷ 343,800                      = ₹10.3505 per kWh
    """
    rule = fx.view(fx.BY_BLOCK_OVER_AT_MULTIPLE, "GJ")
    schedule = exchange.schedule_of(rule)
    assert schedule is not None
    got = exchange.month_cost(
        kwh_price(june("GDAM")),
        [D(1500) if (t % 96 < 24 or t % 96 >= 88) else D(0) for t in range(2880)],
        H,
        loss_pct=D("4.5"),
        charges_inr_per_kwh=D("0.90"),
        schedule=schedule,
        terms=exchange.deviation_of(rule),
        deviation_pct=D(5),
        grid_inr_per_kwh=[D("6.50")] * 2880,
    )
    assert got.scheduled_kwh == D(360000) and got.delivered_kwh == D(343800)
    assert got.price_inr == D("3193229.8125") and got.charges_inr == D(309420)
    assert abs(got.deviation_inr - D("55867.5")) < D("0.000001")
    assert abs(got.total_inr - D("3558517.3125")) < D("0.000001")
    assert got.landed_inr_per_kwh.quantize(D("0.0001")) == D("10.3505")
    v = exchange.verdict("Gujarat", rule, D(5000), D(33))
    assert v.offers and "deviation_under" in {c.field for c in v.not_stated}
