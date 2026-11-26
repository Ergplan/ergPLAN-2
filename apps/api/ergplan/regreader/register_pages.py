"""Every register figure opens its page (Stage 12).

The State Regulation Master (`tools/dev/import-register.py`) gave ten states' open-access charges,
ToD slots, banking and limits as approved rules whose refs are of kind `register`: the analyst's
citation of an order, table and page by name, with no stored PDF. This command finds each figure on
the cited page of the stored order and proposes a DRAFT that cites the page:

1. The order a row cites is looked up in `packages/rulebooks/register/orders.yaml` (by the start
   of the register's citation) and must be stored in the document library (`--store DIR` first
   stores the catalogue's PDFs found in DIR, hash checked, through `documents.service`).
2. The figure is looked for in the text layer of the cited page and the two pages either side (the
   page as the order prints it, and as the PDF numbers it). A line that prints the figure on a page
   that names the charge is the quote; a figure printed on more than one line, or not at all, is not
   guessed: the row keeps its register ref and is listed with the reason. `figures` in the
   catalogue may name the pages to read when the register names none, or text the line must hold.
3. The quote passes `regreader.grounding.quote_on_page` and prints the figure (in rupees, or in
   paise or per MWh where the line says so, converted by `core.units`). No figure is ever changed:
   the draft carries the register's figure, now cited to the page that prints it.
4. A version with at least one figure grounded is re-issued as a draft through
   `rulebook.service.propose` (origin `register_pages`), every other row and ref kept, and its golden
   cases (the approved version's, which still apply) are run. Nothing here approves.

No model is called. Idempotent: a draft whose rows are already these says "unchanged".

    python -m ergplan.regreader.register_pages [--state BR ...] [--dry-run] [--store DIR ...] [--json PATH]

`--dry-run` writes nothing (documents included). The report counts, per state, the rule book's
figures that open a stored page (`report.statebook.trace`) on the approved rules, and as they would
be if each draft replaced the version it re-issues (computed in memory, never stored or shown to a
client).
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import pypdfium2 as pdfium
import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.ids import document_id
from ..core.settings import get_settings
from ..core.units import Unit, convert
from ..db.models import Rule, RulebookVersion, SourceDocument
from ..documents import service as documents
from ..rulebook import golden, repository, sandbox, service
from ..rulebook.models import RuleView
from . import grounding

ORIGIN = "register_pages"
ACTOR = "register-pages@ergplan"
TITLE_SUFFIX = " · figures cited to the order's page"
WINDOW = 2  # the cited page and the two pages either side

# what a page must say for a figure on it to be this charge
KEYWORDS: dict[str, str] = {
    "cross_subsidy_surcharge": r"cross[\s-]*subsidy|\bCSS\b",
    "additional_surcharge": r"additional\s+surcharge",
    "wheeling": r"wheeling",
    "wheeling_losses": r"wheeling|distribution\s+loss|\bloss(es)?\b",
    "stu_transmission": r"transmission\s+charge",
    "stu_losses": r"transmission\s+loss|\bloss(es)?\b",
    "sldc_operating": r"\bSLDC\b|load\s+despatch|scheduling",
    "electricity_duty_oa": r"electricity\s+duty|\btax\b",
    "standby_charge": r"stand[\s-]*by",
    "reactive_energy_charge": r"reactive",
    "grid_support_charge": r"grid\s+support|parallel\s+operation",
}
# words that state a charge is nil (a figure of 0 is grounded only by these, beside the charge)
ZERO_WORDS = re.compile(
    r"\bnil\b|\bzero\b|not\s+(be\s+)?(levied|leviable|payable|applicable)|not\s+determin(ed|ing)|"
    r"shall\s+not\s+be\s+(levied|payable|applicable)|\bexempt|"
    r"\bno\s+(additional\s+|cross[\s-]*subsidy\s+)?(surcharge|charges?)\b",
    re.I,
)
EHV_WORDS = re.compile(r"\bEHV\b|\bEHT\b|extra\s+high|\b(66|110|132|220|400)\s*k\s*V", re.I)
_PAGE_MARK = (
    re.compile(r"\bpage\s*[|:]?\s*(\d{1,4})\b(?:\s*of\s*\d+)?", re.I),
    re.compile(r"^[-–(]?\s*(\d{1,4})\s*[-–)]?$"),
)
_HEAD_NUMBER = re.compile(r"^(\d{1,4})\s+[A-Za-z]|\S\s+(\d{1,4})$")
_PAGES_CITED = re.compile(r"\bpp?\.\s*(\d{1,4})(?:\s*[-–]\s*(\d{1,4}))?")


# ---------------------------------------------------------------- the catalogue


@dataclass(frozen=True)
class Order:
    key: str
    state: str
    cites: tuple[str, ...]
    title: str
    regulator: str | None = None
    order_date: date | None = None
    url_public: str | None = None
    documents: tuple[str, ...] = ()  # SHA-256 of each PDF of the order
    kind: str = "order"  # order | regulation | not_an_order
    obtained: bool = True
    why: str | None = None


@dataclass(frozen=True)
class FigureHint:
    """What a person read on the stored page and the citation does not say."""

    state: str
    component: str
    voltage: str | None = None
    order: str | None = None  # the order to read when the register cites none
    pages: tuple[int, ...] = ()  # pages as the order prints them (each read with WINDOW around it)
    pdf_pages: tuple[int, ...] = ()  # pages as the PDF numbers them (read exactly)
    anchor: str | None = None  # text the quoted line must contain
    previous: bool = False  # quote the line before with the line (a sentence that ends on it)
    why: str | None = None  # why the figure is not on the page (listed when it is not found)
    differs: str | None = None  # what the page says that the register row does not (for the analyst)


@dataclass(frozen=True)
class Catalogue:
    orders: tuple[Order, ...]
    figures: tuple[FigureHint, ...]

    def order(self, key: str) -> Order | None:
        return next((o for o in self.orders if o.key == key), None)

    def match(self, state: str, citation: str) -> Order | None:
        """The order a register citation names: the longest `cites` prefix of the state's orders."""
        best: tuple[int, Order] | None = None
        for o in self.orders:
            if o.state != state:
                continue
            for c in o.cites:
                if citation.startswith(c) and (best is None or len(c) > best[0]):
                    best = (len(c), o)
        return best[1] if best else None

    def hint(self, state: str, component: str, voltage: str | None) -> FigureHint | None:
        exact = [h for h in self.figures if (h.state, h.component, h.voltage) == (state, component, voltage)]
        if exact:
            return exact[0]
        return next(
            (h for h in self.figures if (h.state, h.component) == (state, component) and h.voltage is None),
            None,
        )


def catalogue_path() -> Path:
    return get_settings().rulebooks_dir / "register" / "orders.yaml"


def _d(v: Any) -> date | None:
    if v is None or isinstance(v, date):
        return v
    return date.fromisoformat(str(v))


def load_catalogue(path: Path | None = None) -> Catalogue:
    d = yaml.safe_load((path or catalogue_path()).read_text(encoding="utf-8"))
    orders = tuple(
        Order(
            key=str(o["key"]),
            state=str(o["state"]),
            cites=tuple(str(c) for c in o["cites"]),
            title=str(o["title"]),
            regulator=o.get("regulator"),
            order_date=_d(o.get("order_date")),
            url_public=o.get("url_public"),
            documents=tuple(str(x).lower() for x in o.get("documents") or ()),
            kind=str(o.get("kind") or "order"),
            obtained=bool(o.get("obtained", True)) and bool(o.get("documents")),
            why=o.get("why"),
        )
        for o in d.get("orders") or []
    )
    figures = tuple(
        FigureHint(
            state=str(f["state"]),
            component=str(f["component"]),
            voltage=None if f.get("voltage") is None else str(f["voltage"]),
            order=f.get("order"),
            pages=tuple(int(p) for p in f.get("pages") or ()),
            pdf_pages=tuple(int(p) for p in f.get("pdf_pages") or ()),
            anchor=f.get("anchor"),
            previous=bool(f.get("previous", False)),
            why=f.get("why"),
            differs=f.get("differs"),
        )
        for f in d.get("figures") or []
    )
    keys = [o.key for o in orders]
    if len(keys) != len(set(keys)):
        raise ValueError("orders.yaml: an order key is used twice")
    for f in figures:
        if f.order is not None and f.order not in keys:
            raise ValueError(
                f"orders.yaml: figure {f.state}/{f.component} names an unknown order {f.order!r}"
            )
    return Catalogue(orders, figures)


def citation(note: str | None) -> str | None:
    """The analyst's citation inside a register ref's note ('… — as entered in the …'), or None
    when the register entered the figure without one."""
    if not note or " — as entered in the " not in note:
        return None  # 'entered without a citation', or the register's own entry ('ToD slots as entered')
    return note.split(" — as entered in the ")[0].strip() or None


def cited_pages(text: str) -> list[int]:
    """The pages a citation names ('p.491', 'pp.492-494'), as the order prints them."""
    out: list[int] = []
    for m in _PAGES_CITED.finditer(text):
        lo = int(m.group(1))
        hi = int(m.group(2)) if m.group(2) else lo
        if hi < lo or hi - lo > 10:
            hi = lo
        out += [p for p in range(lo, hi + 1) if p not in out]
    return out


# ---------------------------------------------------------------- the stored order's pages


def printed_number(text: str) -> int | None:
    """The page number an order prints on a page: 'Page 236 of 440' or a number alone in its first
    or last three lines, else a number that begins or ends one of its first or last two lines (a
    running head such as 'Uttarakhand Electricity Regulatory Commission 253')."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    ends = lines[-3:][::-1] + lines[:3]
    for ln in ends:
        for rx in _PAGE_MARK:
            m = rx.search(ln)
            if m:
                return int(m.group(1))
    for ln in lines[:2] + lines[-2:]:
        m = _HEAD_NUMBER.search(ln)
        if m:
            return int(m.group(1) or m.group(2))
    return None


class StoredOrder:
    """The stored PDFs of an order, read once each: page text (the OCR'd text where the page is a
    scan, as `documents.page_text` gives it) and the page numbers the order prints."""

    def __init__(self, session: Session, order: Order) -> None:
        self.order = order
        self.docs: list[SourceDocument] = []
        for sha in order.documents:
            doc = session.scalar(select(SourceDocument).where(SourceDocument.file_sha256 == sha))
            if doc is not None and documents.is_stored(doc):
                self.docs.append(doc)
        self._text: dict[tuple[str, int], str] = {}
        self._layer: dict[str, list[str]] = {}

    @property
    def stored(self) -> bool:
        return bool(self.docs)

    def text(self, doc: SourceDocument, page: int) -> str:
        key = (doc.id, page)
        if key not in self._text:
            self._text[key] = documents.page_text(doc, page)
        return self._text[key]

    def layer(self, doc: SourceDocument) -> list[str]:
        """Every page's text layer, for the printed page numbers."""
        if doc.id not in self._layer:
            pdf = pdfium.PdfDocument(str(documents.file_path(doc)))
            try:
                self._layer[doc.id] = [str(pdf[i].get_textpage().get_text_range()) for i in range(len(pdf))]
            finally:
                pdf.close()
        return self._layer[doc.id]

    def window(self, doc: SourceDocument, printed: list[int], pdf_pages: tuple[int, ...] = ()) -> list[int]:
        """PDF pages to read: each printed page and the two either side, found by the number the
        order prints and by the PDF's own numbering; and the given PDF pages exactly."""
        out: set[int] = {p for p in pdf_pages if 1 <= p <= doc.pages}
        layer = self.layer(doc) if printed else []
        marks = {i + 1: printed_number(t) for i, t in enumerate(layer)}
        for c in printed:
            for p in range(c - WINDOW, c + WINDOW + 1):
                if 1 <= p <= doc.pages:
                    out.add(p)
            for idx, n in marks.items():
                if n is not None and abs(n - c) <= WINDOW:
                    out.add(idx)
        return sorted(out)


# ---------------------------------------------------------------- finding a figure on a page


@dataclass
class Found:
    document_id: str
    page: int
    printed_page: int | None
    quote: str
    printed_as: str  # how the page prints the figure ('0.53', '53 paise', 'nil')


@dataclass
class NotFound:
    reason: str
    seen: list[str] = field(default_factory=list)  # lines on the pages that name the charge, for the analyst


def plain(value: Decimal) -> str:
    """A figure as the rulebook stores it: no exponent, no trailing zeros."""
    t = format(value.normalize(), "f")
    return "0" if t in ("-0", "") else t


def _number_forms(value: Decimal) -> set[str]:
    return grounding.printed_numbers([plain(value)])


def printed_forms(value: Decimal, basis: str | None) -> list[tuple[set[str], re.Pattern[str] | None, str]]:
    """The ways a page may print the figure: as stored, and for a rate per kWh in paise or per MWh
    where the same line says so (converted by core.units)."""
    out: list[tuple[set[str], re.Pattern[str] | None, str]] = [(_number_forms(value), None, "")]
    if basis == "inr_per_kwh":
        paise = convert(value, Unit.INR_PER_KWH, Unit.PAISE_PER_KWH)
        mwh = convert(value, Unit.INR_PER_KWH, Unit.INR_PER_MWH)
        out.append((_number_forms(paise), re.compile(r"pais|\bps\b", re.I), " paise/kWh"))
        out.append((_number_forms(mwh), re.compile(r"MWh", re.I), " per MWh"))
    return out


def _numbers(line: str) -> set[str]:
    """The numbers a line prints ('Rs.240/MWh' prints 240: the abbreviation's stop is not a
    decimal point)."""
    return grounding.printed_numbers([re.sub(r"\b(Rs|Re|INR)\.(?=\d)", r"\1. ", line)])


def _voltage_rx(kv: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![\d.]){re.escape(kv)}\s*-?\s*k\s*V", re.I)


@dataclass
class _Cand:
    score: int
    doc: SourceDocument
    page: int
    index: int
    line: str
    printed_as: str


def find_figure(
    order: StoredOrder,
    pages: dict[str, list[int]],
    *,
    value: Decimal,
    component: str,
    basis: str | None,
    voltage: str | None = None,
    anchor: str | None = None,
    with_previous: bool = False,
) -> Found | NotFound:
    """Look for one figure on the given pages (document id → PDF pages). See the module docstring."""
    kw = re.compile(KEYWORDS.get(component, re.escape(component.replace("_", " "))), re.I)
    zero = value == 0
    forms = printed_forms(value, basis)
    anchor_n = grounding.normalise(anchor) if anchor else None
    cands: list[_Cand] = []
    seen: list[str] = []
    for doc in order.docs:
        for p in pages.get(doc.id, []):
            text = order.text(doc, p)
            # the page names the charge, or it continues a table the page before heads
            if not kw.search(text) and not (p > 1 and kw.search(order.text(doc, p - 1))):
                continue
            lines = [ln.strip() for ln in text.splitlines()]
            for i, line in enumerate(lines):
                if not line:
                    continue
                on_line = bool(kw.search(line))
                before = lines[i - 1] if i else ""
                after = lines[i + 1] if i + 1 < len(lines) else ""
                if on_line and _numbers(line) and len(seen) < 12:
                    seen.append(f"p.{p}: {line[:160]}")
                if anchor_n is not None and anchor_n not in grounding.normalise(line):
                    continue
                if zero:
                    # 'not levied', 'nil' … on the line, the charge named on it or the line before
                    if not ZERO_WORDS.search(line) or not (on_line or kw.search(before) or anchor_n):
                        continue
                    if voltage is not None and not anchor_n and not EHV_WORDS.search(f"{line} {after}"):
                        continue
                    printed_as = "a nil charge"
                else:
                    nums = _numbers(line)
                    head = " ".join(lines[max(0, i - 3) : i + 1])  # a unit may head the table's column
                    printed_as = ""
                    for fs, unit_rx, label in forms:
                        if fs & nums and (unit_rx is None or unit_rx.search(head)):
                            printed_as = next(iter(sorted(fs & nums, key=len))) + label
                            break
                    if not printed_as:
                        continue
                    # a one- or two-character figure ('1') is a figure only beside the charge's name
                    if len(plain(value)) < 3 and not (on_line or anchor_n):
                        continue
                score = 2 * on_line + (1 if voltage and _voltage_rx(voltage).search(line) else 0)
                cands.append(_Cand(score, doc, p, i, line, printed_as))
    if not cands:
        what = "a nil charge" if zero else f"the figure {plain(value)}"
        where = ", ".join(f"{p}" for ps in pages.values() for p in ps) or "none"
        return NotFound(
            f"{what} is not printed beside the charge on the pages read (PDF pages {where})", seen
        )
    top = max(c.score for c in cands)
    best = [c for c in cands if c.score == top]
    if len({grounding.normalise(c.line) for c in best}) > 1:
        shown = "; ".join(f"p.{c.page}: {c.line[:80]}" for c in best[:4])
        return NotFound(f"printed on {len(best)} lines of the pages read, so not unambiguous ({shown})", seen)
    c = best[0]
    text = order.text(c.doc, c.page)
    quote = _quote(
        text, c.index, c.line, kw, with_next=zero and voltage is not None, with_previous=with_previous
    )
    ocr = documents.page_is_ocr(c.doc, c.page)
    if not grounding.quote_on_page(quote, text, ocr):
        return NotFound(f"the line found on page {c.page} does not pass the grounding check", seen)
    if not zero and not (_numbers(quote) & {c.printed_as.split(" ")[0]}):
        return NotFound(f"the quote taken on page {c.page} does not print the figure", seen)
    printed = printed_number(order.layer(c.doc)[c.page - 1]) if c.page <= len(order.layer(c.doc)) else None
    return Found(c.doc.id, c.page, printed, quote, c.printed_as)


def _clip(line: str, limit: int) -> str:
    return line if len(line) <= limit else line[:limit].rsplit(" ", 1)[0]


def _quote(
    text: str,
    index: int,
    line: str,
    kw: re.Pattern[str],
    with_next: bool = False,
    with_previous: bool = False,
) -> str:
    """The line (with the next one where it ends a sentence the line begins, or the line before
    where the line ends a sentence that one begins); when the quote does not name the charge, the
    nearest line above it on the page that does, joined with ' … ' (the grounding check reads each
    fragment on the page)."""
    lines = [ln.strip() for ln in text.splitlines()]
    body = line
    if with_next and index + 1 < len(lines) and lines[index + 1]:
        body = f"{body} {lines[index + 1]}"
    if with_previous and index > 0 and lines[index - 1]:
        body = f"{lines[index - 1]} {body}"
        index -= 1
    if len(body) > 300:  # keep the end, where the figure is, when the line before was added
        body = body[-300:].split(" ", 1)[-1] if with_previous else _clip(body, 300)
    if kw.search(body) or with_previous:
        return body
    head = next((ln for ln in reversed(lines[:index]) if ln and kw.search(ln)), None)
    if head is None:
        return body
    return f"{_clip(head, 90)} … {body}"


# ---------------------------------------------------------------- one rule row


@dataclass
class FigureResult:
    state: str
    family: str
    label: str
    pointer: str
    value: str
    grounded: bool
    reason: str | None = None
    document_id: str | None = None
    page: int | None = None
    printed_page: int | None = None
    quote: str | None = None
    citation: str | None = None
    order: str | None = None
    differs: str | None = None
    seen: list[str] = field(default_factory=list)


def _figures(body: dict[str, Any]) -> list[tuple[str, str | None, Decimal]]:
    """(pointer, voltage, figure) for the figures of an open-access charge row."""
    out: list[tuple[str, str | None, Decimal]] = []
    try:
        if isinstance(body.get("value_by_voltage"), dict):
            for kv, v in body["value_by_voltage"].items():
                out.append((f"/value_by_voltage/{kv}", str(kv), Decimal(str(v))))
        elif body.get("value") is not None:
            out.append(("/value", None, Decimal(str(body["value"]))))
    except InvalidOperation:
        return []
    return out


def order_ref(found: Found, cite: str, effective_from: date, printed_as: str) -> dict[str, Any]:
    note = f"State Regulation Master: {cite}. The figure is printed on this page"
    if printed_as.endswith(("paise/kWh", "per MWh")):
        note += f" as {printed_as.strip()}"
    elif printed_as == "a nil charge":
        note += " as a charge not levied"
    ref: dict[str, Any] = {
        "kind": "order",
        "document_id": found.document_id,
        "page": found.page,
        "quote": found.quote[:400],
        "effective_from": effective_from.isoformat(),
        "note": (note + ".")[:500],
    }
    if found.printed_page is not None and found.printed_page != found.page:
        ref["printed_page"] = str(found.printed_page)
    return ref


def ground_row(
    session: Session,
    cat: Catalogue,
    state: str,
    v: RulebookVersion,
    row: Rule,
    cache: dict[str, StoredOrder],
) -> tuple[dict[str, Any] | None, list[FigureResult]]:
    """The row re-cited where its figures are found (None when none is), and a result per figure."""
    ref = row.source_ref or {}
    label = row.label or row.id
    if ref.get("kind") != "register":
        return None, []
    if v.family != "oa_charge":
        return None, [
            FigureResult(
                state,
                v.family,
                label,
                "",
                "",
                False,
                f"a {v.family.replace('_', ' ')} row: the register entered it without citing an order page "
                "(its slots, cycles or limits are the register's own)",
            )
        ]
    body = row.body or {}
    comp = str(body.get("component") or "")
    cite = citation(ref.get("note"))
    figures = _figures(body)
    results: list[FigureResult] = []
    new_refs: dict[str, dict[str, Any]] = {}
    for pointer, kv, value in figures:
        res = FigureResult(state, v.family, label, pointer, plain(value), False, citation=cite)
        results.append(res)
        hint = cat.hint(state, comp, kv)
        res.differs = hint.differs if hint else None
        order: Order | None = None
        if hint and hint.order:
            order = cat.order(hint.order)
        elif cite:
            order = cat.match(state, cite)
        if order is None:
            res.reason = (
                "the register entered the figure without citing an order"
                if not cite
                else "the citation names no order in the catalogue (packages/rulebooks/register/orders.yaml)"
            )
            if hint and hint.why:
                res.reason += f"; {hint.why}"
            continue
        res.order = order.key
        if not order.obtained:
            res.reason = (
                f"the cited {order.kind.replace('_', ' ')} is not in the library ({order.title}): {order.why}"
            )
            continue
        stored = cache.setdefault(order.key, StoredOrder(session, order))
        if not stored.stored:
            res.reason = f"the order's PDF is not stored on this server ({order.title})"
            continue
        printed = list(hint.pages) if hint and (hint.pages or hint.pdf_pages) else cited_pages(cite or "")
        pdf_pages = hint.pdf_pages if hint else ()
        if not printed and not pdf_pages:
            res.reason = "the citation names no page, and no page is given for it in the catalogue"
            if hint and hint.why:
                res.reason += f"; {hint.why}"
            continue
        pages = {d.id: stored.window(d, printed, pdf_pages) for d in stored.docs}
        got = find_figure(
            stored, pages, value=value, component=comp, basis=body.get("basis"), voltage=kv,
            anchor=hint.anchor if hint else None, with_previous=bool(hint and hint.previous),
        )  # fmt: skip
        if isinstance(got, NotFound):
            res.reason = got.reason + (f"; {hint.why}" if hint and hint.why else "")
            res.seen = got.seen
            continue
        res.grounded = True
        res.document_id, res.page, res.printed_page, res.quote = (
            got.document_id,
            got.page,
            got.printed_page,
            got.quote,
        )
        new_refs[pointer] = order_ref(got, cite or order.title, v.effective_from, got.printed_as)
    if not new_refs:
        return None, results
    field_refs: dict[str, Any] = copy.deepcopy(row.field_refs or {})
    source_ref: dict[str, Any] = copy.deepcopy(ref)
    if len(new_refs) == len(figures):
        # every figure of the row is on a page: the row cites the first, and any other number of
        # the body without its own ref (a waiver) keeps the register's ref it had
        for i, ex in enumerate(body.get("exemptions") or []):
            ptr = f"/exemptions/{i}/waived_pct"
            if isinstance(ex, dict) and "waived_pct" in ex and ptr not in field_refs:
                field_refs[ptr] = copy.deepcopy(ref)
        source_ref = next(iter(new_refs.values()))
        for pointer, r in new_refs.items():
            if r != source_ref:
                field_refs[pointer] = r
    else:
        field_refs.update(new_refs)
    out: dict[str, Any] = {
        "label": row.label,
        "scope": copy.deepcopy(row.scope or {}),
        "body": copy.deepcopy(body),
        "source_ref": source_ref,
        "position": row.position,
    }
    if field_refs:
        out["field_refs"] = field_refs
    return out, results


# ---------------------------------------------------------------- a version


@dataclass
class VersionOutcome:
    state: str
    family: str
    jurisdiction: str
    base_version: int
    base_id: str
    status: str  # proposed | revised | unchanged | nothing_grounded
    draft_id: str | None = None
    draft_version: int | None = None
    grounded: int = 0
    figures: int = 0
    golden: str | None = None


def _row_key(r: dict[str, Any]) -> tuple[Any, ...]:
    return (r.get("label"), r.get("scope") or {}, r["body"], r["source_ref"], r.get("field_refs") or {})


def _rows_of(session: Session, version_id: str) -> list[Rule]:
    return list(
        session.scalars(select(Rule).where(Rule.rulebook_version_id == version_id).order_by(Rule.position))
    )


def register_versions(session: Session, states: list[str] | None = None) -> list[RulebookVersion]:
    """The approved versions with at least one row on a register ref, in the given states."""
    from ..intake import defaults

    out = []
    for v in session.scalars(
        select(RulebookVersion)
        .where(RulebookVersion.status == "approved")
        .order_by(RulebookVersion.jurisdiction_id, RulebookVersion.family, RulebookVersion.version)
    ):
        st = state_of(v.jurisdiction_id, defaults.DISCOM_STATE)
        if states and st not in states:
            continue
        if any((r.source_ref or {}).get("kind") == "register" for r in _rows_of(session, v.id)):
            out.append(v)
    return out


def state_of(jurisdiction_id: str, discom_state: dict[str, str]) -> str:
    return discom_state.get(jurisdiction_id) or jurisdiction_id.split("_")[0].upper()


def draft_for(session: Session, base: RulebookVersion) -> RulebookVersion | None:
    return session.scalar(
        select(RulebookVersion)
        .where(
            RulebookVersion.family == base.family,
            RulebookVersion.jurisdiction_id == base.jurisdiction_id,
            RulebookVersion.origin == ORIGIN,
            RulebookVersion.title == (base.title or "") + TITLE_SUFFIX,
            RulebookVersion.status == "draft",
        )
        .order_by(RulebookVersion.version.desc())
    )


def reissue(
    session: Session,
    cat: Catalogue,
    base: RulebookVersion,
    *,
    dry_run: bool = False,
    cache: dict[str, StoredOrder] | None = None,
) -> tuple[VersionOutcome, list[FigureResult]]:
    from ..intake import defaults

    cache = {} if cache is None else cache
    state = state_of(base.jurisdiction_id, defaults.DISCOM_STATE)
    rules = _rows_of(session, base.id)
    rows: list[dict[str, Any]] = []
    results: list[FigureResult] = []
    changed = 0
    for r in rules:
        new, res = ground_row(session, cat, state, base, r, cache)
        results += res
        if new is not None:
            changed += 1
            rows.append(new)
        else:
            row = {"label": r.label, "scope": r.scope or {}, "body": r.body, "source_ref": r.source_ref,
                   "position": r.position}  # fmt: skip
            if r.field_refs:
                row["field_refs"] = r.field_refs
            rows.append(row)
    out = VersionOutcome(
        state, base.family, base.jurisdiction_id, base.version, base.id, "nothing_grounded",
        grounded=sum(1 for x in results if x.grounded), figures=len(results),
    )  # fmt: skip
    if not changed:
        return out, results
    existing = draft_for(session, base)
    if existing is not None:
        have = [
            _row_key(
                {
                    "label": r.label,
                    "scope": r.scope,
                    "body": r.body,
                    "source_ref": r.source_ref,
                    "field_refs": r.field_refs,
                }
            )
            for r in _rows_of(session, existing.id)
        ]
        out.draft_id, out.draft_version = existing.id, existing.version
        if have == [_row_key(r) for r in rows]:
            out.status, out.golden = "unchanged", existing.golden_status
            return out, results
        out.status = "revised"
        if not dry_run:
            service.revise(session, existing.id, rows)
    else:
        out.status = "proposed"
        if not dry_run:
            v = service.propose(
                session, family=base.family, jurisdiction_id=base.jurisdiction_id, rows=rows, actor=ACTOR,
                effective_from=base.effective_from, effective_to=base.effective_to,
                title=(base.title or "") + TITLE_SUFFIX, origin=ORIGIN,
                notes=f"Re-issue of version {base.version} ({base.id}) with {out.grounded} of {out.figures} "
                "register figures cited to the page of the stored order that prints them; every figure is "
                "the register's, and every other row and ref is unchanged. A draft: an analyst approves it.",
            )  # fmt: skip
            out.draft_id, out.draft_version = v.id, v.version
    if not dry_run and out.draft_id:
        g = service.run_golden(session, out.draft_id)
        out.golden = f"{g.passed} of {g.total}"
    return out, results


# ---------------------------------------------------------------- the rule book's trace


@contextmanager
def drafts_in_place(replace: dict[str, str]) -> Iterator[None]:
    """For the report only: `repository.approved_rules` answers with each draft's rows in place of
    the approved version it re-issues (approved version id → draft id), in this process, inside
    the block. Nothing is written; no client request runs in the command's process."""
    original = repository._approved_rules

    def patched(session: Session, family: str, jurisdiction_ids: list[str], on: date) -> list[RuleView]:
        out: list[RuleView] = []
        done: set[str] = set()
        for r in original(session, family, jurisdiction_ids, on):
            d = replace.get(r.version_id)
            if d is None:
                out.append(r)
            elif d not in done:
                done.add(d)
                out += sandbox.rules(session, d)
        return out

    repository._CACHE.clear()
    repository._CACHE_STAMP[0] = None
    setattr(repository, "_approved_rules", patched)  # noqa: B010 (a projection, undone below)
    try:
        yield
    finally:
        setattr(repository, "_approved_rules", original)  # noqa: B010
        repository._CACHE.clear()
        repository._CACHE_STAMP[0] = None


def trace_states(session: Session, states: list[str], month: date) -> dict[str, dict[str, int]]:
    """Per state, the rule book's trace (statebook.trace) summed over its DISCOMs' reference books."""
    from ..intake import defaults
    from ..report import statebook

    out: dict[str, dict[str, int]] = {}
    for discom, st in sorted(defaults.DISCOM_STATE.items()):
        if st not in states:
            continue
        try:
            t = statebook.build(session, discom=discom, month=month)["trace"]
        except statebook.BookError:
            continue
        acc = out.setdefault(
            st, {"page": 0, "unstored": 0, "register": 0, "assumption": 0, "other": 0, "books": 0}
        )
        for k, n in t.items():
            acc[k] = acc.get(k, 0) + int(n)
        acc["books"] += 1
    return out


# ---------------------------------------------------------------- storing the catalogue's PDFs


def store_from(session: Session, cat: Catalogue, folders: list[Path]) -> list[str]:
    """Store every catalogue PDF found in the folders (any file name; matched by SHA-256)."""
    want = {sha: o for o in cat.orders for sha in o.documents}
    done: list[str] = []
    for folder in folders:
        for p in sorted(folder.rglob("*.pdf")):
            data = p.read_bytes()
            sha = hashlib.sha256(data).hexdigest()
            o = want.get(sha)
            if o is None:
                continue
            doc, created = documents.register(
                session, data,
                documents.DocumentMeta(
                    title=o.title, regulator=o.regulator, jurisdiction=o.state,
                    document_kind="regulation" if o.kind == "regulation" else "order",
                    order_date=o.order_date, url_public=o.url_public, original_filename=p.name,
                ),
                ACTOR,
            )  # fmt: skip
            done.append(f"{'stored' if created else 'present'} {o.key} {doc.id}")
            want.pop(sha)
    return done


def fetch(session: Session, cat: Catalogue, folder: Path, client: Any | None = None) -> list[str]:
    """Download, from the regulator's own link, every catalogue PDF not stored here, into
    `folder/<sha256>.pdf`. Certificate checks stay on; a file whose SHA-256 is not the catalogue's
    is not kept (a re-issued or re-compressed copy cannot stand in for the cited page)."""
    import httpx

    folder.mkdir(parents=True, exist_ok=True)
    http = client or httpx.Client(
        timeout=180, follow_redirects=True, headers={"User-Agent": "ergplan-library"}
    )
    out: list[str] = []
    for o in cat.orders:
        for sha in o.documents:
            d = session.get(SourceDocument, document_id(sha))
            if (d is not None and documents.is_stored(d)) or (folder / f"{sha}.pdf").exists():
                continue
            if not o.url_public or not o.url_public.lower().endswith(".pdf"):
                out.append(f"no link    {o.key}: the catalogue has no PDF link")
                continue
            try:
                data = http.get(o.url_public).raise_for_status().content
            except Exception as e:  # a regulator's site being down or refusing TLS is reported
                out.append(f"not fetched {o.key}: {type(e).__name__}: {e}"[:300])
                continue
            if hashlib.sha256(data).hexdigest() != sha:
                out.append(
                    f"not kept   {o.key}: the file at {o.url_public} is not the catalogue's (SHA-256 differs)"
                )
                continue
            (folder / f"{sha}.pdf").write_bytes(data)
            out.append(f"fetched    {o.key} → {sha}.pdf")
    return out


def missing_orders(session: Session, cat: Catalogue, states: list[str] | None) -> list[dict[str, Any]]:
    out = []
    for o in cat.orders:
        if states and o.state not in states:
            continue
        stored = [
            sha
            for sha in o.documents
            if (d := session.get(SourceDocument, document_id(sha))) is not None and documents.is_stored(d)
        ]
        if not o.obtained or not stored:
            why = o.why or "the PDF is not stored on this server"
            out.append({"order": o.key, "state": o.state, "kind": o.kind, "title": o.title,
                        "url_public": o.url_public, "why": why})  # fmt: skip
    return out


# ---------------------------------------------------------------- the command


def run(
    session: Session,
    *,
    states: list[str] | None = None,
    dry_run: bool = False,
    catalogue: Catalogue | None = None,
    month: date | None = None,
    trace: bool = True,
) -> dict[str, Any]:
    cat = catalogue or load_catalogue()
    if not dry_run:
        service.sync_jurisdictions(session)
        golden.sync_tests(session)
    bases = register_versions(session, states)
    scope = sorted(set(states) if states else {state_of_v(v) for v in bases})
    month = (month or date.today()).replace(day=1)
    before = trace_states(session, scope, month) if trace else {}
    cache: dict[str, StoredOrder] = {}
    outcomes: list[VersionOutcome] = []
    figures: list[FigureResult] = []
    for v in bases:
        o, res = reissue(session, cat, v, dry_run=dry_run, cache=cache)
        outcomes.append(o)
        figures += res
    after: dict[str, dict[str, int]] = {}
    if trace and not dry_run:
        replace = {o.base_id: o.draft_id for o in outcomes if o.draft_id}
        with drafts_in_place(replace):
            after = trace_states(session, scope, month)
    return {
        "month": month.isoformat(),
        "states": scope,
        "versions": [asdict(o) for o in outcomes],
        "figures": [asdict(f) for f in figures],
        "missing_orders": missing_orders(session, cat, scope),
        "trace_before": before,
        "trace_after": after,
    }


def state_of_v(v: RulebookVersion) -> str:
    from ..intake import defaults

    return state_of(v.jurisdiction_id, defaults.DISCOM_STATE)


def summary(result: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for o in result["versions"]:
        head = f"{o['state']} {o['family']:17} {o['jurisdiction']} v{o['base_version']}: {o['status']}"
        if o["draft_id"]:
            head += f" → draft v{o['draft_version']} ({o['draft_id']})"
        out.append(
            f"{head}; {o['grounded']} of {o['figures']} figures on a page; golden {o['golden'] or '-'}"
        )
    for st in result["states"]:
        b, a = result["trace_before"].get(st), result["trace_after"].get(st)
        if b:
            line = f"trace {st}: before page {b['page']} register {b['register']} unstored {b['unstored']}"
            if a:
                line += (
                    f"; with the drafts page {a['page']} register {a['register']} unstored {a['unstored']}"
                )
            out.append(line + f" ({b['books']} books)")
    for f in result["figures"]:
        if not f["grounded"]:
            out.append(f"kept register: {f['state']} {f['label']} {f['pointer']} {f['value']}: {f['reason']}")
        if f.get("differs"):
            out.append(f"differs: {f['state']} {f['label']} {f['pointer']}: {f['differs']}")
    for m in result["missing_orders"]:
        out.append(f"not obtained: {m['state']} {m['title']} ({m['url_public'] or 'no link'}): {m['why']}")
    return out


def main(argv: list[str] | None = None) -> int:
    from ..db.base import session_scope

    ap = argparse.ArgumentParser(prog="ergplan.regreader.register_pages", description=__doc__.split("\n")[0])
    ap.add_argument(
        "--state", action="append", default=None, help="a state code (repeat); default every state"
    )
    ap.add_argument("--dry-run", action="store_true", help="write nothing")
    ap.add_argument(
        "--store", action="append", type=Path, default=[], help="store the catalogue's PDFs found here"
    )
    ap.add_argument("--json", type=Path, default=None, help="write the full result as JSON here")
    ap.add_argument("--month", default=None, help="the rule book month for the trace (YYYY-MM)")
    ap.add_argument(
        "--fetch", type=Path, default=None,
        help="first download the catalogue's PDFs not stored here into this folder (SHA-256 checked); "
        "they are then stored as with --store",
    )  # fmt: skip
    a = ap.parse_args(argv)
    states = [s.upper() for s in a.state] if a.state else None
    month = date.fromisoformat(a.month + "-01") if a.month else None
    if a.fetch is not None and not a.dry_run:
        with session_scope() as s:
            for line in fetch(s, load_catalogue(), a.fetch):
                print(line)
        a.store.append(a.fetch)
    if a.store and not a.dry_run:
        with session_scope(actor=ACTOR) as s:
            for line in store_from(s, load_catalogue(), a.store):
                print(line)
    with session_scope(actor=ACTOR) as s:
        result = run(s, states=states, dry_run=a.dry_run, month=month)
        if a.dry_run:
            s.rollback()
    for line in summary(result):
        print(line)
    if a.json:
        a.json.write_text(json.dumps(result, indent=1, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
