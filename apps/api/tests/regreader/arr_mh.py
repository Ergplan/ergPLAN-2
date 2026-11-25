"""Maharashtra's Mumbai licensees as the tests see them: trimmed copies of the real section pages of
MERC's multi-year tariff orders of 28 March 2025 (ARR Network orders 100 TPC-D, 104 AEML-D and
108 BEST, fetched 25 Nov 2026), each imported once per tariff year."""

from __future__ import annotations

from dataclasses import replace
from datetime import date

from ergplan.core.ids import document_id
from ergplan.regreader import arr_import, arr_map
from ergplan.regreader.tariff_oder import Mapped

from .arr_helpers import offline_client, read

MH_YEARS = ("FY2025-26", "FY2026-27")
MH_ORDERS = [f"{order_id}@{year}" for order_id in (100, 104, 108) for year in MH_YEARS]
# each order: 'The Tariffs determined in this Order shall be applicable from 1 April, 2025' (TPC-D
# p. 290, AEML-D p. 442, BEST p. 367); the second year's tables are titled 'w.e.f. 1 April, 2026'
MH_EFFECTIVE = {
    spec: date(2025, 4, 1) if spec.endswith("FY2025-26") else date(2026, 4, 1) for spec in MH_ORDERS
}
# the ARR Network names the utilities 'TPC-D' and 'AEML-D'; the jurisdictions file has tpcd and aeml
MH_JURISDICTIONS = {100: "tpcd", 104: "aeml"}


def mapped_year(order_id: int, year: str) -> Mapped:
    """One tariff year of a multi-year order, mapped from the fixtures."""
    order, sections = read(order_id)
    order = replace(order, arr_year=year)
    effective = date(int(year[2:6]), 4, 1)
    return arr_map.map_order(arr_map.Source(order, document_id(order.sha256), effective), sections)


def import_mh(dry_run: bool = False) -> list[arr_import.OrderOutcome]:
    """Both tariff years of the three orders as drafts in the test database."""
    return arr_import.run(
        offline_client(),
        "mh",
        MH_ORDERS,
        dry_run=dry_run,
        effective=MH_EFFECTIVE,
        jurisdictions=MH_JURISDICTIONS,
    )
