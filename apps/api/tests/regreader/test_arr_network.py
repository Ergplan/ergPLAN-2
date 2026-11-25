"""The ARR Network client and its section-page parser, on trimmed copies of real pages."""

from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path

import httpx
import pytest

from ergplan.regreader import arr_network as an
from tests.fixtures.pdfgen import make_pdf

from .arr_helpers import FIXTURES, REQUESTS, offline_client


def test_order_metadata() -> None:
    o = offline_client().order(67)
    assert o.order_id == 67 and o.commission == "UPERC" and o.arr_year == "FY2026-27"
    assert o.url == "https://www.uperc.org/App_File/UPPCLTariffOrder-pdf72202630151PM.pdf"
    assert o.sha256 == "3a046bfa2e2fb0e76aa93dca2d1ecc602ef09a62ba72977fb8351b7a01ab8d95"
    assert o.pages == 501 and o.utilities == ("DVVNL", "KESCO", "MVVNL", "PUVVNL", "PVVNL")
    assert o.order_date is None  # the site gives none
    assert offline_client().order(66).utilities == ("NPCL",)


def test_a_table_under_a_category_with_its_page_and_reader() -> None:
    tables = offline_client().sections(67, "tariff_schedule")
    [hv2] = [t for t in tables if t.category == "HV-2"]
    assert (hv2.label, hv2.title, hv2.page, hv2.fiscal_year) == ("HV-2", "Urban Schedule", 445, None)
    assert hv2.columns[1] == "For supply up to 11 kV BASE RATE"
    assert hv2.rows[0].cells[:2] == ("Demand Charges", "Rs. 300.00 / kVA / month")
    assert hv2.rows[1].cells[2] == "Rs. 6.80 / kVAh" and hv2.rows[1].page == 445
    assert hv2.reader_version == "section-reader 3" and not hv2.as_text
    assert hv2.table_id.startswith("t") and hv2.order_id == 67 and hv2.section == "tariff_schedule"


def test_a_numbered_table_has_no_category_and_a_fiscal_year() -> None:
    tables = offline_client().sections(67, "wheeling")
    [t] = [t for t in tables if t.label == "Table 10-3"]
    assert t.category is None and t.fiscal_year == "FY2026-27" and t.page == 379
    assert t.title == "WHEELING CHARGE APPROVED BY THE COMMISSION FOR FY 2026-27"
    assert t.columns == ("Particulars", "Units", "Approved")
    assert t.rows[2].cells == ("Average Wheeling charge", "Rs. /kWh", "0.9318")


def test_rows_of_a_table_over_two_pages_carry_their_own_page() -> None:
    tables = offline_client().sections(67, "wheeling")
    [t] = [t for t in tables if t.label == "Table 10-5"]
    assert t.page == 380 and {r.page for r in t.rows} == {380, 381}
    assert all(not c.startswith("p. ") for r in t.rows for c in r.cells)  # the page cell is not a cell


def test_rate_lines_read_from_text_are_marked() -> None:
    tables = offline_client().sections(67, "tariff_schedule")
    assert [t.label for t in tables if t.as_text] == ["LMV-8"]


def test_a_section_without_tables_and_a_page_that_is_not_a_section() -> None:
    assert offline_client().sections(64, "re_portfolio") == []
    with pytest.raises(an.ArrNetworkError, match="not a section page"):
        an.parse_section("<html><body>Not found</body></html>", 1, "tod", date(2026, 11, 25))
    with pytest.raises(an.ArrNetworkError, match="unknown section"):
        offline_client().sections(67, "banking")


def test_pages_are_read_from_disk_and_fetched_once_when_absent(tmp_path: Path) -> None:
    before = len(REQUESTS)
    offline_client().sections(67, "tod")
    assert len(REQUESTS) == before  # cached: no request at all
    page = (FIXTURES / "sections" / "67_tod.html").read_text(encoding="utf-8")
    meta = (FIXTURES / "orders" / "67.json").read_text(encoding="utf-8")
    seen: list[str] = []

    def serve(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path + ("?" + request.url.query.decode() if request.url.query else ""))
        if request.url.path == "/api/v1/orders/67":
            return httpx.Response(200, text=meta)
        if request.url.path == "/order/67/sections":
            return httpx.Response(200, text=page)
        return httpx.Response(404)

    c = an.ArrNetworkClient(
        cache_dir=tmp_path, min_interval_s=0, http=httpx.Client(transport=httpx.MockTransport(serve))
    )
    assert len(c.sections(67, "tod")) == 5 and c.order(67).pages == 501
    assert len(c.sections(67, "tod")) == 5 and c.order(67).pages == 501  # the second time from disk
    assert seen == ["/order/67/sections?s=tod", "/api/v1/orders/67"]
    assert (tmp_path / "sections" / "67_tod.html").exists()
    assert c.sections(67, "tod")[0].fetched_on == date.today()
    with pytest.raises(an.ArrNetworkError, match="HTTP 404"):
        c.order(9999)


def _serving(data: bytes) -> an.ArrNetworkClient:
    return an.ArrNetworkClient(
        cache_dir=FIXTURES,
        min_interval_s=0,
        http=httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, content=data))),
    )


def test_the_order_pdf_is_accepted_only_with_the_sha256_the_site_read() -> None:
    pdf = make_pdf([["RATE SCHEDULE HV-2"]])
    raw = json.loads((FIXTURES / "orders" / "67.json").read_text())
    raw["order"]["sha256"] = hashlib.sha256(pdf).hexdigest()
    order = an.parse_order(raw, date(2026, 11, 25))
    assert _serving(pdf).pdf(order) == pdf
    with pytest.raises(an.ArrNetworkError, match="replaced since"):
        _serving(make_pdf([["A CORRIGENDUM"]])).pdf(order)
    with pytest.raises(an.ArrNetworkError, match="did not return a PDF"):
        _serving(b"<html>moved</html>").pdf(order)
    with pytest.raises(an.ArrNetworkError, match="HTTP 503"):
        offline_client().pdf(order)
    plain = an.parse_order({"order": {**raw["order"], "url": "http://example.org/o.pdf"}}, date(2026, 11, 25))
    with pytest.raises(an.ArrNetworkError, match="not https"):
        _serving(pdf).pdf(plain)


def test_requests_are_spaced() -> None:
    import time

    c = an.ArrNetworkClient(
        cache_dir=FIXTURES,
        min_interval_s=0.2,
        http=httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, text="{}"))),
    )
    t0 = time.monotonic()
    c._get("https://arr.test/a")
    c._get("https://arr.test/b")
    assert time.monotonic() - t0 >= 0.2


@pytest.mark.external
def test_live_site_still_serves_what_the_parser_reads(tmp_path: Path) -> None:
    c = an.ArrNetworkClient(cache_dir=tmp_path)
    assert c.order(67).sha256.startswith("3a046bfa")
    assert any(t.category == "HV-2" for t in c.sections(67, "tariff_schedule"))
