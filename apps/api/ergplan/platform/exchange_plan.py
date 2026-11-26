"""Exchange purchase in a run and a plan (Stage 18): the state's verdict for a connection from the
rules the caller may read, what a result says about it in words with its sources, and when the
exchange lowered the cost (which hours, which months).

The verdict itself is `engine.scope.exchange_verdict` (pure). A client's run reads approved rules
only (`rulebook.repository`, or a replay's pinned versions); a draft is read here only for the
analyst's sandbox endpoints (`rulebook.sandbox`, invariant 2).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from ..core.settings import get_settings
from ..core.sourced import RefKind, SourcedValue, SourceRef, derive
from ..core.units import Unit
from ..db.models import Jurisdiction
from ..engine import exchange
from ..engine import scope as escope
from ..rulebook.models import RuleView
from ..rulebook.service import jurisdiction_chain

ZERO = Decimal(0)
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
PINS: dict[str, Any] = {
    "gate": 1
}  # how a run treats exchange sources (pinned, invariant 5); absent: before Stage 18

Reader = Callable[[Session, str, list[str], date], list[RuleView]]


def state_name(s: Session, code: str) -> str:
    j = s.get(Jurisdiction, code)
    return j.name if j is not None else code


def verdict_for(
    s: Session, facts: escope.ConnectionFacts, on: date, reader: Reader, segment: str | None
) -> exchange.Verdict:
    """The verdict for a connection from the rows `reader` gives (approved rows for a client)."""
    chain = jurisdiction_chain(s, facts.discom)
    rows = reader(s, exchange.FAMILY, chain, on)
    return escope.exchange_verdict(rows, facts, on, chain, segment, state_name(s, facts.state))


def deviation_pct(pins: dict[str, Any] | None = None) -> SourcedValue:
    """How far the consumer's drawal is taken to differ from its day-ahead schedule, each way: an
    assumption of ergPLAN's, shown as one (a replay uses the value its run pinned)."""
    pct = Decimal(str((pins or {}).get("deviation_pct") or get_settings().ergplan_exchange_deviation_pct))
    return SourcedValue(
        value=pct,
        unit=Unit.PERCENT,
        source_refs=(
            SourceRef(
                kind=RefKind.ASSUMPTION,
                note=f"drawal taken to fall short of the day-ahead schedule by {pct}% of it and to exceed it "
                f"by {pct}%; the regulations state no figure",
            ),
        ),
        label="Deviation from the schedule",
    )


def _cond(c: exchange.Condition) -> dict[str, Any]:
    return {"field": c.field, "text": c.text, "refs": [r.model_dump(mode="json") for r in c.refs]}


def verdict_json(v: exchange.Verdict, kind: str | None = None, label: str | None = None) -> dict[str, Any]:
    """The verdict as a result carries it: words, and every condition with its citations."""
    sched = None
    if v.schedule is not None:
        sched = (
            "one flat block over the day"
            if v.schedule.uniform and v.schedule.uniform_hours >= 24
            else f"flat over each {exchange._fmt(v.schedule.uniform_hours)} hours"
            if v.schedule.uniform
            else "block by block"
        )
    return {
        "kind": kind,
        "label": label,
        "state": v.state,
        "verdict": v.verdict,
        "verdict_label": v.label,
        "offered": v.offers,
        "words": v.words,
        "reasons": [_cond(c) for c in v.reasons],
        "conditions": [_cond(c) for c in v.conditions],
        "apply_for": [_cond(c) for c in v.apply_for],
        "not_stated": [_cond(c) for c in v.not_stated],
        "schedule": sched,
        "rule_id": v.rule_id,
        "version_id": v.version_id,
    }


def cited(conditions: Sequence[exchange.Condition]) -> str:
    """Where the reasons come from, in words, for a note (the result carries the links themselves)."""
    out = []
    for c in conditions:
        for r in c.refs:
            if r.document_id and r.page:
                out.append(f"page {r.page} of document {r.document_id}")
    return f" (source: {'; '.join(dict.fromkeys(out))})" if out else ""


def left_out_note(label: str, v: exchange.Verdict) -> str:
    return f"{label}: not offered. {v.words}{cited(v.reasons)}"


def when_bought(
    kwh: Sequence[Decimal],
    months: Sequence[int],
    hours: Sequence[int],
    landed: Sequence[Decimal],
    grid: Sequence[Decimal],
) -> dict[str, Any]:
    """When the exchange was bought and what it cost against the grid in those blocks: by calendar
    month and by hour of the day, and one sentence naming the months and hours that carry most of
    it. `kwh`, `landed` and `grid` are per block (energy at the meter, INR/kWh). The figures are
    Decimals; the caller gives each its sources."""
    by_month: dict[int, list[Decimal]] = {}
    by_hour = [ZERO] * 24
    total = ZERO
    for e, mo, h, c, g in zip(kwh, months, hours, landed, grid, strict=True):
        if e <= 0:
            continue
        row = by_month.setdefault(mo, [ZERO, ZERO, ZERO])
        row[0] += e
        row[1] += e * c
        row[2] += e * g
        by_hour[h] += e
        total += e
    out_months = [
        {
            "month": mo,
            "name": MONTHS[mo - 1],
            "kwh": row[0],
            "avg_landed": row[1] / row[0],
            "avg_grid": row[2] / row[0],
            "saved": row[2] - row[1],
        }
        for mo, row in sorted(by_month.items())
    ]
    if total <= 0:
        words = (
            "The exchange does not lower the cost in any hour: at the year's clearing prices and with the "
            "open-access charges, it is dearer than what it would replace in every block it may be scheduled."
        )
    else:
        top_months = [m for m, _ in sorted(by_month.items(), key=lambda kv: -kv[1][0])[:3]]
        top_hours = sorted(sorted(range(24), key=lambda h: -by_hour[h])[:8])
        share = sum((by_hour[h] for h in top_hours), ZERO) / total * 100
        words = (
            f"The exchange lowers the cost in {len(by_month)} month{'s' if len(by_month) != 1 else ''} "
            f"of the year, most in {', '.join(MONTHS[m - 1] for m in sorted(top_months))}; "
            f"{share.quantize(Decimal('1'))}% of what is bought falls in the hours starting "
            f"{', '.join(f'{h:02d}:00' for h in top_hours)}."
        )
    return {
        "words": words,
        "by_month": out_months,
        "by_hour_kwh": by_hour,
    }


def deviation_cost(
    kwh: Sequence[Decimal], adders: Sequence[Decimal] | None, inputs: Sequence[SourcedValue]
) -> SourcedValue | None:
    """The year's cost of the deviation risk the rule creates, inside the source's energy cost."""
    if adders is None:
        return None
    total = sum((e * a for e, a in zip(kwh, adders, strict=True)), ZERO)
    return derive(total.quantize(Decimal("0.01")), Unit.INR, inputs, label="Deviation risk, for the year")
