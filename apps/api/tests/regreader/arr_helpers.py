"""The ARR Network as the tests see it: trimmed copies of real section pages and order metadata
(UPERC orders 64–67, fetched 25 Nov 2026) laid out like the client's disk cache, and a transport
that refuses every request, so no test reaches the network."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import httpx

from ergplan.core.ids import document_id
from ergplan.regreader import arr_import, arr_map
from ergplan.regreader.arr_network import ArrNetworkClient, Order, SectionTable
from ergplan.regreader.tariff_oder import Mapped

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "arr_network"
UP_ORDERS = [67, 66, 65, 64]  # FY 2026-27 state DISCOMs and NPCL, FY 2025-26 state DISCOMs and NPCL
# the FY 2025-26 orders are dated 22 November 2025 (the FY 2026-27 orders name them so: order 67,
# Table 10-14, 'T.O dated November 22, 2025'); the ARR Network gives no date for them
UP_EFFECTIVE = {65: date(2025, 11, 22), 64: date(2025, 11, 22), 66: date(2026, 7, 2)}
REQUESTS: list[str] = []


def _refuse(request: httpx.Request) -> httpx.Response:
    REQUESTS.append(str(request.url))
    return httpx.Response(503, text="no network in tests")


def offline_client(cache_dir: Path = FIXTURES) -> ArrNetworkClient:
    return ArrNetworkClient(
        cache_dir=cache_dir, min_interval_s=0, http=httpx.Client(transport=httpx.MockTransport(_refuse))
    )


def read(order_id: int) -> tuple[Order, dict[str, list[SectionTable]]]:
    c = offline_client()
    return c.order(order_id), {s: c.sections(order_id, s) for s in arr_map.MAPPED_SECTIONS}


def mapped(order_id: int, effective_from: date = date(2026, 7, 2)) -> Mapped:
    order, sections = read(order_id)
    return arr_map.map_order(arr_map.Source(order, document_id(order.sha256), effective_from), sections)


def import_up(dry_run: bool = False) -> list[arr_import.OrderOutcome]:
    """The four Uttar Pradesh orders as drafts in the test database (jurisdictions must be synced)."""
    return arr_import.run(offline_client(), "up", UP_ORDERS, dry_run=dry_run, effective=UP_EFFECTIVE)
