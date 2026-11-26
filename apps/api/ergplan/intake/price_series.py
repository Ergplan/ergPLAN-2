"""Reading `price_series`: the mean clearing price of a market by calendar month and 15-minute
block of the day, from the live vintages (or exactly the vintages a run pinned).

Several vintages of one market can be live at once: the year copied from `Ergplan/tenderos2`
(`intake.iex_vintage`) and what an analyst has uploaded since. Each calendar month is read from
ONE vintage, the one whose data for that month is newest (its latest day in the month; a tie goes
to the later upload), so an analyst's upload is used for the months it is newer in and the copied
year for the rest. Prices leave here in INR/kWh, converted by `core.units.convert` only.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from ..core.units import Unit, convert


@dataclass(frozen=True)
class VintageUsed:
    id: str
    filename: str
    file_sha256: str
    date_from: date
    date_to: date


@dataclass(frozen=True)
class MonthPrices:
    segment: str
    inr_per_kwh: dict[int, dict[int, Decimal]]  # month (1-12) → block of the day (1-96) → mean price
    vintage_of: dict[int, VintageUsed]  # month → the vintage its prices were read from

    @property
    def vintage_ids(self) -> list[str]:
        return sorted({v.id for v in self.vintage_of.values()})


def pinned_ids(pin: str | list[str] | None) -> list[str] | None:
    """A run's pin for a market: one vintage id (runs before Stage 18) or a list."""
    if pin is None:
        return None
    return [pin] if isinstance(pin, str) else list(pin)


def month_block_means(session: Any, segment: str, pinned: list[str] | None = None) -> MonthPrices | None:
    """None when no vintage of the market is loaded (or none of the pinned ones exists)."""
    from sqlalchemy import func, select

    from ..db.models import PricePoint, PriceVintage

    q = select(PriceVintage).where(PriceVintage.segment == segment)
    q = q.where(PriceVintage.id.in_(pinned)) if pinned else q.where(PriceVintage.superseded_by.is_(None))
    vintages = {v.id: v for v in session.scalars(q)}
    if not vintages:
        return None
    month = func.extract("month", PricePoint.day)
    rows = session.execute(
        select(
            PricePoint.vintage_id,
            month,
            PricePoint.block,
            func.avg(PricePoint.mcp_inr_per_mwh),
            func.max(PricePoint.day),
        )
        .where(PricePoint.vintage_id.in_(list(vintages)))
        .group_by(PricePoint.vintage_id, month, PricePoint.block)
    ).all()
    newest: dict[tuple[str, int], date] = {}
    means: dict[tuple[str, int], dict[int, Decimal]] = {}
    for vid, mo, blk, mean, last in rows:
        key = (str(vid), int(mo))
        newest[key] = max(newest.get(key, last), last)
        means.setdefault(key, {})[int(blk)] = convert(Decimal(str(mean)), Unit.INR_PER_MWH, Unit.INR_PER_KWH)
    chosen: dict[int, str] = {}
    for (vid, mo), last in newest.items():
        cur = chosen.get(mo)
        if cur is None or (last, vintages[vid].uploaded_at, vid) > (
            newest[(cur, mo)],
            vintages[cur].uploaded_at,
            cur,
        ):
            chosen[mo] = vid
    return MonthPrices(
        segment=segment,
        inr_per_kwh={mo: means[(vid, mo)] for mo, vid in chosen.items()},
        vintage_of={
            mo: VintageUsed(
                vid,
                vintages[vid].filename,
                vintages[vid].file_sha256,
                vintages[vid].date_from,
                vintages[vid].date_to,
            )
            for mo, vid in chosen.items()
        },
    )
