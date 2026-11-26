"""The landed-cost calculator (Stage 19): the /calculator screen's answer for a consumer described
by state, DISCOM, category, voltage and contract demand, with no client, site or bill. The same
function as the screen's endpoint (platform.routers.calculator.run), so the same figures."""

from __future__ import annotations

from ...platform.routers import calculator as calc_api
from ...report import calculator as calc
from ..registry import call, signed_in, tool


@tool(title="Landed-cost calculator", read_only=True)
async def landed_cost_calculator(consumer: calc.CalculatorIn) -> calc.CalculatorOut:
    """The landed cost at the meter of solar and wind by route — captive from an own plant, captive
    with 26% equity, third party — and of buying on IEX (day-ahead, green day-ahead or real-time),
    line by line (generation cost or tariff, transmission, wheeling, losses, cross-subsidy and
    additional surcharge, duty, exemptions; the banking charge beside it, on banked units only) per
    ToD slot, beside the grid tariff and the saving, from the state's approved rules. consumer:
    discom (required; e.g. pvvnl, msedcl), category (default the DISCOM's HT industrial), voltage_kv,
    contract_demand_kva, month (YYYY-MM-01), connection (intra_state or ists), per technology
    (solar, wind) the PPA tariff or the plant's capital cost and CUF, equity_share_pct, and
    iex_market (dam, gdam, rtm). A line the rules do not state is listed as not yet sourced; a route
    the rules do not allow says why. Nothing is stored."""
    who = signed_in()
    return await call(lambda: calc_api.run(consumer, who))
