"""The ARR Network (https://arr-101.vercel.app, built from Ergplan/tariff-oder): the person's source
for DISCOM tariffs. This module is the only place that talks to it.

    python -m ergplan.regreader.arr_network import --state up --orders 67,66,65,64 [--dry-run]

What the site gives today: order metadata as JSON (`/api/v1/orders/{id}`: the regulator's URL, the
PDF's SHA-256, pages, tariff year, utilities) and the order's tariff tables as server-rendered HTML
(`/order/{id}/sections?s=<section>`), every cell the order's own printed text with the PDF page of
its row. `ArrNetworkClient.sections()` is the one door to those tables: when the site publishes them
as JSON, only that method changes.

The site says its figures are not reviewed by a person, so everything read here becomes a DRAFT
rulebook version (`regreader.arr_import`), never an approved one. No model is involved anywhere:
the tables are mapped by `regreader.arr_map` with the rate parser of `regreader.arr_rates`.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import httpx

from ..core.settings import get_settings

BASE_URL = "https://arr-101.vercel.app"
SECTIONS = ("tariff_schedule", "tod", "wheeling", "re_portfolio", "power_purchase")
USER_AGENT = "ergplan-regreader"
MAX_PDF_BYTES = 200 * 1024 * 1024


class ArrNetworkError(RuntimeError):
    pass


@dataclass(frozen=True)
class Order:
    order_id: int
    commission: str
    title: str
    url: str  # the regulator's own link to the PDF
    sha256: str
    pages: int
    arr_year: str  # 'FY2026-27'
    utilities: tuple[str, ...]
    order_date: date | None
    fetched_on: date  # the day this copy of the order's metadata was read from the site


@dataclass(frozen=True)
class Row:
    cells: tuple[str, ...]
    page: int


@dataclass(frozen=True)
class SectionTable:
    order_id: int
    section: str
    table_id: str  # the site's anchor ('t16241'): stable for one reading of the order
    label: str  # the tag before the title: a category code ('HV-2') or a table number ('Table 10-3')
    category: str | None  # the tariff category the table is printed under, when the site groups by one
    title: str
    fiscal_year: str | None
    page: int
    columns: tuple[str, ...]
    rows: tuple[Row, ...]
    reader_version: str
    as_text: bool  # the site read rate lines from running text, not from a ruled table
    fetched_on: date

    def name(self) -> str:
        return (
            f"order {self.order_id} · {self.section} · {self.label} {self.title}"[:160] + f" · p. {self.page}"
        )


def _text(fragment: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<!--.*?-->|<[^>]+>", "", fragment))).strip()


_ARTICLE = re.compile(r'<article class="sectable" id="([^"]+)"[^>]*>(.*?)</article>', re.S)
_PAGE_CELL = re.compile(r"^p\.\s*(\d+)$")


def parse_section(page_html: str, order_id: int, section: str, fetched_on: date) -> list[SectionTable]:
    """The tables of one section page. Raises ArrNetworkError when the page is not a section page
    (so a changed layout is noticed, never read as 'no tables')."""
    if 'class="sectionhead"' not in page_html and 'class="sectable"' not in page_html:
        raise ArrNetworkError(f"order {order_id} section {section}: the page is not a section page")
    rv = re.search(r"\(the Section Reader,\s*(.*?)\)", _text(page_html))
    reader_version = rv.group(1).strip() if rv else "unknown"
    # which category heading each table sits under
    category_of: dict[str, str] = {}
    for block in re.split(r'(?=<h3 class="cathead">)', page_html)[1:]:
        head = re.match(r'<h3 class="cathead">(.*?)</h3>', block, re.S)
        if not head:
            continue
        for m in _ARTICLE.finditer(block):
            category_of[m.group(1)] = _text(head.group(1))
    out: list[SectionTable] = []
    for m in _ARTICLE.finditer(page_html):
        tid, body = m.group(1), m.group(2)
        h4 = re.search(r"<h4>(.*?)</h4>", body, re.S)
        note = re.search(r'<p class="note">(.*?)</p>', body, re.S)
        if not h4 or not note:
            raise ArrNetworkError(f"order {order_id} section {section} table {tid}: no heading or note")
        tag = re.search(r'<span class="tag">(.*?)</span>', h4.group(1), re.S)
        label = _text(tag.group(1)) if tag else ""
        title = _text(h4.group(1)[tag.end() :] if tag else h4.group(1))
        note_text = _text(note.group(1))
        pg = re.search(r"page\s*(\d+)", note_text)
        if not pg:
            raise ArrNetworkError(f"order {order_id} section {section} table {tid}: no page")
        fy = re.search(r"\bFY\s?(\d{4}-\d{2})\b", note_text)
        columns = [_text(c) for c in re.findall(r"<th[^>]*>(.*?)</th>", body, re.S)]
        paged = bool(columns) and columns[-1] == "Page"
        rows: list[Row] = []
        tbody = re.search(r"<tbody>(.*?)</tbody>", body, re.S)
        for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", tbody.group(1) if tbody else "", re.S):
            cells = [_text(c) for c in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
            if not cells:
                continue
            row_page = int(pg.group(1))
            last = _PAGE_CELL.match(cells[-1])
            if last and (paged or len(cells) > len(columns)):
                row_page, cells = int(last.group(1)), cells[:-1]
            rows.append(Row(tuple(cells), row_page))
        category = category_of.get(tid)
        out.append(
            SectionTable(
                order_id=order_id,
                section=section,
                table_id=tid,
                label=label,
                category=category,
                title=title,
                fiscal_year=f"FY{fy.group(1)}" if fy else None,
                page=int(pg.group(1)),
                columns=tuple(columns[:-1] if paged else columns),
                rows=tuple(rows),
                reader_version=reader_version,
                as_text="printed as text" in note_text,
                fetched_on=fetched_on,
            )
        )
    return out


def parse_order(data: dict[str, Any], fetched_on: date) -> Order:
    o = data.get("order") or {}
    try:
        od = o.get("order_date")
        return Order(
            order_id=int(o["order_id"]),
            commission=str(o.get("commission") or ""),
            title=str(o["title"]),
            url=str(o["url"]),
            sha256=str(o["sha256"]).lower(),
            pages=int(o["pages"]),
            arr_year=str(o["arr_year"]),
            utilities=tuple(str(u) for u in o.get("utilities") or []),
            order_date=date.fromisoformat(str(od)[:10]) if od else None,
            fetched_on=fetched_on,
        )
    except (KeyError, TypeError, ValueError) as e:
        raise ArrNetworkError(f"order metadata lacks {e}") from e


class ArrNetworkClient:
    """Reads the ARR Network politely: at most two requests a second, every page kept on disk under
    data/arr_network/ (gitignored) and read from there afterwards (`refresh=True` fetches again).
    Certificates are always verified."""

    def __init__(
        self,
        base_url: str = BASE_URL,
        cache_dir: Path | None = None,
        refresh: bool = False,
        min_interval_s: float = 0.5,
        http: httpx.Client | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.cache_dir = cache_dir or get_settings().ergplan_data_dir / "arr_network"
        self.refresh = refresh
        self.min_interval_s = min_interval_s
        self.http = http or httpx.Client(
            timeout=120, follow_redirects=True, headers={"User-Agent": USER_AGENT}, verify=True
        )
        self._last = 0.0

    def _get(self, url: str) -> httpx.Response:
        wait = self.min_interval_s - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        try:
            r = self.http.get(url)
        except httpx.HTTPError as e:
            raise ArrNetworkError(f"{url}: {e.__class__.__name__}") from e
        finally:
            self._last = time.monotonic()
        if r.status_code != 200:
            raise ArrNetworkError(f"{url}: HTTP {r.status_code}")
        return r

    def _cached(self, name: str, url: str) -> tuple[str, date]:
        """The page's text and the day it was fetched (the cached file's date)."""
        path = self.cache_dir / name
        if self.refresh or not path.exists():
            text = self._get(url).text
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".part")
            tmp.write_text(text, encoding="utf-8")
            tmp.replace(path)
        return path.read_text(encoding="utf-8"), date.fromtimestamp(path.stat().st_mtime)

    def order(self, order_id: int) -> Order:
        text, on = self._cached(f"orders/{order_id}.json", f"{self.base_url}/api/v1/orders/{order_id}")
        try:
            return parse_order(json.loads(text), on)
        except json.JSONDecodeError as e:
            raise ArrNetworkError(f"order {order_id}: the site did not answer with JSON") from e

    def sections(self, order_id: int, section: str) -> list[SectionTable]:
        """The tables of one section of one order. The one method to replace when the site serves
        the section tables as JSON."""
        if section not in SECTIONS:
            raise ArrNetworkError(f"unknown section {section}")
        text, on = self._cached(
            f"sections/{order_id}_{section}.html", f"{self.base_url}/order/{order_id}/sections?s={section}"
        )
        return parse_section(text, order_id, section, on)

    def pdf(self, order: Order) -> bytes:
        """The order's PDF from the regulator's own link, accepted only when its SHA-256 is the one
        the ARR Network read. Raises ArrNetworkError otherwise (the caller then keeps the link and
        the page and says the page is not stored)."""
        if not order.url.lower().startswith("https://"):
            raise ArrNetworkError("the regulator's link is not https")
        r = self._get(order.url)
        data = r.content
        if len(data) > MAX_PDF_BYTES:
            raise ArrNetworkError("the file is larger than 200 MB")
        if not data.startswith(b"%PDF"):
            raise ArrNetworkError("the regulator's link did not return a PDF")
        got = hashlib.sha256(data).hexdigest()
        if got != order.sha256:
            raise ArrNetworkError(
                f"the PDF at the regulator's link has SHA-256 {got[:12]}…, not {order.sha256[:12]}… "
                "(the order was replaced since the ARR Network read it)"
            )
        return data


def main(argv: list[str] | None = None) -> int:
    from .arr_import import main as import_main

    return import_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
