"""The ARR Network's section tables → draft rule rows of ergPLAN's families (tariff_schedule,
tod_slots, oa_charge), each value with the order page and the printed cells as its source_ref.

Only the commission's own rate tables are mapped: a table of claimed, existing, proposed or
computed-before-approval figures, a table for another year, a table whose shape or units this
module does not recognise, and every row that cannot be read with confidence is listed in
`Mapped.unmapped` with the reason. Nothing is guessed, converted beyond paise to rupees
(core/units.py) or filled in from another table. No model is involved.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from ..core.units import Unit
from . import arr_rates
from .arr_network import Order, Row, SectionTable
from .tariff_oder import Mapped, Unmapped, _months
from .voltage import disjoint, parse_band

READER = "arr_network"
# the sections that hold something a rule family takes (power_purchase holds costs, not tariffs)
MAPPED_SECTIONS = ("tariff_schedule", "tod", "wheeling", "re_portfolio")
# DISCOM-side open-access charges are levied by the consumer's DISCOM whichever network the
# generator is on (the reading of the approved rulebooks: connection intra_state and ists)
OA_CONNECTIONS = ["intra_state", "ists"]

_NOT_APPROVED = re.compile(
    r"\b(claimed|submitted|submission|proposed|proposal|petitioner'?s?|petitioners|as per petition|"
    r"existing|projected|filed)\b",
    re.I,
)
# inside a rate schedule a heading may speak of existing consumers: only these mark a table of
# figures that are not the approved ones
_NOT_APPROVED_SCHEDULE = re.compile(
    r"\b(existing|proposed|current|present|revised)\s+(tariffs?|rates?|charges?)\b|"
    r"\b(claimed|submitted|proposal|petitioner'?s?|petitioners)\b",
    re.I,
)
_APPROVED = re.compile(r"\b(approved|determined|decided|allowed)\b", re.I)
_COMPUTATION = re.compile(
    r"\bfor\s+(the\s+purpose\s+of\s+)?computation\b|\bworked\s+out\b|\b(aggregate|average)\s+of\s+transmission\b",
    re.I,
)
_FOR_OA = re.compile(r"\b(oa|open\s+access)\s+(consumers?|customers?)\b", re.I)
_GENERIC_TITLE = re.compile(
    r"^(rate\s+schedule\b|rate\s+is\b|\(?[a-z0-9]{1,3}\)?[.)]?\s*(demand|energy|fixed)\s+charges?\s*:?$)",
    re.I,
)
_GENERIC_DESCRIPTOR = {"", "metered", "description", "charges", "base rate", "particulars", "rate"}
_FIXED = re.compile(r"\b(fixed|demand)\s+charges?\b", re.I)
_ENERGY = re.compile(r"\benergy\s+charges?\b", re.I)
_SLABBED = re.compile(r"\b(consumption\s+range|slab|units?\s+consumed)\b", re.I)
_SUBSIDY = re.compile(r"\bsubsid", re.I)
_CATEGORY = re.compile(r"\b([A-Z]{1,5})\s?[-–]?\s?(\d{1,2}[A-Z]?)\b")
_KV_NUMBER = re.compile(r"(\d+(?:\.\d+)?)\s*k\s*v(?![a-z])", re.I)

DEMAND_BASIS = {Unit.INR_PER_KVA_MONTH: "inr_per_kva_month", Unit.INR_PER_KW_MONTH: "inr_per_kw_month"}
ENERGY_BASIS = {Unit.INR_PER_KWH: "kwh", Unit.INR_PER_KVAH: "kvah"}
OA_BASIS = {
    Unit.INR_PER_KWH: "inr_per_kwh",
    Unit.INR_PER_KVAH: "inr_per_kvah",
    Unit.INR_PER_MWH: "inr_per_mwh",
    Unit.INR_PER_KW_MONTH: "inr_per_kw_month",
    Unit.INR_PER_MW_MONTH: "inr_per_mw_month",
    Unit.INR_PER_MW_DAY: "inr_per_mw_day",
}


@dataclass(frozen=True)
class Source:
    """What every row of one order's import carries."""

    order: Order
    document_id: str
    effective_from: date


class _Skip(Exception):
    """A table or row that is not mapped; the message is the reason."""


def _quote(cells: list[str] | tuple[str, ...]) -> str:
    """The printed cells of a row, joined; cut at a cell boundary to the schema's 400 characters."""
    out = ""
    for c in (c.strip() for c in cells if c and c.strip()):
        nxt = c if not out else f"{out} | {c}"
        if len(nxt) > 400:
            return out or c[:400]
        out = nxt
    return out


def _ref(
    src: Source,
    t: SectionTable,
    page: int,
    cells: list[str] | tuple[str, ...],
    lead: list[str] | tuple[str, ...] = (),
) -> dict[str, Any]:
    """The citation of one printed row. `lead` is what the table prints above the row (its title, a
    column heading): quoted with the row when the row is on the table's first page, where it is."""
    if page == t.page:
        cells = [*lead, *cells]
    note = (
        f"ARR Network order {src.order.order_id} · {t.section} · {t.label or t.table_id} · "
        f"{t.reader_version} · not reviewed by a person there"
    )
    link = f"; regulator's copy: {src.order.url}#page={page}"
    return {
        "kind": "order",
        "document_id": src.document_id,
        "page": page,
        "quote": _quote(cells),
        "effective_from": src.effective_from.isoformat(),
        "note": (note + link) if len(note + link) <= 500 else note[:500],
    }


def _review(src: Source, t: SectionTable) -> dict[str, Any]:
    return {
        "reader": READER,
        "decision": "proposed",
        "order_id": src.order.order_id,
        "table_id": t.table_id,
        "reader_version": t.reader_version,
        "fetched_on": t.fetched_on.isoformat(),
    }


def _band(text: str) -> dict[str, Any] | None:
    """The voltage band a descriptor names, or None when it names none. Raises when the text names
    a voltage this module cannot read whole (a number left over beside the kV figures: a heading the
    text layer broke, such as 'above 66 V kV and up to 132 k')."""
    bare = _CATEGORY.sub(" ", text)
    if not re.search(r"k\s*v(?![a-z])|\d\s*k\b|\bvolt", bare, re.I):
        return None
    band = parse_band(bare)
    in_kv = _KV_NUMBER.findall(bare)
    stray = [n for n in re.findall(r"\d+(?:\.\d+)?", bare) if n not in in_kv]
    both_ways = (
        len(set(in_kv)) == 1
        and re.search(r"\b(above|exceeding|more than)\b", bare, re.I)
        and re.search(r"\b(below|up\s*to|upto|less than|not exceeding)\b", bare, re.I)
    )
    if band is None or stray or both_ways:
        raise _Skip(f"voltage not read with confidence: '{text.strip()}'")
    return band


def _plain(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[#*$@]+", "", text)).strip(" :-–·")


def _is_generic(descriptor: str) -> bool:
    return _plain(descriptor).lower() in _GENERIC_DESCRIPTOR


def _table_guard(t: SectionTable, order: Order, pattern: re.Pattern[str] = _NOT_APPROVED) -> None:
    if t.fiscal_year and t.fiscal_year != order.arr_year:
        raise _Skip(f"the table is for {t.fiscal_year}; the order's tariff year is {order.arr_year}")
    if pattern.search(t.title) and not _APPROVED.search(t.title):
        raise _Skip("claimed, existing or proposed figures, not the commission's approved rate")


def _one_column(columns: tuple[str, ...], pattern: re.Pattern[str], what: str) -> int | None:
    """The single column a pattern names; where several do (existing, proposed and approved side by
    side) the one headed approved. None when no column matches; a _Skip when the choice is not clear."""
    hits = [i for i, c in enumerate(columns) if pattern.search(c)]
    if len(hits) > 1:
        approved = [i for i in hits if _APPROVED.search(columns[i]) and not _NOT_APPROVED.search(columns[i])]
        if len(approved) != 1:
            raise _Skip(f"{len(hits)} {what} columns and not exactly one headed approved")
        hits = approved
    if hits and _NOT_APPROVED.search(columns[hits[0]]) and not _APPROVED.search(columns[hits[0]]):
        raise _Skip(f"the {what} column is headed as claimed, existing or proposed")
    return hits[0] if hits else None


# ------------------------------------------------------------------ shapes of other orders

# a row meant for one licensee of a several-licensee order carries that licensee here; the import
# proposes it under that jurisdiction and removes the key (it is not part of a rule row)
JURISDICTION_KEY = "_jurisdiction"
_CODED_CATEGORY = re.compile(r"^\s*([A-Z]{1,5})\s?[-–]?\s?(\d{1,2}[A-Z]?)\s*:")
# a heading that only announces the rates: 'Tariff', '2. Rate of Charge', 'For FY 2025-26',
# 'The following rates shall apply to consumers of this category'
_LEAD_IN_TITLE = re.compile(
    r"^(tariff(\s+tariff)?|(\d+\.\s*)?rate\s+of\s+charges?|for\s+fy\s*\d{4}\s*-\s*\d{2,4}\s*-?|"
    r"the\s+following\s+rates?\b.*\bshall\s+apply\b.*)$",
    re.I,
)
_TIME_SLOT = re.compile(r"\btime\s+slots?\b", re.I)
_CONSUMPTION_RANGE = re.compile(
    r"\bunits?\b.*\b(per\s+month|monthly|bi-?monthly|for\s+two\s+months)\b|\d\s*(to|-|–)\s*\d+\s*units?\b",
    re.I,
)
_BRACKETED = re.compile(r"\(([^()]*)\)")
_COMPARISONS = (
    ("<=", "up to"),
    ("≤", "up to"),
    (">=", "at or above"),
    ("≥", "at or above"),
    (">", "above"),
    ("<", "below"),
)
_PAYABLE = re.compile(r"\b(leviable|applicable|payable|approved|determined)\b", re.I)
_LICENSEE_HEAD = re.compile(r"^\s*(name\s+of\s+(the\s+)?)?(licensees?|discoms?|utilit(y|ies))\s*$", re.I)
_LEVELS = {"eht": "eht", "ehv": "eht", "ht": "ht", "hv": "ht", "lt": "lt", "lv": "lt"}


def _category_names(category: str) -> list[str]:
    """The names a category answers to: as printed, and its code alone where the order prints the
    code before the name ('HV-3: Other Industrial and General Purpose Non-Industrial' → also 'HV-3',
    'LV- 4: …' → 'LV-4', 'RTS 6: Mixed Load' → 'RTS-6'), which is what a bill prints."""
    m = _CODED_CATEGORY.match(category)
    code = f"{m.group(1)}-{m.group(2)}" if m else category
    return [category] if code == category else [category, code]


def _words_of(text: str) -> set[str]:
    return set(re.findall(r"[a-z]{2,}", text.lower())) - {"and", "of", "the", "for", "or"}


def _restates(descriptor: str, category: str) -> bool:
    """A row heading that only repeats the name of the category it is printed under ('Residential'
    under 'HV-6: Residential') tells one rate from another no further."""
    d, c = _words_of(_CODED_CATEGORY.sub(" ", descriptor)), _words_of(_CODED_CATEGORY.sub(" ", category))
    return bool(d) and (d <= c or (len(c) > 1 and c <= d))


def _category_column(columns: tuple[str, ...]) -> int | None:
    """The first column when it is headed 'Tariff category' (or 'Tariff'): each row then names its
    own category ('High Tension Tariff I') under a heading that covers several, and a cell left
    empty continues the category above it."""
    return 0 if columns and re.fullmatch(r"\s*tariff(\s+category)?\s*", columns[0], re.I) else None


def _slab_guard(descriptor: str) -> None:
    if _CONSUMPTION_RANGE.search(descriptor):
        raise _Skip("a rate for one consumption slab: the tariff_schedule family holds one rate per row")


def _in_words(condition: str) -> str:
    """'Load factor >15%' → 'Load factor above 15%': rulebook.scope matches a rate block on its
    words, so a comparison printed as a sign is written as the words it stands for."""
    out = condition
    for sign, words in _COMPARISONS:
        out = out.replace(sign, f" {words} ")
    return _plain(out)


def _band_and_qualifier(descriptor: str) -> tuple[dict[str, Any] | None, str | None]:
    """The voltage of a row and, where the row prints one condition in brackets beside it
    ('33 kV supply (Load factor >15%)'), that condition in words. A row whose voltage is still not
    read whole is refused as before."""
    try:
        return _band(descriptor), None
    except _Skip:
        inside = _BRACKETED.findall(descriptor)
        if len(inside) != 1 or _KV_NUMBER.search(inside[0]) or not re.search(r"[a-z]{3,}", inside[0], re.I):
            raise
        band = _band(_BRACKETED.sub(" ", descriptor))
        if band is None:
            raise
        return band, _in_words(inside[0])


def _supply_level(text: str) -> list[str] | None:
    """['lt'] for a row or heading that names a supply level and nothing else ('LT', 'HT & EHT')."""
    words = re.findall(r"[a-z]+", re.sub(r"[()]", " ", text).lower())
    kinds = [_LEVELS[w] for w in words if w in _LEVELS]
    rest = [w for w in words if w not in _LEVELS and w not in ("and", "consumers", "consumer", "only")]
    return sorted(set(kinds)) if kinds and not rest else None


def _levels_named(heading: str) -> list[str]:
    """The supply levels a column heading names anywhere in it ('… applicable to HT & LT Consumers')."""
    return sorted({_LEVELS[w] for w in re.findall(r"[a-z]+", heading.lower()) if w in _LEVELS})


def _is_licensee_table(t: SectionTable, order: Order) -> bool:
    """A table of charges with one row per licensee of the order ('Name of the licensee | Cross
    Subsidy Surcharge (P/U) EHT | … | Wheeling Charge P/U applicable to HT Consumers only')."""
    if not t.columns or not _LICENSEE_HEAD.match(t.columns[0]) or len(order.utilities) < 2:
        return False
    if not any(arr_rates.unit_of(c) for c in (*t.columns[1:], t.title)):
        return False  # no heading states a per-unit rate: a table of amounts (Rs. crore), not charges
    names = {u.upper() for u in order.utilities}
    return sum(1 for r in t.rows if r.cells and r.cells[0].strip().upper() in names) >= 2


def _licensee_component(heading: str) -> tuple[str, str] | None:
    if re.search(r"cross[- ]?subsidy\s+surcharge|\bCSS\b", heading, re.I):
        return "cross_subsidy_surcharge", "CSS"
    if re.search(r"additional\s+surcharge", heading, re.I):
        return "additional_surcharge", "Additional surcharge"
    if re.search(r"wheeling\s+charges?", heading, re.I):
        return "wheeling", "Wheeling charge"
    if re.search(r"transmission\s+charges?", heading, re.I):
        return "stu_transmission", "Intra-state transmission charge"
    return None


def _by_licensee_rows(
    src: Source, t: SectionTable, so_far: list[tuple[Any, dict[str, Any]]], out: Mapped
) -> list[dict[str, Any]]:
    """Charges tabulated per licensee: each row goes to its licensee's own jurisdiction. A charge
    printed once in words for all of them (a cell that spans the rows: 'shall pay Rs.5760/MW-day
    (Rs.240/MWh) as transmission charges') goes to the order's jurisdiction. A value a table before
    this one already gave for the same licensee, charge and supply levels is not repeated; where
    this table gives it for more supply levels, the wider row replaces the narrower."""
    if _NOT_APPROVED.search(t.title) or _COMPUTATION.search(t.title):
        raise _Skip("claimed, proposed or computed figures, not the charges payable")
    if not (_PAYABLE.search(t.title) or any(_PAYABLE.search(c) for c in t.columns)):
        raise _Skip("neither the title nor a column says the charges are applicable or approved")
    names = {u.upper() for u in src.order.utilities}
    rows: list[dict[str, Any]] = []
    for j in range(1, len(t.columns)):
        head = t.columns[j]
        column = f"{t.name()} · column '{head[:60]}'"
        what = _licensee_component(head)
        if what is None:
            out.unmapped.append(Unmapped(column, "oa_charge", "no ergPLAN family for this column"))
            continue
        component, short = what
        kinds = _levels_named(head)
        scope: dict[str, Any] = {"connection": list(OA_CONNECTIONS)}
        if kinds:
            scope["consumer_kind"] = kinds
        printed = [r for r in t.rows if j < len(r.cells) and not arr_rates.is_blank(r.cells[j])]
        spanning = [r for r in printed if re.search(r"[a-z]{4,}", r.cells[j], re.I)]
        if spanning:
            # words, not a figure per licensee: read only when printed once, for all the rows
            if len(printed) != 1:
                out.unmapped.append(Unmapped(column, "oa_charge", "charges in words in more than one row"))
                continue
            r = printed[0]
            # the second expression of the same charge, in brackets, is left to the quote; 'Rs.5760'
            # is printed without a space
            got = arr_rates.rate(re.sub(r"\bRs\.(?=\d)", "Rs. ", _BRACKETED.sub(" ", r.cells[j])))
            if got is None:
                out.unmapped.append(Unmapped(column, "oa_charge", f"rate not read: '{r.cells[j][:60]}'"))
                continue
            row = _oa_row(src, t, r, component, got, f"{short} · all licensees of the order", dict(scope))
            row["source_ref"] = _ref(src, t, r.page, [head, r.cells[j]], [f"{t.label} {t.title}".strip()])
            rows.append(row)
            continue
        for r in printed:
            who = r.cells[0].strip().upper()
            if who not in names:
                continue
            got = arr_rates.rate(r.cells[j], heading=head) or arr_rates.rate(r.cells[j], heading=t.title)
            if got is None:
                why = f"rate not read: '{r.cells[j][:60]}' (no unit in the heading or the title)"
                out.unmapped.append(Unmapped(f"{column} · {who}", "oa_charge", why))
                continue
            label = " · ".join(x for x in (short, who, " & ".join(k.upper() for k in kinds)) if x)
            row = _oa_row(src, t, r, component, got, label, dict(scope))
            row["source_ref"] = _ref(
                src, t, r.page, [who, head, r.cells[j]], [f"{t.label} {t.title}".strip()]
            )
            row[JURISDICTION_KEY] = who.lower()
            rows.append(row)
    fresh: list[dict[str, Any]] = []
    narrower: set[int] = set()
    for row in rows:
        mine = set(row["scope"].get("consumer_kind") or [])
        same = [
            (i, set(old["scope"].get("consumer_kind") or []))
            for i, (_, old) in enumerate(so_far)
            if old.get(JURISDICTION_KEY) == row.get(JURISDICTION_KEY)
            and old["body"] == row["body"]
            and {**old["scope"], "consumer_kind": None} == {**row["scope"], "consumer_kind": None}
        ]
        if any(mine <= theirs for _, theirs in same):
            continue
        narrower |= {i for i, theirs in same if theirs < mine}
        fresh.append(row)
    so_far[:] = [x for i, x in enumerate(so_far) if i not in narrower]
    if not rows:
        raise _Skip("no row states a value")
    return fresh


# ------------------------------------------------------------------ tariff schedule


@dataclass
class _Entry:
    """One rate of a category as a table prints it, before the family's row is built."""

    table: SectionTable
    category: str
    descriptor: str  # the row or column heading the rate is printed against
    fixed: tuple[arr_rates.Rate, dict[str, Any]] | None = None
    energy: tuple[arr_rates.Rate, dict[str, Any]] | None = None
    band: dict[str, Any] | None = None
    qualifier: str | None = None  # a condition printed in brackets beside the voltage, in words


def _component_rows(t: SectionTable) -> tuple[Row, Row] | None:
    """The demand and the energy row of a table printed with its components down the side."""
    fixed = [r for r in t.rows if r.cells and _FIXED.search(r.cells[0])]
    energy = [r for r in t.rows if r.cells and _ENERGY.search(r.cells[0])]
    if len(fixed) == 1 and len(energy) == 1 and len(t.rows) == 2:
        return fixed[0], energy[0]
    return None


def _schedule_entries(src: Source, t: SectionTable, out: Mapped) -> list[_Entry]:
    cat = t.category or ""
    entries: list[_Entry] = []
    skipped: list[Unmapped] = []
    if any(_SUBSIDY.search(c) for c in t.columns):
        raise _Skip("the tariff is printed with subsidy columns: several fixed and energy charges per row")
    if any(_SLABBED.search(c) for c in t.columns):
        raise _Skip("energy charge in consumption slabs: the tariff_schedule family holds one rate per row")
    if any(_TIME_SLOT.search(c) for c in t.columns):
        raise _Skip("energy charge by time slot: the tariff_schedule family holds one rate per row")
    sides = _component_rows(t)
    f_col = _one_column(t.columns, _FIXED, "fixed-charge")
    e_col = _one_column(t.columns, _ENERGY, "energy-charge")
    if sides and f_col is None and e_col is None:
        # components down the side, one column per voltage (or a single 'Charges' column)
        fixed_row, energy_row = sides
        for j in range(1, len(t.columns)):
            head = t.columns[j]
            try:
                if j >= len(fixed_row.cells) or j >= len(energy_row.cells):
                    raise _Skip("the row has fewer cells than the table has columns")
                e = _Entry(t, cat, head)
                e.band = _band(head)
                e.fixed = _rate_and_ref(
                    src, t, fixed_row, j, [fixed_row.cells[0], fixed_row.cells[j]], [head]
                )
                e.energy = _rate_and_ref(
                    src, t, energy_row, j, [energy_row.cells[0], energy_row.cells[j]], [head]
                )
                entries.append(e)
            except _Skip as why:
                skipped.append(Unmapped(f"{t.name()} · column '{head}'", "tariff_schedule", str(why)))
        return _or_table_skip(entries, skipped, out)
    if f_col is not None and e_col is not None:
        # one row per voltage or sub-category, a fixed and an energy column
        charges = {i for i, c in enumerate(t.columns) if _FIXED.search(c) or _ENERGY.search(c)}
        own = _category_column(t.columns)
        named = ""
        for r in t.rows:
            others = [c for i, c in enumerate(r.cells) if i not in charges and i != own]
            descriptor = " ".join(c for c in others if c.strip())
            if own is not None and own < len(r.cells) and r.cells[own].strip():
                named = _plain(r.cells[own])
            try:
                if max(f_col, e_col) >= len(r.cells):
                    raise _Skip("the row has fewer cells than the table has columns")
                _slab_guard(descriptor)
                e = _Entry(t, named or cat, descriptor)
                e.band, e.qualifier = _band_and_qualifier(descriptor)
                e.fixed = _rate_and_ref(src, t, r, f_col, list(r.cells))
                e.energy = _rate_and_ref(src, t, r, e_col, list(r.cells))
                entries.append(e)
            except _Skip as why:
                skipped.append(
                    Unmapped(f"{t.name()} · row '{_quote(r.cells)[:80]}'", "tariff_schedule", str(why))
                )
        return _or_table_skip(entries, skipped, out)
    one = _FIXED.search(t.title) is not None, _ENERGY.search(t.title) is not None
    if one in ((True, False), (False, True)) and len(t.columns) == 2:
        # a table of one component by voltage ('(a) Demand Charges'); its pair is joined afterwards
        for r in t.rows:
            try:
                if len(r.cells) < 2:
                    raise _Skip("the row has fewer cells than the table has columns")
                e = _Entry(t, cat, r.cells[0])
                e.band = _band(r.cells[0])
                got = _rate_and_ref(src, t, r, 1, list(r.cells), [t.title])
                if one[0]:
                    e.fixed = got
                else:
                    e.energy = got
                entries.append(e)
            except _Skip as why:
                skipped.append(
                    Unmapped(f"{t.name()} · row '{_quote(r.cells)[:80]}'", "tariff_schedule", str(why))
                )
        return _or_table_skip(entries, skipped, out)
    raise _Skip(f"table shape not recognised (columns: {', '.join(t.columns)[:120]})")


def _or_table_skip(entries: list[_Entry], skipped: list[Unmapped], out: Mapped) -> list[_Entry]:
    """Rows that were not read are listed one by one beside the rows that were; a table none of
    whose rows was read is listed once (it is not a rate table of this shape at all)."""
    if entries or not skipped:
        out.unmapped += skipped
        return entries
    raise _Skip(f"no row states a rate ({len(skipped)} rows; the first: {skipped[0].reason})")


def _rate_and_ref(
    src: Source, t: SectionTable, row: Row, col: int, quote: list[str], lead: tuple[str, ...] | list[str] = ()
) -> tuple[arr_rates.Rate, dict[str, Any]]:
    cell = row.cells[col]
    if arr_rates.is_blank(cell):
        raise _Skip("no fixed or no energy charge is printed for the row")
    r = arr_rates.rate(cell, heading=t.columns[col] if col < len(t.columns) else None)
    if r is None:
        raise _Skip(f"rate not read: '{cell[:60]}'")
    return r.in_rupees(), _ref(src, t, row.page, quote, lead)


def _rate_block(t: SectionTable, descriptor: str, band: dict[str, Any] | None) -> str | None:
    parts = []
    if (
        not _GENERIC_TITLE.search(t.title)
        and not _LEAD_IN_TITLE.search(_plain(t.title))
        and _plain(t.title).lower() != (t.category or "").lower()
    ):
        parts.append(_plain(t.title))
    if band is None and not _is_generic(descriptor) and not _restates(descriptor, t.category or ""):
        parts.append(_plain(descriptor))
    return " · ".join(parts)[:200] or None


def _schedule_rows(src: Source, tables: list[SectionTable], out: Mapped) -> None:
    entries: list[_Entry] = []
    for t in tables:
        try:
            if t.category is None:
                raise _Skip("not printed under a tariff category (a summary or billing-rate table)")
            if t.as_text:
                raise _Skip("rate lines read from running text, not from a ruled table")
            _table_guard(t, src.order, _NOT_APPROVED_SCHEDULE)
            entries += _schedule_entries(src, t, out)
        except _Skip as why:
            out.unmapped.append(Unmapped(t.name(), "tariff_schedule", str(why)))
    # a category printed as a demand table and an energy table: join the halves on the voltage text
    halves: dict[tuple[str, str], _Entry] = {}
    whole: list[_Entry] = []
    for e in entries:
        if e.fixed and e.energy:
            whole.append(e)
            continue
        key = (e.category, re.sub(r"\W+", "", e.descriptor.lower()))
        other = halves.get(key)
        if other is None:
            halves[key] = e
        elif (other.fixed is None) != (e.fixed is None) and abs(other.table.page - e.table.page) <= 1:
            other.fixed, other.energy = other.fixed or e.fixed, other.energy or e.energy
            whole.append(halves.pop(key))
        else:
            out.unmapped.append(Unmapped(e.table.name(), "tariff_schedule", "two tables of the same charge"))
    for e in halves.values():
        out.unmapped.append(
            Unmapped(
                f"{e.table.name()} · '{e.descriptor[:60]}'",
                "tariff_schedule",
                "only a demand or only an energy charge is tabulated for this row",
            )
        )
    # the bands of one table's rows do not overlap
    by_table: dict[str, list[_Entry]] = defaultdict(list)
    for e in whole:
        by_table[e.table.table_id].append(e)
    for group in by_table.values():
        for e, b in zip(group, disjoint([e.band for e in group]), strict=True):
            e.band = b
    rows: list[tuple[_Entry, dict[str, Any]]] = []
    for e in whole:
        assert e.fixed and e.energy
        (fixed, fixed_ref), (energy, energy_ref) = e.fixed, e.energy
        if fixed.unit not in DEMAND_BASIS or energy.unit not in ENERGY_BASIS:
            out.unmapped.append(
                Unmapped(
                    f"{e.table.name()} · '{e.descriptor[:60]}'",
                    "tariff_schedule",
                    f"units outside the family: fixed charge in {fixed.unit}, energy charge in {energy.unit}",
                )
            )
            continue
        block = _rate_block(e.table, e.descriptor, e.band)
        if e.qualifier:
            block = " · ".join(x for x in (block, e.qualifier) if x)[:200]
        scope: dict[str, Any] = {"category": _category_names(e.category)}
        if block:
            scope["rate_block"] = block
        if e.band:
            scope["voltage_kv"] = e.band
        same = fixed_ref == energy_ref
        row: dict[str, Any] = {
            "label": " · ".join(
                x for x in (e.category, block, _plain(e.descriptor) if e.band else None) if x
            )[:200],
            "scope": scope,
            "body": {
                "category": e.category,
                "billing_basis": ENERGY_BASIS[energy.unit],
                "demand_charge": {"value": fixed.text(), "basis": DEMAND_BASIS[fixed.unit]},
                "energy_charge": {"base": energy.text()},
            },
            "source_ref": energy_ref
            if same
            else {**energy_ref, "quote": _quote([fixed_ref["quote"], energy_ref["quote"]])},
            "review": _review(src, e.table),
        }
        if not same:
            row["field_refs"] = {"/demand_charge/value": fixed_ref, "/energy_charge/base": energy_ref}
        rows.append((e, row))
    _keep_unambiguous(rows, "tariff_schedule", out)


def _keep_unambiguous(rows: list[tuple[Any, dict[str, Any]]], family: str, out: Mapped) -> None:
    """Rows whose scope is the same as another's cannot both apply: none of them is kept."""
    seen: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for _, row in rows:
        key = [row["scope"], row["body"].get("component"), row.get(JURISDICTION_KEY)]
        seen[json.dumps(key, sort_keys=True)].append(row)
    for group in seen.values():
        if len(group) == 1:
            out.by_family[family].append(group[0])
            continue
        pages = sorted({r["source_ref"]["page"] for r in group})
        for r in group:
            out.unmapped.append(
                Unmapped(
                    f"order {r['review']['order_id']} · {r['label']} · p. {r['source_ref']['page']}",
                    family,
                    f"{len(group)} tables print a rate for the same scope (pages "
                    f"{', '.join(map(str, pages))}) and their headings do not say which applies",
                )
            )


# ------------------------------------------------------------------ time of day


def _minutes(hhmm: str) -> int:
    return int(hhmm[:2]) * 60 + int(hhmm[3:])


def _span(slots: list[dict[str, Any]]) -> int:
    """Minutes of the day the slots cover (a slot ending at or before its start runs past midnight)."""
    total = 0
    for s in slots:
        a, b = _minutes(s["start"]), _minutes(s["end"])
        total += (b - a) if b > a else (b + 1440 - a)
    return total


def _tod_slots(t: SectionTable) -> list[dict[str, Any]]:
    h_col = next(
        (i for i, c in enumerate(t.columns) if re.search(r"\b(hours?|time|period|slot)", c, re.I)), 0
    )
    v_cols = [i for i in range(len(t.columns)) if i != h_col]
    if len(v_cols) != 1:
        raise _Skip(f"table shape not recognised (columns: {', '.join(t.columns)[:120]})")
    slots = []
    for r in t.rows:
        if len(r.cells) <= max(h_col, v_cols[0]):
            raise _Skip("a row has fewer cells than the table has columns")
        band = arr_rates.hours(r.cells[h_col])
        if band is None:
            raise _Skip(f"hours not read: '{r.cells[h_col][:60]}'")
        cell = r.cells[v_cols[0]]
        slot: dict[str, Any] = {"name": r.cells[h_col].strip(), "start": band[0], "end": band[1]}
        pct = arr_rates.adjustment_pct(cell)
        if pct is None:
            raise _Skip(f"adjustment not read (no sign, or not a percentage): '{cell[:60]}'")
        if pct > 0:
            slot["surcharge_pct"] = format(pct, "f")
        elif pct < 0:
            slot["rebate_pct"] = format(-pct, "f")
        slots.append(slot)
    if not slots:
        raise _Skip("no rows")
    return slots


def _tod_rows(src: Source, tables: list[SectionTable], out: Mapped) -> None:
    parts: list[tuple[SectionTable, list[dict[str, Any]], list[Row]]] = []
    for t in tables:
        try:
            if t.category is None:
                raise _Skip("not printed under a tariff category")
            if t.as_text:
                raise _Skip("slots read from running text, not from a ruled table")
            _table_guard(t, src.order, _NOT_APPROVED_SCHEDULE)
            slots = _tod_slots(t)
        except _Skip as why:
            out.unmapped.append(Unmapped(t.name(), "tod_slots", str(why)))
            continue
        prev = parts[-1] if parts else None
        continues = (
            prev is not None
            and prev[0].category == t.category
            and t.page - prev[0].page == 1
            and _GENERIC_TITLE.search(t.title) is not None
            and prev[0].columns == t.columns
            and _span(prev[1]) < 1440
            and prev[1][-1]["end"] == slots[0]["start"]
            and _span(prev[1] + slots) == 1440
        )
        if continues and prev is not None:
            # the same table carried over a page break: the two parts chain and complete the day
            prev[1].extend(slots)
            prev[2].extend(t.rows)
        else:
            parts.append((t, slots, list(t.rows)))
    rows: list[tuple[Any, dict[str, Any]]] = []
    for t, slots, printed in parts:
        if _span(slots) != 1440:
            hours = Decimal(_span(slots)) / Decimal(60)
            out.unmapped.append(
                Unmapped(
                    t.name(),
                    "tod_slots",
                    f"the slots cover {hours.normalize():f} of 24 hours (a table split across pages, "
                    "or an order that prints only some hours)",
                )
            )
            continue
        months = sorted(_months(t.title) or [])  # calendar order, as the approved rulebooks hold them
        scope: dict[str, Any] = {"category": [t.category]}
        if months:
            scope["months"] = months
        body: dict[str, Any] = {"slots": slots}
        if months:
            body["season_note"] = _plain(t.title)
        title = [] if _GENERIC_TITLE.search(t.title) else [_plain(t.title)]
        pages = sorted({r.page for r in printed})
        ref = _ref(src, t, pages[0], [" ".join(r.cells) for r in printed], title)
        row: dict[str, Any] = {
            "label": " · ".join([str(t.category), "ToD", *title])[:200],
            "scope": scope,
            "body": body,
            "source_ref": ref,
            "review": _review(src, t),
        }
        if len(pages) > 1:
            row["field_refs"] = {
                f"/slots/{i}": _ref(src, t, r.page, list(r.cells))
                for i, r in enumerate(printed)
                if r.page != pages[0]
            }
        rows.append((t, row))
    _keep_unambiguous(rows, "tod_slots", out)


# ------------------------------------------------------------------ open-access charges


def _approved_value_column(t: SectionTable) -> int:
    """The column of a table that holds what the commission approved."""
    heads = [i for i, c in enumerate(t.columns) if re.match(r"\s*approved\b", c, re.I)]
    if len(heads) == 1:
        return heads[0]
    named = [
        i
        for i, c in enumerate(t.columns)
        if i > 0 and _APPROVED.search(c) and not _NOT_APPROVED.search(c) and not re.search(r"\bT\.?\s?O\b", c)
    ]
    if len(named) == 1:
        return named[0]
    if len(t.columns) == 2:
        return 1
    raise _Skip("cannot tell which column is the commission's approved figure")


def _unit_cell_column(t: SectionTable) -> int | None:
    return next((i for i, c in enumerate(t.columns) if re.fullmatch(r"\s*units?\s*", c, re.I)), None)


def _oa_row(
    src: Source,
    t: SectionTable,
    r: Row,
    component: str,
    value: arr_rates.Rate,
    label: str,
    scope: dict[str, Any],
) -> dict[str, Any]:
    v = value.in_rupees()
    if v.unit == Unit.PERCENT:
        basis = "percent_in_kind"
    elif v.unit in OA_BASIS:
        basis = OA_BASIS[v.unit]
    else:
        raise _Skip(f"unit outside the oa_charge family: {v.unit}")
    return {
        "label": label[:200],
        "scope": scope,
        "body": {"component": component, "basis": basis, "value": v.text()},
        "source_ref": _ref(src, t, r.page, list(r.cells), [f"{t.label} {t.title}".strip()]),
        "review": _review(src, t),
    }


def _single_rate(src: Source, t: SectionTable, pattern: str, component: str, label: str) -> dict[str, Any]:
    """A table that works one average charge out ('Average Wheeling charge | Rs. /kWh | 0.9318')."""
    col, unit_col = _approved_value_column(t), _unit_cell_column(t)
    hits = [r for r in t.rows if r.cells and re.search(pattern, r.cells[0], re.I) and len(r.cells) > col]
    rated = []
    for r in hits:
        heading = r.cells[unit_col] if unit_col is not None and unit_col < len(r.cells) else t.columns[col]
        got = arr_rates.rate(r.cells[col], heading=heading) or arr_rates.rate(r.cells[col], heading=t.title)
        if got is not None and got.unit != Unit.PERCENT:
            rated.append((r, got))
    if len(rated) != 1:
        raise _Skip(f"no single per-unit rate in the table ({len(rated)} rows state one)")
    r, got = rated[0]
    return _oa_row(src, t, r, component, got, label, {"connection": list(OA_CONNECTIONS)})


def _by_category_rows(src: Source, t: SectionTable, component: str, short: str) -> list[dict[str, Any]]:
    """A surcharge tabulated per category and voltage ('HV-2 (Supply at 11 kV) | … | 0.38')."""
    col = _approved_value_column(t)
    drafts: list[tuple[Row, arr_rates.Rate, str | None, dict[str, Any] | None, str]] = []
    for r in t.rows:
        if len(r.cells) <= col or arr_rates.is_blank(r.cells[col]):
            continue
        got = arr_rates.rate(r.cells[col], heading=t.columns[col]) or arr_rates.rate(
            r.cells[col], heading=t.title
        )
        if got is None:
            raise _Skip(f"rate not read: '{r.cells[col][:60]}' (no unit in the heading or the title)")
        m = _CATEGORY.search(r.cells[0])
        code = f"{m.group(1)}-{m.group(2)}" if m else None
        rest = _CATEGORY.sub(" ", r.cells[0], count=1) if m else r.cells[0]
        drafts.append((r, got, code, _band(rest), rest))
    by_code: dict[str | None, list[int]] = defaultdict(list)
    for i, d in enumerate(drafts):
        by_code[d[2]].append(i)
    bands: dict[int, dict[str, Any] | None] = {}
    for idx in by_code.values():
        for i, b in zip(idx, disjoint([drafts[i][3] for i in idx]), strict=True):
            bands[i] = b
    rows = []
    for i, (r, got, code, _, rest) in enumerate(drafts):
        scope: dict[str, Any] = {"connection": list(OA_CONNECTIONS)}
        if code:
            scope["category"] = [code]
        if bands[i]:
            scope["voltage_kv"] = bands[i]
        elif _supply_level(rest):
            scope["consumer_kind"] = _supply_level(rest)
        else:
            block = _plain(re.sub(r"[()]", " ", rest))
            if block:
                scope["rate_block"] = block
        label = " · ".join(x for x in (short, _plain(r.cells[0])) if x)
        rows.append(_oa_row(src, t, r, component, got, label, scope))
    if not rows:
        raise _Skip("no row states a value")
    return rows


def _loss_rows(src: Source, t: SectionTable) -> list[dict[str, Any]]:
    rows = []
    for r in t.rows:
        if len(r.cells) < 2:
            continue
        got = arr_rates.rate(r.cells[-1], heading=t.columns[-1])
        if got is None or got.unit != Unit.PERCENT:
            raise _Skip(f"loss not read as a percentage: '{r.cells[-1][:60]}'")
        name = r.cells[0]
        scope: dict[str, Any] = {"connection": list(OA_CONNECTIONS)}
        if re.search(r"inter[- ]?state", name, re.I):
            component = "ists_losses"
        elif re.search(r"intra[- ]?state|transmission", name, re.I):
            component = "stu_losses"
        else:
            component = "wheeling_losses"
            band = _band(name)
            if band is None:
                raise _Skip(f"a loss row names no voltage: '{name[:60]}'")
            scope["voltage_kv"] = band
        rows.append(_oa_row(src, t, r, component, got, _plain(name), scope))
    if not rows:
        raise _Skip("no row states a value")
    return rows


def _oa_rows(src: Source, tables: list[SectionTable], out: Mapped) -> None:
    rows: list[tuple[Any, dict[str, Any]]] = []
    for t in tables:
        title = t.title
        try:
            _table_guard(t, src.order)
            got: list[dict[str, Any]]
            approved = bool(_APPROVED.search(title)) or any(_APPROVED.search(c) for c in t.columns)
            if re.search(r"green\s+(energy\s+)?tariff", title, re.I):
                if not approved:
                    # a tariff the order computes and then caps or changes in its text is not the
                    # tariff payable (UPERC FY 2026-27: 0.48 computed, 0.34 approved in para 8.3.11)
                    raise _Skip("not headed as approved by the commission (computed, or an input)")
                got = [
                    {
                        **_single_rate(
                            src,
                            t,
                            r"green\s+(energy\s+)?tariff.*\b(approved|determined)\b",
                            "green_tariff_premium",
                            "Green tariff · additional to the tariff of the category",
                        ),
                        "scope": {},
                    }
                ]
            elif t.section != "wheeling":
                raise _Skip("no ergPLAN family for this table")
            elif _is_licensee_table(t, src.order):
                got = _by_licensee_rows(src, t, rows, out)
            elif not approved:
                raise _Skip("not headed as approved by the commission (computed, or an input)")
            elif re.search(r"cross[- ]?subsidy\s+surcharge|\bCSS\b", title, re.I) and not _COMPUTATION.search(
                title
            ):
                got = _by_category_rows(src, t, "cross_subsidy_surcharge", "CSS")
            elif re.search(r"additional\s+surcharge", title, re.I) and not _COMPUTATION.search(title):
                got = [
                    _single_rate(
                        src, t, r"additional\s+surcharge", "additional_surcharge", "Additional surcharge"
                    )
                ]
            elif re.search(r"\bloss", title, re.I):
                if _COMPUTATION.search(title) and not _FOR_OA.search(title):
                    raise _Skip(
                        "an input to the surcharge computation, not stated as applying to open access"
                    )
                got = _loss_rows(src, t)
            elif _COMPUTATION.search(title):
                raise _Skip("an input to the surcharge computation, not a charge payable")
            elif re.search(r"wheeling\s+charges?", title, re.I) and not re.search(r"\bARR\b", title):
                got = [_single_rate(src, t, r"wheeling\s+charges?", "wheeling", "Average wheeling charge")]
            elif re.search(r"transmission\s+charges?", title, re.I):
                got = [
                    _single_rate(
                        src,
                        t,
                        r"transmission\s+charges?",
                        "stu_transmission",
                        "Intra-state transmission charge",
                    )
                ]
            else:
                raise _Skip("no ergPLAN family for this table")
            rows += [(t, g) for g in got]
        except _Skip as why:
            out.unmapped.append(Unmapped(t.name(), "oa_charge", str(why)))
    _keep_unambiguous(rows, "oa_charge", out)


# ------------------------------------------------------------------ the order


def map_order(src: Source, sections: dict[str, list[SectionTable]]) -> Mapped:
    """Draft rows per family for one order, and everything that was not mapped with its reason."""
    out = Mapped(facts_read=sum(len(v) for v in sections.values()))
    _schedule_rows(src, sections.get("tariff_schedule", []), out)
    _tod_rows(src, sections.get("tod", []), out)
    _oa_rows(src, [*sections.get("wheeling", []), *sections.get("re_portfolio", [])], out)
    for section, tables in sections.items():
        if section not in MAPPED_SECTIONS:
            out.unmapped += [
                Unmapped(t.name(), section, "no ergPLAN family for this section") for t in tables
            ]
    return out
