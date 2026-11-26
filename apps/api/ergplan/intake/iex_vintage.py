"""The year of IEX prices copied from `Ergplan/tenderos2` (packages/prices/iex/tenderos2), loaded
into `price_series` as one vintage per market (Stage 18).

The repository is a read-only input (operating rule 7): its two price files were copied into this
repository with their origin recorded in `ORIGIN.yaml` (repository, commit, path, SHA-256), and
this module reads only those copies. Nothing is fetched at run time.

- `iex_15min_prices.json` is the series loaded: 35,040 fifteen-minute market-clearing prices per
  market (GDAM, DAM, RTM) in Rs/MWh, a calendar year of 365 days, each day taken by the builder
  from the latest year with data. The real date of every day is rebuilt here from the market's
  `from` and `to` dates and checked against the file's own `daysFromYear` count.
- `iex_hourly_prices.json` is the cross-check: the mean of each hour's four blocks must equal its
  hourly price. The builder rounds the blocks to Rs 0.1 and the hours to Rs 1 from unrounded
  blocks, so the two can differ by up to 0.5 + 0.05; `cross_check` reports every hour's
  difference and the loader refuses a file that differs by more.

Prices are stored as the files print them (Rs/MWh, `price_series.mcp_inr_per_mwh`); they become
INR/kWh only in `core.units.convert`, where they are read.

    python -m ergplan.intake.iex_vintage            # load the three markets (idempotent)
    python -m ergplan.intake.iex_vintage --check    # the cross-check only, nothing written
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

from ..core.settings import REPO_ROOT
from .iex_loader import COPIED_BY, ParsedPrices, PriceFileError, PriceRow, store

FOLDER = REPO_ROOT / "packages" / "prices" / "iex" / "tenderos2"
MARKETS = {"GDAM": "gdam", "DAM": "dam", "RTM": "rtm"}  # the file's market key → price_series.segment
MONTH_DAYS = (31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)
BLOCKS = 35040
# the most the mean of four prices rounded to 0.1 can differ from the hour rounded to 1
TOLERANCE = Decimal("0.55")


@dataclass(frozen=True)
class Origin:
    repository: str
    commit: str
    series_path: str
    series_file: Path
    series_sha256: str
    check_path: str
    check_file: Path
    check_sha256: str
    builder_path: str
    builder_sha256: str

    def name(self) -> str:
        """What the vintage is called: repository, commit and path of the file loaded."""
        return f"{self.repository}@{self.commit[:12]}:{self.series_path}"


@dataclass(frozen=True)
class HourDiff:
    hour: int  # 0..8759 of the file's calendar year
    day: date
    hour_of_day: int
    mean_of_blocks: Decimal
    hourly: Decimal

    @property
    def diff(self) -> Decimal:
        return self.mean_of_blocks - self.hourly


@dataclass(frozen=True)
class CrossCheck:
    market: str
    hours: int
    max_abs_diff: Decimal
    worst: HourDiff
    above_half: int  # hours whose difference is above Rs 0.5/MWh (the hourly rounding alone)
    above_tolerance: list[HourDiff]

    @property
    def agrees(self) -> bool:
        return self.hours == 8760 and not self.above_tolerance

    def words(self) -> str:
        w = self.worst
        return (
            f"cross-check against the hourly file: {self.hours} hours, largest difference "
            f"Rs {abs(w.diff)}/MWh ({w.day} hour {w.hour_of_day}: blocks' mean {w.mean_of_blocks}, "
            f"hourly {w.hourly}); {self.above_half} hours differ by more than Rs 0.5 and none by more "
            f"than Rs {TOLERANCE} (the two files' rounding)"
        )


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def origin(folder: Path | None = None) -> Origin:
    d = folder or FOLDER
    o = yaml.safe_load((d / "ORIGIN.yaml").read_text())
    if o.get("unit") != "Rs/MWh":
        raise PriceFileError("ORIGIN.yaml: the unit must be Rs/MWh")
    return Origin(
        repository=str(o["repository"]),
        commit=str(o["commit"]),
        series_path=str(o["series"]["path"]),
        series_file=d / str(o["series"]["file"]),
        series_sha256=str(o["series"]["sha256"]),
        check_path=str(o["cross_check"]["path"]),
        check_file=d / str(o["cross_check"]["file"]),
        check_sha256=str(o["cross_check"]["sha256"]),
        builder_path=str(o["builder"]["path"]),
        builder_sha256=str(o["builder"]["sha256"]),
    )


def read_files(o: Origin) -> tuple[dict[str, Any], dict[str, Any]]:
    """The two copies, each checked against the SHA-256 its origin records."""
    out: list[dict[str, Any]] = []
    for path, sha in ((o.series_file, o.series_sha256), (o.check_file, o.check_sha256)):
        data = path.read_bytes()
        if _sha(data) != sha:
            raise PriceFileError(f"{path.name}: its SHA-256 is not the one recorded in ORIGIN.yaml")
        doc = json.loads(data)
        if doc.get("unit") != "Rs/MWh":
            raise PriceFileError(f"{path.name}: the unit must be Rs/MWh, found {doc.get('unit')!r}")
        out.append(doc)
    return out[0], out[1]


def calendar_days(market: dict[str, Any]) -> list[date]:
    """The real date of each of the file's 365 days (1 January to 31 December): the latest year
    with data for that day between the market's `from` and `to`, as the builder chose it. Checked
    against the file's `daysFromYear`."""
    first, last = date.fromisoformat(market["from"]), date.fromisoformat(market["to"])
    days: list[date] = []
    for m, n in enumerate(MONTH_DAYS, start=1):
        for d in range(1, n + 1):
            for year in range(last.year, first.year - 1, -1):
                day = date(year, m, d)
                if first <= day <= last:
                    days.append(day)
                    break
            else:
                raise PriceFileError(f"no day {m:02d}-{d:02d} between {first} and {last}")
    counted: dict[str, int] = {}
    for day in days:
        counted[str(day.year)] = counted.get(str(day.year), 0) + 1
    if counted != {str(k): int(v) for k, v in market["daysFromYear"].items()}:
        raise PriceFileError(
            f"the days rebuilt per year {counted} are not the file's daysFromYear {market['daysFromYear']}"
        )
    return days


def _dec(v: Any) -> Decimal:
    return Decimal(str(v))


def parse_market(series: dict[str, Any], key: str, sha256: str) -> ParsedPrices:
    """One market of the 15-minute file as price rows with their real dates."""
    m = series["markets"][key]
    blocks = m["blocksRsPerMwh"]
    if len(blocks) != BLOCKS:
        raise PriceFileError(f"{key}: {len(blocks)} blocks, expected {BLOCKS}")
    mcv = m.get("blocksMcvMw")
    days = calendar_days(m)
    rows = [
        PriceRow(
            day,
            b + 1,
            _dec(blocks[i * 96 + b]),
            _dec(mcv[i * 96 + b]) if mcv is not None and mcv[i * 96 + b] is not None else None,
        )
        for i, day in enumerate(days)
        for b in range(96)
    ]
    rows.sort(key=lambda r: (r.day, r.block))
    return ParsedPrices(MARKETS[key], sha256, rows, len(rows), 0, 0)


def cross_check(series: dict[str, Any], hourly: dict[str, Any], key: str) -> CrossCheck:
    """Every hour's mean of four blocks against the hourly file's price for that hour."""
    blocks = [_dec(v) for v in series["markets"][key]["blocksRsPerMwh"]]
    hours = [_dec(v) for v in hourly["markets"][key]["hourlyRsPerMwh"]]
    days = calendar_days(series["markets"][key])
    diffs = [
        HourDiff(h, days[h // 24], h % 24, sum(blocks[h * 4 : h * 4 + 4], Decimal(0)) / 4, hours[h])
        for h in range(min(len(hours), len(blocks) // 4))
    ]
    worst = max(diffs, key=lambda x: abs(x.diff))
    return CrossCheck(
        market=key,
        hours=len(hours),
        max_abs_diff=abs(worst.diff),
        worst=worst,
        above_half=sum(1 for x in diffs if abs(x.diff) > Decimal("0.5")),
        above_tolerance=[x for x in diffs if abs(x.diff) > TOLERANCE],
    )


def source_note(o: Origin, market: dict[str, Any], check: CrossCheck) -> str:
    years = ", ".join(f"{y}: {n} days" for y, n in sorted(market["daysFromYear"].items()))
    filled = int(market.get("blocksFilled") or 0)
    return (
        f"{market['source']}. Copied from {o.repository} at commit {o.commit}, file {o.series_path} "
        f"(SHA-256 {o.series_sha256}), built by {o.builder_path} (SHA-256 {o.builder_sha256}) from IEX's "
        f"15-minute market files. A calendar year of 365 days, each day from the latest year with data "
        f"({years}); prices as printed in the file, rounded there to Rs 0.1/MWh; "
        f"{filled} block{'s' if filled != 1 else ''} filled by the builder from the same block of the "
        f"nearest day. Hourly file {o.check_path} (SHA-256 {o.check_sha256}): {check.words()}."
    )


def load(session: Any, folder: Path | None = None) -> list[tuple[Any, CrossCheck]]:
    """Store one vintage per market beside whatever an analyst has uploaded (nothing is superseded:
    where two live vintages hold the same calendar month, the newer data is read, see
    `intake.price_series`). A market already loaded from the same file is returned as it is.
    Refuses the file when its hourly cross-check fails."""
    o = origin(folder)
    series, hourly = read_files(o)
    out = []
    for key in MARKETS:
        check = cross_check(series, hourly, key)
        if not check.agrees:
            w = check.above_tolerance[0] if check.above_tolerance else check.worst
            raise PriceFileError(
                f"{key}: the 15-minute and hourly files disagree: {len(check.above_tolerance)} hours differ "
                f"by more than Rs {TOLERANCE}/MWh, first {w.day} hour {w.hour_of_day} "
                f"(blocks' mean {w.mean_of_blocks}, hourly {w.hourly})"
            )
        parsed = parse_market(series, key, o.series_sha256)
        v = store(
            session,
            parsed,
            o.name(),
            COPIED_BY,
            source_note=source_note(o, series["markets"][key], check),
            alongside=True,
        )
        out.append((v, check))
    return out


def main(argv: list[str] | None = None) -> int:
    import argparse

    from ..db.base import session_scope

    ap = argparse.ArgumentParser(prog="ergplan.intake.iex_vintage")
    ap.add_argument("--check", action="store_true", help="cross-check the two files; write nothing")
    a = ap.parse_args(argv)
    o = origin()
    series, hourly = read_files(o)
    ok = True
    for key in MARKETS:
        c = cross_check(series, hourly, key)
        ok = ok and c.agrees
        m = series["markets"][key]
        print(f"{key}: {m['from']} to {m['to']}; {c.words()}")
    if a.check or not ok:
        return 0 if ok else 1
    with session_scope(actor=COPIED_BY) as s:
        for v, _ in load(s):
            print(f"{v.segment}: vintage {v.id}, {v.rows} blocks, {v.date_from} to {v.date_to}, {v.filename}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
