"""Cycle settlement (ergPLAN 2): the settlement and banking engine of erg_plan 2.0, in Decimal.

Ported from erg_plan 2.0 `src/lib/settlement-data.ts` (`runSettlement`, `allocateBankedEnergy`,
`bankingLimitKwh`), `settlement-settings.ts` (`bankingMatrixFor`) and `rooftop-settlement.ts`
(`settleRooftop`), which themselves follow jouleWise's reference Excel model. The port is held to the
original by `tests/settlement/test_cycle_vs_erg2.py` on fixtures the original produced.

The model, per banking cycle (a month, or the year):
1. real-time settlement — renewable energy at the drawal point against demand, per 15-minute block
   (`basis="block"`) or on each month's ToD-category totals (`basis="tod"`);
2. the surplus is banked, by the ToD category it was generated in, up to the cycle's banking limit
   (the higher / lower of a share of the grid drawal left after real-time settlement and a share of
   the generation at the injection point, or the drawal leg alone; and a share of consumption where
   a regulation sets one), less the banking charge in kind;
3. banked energy is drawn against the cycle's shortfall wherever the rule's matrix allows
   (`matrix[banked_in]` is the set of categories it may be drawn in), the drawal category with the
   highest grid rate first, each as far as possible without taking from a higher-rate category
   (exact for any matrix: a max flow per step);
4. what is left at the end of the cycle lapses (or is sold).

Pure: no database, no clock, no model.
"""

from __future__ import annotations

from collections.abc import Callable, Hashable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

ZERO = Decimal(0)
ONE = Decimal(1)
HUNDRED = Decimal(100)
EPS = Decimal("1e-9")
CATS: tuple[str, ...] = ("peak", "normal", "off_peak")  # value order: peak > normal > off-peak
_RANK = {c: i for i, c in enumerate(CATS)}

Matrix = Mapping[str, frozenset[str]]


def matrix_for(rule: str, custom: Mapping[str, Sequence[str]] | None = None) -> dict[str, frozenset[str]]:
    """`same_tod`: a category serves itself only; `higher_to_lower`: itself and every lower-value
    category; `custom`: the given map (a category it omits may be drawn only in itself)."""
    if rule == "same_tod":
        return {c: frozenset({c}) for c in CATS}
    if rule == "higher_to_lower":
        return {c: frozenset(t for t in CATS if _RANK[c] <= _RANK[t]) for c in CATS}
    if rule == "custom":
        custom = custom or {}
        return {c: frozenset(custom[c]) if c in custom else frozenset({c}) for c in CATS}
    raise ValueError(f"unknown banking rule {rule!r}")


@dataclass(frozen=True)
class Limit:
    """The banking limit of a cycle, on energy banked before the charge. Percentages; a leg that is
    None does not apply."""

    drawal_pct: Decimal | None = None  # of the grid drawal left after real-time settlement
    generation_pct: Decimal | None = None  # of generation at the injection point
    combine: str = "max"  # max | min | drawal


DEFAULT_LIMIT = Limit(Decimal(30), Decimal(25), "max")


@dataclass(frozen=True)
class Config:
    banking_allowed: bool = True
    cycle: str = "monthly"  # monthly | annual
    charge_pct: Decimal = ZERO  # in kind, percent of the energy banked
    basis: str = "block"  # block | tod
    matrix: Matrix = field(default_factory=lambda: matrix_for("higher_to_lower"))
    limit: Limit | None = None  # None: no limit
    cap_pct_of_consumption: Decimal | None = None
    surplus: str = "lapse"  # lapse | sold


@dataclass
class Pool:
    """One (banking cycle, ToD category) pool after settlement. kWh; generation at the drawal point."""

    group: Hashable
    cat: str
    consumption: Decimal
    generation: Decimal
    direct: Decimal
    surplus: Decimal
    shortfall: Decimal
    banked_before_charge: Decimal
    banked_after_charge: Decimal
    banked_used: Decimal  # drawn INTO this category
    grid: Decimal
    exported: Decimal  # over the limit, or banked and never drawn
    charge: Decimal


@dataclass
class CycleLimit:
    group: Hashable
    drawal_after_real_time: Decimal
    generation_injection: Decimal
    consumption: Decimal
    limit: Decimal | None
    surplus: Decimal
    banked_before_charge: Decimal


@dataclass
class Cell:
    """One (month, ToD category) after banking: what savings are priced on."""

    month: Hashable
    cat: str
    consumption: Decimal
    generation_injection: Decimal
    generation: Decimal
    direct: Decimal
    banked_drawn: Decimal
    grid: Decimal


@dataclass
class CycleResult:
    basis: str
    consumption: Decimal
    generation_injection: Decimal
    generation: Decimal  # at the drawal point
    direct: Decimal
    banked_used: Decimal
    banked_before_charge: Decimal
    charge: Decimal
    over_limit: Decimal
    grid: Decimal
    exported: Decimal
    sold: Decimal
    lapsed: Decimal
    re_fraction: Decimal
    unutilised_fraction: Decimal
    balance_ok: bool
    pools: list[Pool]
    limits: list[CycleLimit]
    drawals: list[tuple[Hashable, str, str, Decimal]]  # (group, banked in, drawn in, kWh after the charge)
    cells: list[Cell]
    # per block
    direct_kwh: list[Decimal]
    withdrawn_kwh: list[Decimal]
    grid_kwh: list[Decimal]
    surplus_kwh: list[Decimal]  # generation at the drawal point not set off in real time
    by_cat: dict[str, tuple[Decimal, Decimal]] = field(default_factory=dict)  # consumption, generation


# ---------------------------------------------------------------- banked-energy allocation


def _max_flow(cap: list[list[Decimal]], s: int, t: int) -> tuple[Decimal, list[list[Decimal]]]:
    """Edmonds-Karp on a small dense graph."""
    n = len(cap)
    flow = [[ZERO] * n for _ in range(n)]
    value = ZERO
    while True:
        prev = [-1] * n
        prev[s] = s
        queue = [s]
        while queue and prev[t] == -1:
            u = queue.pop(0)
            for v in range(n):
                if prev[v] == -1 and cap[u][v] - flow[u][v] > EPS:
                    prev[v] = u
                    queue.append(v)
        if prev[t] == -1:
            return value, flow
        aug: Decimal | None = None
        v = t
        while v != s:
            room = cap[prev[v]][v] - flow[prev[v]][v]
            aug = room if aug is None else min(aug, room)
            v = prev[v]
        assert aug is not None
        v = t
        while v != s:
            flow[prev[v]][v] += aug
            flow[v][prev[v]] -= aug
            v = prev[v]
        value += aug


def allocate(
    bank: Mapping[str, Decimal],
    need: Mapping[str, Decimal],
    matrix: Matrix,
    cycle_cap: Decimal | None,
    sink_order: Sequence[str] = CATS,
) -> dict[str, dict[str, Decimal]]:
    """Banked energy (after the charge) drawn against shortfall in one cycle: drawn[banked_in][drawn_in].
    Drawal categories are filled in `sink_order`, each as far as the matrix, each category's bank and
    the cycle's cap allow without reducing what an earlier category received."""
    # nodes: 0 source, 1 limit, 2-4 banked-in, 5-7 drawn-in, 8 sink
    s, lim, t = 0, 1, 8
    src = {c: 2 + i for i, c in enumerate(CATS)}
    snk = {c: 5 + i for i, c in enumerate(CATS)}
    big = sum((max(ZERO, bank[c]) + max(ZERO, need[c]) for c in CATS), ONE)

    def graph(sink_cap: Mapping[str, Decimal]) -> list[list[Decimal]]:
        cap = [[ZERO] * 9 for _ in range(9)]
        cap[s][lim] = big if cycle_cap is None else min(big, max(ZERO, cycle_cap))
        for f in CATS:
            cap[lim][src[f]] = max(ZERO, bank[f])
            for to in CATS:
                if to in matrix.get(f, frozenset()):
                    cap[src[f]][snk[to]] = big
        for to in CATS:
            cap[snk[to]][t] = max(ZERO, sink_cap[to])
        return cap

    served = dict.fromkeys(CATS, ZERO)
    open_ = dict.fromkeys(CATS, ZERO)
    prior = ZERO
    for to in sink_order:
        open_[to] = max(ZERO, need[to])
        value, _ = _max_flow(graph(open_), s, t)
        served[to] = max(ZERO, value - prior)
        open_[to] = served[to]
        prior += served[to]
    _, flow = _max_flow(graph(served), s, t)
    return {f: {to: max(ZERO, flow[src[f]][snk[to]]) for to in CATS} for f in CATS}


def limit_kwh(
    cfg: Config, drawal_after_real_time: Decimal, generation_injection: Decimal, consumption: Decimal
) -> Decimal | None:
    """The energy that may be banked in a cycle, before the charge; None: no limit."""
    cap: Decimal | None = None
    lim = cfg.limit
    if lim is not None:
        drawal = None if lim.drawal_pct is None else lim.drawal_pct / HUNDRED * drawal_after_real_time
        gen = None if lim.generation_pct is None else lim.generation_pct / HUNDRED * generation_injection
        legs = [x for x in (drawal, gen) if x is not None]
        if lim.combine == "drawal" and drawal is not None:
            cap = drawal
        elif legs:
            cap = min(legs) if lim.combine == "min" else max(legs)
    if cfg.cap_pct_of_consumption is not None:
        c = cfg.cap_pct_of_consumption / HUNDRED * consumption
        cap = c if cap is None else min(cap, c)
    return cap


# ---------------------------------------------------------------- rooftop (behind the meter)


def settle_rooftop(
    demand: Sequence[Decimal], rooftop: Sequence[Decimal], cap_kwh_per_block: Decimal | None = None
) -> tuple[list[Decimal], list[Decimal]]:
    """Rooftop generation against demand, block by block: used = min(generation, demand, cap); what
    is not used in its block is not banked. Returns (remaining demand, rooftop used)."""
    remaining: list[Decimal] = []
    used: list[Decimal] = []
    for d, g in zip(demand, rooftop, strict=True):
        u = min(max(ZERO, g), max(ZERO, d))
        if cap_kwh_per_block is not None:
            u = min(u, cap_kwh_per_block)
        remaining.append(d - u)
        used.append(u)
    return remaining, used


# ---------------------------------------------------------------- settlement


def settle(
    month: Sequence[Hashable],
    cat: Sequence[str],
    consumption: Sequence[Decimal],
    generation: Sequence[Decimal],
    generation_injection: Sequence[Decimal] | None,
    cfg: Config,
    sink_order: Callable[[Hashable], Sequence[str]] | None = None,
) -> CycleResult:
    """Settle a year of blocks. `month[i]` is any sortable key of the block's month, `cat[i]` its ToD
    category (one of CATS), `generation` is at the drawal point (after losses), `generation_injection`
    before them (None: the same). `sink_order(group)` gives the drawal order of a cycle (the month
    key, or None for an annual cycle); the default is peak, normal, off-peak."""
    n = len(month)
    inj = generation if generation_injection is None else generation_injection
    direct_b = [ZERO] * n
    surplus_b = [ZERO] * n
    short_b = [ZERO] * n
    for i in range(n):
        d = min(consumption[i], generation[i])
        direct_b[i] = d
        surplus_b[i] = generation[i] - d
        short_b[i] = consumption[i] - d

    # (month, category) cells
    cells: dict[tuple[Any, str], dict[str, Decimal]] = {}
    members: dict[tuple[Any, str], list[int]] = {}
    by_cat = {c: [ZERO, ZERO] for c in CATS}
    for i in range(n):
        key = (month[i], cat[i])
        cl = cells.get(key)
        if cl is None:
            cl = cells[key] = dict.fromkeys(("cons", "inj", "gen", "direct", "gi", "gd"), ZERO)
            members[key] = []
        members[key].append(i)
        cl["cons"] += consumption[i]
        cl["gen"] += generation[i]
        cl["inj"] += inj[i]
        cl["direct"] += direct_b[i]
        cl["gi"] += surplus_b[i]
        cl["gd"] += short_b[i]
        by_cat[cat[i]][0] += consumption[i]
        by_cat[cat[i]][1] += generation[i]
    if cfg.basis == "tod":
        for cl in cells.values():
            cl["direct"] = min(cl["cons"], cl["gen"])
            cl["gi"] = cl["gen"] - cl["direct"]
            cl["gd"] = cl["cons"] - cl["direct"]

    annual = cfg.cycle == "annual"
    pools: dict[tuple[Any, str], dict[str, Decimal]] = {}
    for (mo, ct), cell in cells.items():
        g = None if annual else mo
        p = pools.setdefault((g, ct), dict.fromkeys(("cons", "inj", "gen", "direct", "gi", "gd"), ZERO))
        for k in p:
            p[k] += cell[k]
    groups = sorted({g for g, _ in pools}, key=lambda x: (x is not None, x))

    keep = ONE - cfg.charge_pct / HUNDRED
    out_pools: list[Pool] = []
    limits: list[CycleLimit] = []
    drawals: list[tuple[Hashable, str, str, Decimal]] = []
    drawn_into_by_group: dict[Any, dict[str, Decimal]] = {}
    tot = dict.fromkeys(("direct", "used", "banked", "over", "charge", "grid", "exported"), ZERO)
    for g in groups:
        surplus_of = {c: max(ZERO, pools[(g, c)]["gi"]) if (g, c) in pools else ZERO for c in CATS}
        need_of = {c: max(ZERO, pools[(g, c)]["gd"]) if (g, c) in pools else ZERO for c in CATS}
        t_drawal = sum((pools[(g, c)]["gd"] for c in CATS if (g, c) in pools), ZERO)
        t_inj = sum((pools[(g, c)]["inj"] for c in CATS if (g, c) in pools), ZERO)
        t_cons = sum((pools[(g, c)]["cons"] for c in CATS if (g, c) in pools), ZERO)
        surplus_total = sum(surplus_of.values(), ZERO)
        lim = limit_kwh(cfg, t_drawal, t_inj, t_cons)
        banked_total = (
            (surplus_total if lim is None else min(surplus_total, lim)) if cfg.banking_allowed else ZERO
        )
        if cfg.banking_allowed and keep > 0:
            order = list(sink_order(g)) if sink_order is not None else list(CATS)
            drawn = allocate(
                {c: surplus_of[c] * keep for c in CATS}, need_of, cfg.matrix, banked_total * keep, order
            )
        else:
            drawn = {f: dict.fromkeys(CATS, ZERO) for f in CATS}
        used_from = {f: sum(drawn[f].values(), ZERO) for f in CATS}
        drawn_into = {to: sum((drawn[f][to] for f in CATS), ZERO) for to in CATS}
        for f in CATS:
            for to in CATS:
                if drawn[f][to] > 0:
                    drawals.append((g, f, to, drawn[f][to]))
        drawn_into_by_group[g] = drawn_into
        used_pre = {c: used_from[c] / keep if keep > 0 else ZERO for c in CATS}
        rest = max(ZERO, banked_total - sum(used_pre.values(), ZERO))
        remaining = sum((max(ZERO, surplus_of[c] - used_pre[c]) for c in CATS), ZERO)
        limits.append(CycleLimit(g, t_drawal, t_inj, t_cons, lim, surplus_total, banked_total))
        for c in CATS:
            if (g, c) not in pools:
                continue
            p = pools[(g, c)]
            share = rest * max(ZERO, surplus_of[c] - used_pre[c]) / remaining if remaining > EPS else ZERO
            banked = min(surplus_of[c], used_pre[c] + share)
            after = banked * keep
            loss = banked - after
            exported = surplus_of[c] - banked + max(ZERO, after - used_from[c])
            grid = need_of[c] - drawn_into[c]
            tot["direct"] += p["direct"]
            tot["used"] += drawn_into[c]
            tot["banked"] += banked
            tot["over"] += surplus_of[c] - banked
            tot["charge"] += loss
            tot["grid"] += grid
            tot["exported"] += exported
            out_pools.append(
                Pool(g, c, p["cons"], p["gen"], p["direct"], p["gi"], p["gd"], banked, after,
                     drawn_into[c], grid, exported, loss)
            )  # fmt: skip

    # settled cells: an annual cycle's drawal into a category is spread over the months pro rata to
    # that category's shortfall
    short_by_cat = dict.fromkeys(CATS, ZERO)
    for (_mo, ct), cell in cells.items():
        short_by_cat[ct] += max(ZERO, cell["gd"])
    out_cells: list[Cell] = []
    grid_b = [ZERO] * n
    wd_b = [ZERO] * n
    for mo, ct in sorted(cells, key=lambda k: (k[0], _RANK[k[1]])):
        cell = cells[(mo, ct)]
        into = drawn_into_by_group.get(None if annual else mo, {}).get(ct, ZERO)
        gd = max(ZERO, cell["gd"])
        drawn_c = into
        if annual:
            drawn_c = into * gd / short_by_cat[ct] if short_by_cat[ct] > 0 else ZERO
        imp = gd - drawn_c
        out_cells.append(Cell(mo, ct, cell["cons"], cell["inj"], cell["gen"], cell["direct"], drawn_c, imp))
        # back on the blocks, pro rata to each block's real-time shortfall (to demand if there is none)
        idx = members[(mo, ct)]
        w = sum((max(ZERO, short_b[i]) for i in idx), ZERO)
        dem = sum((max(ZERO, consumption[i]) for i in idx), ZERO)
        for i in idx:
            if w > 0:
                share_i = max(ZERO, short_b[i]) / w
            elif dem > 0:
                share_i = max(ZERO, consumption[i]) / dem
            else:
                share_i = ZERO
            grid_b[i] = imp * share_i
            wd_b[i] = drawn_c * share_i
            if cfg.basis == "tod":  # the slot's netting, back on its blocks
                direct_b[i] = consumption[i] - grid_b[i] - wd_b[i]
                surplus_b[i] = cell["gi"] * generation[i] / cell["gen"] if cell["gen"] > 0 else ZERO

    cons_total = sum((x["cons"] for x in cells.values()), ZERO)
    inj_total = sum((x["inj"] for x in cells.values()), ZERO)
    gen_total = sum(generation, ZERO)
    utilised = tot["direct"] + tot["used"]
    sold = tot["exported"] if cfg.surplus == "sold" else ZERO
    lhs = gen_total + tot["grid"]
    rhs = cons_total + tot["exported"] + tot["charge"]
    return CycleResult(
        basis=cfg.basis,
        consumption=cons_total,
        generation_injection=inj_total,
        generation=gen_total,
        direct=tot["direct"],
        banked_used=tot["used"],
        banked_before_charge=tot["banked"],
        charge=tot["charge"],
        over_limit=tot["over"],
        grid=tot["grid"],
        exported=tot["exported"],
        sold=sold,
        lapsed=tot["exported"] - sold,
        re_fraction=utilised / cons_total if cons_total > 0 else ZERO,
        unutilised_fraction=(gen_total - utilised) / inj_total if inj_total > 0 else ZERO,
        balance_ok=abs(lhs - rhs) <= max(ONE, rhs * Decimal("0.0001")),
        pools=out_pools,
        limits=limits,
        drawals=drawals,
        cells=out_cells,
        direct_kwh=direct_b,
        withdrawn_kwh=wd_b,
        grid_kwh=grid_b,
        surplus_kwh=surplus_b,
        by_cat={c: (v[0], v[1]) for c, v in by_cat.items()},
    )
