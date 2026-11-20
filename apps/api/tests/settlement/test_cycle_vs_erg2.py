"""The cycle settlement held to the original: erg_plan 2.0's own `runSettlement` and
`allocateBankedEnergy` (TypeScript, floats) produced these fixtures on its own series
(tools/dev/erg2-fixtures/harness.ts); the Decimal port must give the same energy everywhere."""

from __future__ import annotations

import gzip
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from ergplan.settlement import cycle

FIX = Path(__file__).parents[1] / "fixtures" / "erg2_settlement"
CAT = {"peak": "peak", "normal": "normal", "offPeak": "off_peak"}
CASES = sorted(p.name[:-8] for p in FIX.glob("*.json.gz") if p.name != "allocation.json.gz")


def _load(name: str) -> Any:
    return json.loads(gzip.decompress((FIX / f"{name}.json.gz").read_bytes()))


def d(x: float) -> Decimal:
    return Decimal(repr(float(x)))


def close(a: Decimal, b: float, what: str) -> None:
    tol = max(Decimal("0.001"), abs(d(b)) * Decimal("1e-9"))  # a watt-hour, or a part in 10⁹
    assert abs(a - d(b)) <= tol, f"{what}: port {a} original {b}"


def _config(cfg: dict[str, Any]) -> cycle.Config:
    lim = cfg["bankingLimit"]
    limit = None
    if lim and lim["enabled"]:
        limit = cycle.Limit(
            None if lim["drawalPct"] is None else d(lim["drawalPct"]) * 100,
            None if lim["generationPct"] is None else d(lim["generationPct"]) * 100,
            lim["combine"],
        )
    return cycle.Config(
        banking_allowed=cfg["bankingAllowed"],
        cycle=cfg["bankingCycle"].lower(),
        charge_pct=d(cfg["bankingChargePct"]) * 100,
        basis="tod" if cfg.get("basis") == "tod" else "block",
        matrix={
            CAT[f]: frozenset(CAT[t] for t, ok in row.items() if ok)
            for f, row in cfg["bankingMatrix"].items()
        },
        limit=limit,
        cap_pct_of_consumption=None
        if cfg.get("capPctOfConsumption") is None
        else d(cfg["capPctOfConsumption"]) * 100,
        surplus="sold" if cfg["unutilisedTreatment"] == "Sold" else "lapse",
    )


def test_fixtures_present() -> None:
    assert len(CASES) >= 6


@pytest.mark.parametrize("name", CASES)
def test_settlement_matches_the_original(name: str) -> None:
    fx = _load(name)
    b, want = fx["blocks"], fx["result"]
    cons = [d(x) for x in b["consumptionKwh"]]
    if b["rooftopGenerationKwh"] is not None:  # rooftop first, against the gross demand
        cap = d(fx["cfg"]["rooftop"]["capacityMw"]) * 250
        remaining, used = cycle.settle_rooftop(
            [d(x) for x in b["grossConsumptionKwh"]], [d(x) for x in b["rooftopGenerationKwh"]], cap
        )
        close(sum(used), want["rooftop"]["utilisedKwh"], "rooftop used")
        for i in range(0, len(cons), 997):
            close(remaining[i], b["consumptionKwh"][i], f"remaining demand, block {i}")
        cons = remaining
    res = cycle.settle(
        b["month"],
        [CAT[c] for c in b["cat"]],
        cons,
        [d(x) for x in b["generationKwh"]],
        [d(x) for x in b["generationInjectionKwh"]],
        _config(fx["cfg"]),
    )
    for mine, theirs in (
        (res.consumption, "consumptionKwh"),
        (res.generation_injection, "generationKwh"),
        (res.generation, "generationAtDrawalKwh"),
        (res.direct, "directKwh"),
        (res.banked_used, "bankedUsedKwh"),
        (res.banked_before_charge, "bankedBeforeChargeKwh"),
        (res.charge, "bankingChargeLossKwh"),
        (res.over_limit, "overLimitSurplusKwh"),
        (res.grid, "gridImportKwh"),
        (res.sold, "soldSurplusKwh"),
        (res.lapsed, "lapsedSurplusKwh"),
    ):
        close(mine, want[theirs], theirs)
    assert abs(res.re_fraction - d(want["reReplacementFraction"])) < Decimal("1e-9")
    assert abs(res.unutilised_fraction - d(want["unutilisedFraction"])) < Decimal("1e-9")
    assert res.balance_ok and want["energyBalanceOk"]
    # every pool, limit, drawal and settled cell
    pools = {(0 if p.group is None else p.group, p.cat): p for p in res.pools}
    assert len(pools) == len(want["slotFlows"])
    for f in want["slotFlows"]:
        p = pools[(f["month"], CAT[f["bucket"]])]
        for mine, key in (
            (p.consumption, "consumptionKwh"), (p.generation, "generationKwh"), (p.direct, "directKwh"),
            (p.surplus, "surplusPreBankKwh"), (p.shortfall, "shortfallPreBankKwh"),
            (p.banked_before_charge, "bankedBeforeChargeKwh"), (p.banked_after_charge, "bankedAfterChargeKwh"),
            (p.banked_used, "bankedUsedKwh"), (p.grid, "gridImportKwh"), (p.exported, "surplusExportedKwh"),
            (p.charge, "bankingChargeLossKwh"),
        ):  # fmt: skip
            close(mine, f[key], f"pool {f['month']}/{f['bucket']} {key}")
    lims = {(0 if x.group is None else x.group): x for x in res.limits}
    for f in want["bankingLimits"]:
        x = lims[f["group"]]
        assert (x.limit is None) == (f["limitKwh"] is None)
        if x.limit is not None:
            close(x.limit, f["limitKwh"], "limit")
        close(x.banked_before_charge, f["bankedBeforeChargeKwh"], "banked in the cycle")
    mine_d = {(0 if g is None else g, f, t): k for g, f, t, k in res.drawals}
    for f in want["drawals"]:
        close(mine_d.get((f["group"], CAT[f["from"]], CAT[f["to"]]), Decimal(0)), f["kwh"], f"drawal {f}")
    cells = {(c.month, c.cat): c for c in res.cells}
    for f in want["settledCells"]:
        c = cells[(f["month"], CAT[f["cat"]])]
        close(c.direct, f["directKwh"], "cell direct")
        close(c.banked_drawn, f["bankedDrawnKwh"], "cell banked")
        close(c.grid, f["gridImportKwh"], "cell grid")
    # block by block: the final shortfall (grid) the original spreads back on the blocks
    for i, v in enumerate(fx["finalShortfallKwh"]):
        if abs(res.grid_kwh[i] - d(v)) > Decimal("1e-6"):
            raise AssertionError(f"block {i}: port {res.grid_kwh[i]} original {v}")
    # and every block balances: demand = direct + banked drawn + grid
    for i in range(0, len(cons), 53):
        assert abs(cons[i] - res.direct_kwh[i] - res.withdrawn_kwh[i] - res.grid_kwh[i]) < Decimal("1e-9")


def test_allocation_matches_the_original() -> None:
    cases = json.loads(gzip.decompress((FIX / "allocation.json.gz").read_bytes()))
    assert len(cases) == 400
    for k, c in enumerate(cases):
        got = cycle.allocate(
            {CAT[x]: d(v) for x, v in c["bank"].items()},
            {CAT[x]: d(v) for x, v in c["need"].items()},
            {CAT[f]: frozenset(CAT[t] for t, ok in row.items() if ok) for f, row in c["matrix"].items()},
            d(c["cap"]),
            [CAT[x] for x in c["order"]],
        )
        # the amount each drawal category receives is unique; the split between banks may not be
        for to in ("peak", "normal", "offPeak"):
            mine = sum(got[f][CAT[to]] for f in cycle.CATS)
            theirs = sum(c["drawn"][f][to] for f in ("peak", "normal", "offPeak"))
            assert abs(mine - d(theirs)) < Decimal("1e-6"), f"case {k}, into {to}"
        for f in cycle.CATS:  # never more than a bank holds, never where the matrix forbids
            assert sum(got[f].values()) <= d(c["bank"][{v: k2 for k2, v in CAT.items()}[f]]) + Decimal("1e-9")
            for to in cycle.CATS:
                if got[f][to] > 0:
                    assert c["matrix"][{v: k2 for k2, v in CAT.items()}[f]][
                        {v: k2 for k2, v in CAT.items()}[to]
                    ]


def test_matrix_rules() -> None:
    assert cycle.matrix_for("same_tod")["peak"] == {"peak"}
    h = cycle.matrix_for("higher_to_lower")
    assert h["peak"] == {"peak", "normal", "off_peak"} and h["normal"] == {"normal", "off_peak"}
    assert h["off_peak"] == {"off_peak"}
    c = cycle.matrix_for("custom", {"normal": ["normal", "off_peak"]})
    assert c["peak"] == {"peak"} and c["normal"] == {"normal", "off_peak"}
