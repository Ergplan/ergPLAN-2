"""The year of IEX prices copied from Ergplan/tenderos2 (Stage 18): the copies match their recorded
origin, the 15-minute series agrees with the hourly file hour by hour, the real dates are rebuilt,
one vintage per market names repository, commit, file and SHA-256, the loader is idempotent, an
analyst's upload is kept and read for the months where it is newer, and Rs/MWh becomes INR/kWh only
through core.units."""

from __future__ import annotations

import hashlib
import json
import shutil
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import func, select

from ergplan.core.units import Unit, convert
from ergplan.db.base import session_scope
from ergplan.db.models import PricePoint, PriceVintage
from ergplan.intake import iex_loader, iex_vintage, price_series

ORIGIN = iex_vintage.origin()
SERIES, HOURLY = iex_vintage.read_files(ORIGIN)


def test_the_copies_are_the_files_their_origin_records() -> None:
    assert ORIGIN.repository == "Ergplan/tenderos2" and len(ORIGIN.commit) == 40
    assert hashlib.sha256(ORIGIN.series_file.read_bytes()).hexdigest() == ORIGIN.series_sha256
    assert hashlib.sha256(ORIGIN.check_file.read_bytes()).hexdigest() == ORIGIN.check_sha256
    assert ORIGIN.name() == f"Ergplan/tenderos2@{ORIGIN.commit[:12]}:web/public/market/iex_15min_prices.json"
    assert set(SERIES["markets"]) == set(HOURLY["markets"]) == {"GDAM", "DAM", "RTM"}


def test_a_copy_that_was_changed_is_refused(tmp_path: Path) -> None:
    for f in ("ORIGIN.yaml", "iex_15min_prices.json", "iex_hourly_prices.json"):
        shutil.copy(iex_vintage.FOLDER / f, tmp_path / f)
    doc = json.loads((tmp_path / "iex_15min_prices.json").read_text())
    doc["markets"]["DAM"]["blocksRsPerMwh"][0] += 1
    (tmp_path / "iex_15min_prices.json").write_text(json.dumps(doc))
    with pytest.raises(iex_loader.PriceFileError, match="SHA-256"):
        iex_vintage.read_files(iex_vintage.origin(tmp_path))


@pytest.mark.parametrize(
    ("key", "above_half"),
    [("GDAM", 17), ("DAM", 18), ("RTM", 14)],
)
def test_the_hourly_mean_of_the_15_minute_series_is_the_hourly_file(key: str, above_half: int) -> None:
    """Every one of the 8,760 hours of each market: the mean of its four blocks against the hourly
    file. The 15-minute file rounds to Rs 0.1 and the hourly file rounds the unrounded mean to
    Rs 1, so the two differ by at most 0.5 + 0.05; the largest difference found is Rs 0.525."""
    c = iex_vintage.cross_check(SERIES, HOURLY, key)
    assert c.hours == 8760 and c.agrees and not c.above_tolerance
    assert c.max_abs_diff == Decimal("0.525")
    assert c.above_half == above_half  # hours the rounding of the two files puts above Rs 0.5


def test_a_series_that_disagrees_with_the_hourly_file_is_refused() -> None:
    series = json.loads(json.dumps(SERIES))
    series["markets"]["DAM"]["blocksRsPerMwh"][4 * 100] += 40  # hour 100's mean moves by Rs 10
    c = iex_vintage.cross_check(series, HOURLY, "DAM")
    assert not c.agrees and [x.hour for x in c.above_tolerance] == [100]
    assert c.above_tolerance[0].day == date(2026, 1, 5) and c.above_tolerance[0].hour_of_day == 4


def test_the_real_date_of_each_day_is_rebuilt() -> None:
    days = iex_vintage.calendar_days(SERIES["markets"]["DAM"])
    assert len(days) == 365 and days[0] == date(2026, 1, 1)
    assert days[321] == date(2026, 11, 18) and days[322] == date(2025, 11, 19)  # the seam
    assert days[-1] == date(2025, 12, 31)
    bad = {**SERIES["markets"]["DAM"], "daysFromYear": {"2025": 40, "2026": 325}}
    with pytest.raises(iex_loader.PriceFileError, match="daysFromYear"):
        iex_vintage.calendar_days(bad)


def test_one_vintage_per_market_names_repository_commit_file_and_sha256() -> None:
    with session_scope(actor="t") as s:
        loaded = iex_vintage.load(s)
        assert [v.segment for v, _ in loaded] == ["gdam", "dam", "rtm"]
        for v, check in loaded:
            assert v.filename == ORIGIN.name() and v.file_sha256 == ORIGIN.series_sha256
            assert (v.rows, v.date_from, v.date_to) == (35040, date(2025, 11, 19), date(2026, 11, 18))
            assert ORIGIN.repository in v.source_note and ORIGIN.commit in v.source_note
            assert ORIGIN.series_sha256 in v.source_note and ORIGIN.check_sha256 in v.source_note
            assert "largest difference Rs 0.525/MWh" in v.source_note and check.agrees
            assert v.uploaded_by == iex_loader.COPIED_BY
        ids = [v.id for v, _ in loaded]
        # stored as the file prints them, Rs/MWh; the first DAM block of 1 Jan 2026
        first = s.get(PricePoint, (ids[1], date(2026, 1, 1), 1))
        assert first is not None
        assert first.mcp_inr_per_mwh == Decimal(str(SERIES["markets"]["DAM"]["blocksRsPerMwh"][0]))
        assert first.mcv_mw is None  # only GDAM carries its cleared volume
        g = s.get(PricePoint, (ids[0], date(2026, 1, 1), 1))
        assert g is not None
        assert g.mcv_mw == Decimal(str(SERIES["markets"]["GDAM"]["blocksMcvMw"][0]))
        assert s.scalar(select(func.count()).select_from(PricePoint)) == 3 * 35040
    with session_scope(actor="t") as s:  # idempotent
        assert [v.id for v, _ in iex_vintage.load(s)] == ids
        assert s.scalar(select(func.count()).select_from(PriceVintage)) == 3


def _month_csv(first: date, days: int, mcp: str) -> bytes:
    lines = ["Date,Time Block,MCP (₹/MWh)"]
    for i in range(days):
        d = first + timedelta(days=i)
        for b in range(96):
            lines.append(f"{d.isoformat()},{b // 4:02d}:{b % 4 * 15:02d} - {b // 4:02d}:59,{mcp}")
    return ("\n".join(lines) + "\n").encode()


def test_prices_are_read_in_inr_per_kwh_and_an_analysts_newer_month_is_kept() -> None:
    blocks = SERIES["markets"]["DAM"]["blocksRsPerMwh"]
    june0 = (31 + 28 + 31 + 30 + 31) * 96
    june_b1 = [Decimal(str(blocks[june0 + d * 96])) for d in range(30)]
    with session_scope(actor="t") as s:
        iex_vintage.load(s)
        got = price_series.month_block_means(s, "dam")
        assert got is not None and sorted(got.inr_per_kwh) == list(range(1, 13))
        assert len(got.vintage_ids) == 1 and got.vintage_of[6].filename == ORIGIN.name()
        # June, block 1: the mean of its 30 days, Rs/MWh → INR/kWh by core.units only
        want = convert(sum(june_b1, Decimal(0)) / 30, Unit.INR_PER_MWH, Unit.INR_PER_KWH)
        assert abs(got.inr_per_kwh[6][1] - want) < Decimal("0.0000000001")
        copied = got.vintage_ids[0]
        # an analyst uploads December 2026 (after the copied year ends) and an older June
        new = iex_loader.parse(_month_csv(date(2026, 12, 1), 31, "5000"), "DAM_dec26.csv")
        old = iex_loader.parse(_month_csv(date(2025, 6, 1), 30, "9999"), "DAM_jun25.csv")
        v_new = iex_loader.store(s, new, "DAM_dec26.csv", "analyst@joulewise.in")
        iex_loader.store(s, old, "DAM_jun25.csv", "analyst@joulewise.in")
        got2 = price_series.month_block_means(s, "dam")
        assert got2 is not None
        assert got2.vintage_of[12].id == v_new.id and got2.inr_per_kwh[12][40] == Decimal("5.000")
        assert got2.vintage_of[6].id == copied  # the copied June 2026 is newer than the upload's June 2025
        assert got2.inr_per_kwh[6] == got.inr_per_kwh[6]
        # an upload that overlaps the copied year is taken beside it, superseding nothing
        nov = iex_loader.parse(_month_csv(date(2026, 11, 1), 30, "4000"), "DAM_nov26.csv")
        v_nov = iex_loader.store(s, nov, "DAM_nov26.csv", "analyst@joulewise.in")
        kept = s.get(PriceVintage, copied)
        assert kept is not None and kept.superseded_by is None
        got3 = price_series.month_block_means(s, "dam")
        assert got3 is not None and got3.vintage_of[11].id == v_nov.id
        # a run's pin reads exactly the pinned vintages, whatever has been uploaded since
        pinned = price_series.month_block_means(s, "dam", [copied])
        assert pinned is not None and pinned.inr_per_kwh == got.inr_per_kwh
        assert price_series.pinned_ids(copied) == [copied] and price_series.pinned_ids(None) is None
        assert price_series.month_block_means(s, "gtam") is None
