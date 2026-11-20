"""Settlement simulator (Stage 5). Pure and deterministic: the same blocks and rule give the same
statement to the watt-hour. The optimizer is re-settled through this (invariant 6).

Inputs are blocks — each with its month, ToD slot (and the slot classes a rule may use, e.g. UP's
`peak` / `off_peak` bound to the DISCOM's ToD rows), the load and each source's energy delivered at
the meter — the sources with their set-off category, and the approved settlement rule.

Netting by the rule's granularity:
- block_15min / hourly: net each block (or hour) in time order; surplus is banked (if the source may
  bank) after the charge in kind, or curtailed / sold; deficits draw on the bank where the block's
  slot is an allowed drawal slot for the bucket, oldest deposits first (FIFO);
- tod_slot_monthly: net within each ToD slot across the month; surplus of each slot is deposited,
  then each slot's deficit draws on the buckets its slot may use (same slot first when the rule says);
- monthly_aggregate / annual: net within the month / year.
At the end of each banking period, the bank carries over (capped as the rule states) or lapses;
at the end of the financial year everything left lapses. Lapsed energy is paid at the rule's price
or not at all.

Set-off follows the rule's `set_off_order` (captive generation, banked energy, bilateral, exchange,
DISCOM). Aggregated results are spread back to blocks in proportion to each block's load and supply,
so every block balances: load = Σ direct set-off + withdrawals + grid drawal.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from ..core.sourced import SourcedValue, derive
from ..core.units import Unit
from ..rulebook.models import RuleView
from . import cycle

ZERO = Decimal(0)
HUNDRED = Decimal(100)
CATEGORIES = ("captive_generation", "bilateral", "exchange")


@dataclass(frozen=True)
class Block:
    month: date  # first of the month
    slot: str  # the DISCOM's ToD slot name
    load_kwh: Decimal
    supply_kwh: dict[str, Decimal]  # source id → energy delivered at the meter in this block
    classes: frozenset[str] = frozenset()  # other names a rule may use for this slot (peak, off_peak)
    hour: int = 0  # hour index of the year, for hourly netting


@dataclass(frozen=True)
class SourceIn:
    id: str
    category: str = "captive_generation"  # captive_generation | bilateral | exchange
    may_bank: bool = True  # the settlement rule's scope covers this source
    surplus: str = "bank"  # bank | sell | curtail when not banked
    delivered_share: Decimal = Decimal(1)  # energy at the meter per unit injected (after losses)


@dataclass
class Lot:
    period: int
    kwh: Decimal


@dataclass
class Result:
    granularity: str
    grid_kwh: list[Decimal]
    setoff_kwh: dict[str, list[Decimal]]  # credited against each block's load
    withdrawn_kwh: list[Decimal]
    deposit_kwh: dict[str, list[Decimal]]  # net of the charge in kind
    charge_kwh: dict[str, list[Decimal]]
    unbanked_kwh: dict[str, list[Decimal]]  # surplus sold or curtailed
    setoff_at_source_kwh: dict[str, list[Decimal]] = field(
        default_factory=dict
    )  # the same set-off, by injection
    lapsed: list[dict[str, Any]] = field(default_factory=list)  # {month, bucket, kwh}
    balances: list[dict[str, Any]] = field(default_factory=list)  # {month, bucket, kwh} at period end
    notes: list[str] = field(default_factory=list)
    cycle: cycle.CycleResult | None = None  # the cycle settlement's own account, when the rule uses it

    def total(self, series: list[Decimal]) -> Decimal:
        return sum(series, ZERO)


class Bank:
    """Bank buckets with FIFO lots. Deposits are net of the charge in kind."""

    def __init__(self, rule: RuleView | None):
        b = (rule.body.get("banking") or {}) if rule else {}
        self.allowed = bool(b.get("allowed"))
        self.per_slot = b.get("buckets", "per_slot") == "per_slot"
        self.windows: list[dict[str, Any]] = list(b.get("drawal_windows") or [])
        self.charge = b.get("charge") or {}
        self.priority = b.get("withdrawal_priority", "same_slot_first")
        self.buckets: dict[str, list[Lot]] = defaultdict(list)

    def charge_pct(self) -> Decimal:
        if self.charge.get("kind") == "in_kind_percent" and self.charge.get("value") not in (None, ""):
            return Decimal(str(self.charge["value"]))
        return ZERO

    @staticmethod
    def _names(slot: str, classes: frozenset[str]) -> set[str]:
        return {slot, *classes}

    def bucket_for(self, slot: str, classes: frozenset[str], month: int) -> str | None:
        """The bucket a deposit in this slot goes to (None: this slot may not bank)."""
        if not self.allowed:
            return None
        names = self._names(slot, classes)
        if not self.windows:
            return slot if self.per_slot else "pool"
        for w in self.windows:
            if w.get("months") and month not in w["months"]:
                continue
            dep = w.get("deposited_in_slots")
            if dep is None:
                return slot if self.per_slot else "pool"
            hit = next((d for d in dep if d in names), None)
            if hit is not None:
                return hit if self.per_slot else "pool"
        return None

    def drawable(self, slot: str, classes: frozenset[str], month: int) -> list[str]:
        """Buckets this slot may draw on, in the rule's priority order."""
        names = self._names(slot, classes)
        if not self.windows:
            return list(self.buckets)
        out: list[str] = []
        for w in self.windows:
            if w.get("months") and month not in w["months"]:
                continue
            if not any(a in names for a in w.get("allowed_slots", [])):
                continue
            deps = w.get("deposited_in_slots") or list(self.buckets)
            for d in deps:
                key = d if self.per_slot else "pool"
                if key not in out:
                    out.append(key)
        if self.priority == "same_slot_first":
            out.sort(key=lambda k: 0 if k in names else 1)
        return out

    def deposit(self, bucket: str, kwh: Decimal, period: int) -> None:
        if kwh > 0:
            lots = self.buckets[bucket]
            if lots and lots[-1].period == period:
                lots[-1].kwh += kwh
            else:
                lots.append(Lot(period, kwh))

    def withdraw(self, buckets: list[str], need: Decimal) -> Decimal:
        got = ZERO
        for k in buckets:
            for lot in self.buckets.get(k, []):
                if need - got <= 0:
                    break
                take = min(lot.kwh, need - got)
                lot.kwh -= take
                got += take
            self.buckets[k] = [lot for lot in self.buckets.get(k, []) if lot.kwh > 0]
        return got

    def balance(self) -> dict[str, Decimal]:
        return {k: sum((lot.kwh for lot in v), ZERO) for k, v in self.buckets.items() if v}

    def keep_order(self, buckets: Sequence[str]) -> list[str]:
        """The order in which buckets keep their energy when a carry-over ceiling binds: the bucket
        drawable in the most slots first (its energy serves the most blocks), then by name. The
        regulations cap the carried total without saying which energy lapses; this order is the
        consumer's best and the optimizer applies the same (optimizer.model)."""

        def reach(k: str) -> int:
            names: set[str] = set()
            for w in self.windows:
                deps = w.get("deposited_in_slots")
                if deps is None or k in deps:
                    names.update(w.get("allowed_slots") or [])
            return len(names)

        return sorted(buckets, key=lambda k: (-reach(k), k))

    def lapse(self, keep: Decimal) -> dict[str, Decimal]:
        """Keep at most `keep` kWh: buckets in `keep_order`, each bucket's oldest lots first (FIFO);
        return what lapses per bucket."""
        lapsed: dict[str, Decimal] = {}
        order = {k: i for i, k in enumerate(self.keep_order(list(self.buckets)))}
        lots = sorted(
            ((order[k], lot.period, k, lot) for k, v in self.buckets.items() for lot in v),
            key=lambda x: (x[0], x[1]),
        )
        room = keep
        for _, _, k, lot in lots:
            stay = min(lot.kwh, max(room, ZERO))
            room -= stay
            gone = lot.kwh - stay
            if gone > 0:
                lapsed[k] = lapsed.get(k, ZERO) + gone
            lot.kwh = stay
        for k in list(self.buckets):
            self.buckets[k] = [lot for lot in self.buckets[k] if lot.kwh > 0]
        return lapsed


def _period_index(m: date, period: str) -> int:
    fy = m.year if m.month >= 4 else m.year - 1
    fm = (m.month - 4) % 12
    if period == "quarterly":
        return fy * 4 + fm // 3
    if period in ("financial_year", "calendar_year"):
        return fy
    return fy * 12 + fm


def simulate(
    blocks: Sequence[Block],
    sources: Sequence[SourceIn],
    rule: RuleView | None,
    set_off_order: Sequence[str] | None = None,
    rates: Sequence[Decimal] | None = None,
) -> Result:
    """`rates` (INR/kWh per block) matter only to a `cycle_pooled` rule: banked energy is drawn in the
    ToD category with the highest grid rate first."""
    body = rule.body if rule else {}
    if str(body.get("method", "chronological")) == "cycle_pooled":
        return _simulate_cycle(blocks, sources, body, set_off_order, rates)
    gran = str(body.get("granularity", "block_15min"))
    order = [
        c for c in (set_off_order or body.get("set_off_order") or []) if c in (*CATEGORIES, "banked_energy")
    ]
    if not order:
        order = ["captive_generation", "banked_energy", "bilateral", "exchange"]
    for c in (*CATEGORIES, "banked_energy"):
        if c not in order:
            order.append(c)
    banking = body.get("banking") or {}
    period_kind = str(banking.get("period", "monthly"))
    carry = bool(banking.get("carry_over"))
    cap = banking.get("cap") or {}
    bank = Bank(rule)
    c_pct = bank.charge_pct()
    n = len(blocks)
    ids = [s.id for s in sources]
    src = {s.id: s for s in sources}
    res = Result(
        gran,
        [ZERO] * n,
        {i: [ZERO] * n for i in ids},
        [ZERO] * n,
        {i: [ZERO] * n for i in ids},
        {i: [ZERO] * n for i in ids},
        {i: [ZERO] * n for i in ids},
        setoff_at_source_kwh={i: [ZERO] * n for i in ids},
    )

    # netting units: lists of block indices
    def unit_key(i: int) -> tuple[Any, ...]:
        b = blocks[i]
        if gran == "hourly":
            return (b.hour,)
        if gran == "tod_slot_monthly":
            return (b.month, b.slot)
        if gran == "monthly_aggregate":
            return (b.month,)
        if gran == "annual":
            return (_period_index(b.month, "financial_year"),)
        return (i,)

    units: dict[tuple[Any, ...], list[int]] = {}
    for i in range(n):
        units.setdefault(unit_key(i), []).append(i)

    # group units by month so period ends are applied in order; within a month keep time order
    by_month: dict[date, list[tuple[Any, ...]]] = {}
    for k, idx in units.items():
        by_month.setdefault(blocks[idx[0]].month, []).append(k)
    months = sorted(by_month)
    inj_in_period: Decimal = ZERO
    grid_in_period: Decimal = ZERO

    def spread(idx: list[int], total: Decimal, weights: list[Decimal]) -> list[Decimal]:
        s = sum(weights, ZERO)
        if total == 0:
            return [ZERO] * len(idx)
        if s == 0:
            return [total / len(idx)] * len(idx)
        return [total * w / s for w in weights]

    for mi, m in enumerate(months):
        keys = by_month[m]
        deficits: list[tuple[tuple[Any, ...], Decimal]] = []
        # pass 1: direct set-off and deposits (for block-level netting withdrawals happen in the same pass)
        for k in keys:
            idx = units[k]
            load = sum((blocks[i].load_kwh for i in idx), ZERO)
            supply = {sid: sum((blocks[i].supply_kwh.get(sid, ZERO) for i in idx), ZERO) for sid in ids}
            b0 = blocks[idx[0]]
            remaining = load
            for cat in order:
                if cat == "banked_energy":
                    if gran in ("block_15min", "hourly") and remaining > 0:
                        got = bank.withdraw(bank.drawable(b0.slot, b0.classes, m.month), remaining)
                        remaining -= got
                        for i, v in zip(
                            idx, spread(idx, got, [blocks[i].load_kwh for i in idx]), strict=True
                        ):
                            res.withdrawn_kwh[i] += v
                    continue
                for sid in ids:
                    if src[sid].category != cat or supply[sid] <= 0:
                        continue
                    use = min(supply[sid], remaining)
                    remaining -= use
                    for i, v in zip(
                        idx, spread(idx, use, [blocks[i].supply_kwh.get(sid, ZERO) for i in idx]), strict=True
                    ):
                        res.setoff_at_source_kwh[sid][i] += v
                    for i, v in zip(idx, spread(idx, use, [blocks[i].load_kwh for i in idx]), strict=True):
                        res.setoff_kwh[sid][i] += v
                    surplus = supply[sid] - use
                    if surplus <= 0:
                        continue
                    bucket = bank.bucket_for(b0.slot, b0.classes, m.month) if src[sid].may_bank else None
                    w = [blocks[i].supply_kwh.get(sid, ZERO) for i in idx]
                    if bucket is not None:
                        charge = surplus * c_pct / HUNDRED
                        net = surplus - charge
                        bank.deposit(bucket, net, mi)
                        inj_in_period += surplus
                        for i, v in zip(idx, spread(idx, net, w), strict=True):
                            res.deposit_kwh[sid][i] += v
                        for i, v in zip(idx, spread(idx, charge, w), strict=True):
                            res.charge_kwh[sid][i] += v
                    else:
                        for i, v in zip(idx, spread(idx, surplus, w), strict=True):
                            res.unbanked_kwh[sid][i] += v
            if remaining > 0:
                if gran in ("block_15min", "hourly"):
                    for i, v in zip(
                        idx, spread(idx, remaining, [blocks[i].load_kwh for i in idx]), strict=True
                    ):
                        res.grid_kwh[i] += v
                    grid_in_period += remaining
                else:
                    deficits.append((k, remaining))
        # pass 2 (aggregated netting): deficits draw on the bank, rest from the grid
        for k, need in deficits:
            idx = units[k]
            b0 = blocks[idx[0]]
            got = (
                bank.withdraw(bank.drawable(b0.slot, b0.classes, m.month), need)
                if "banked_energy" in order
                else ZERO
            )
            weights = [
                blocks[i].load_kwh for i in idx
            ]  # consumption-side quantities follow each block's load
            for i, v in zip(idx, spread(idx, got, weights), strict=True):
                res.withdrawn_kwh[i] += v
            for i, v in zip(idx, spread(idx, need - got, weights), strict=True):
                res.grid_kwh[i] += v
            grid_in_period += need - got
        # period end
        nxt = months[mi + 1] if mi + 1 < len(months) else None
        period_ends = nxt is None or _period_index(nxt, period_kind) != _period_index(m, period_kind)
        fy_ends = nxt is None or _period_index(nxt, "financial_year") != _period_index(m, "financial_year")
        if period_ends:
            if fy_ends or not carry:
                keep = ZERO
            else:
                ceilings = []
                if cap.get("pct_of_injection") not in (None, ""):
                    ceilings.append(inj_in_period * Decimal(str(cap["pct_of_injection"])) / HUNDRED)
                if cap.get("pct_of_consumption") not in (None, ""):
                    ceilings.append(grid_in_period * Decimal(str(cap["pct_of_consumption"])) / HUNDRED)
                if not ceilings:
                    keep = sum(bank.balance().values(), ZERO)
                elif cap.get("combine") == "lower":
                    keep = min(ceilings)
                else:
                    keep = max(ceilings)
            for bucket, kwh in bank.lapse(keep).items():
                res.lapsed.append({"month": m, "bucket": bucket, "kwh": kwh})
            for bucket, kwh in bank.balance().items():
                res.balances.append({"month": m, "bucket": bucket, "kwh": kwh})
            inj_in_period = ZERO
            grid_in_period = ZERO
    return res


# ---------------------------------------------------------------- cycle settlement (ergPLAN 2)


def _simulate_cycle(
    blocks: Sequence[Block],
    sources: Sequence[SourceIn],
    body: dict[str, Any],
    set_off_order: Sequence[str] | None,
    rates: Sequence[Decimal] | None,
) -> Result:
    """A `cycle_pooled` rule: `settlement.cycle.settle` (the erg_plan 2.0 engine) on the energy of the
    sources that may bank; a source that may not bank (an exchange purchase) is set off in its own
    block against what the banking sources leave, and its surplus is not banked."""
    cfg = cycle.config_from_rule(body)
    n = len(blocks)
    ids = [s.id for s in sources]
    order = [c for c in (set_off_order or body.get("set_off_order") or []) if c in CATEGORIES]
    order += [c for c in CATEGORIES if c not in order]
    ranked = sorted(sources, key=lambda s: order.index(s.category) if s.category in order else len(order))
    bank_src = [s for s in ranked if s.may_bank]
    other = [s for s in ranked if not s.may_bank]
    res = Result(
        "tod_slot_monthly" if cfg.basis == "tod" else "block_15min",
        [ZERO] * n,
        {i: [ZERO] * n for i in ids},
        [ZERO] * n,
        {i: [ZERO] * n for i in ids},
        {i: [ZERO] * n for i in ids},
        {i: [ZERO] * n for i in ids},
        setoff_at_source_kwh={i: [ZERO] * n for i in ids},
    )
    gen = [sum((b.supply_kwh.get(s.id, ZERO) for s in bank_src), ZERO) for b in blocks]
    inj = [sum((b.supply_kwh.get(s.id, ZERO) / s.delivered_share for s in bank_src), ZERO) for b in blocks]
    cats = [cycle.category_of(b.classes) for b in blocks]
    months = [b.month for b in blocks]
    # sources that may not bank: set off after the banking sources, in their own netting unit (the
    # block, or the month's ToD slot on a ToD basis); their surplus is not banked
    units: dict[Any, list[int]] = {}
    for i in range(n):
        units.setdefault((months[i], cats[i]) if cfg.basis == "tod" else i, []).append(i)
    cons = [b.load_kwh for b in blocks]
    for idx in units.values() if other else ():
        load_u = sum((blocks[i].load_kwh for i in idx), ZERO)
        left = load_u - min(load_u, sum((gen[i] for i in idx), ZERO))
        for s in other:
            have = sum((blocks[i].supply_kwh.get(s.id, ZERO) for i in idx), ZERO)
            if have <= 0:
                continue
            use = min(have, left)
            left -= use
            for i in idx:
                mine = blocks[i].supply_kwh.get(s.id, ZERO)
                share_load = blocks[i].load_kwh / load_u if load_u > 0 else ZERO
                res.setoff_kwh[s.id][i] = use * share_load
                res.setoff_at_source_kwh[s.id][i] = use * mine / have
                res.unbanked_kwh[s.id][i] = (have - use) * mine / have
                cons[i] -= use * share_load
    annual = cfg.cycle == "annual"

    def order_of(group: Any) -> list[str]:
        if rates is None:
            return list(cycle.CATS)
        idx = [i for i in range(n) if annual or months[i] == group]
        return cycle.sink_order([cats[i] for i in idx], [rates[i] for i in idx])

    out = cycle.settle(months, cats, cons, gen, inj, cfg, order_of)
    res.grid_kwh = out.grid_kwh
    res.withdrawn_kwh = out.withdrawn_kwh
    # per source: direct set-off and surplus pro rata to each banking source's supply in the block
    pool = {(p.group, p.cat): p for p in out.pools}
    # a ToD-basis slot nets its totals: its direct energy sits on the blocks with demand, so each
    # source's part of it follows the source's share of the slot's generation
    cell_gen: dict[tuple[Any, str], Decimal] = defaultdict(lambda: ZERO)
    cell_src: dict[tuple[Any, str, str], Decimal] = defaultdict(lambda: ZERO)
    cell_direct: dict[tuple[Any, str], Decimal] = defaultdict(lambda: ZERO)
    if cfg.basis == "tod":
        for i, b in enumerate(blocks):
            cell_gen[(months[i], cats[i])] += gen[i]
            cell_direct[(months[i], cats[i])] += out.direct_kwh[i]
            for s in bank_src:
                cell_src[(months[i], cats[i], s.id)] += b.supply_kwh.get(s.id, ZERO)
    for i, b in enumerate(blocks):
        g = gen[i]
        key = (months[i], cats[i])
        if cfg.basis == "tod" and cell_gen[key] > 0:
            for s in bank_src:
                res.setoff_kwh[s.id][i] = out.direct_kwh[i] * cell_src[(*key, s.id)] / cell_gen[key]
        if g <= 0:
            continue
        p = pool[(None if annual else months[i], cats[i])]
        surplus = out.surplus_kwh[i]
        banked = surplus * p.banked_before_charge / p.surplus if p.surplus > 0 else ZERO
        net = banked * p.banked_after_charge / p.banked_before_charge if p.banked_before_charge > 0 else ZERO
        for s in bank_src:
            have = b.supply_kwh.get(s.id, ZERO)
            share = have / g
            if share <= 0:
                continue
            if cfg.basis == "tod":
                res.setoff_at_source_kwh[s.id][i] = cell_direct[key] * have / cell_gen[key]
            else:
                res.setoff_kwh[s.id][i] = out.direct_kwh[i] * share
                res.setoff_at_source_kwh[s.id][i] = out.direct_kwh[i] * share
            res.deposit_kwh[s.id][i] = net * share
            res.charge_kwh[s.id][i] = (banked - net) * share
            res.unbanked_kwh[s.id][i] = (surplus - banked) * share
    last = max(months) if months else None
    for p in out.pools:
        if p.lapsed_banked > 0:
            res.lapsed.append({"month": last if annual else p.group, "bucket": p.cat, "kwh": p.lapsed_banked})
    res.notes.append(
        "cycle settlement: real-time set-off, then banked energy drawn within the "
        + ("year" if annual else "month")
        + ", the highest-rate ToD category first"
    )
    res.cycle = out
    return res


# ---------------------------------------------------------------- statement


def statement(
    res: Result,
    rule: RuleView | None,
    load_ref: SourcedValue,
    supply_refs: dict[str, SourcedValue],
    lapse_price: SourcedValue | None = None,
) -> list[SourcedValue]:
    """The year's settlement as cited lines: what each source set off, what was banked, charged,
    withdrawn and lapsed, and what came from the grid."""
    rule_refs: list[SourcedValue] = []
    if rule is not None:
        for p in ("/granularity", "/banking/charge/value", "/banking/period", "/banking/drawal_windows"):
            if rule.get(p) is not None:
                rule_refs.append(
                    SourcedValue(value=ZERO, unit=Unit.COUNT, source_refs=(rule.ref(p),), label=p)
                )
    base = [load_ref, *supply_refs.values(), *rule_refs]
    out: list[SourcedValue] = []

    def add(value: Decimal, label: str, extra: Sequence[SourcedValue] = ()) -> None:
        out.append(derive(value, Unit.KWH, [*base, *extra], label=label))

    add(
        res.total(res.grid_kwh)
        + sum((res.total(v) for v in res.setoff_kwh.values()), ZERO)
        + res.total(res.withdrawn_kwh),
        "Consumption",
    )
    for sid, series in res.setoff_kwh.items():
        add(res.total(series), f"Set off from {sid}", [supply_refs[sid]] if sid in supply_refs else [])
    add(sum((res.total(v) for v in res.deposit_kwh.values()), ZERO), "Banked (after the charge in kind)")
    add(sum((res.total(v) for v in res.charge_kwh.values()), ZERO), "Banking charge in kind")
    add(res.total(res.withdrawn_kwh), "Withdrawn from the bank")
    lapsed = sum((x["kwh"] for x in res.lapsed), ZERO)
    add(lapsed, "Lapsed")
    add(
        sum((res.total(v) for v in res.unbanked_kwh.values()), ZERO), "Surplus not banked (sold or curtailed)"
    )
    add(res.total(res.grid_kwh), "Drawn from the DISCOM")
    if lapse_price is not None and lapsed > 0:
        out.append(
            derive(lapsed * lapse_price.value, Unit.INR, [*base, lapse_price], label="Paid for lapsed energy")
        )
    return out
