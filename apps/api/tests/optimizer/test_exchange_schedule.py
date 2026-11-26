"""The schedule an exchange purchase must keep (Stage 18): where the rule requires a uniform
schedule the solver buys one flat block over each day, sized by what the whole day is worth, and the
judge agrees; block by block it buys only the blocks that pay."""

from __future__ import annotations

from decimal import Decimal

import pytest

from ergplan.engine import exchange
from ergplan.optimizer.model import Candidate, coarsen
from tests.optimizer.test_model import P, check

# two days of six 4-hour blocks; the grid costs 8 throughout
LOAD = [10.0, 10.0, 30.0, 30.0, 20.0, 10.0] * 2
PRICE = [3.0, 3.0, 5.0, 5.0, 12.0, 12.0, 3.0, 3.0, 9.0, 9.0, 12.0, 12.0]  # day 1, day 2


def iex(uniform_hours: float) -> Candidate:
    return Candidate(
        "iex",
        shape=[4.0] * 12,  # kWh per kW in a 4-hour block
        energy_inr_per_kwh=PRICE,
        max_kw=50,
        category="exchange",
        dispatchable=True,
        uniform_hours=uniform_hours,
    )


def problem(uniform_hours: float):  # type: ignore[no-untyped-def]
    p = P(LOAD, [iex(uniform_hours)], grid={"cd_kva": 0})
    return type(p)(**{**p.__dict__, "hours": 4.0})


def test_block_by_block_buys_only_the_blocks_that_pay() -> None:
    s = check(problem(0))
    assert [round(x, 4) for x in s.delivered["iex"]] == [10, 10, 30, 30, 0, 0, 10, 10, 0, 0, 0, 0]


def test_a_uniform_rule_buys_one_flat_block_over_each_day() -> None:
    """Day 1: the mean price is (3+3+5+5+12+12)/6 = 6.67 < 8, so a flat block pays up to the least
    load of the day (10 kWh a block = 2.5 kW; above it the surplus would not be set off).
    Day 2: the mean is (3+3+9+9+12+12)/6 = 8.0, no cheaper than the grid: nothing is bought beyond
    what costs the same. The flat block may not exceed any block's load (an exchange purchase is set
    off in its own block only)."""
    s = check(problem(24))
    day1, day2 = s.delivered["iex"][:6], s.delivered["iex"][6:]
    assert [round(x, 4) for x in day1] == [10.0] * 6
    assert max(day2) - min(day2) < 1e-6 and max(day2) <= 10.0 + 1e-6
    # the judge's check of the schedule against the rule: flat in each day, in kW
    kw = [Decimal(str(round(x / 4.0, 6))) for x in s.delivered["iex"]]
    exchange.check_schedule(exchange.Schedule(Decimal(24)), kw, Decimal(4))
    # and the block-by-block schedule of the same problem is refused under the uniform rule
    free = check(problem(0))
    with pytest.raises(exchange.ScheduleRefused, match="day 1 varies"):
        exchange.check_schedule(
            exchange.Schedule(Decimal(24)), [Decimal(str(x / 4.0)) for x in free.delivered["iex"]], Decimal(4)
        )


def test_a_uniform_rule_costs_more_than_buying_block_by_block() -> None:
    assert check(problem(24)).objective > check(problem(0)).objective


def test_the_uniform_window_survives_coarsening_and_shorter_windows() -> None:
    eight = check(problem(8))  # uniform over each 8 hours: pairs of blocks
    d = eight.delivered["iex"]
    assert all(abs(d[i] - d[i + 1]) < 1e-6 for i in range(0, 12, 2))
    assert [round(x, 4) for x in d[:4]] == [10, 10, 30, 30]
    c = coarsen(problem(24), 2)  # 8-hour blocks: the day is still one window of three blocks
    assert c.candidates[0].uniform_hours == 24 and c.hours == 8.0
    s = check(c)
    assert all(max(day) - min(day) < 1e-6 for day in (s.delivered["iex"][:3], s.delivered["iex"][3:]))
