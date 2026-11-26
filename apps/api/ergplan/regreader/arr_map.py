"""The ARR Network's section tables → draft rule rows of ergPLAN's families (tariff_schedule,
tod_slots, oa_charge), each value with the order page and the printed cells as its source_ref.

Only the commission's own rate tables are mapped: a table of claimed, existing, proposed or
computed-before-approval figures, a table for another year, a table whose shape or units this
module does not recognise, and every row that cannot be read with confidence is listed in
`Mapped.unmapped` with the reason. Nothing is guessed, converted beyond paise to rupees
(core/units.py) or filled in from another table. No model is involved.
"""

from __future__ import annotations

import calendar
import json
import re
from collections import defaultdict
from dataclasses import dataclass, replace
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
    # a reading given by the person running the import: what a 'unit' is where the order's schedule
    # prices energy 'per unit' on a row whose demand charge is per kVA ('kvah' or 'kwh'); None: such
    # rows are not mapped
    per_unit: str | None = None


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
    text layer broke, such as 'above 66 V kV and up to 132 k'). Two voltages listed with a stroke
    ('132 kV / 220 kV') and no other number are the band between them."""
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
        listed = _listed_band(bare)
        if listed is not None:
            return listed
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
    under 'HV-6: Residential') tells one rate from another no further. A supply level does ('HT'
    beside 'EHV' under 'EHT I and HT I: HT – Industry'), though the heading holds the word."""
    if _supply_level(descriptor) or re.search(r"\d", _CODED_CATEGORY.sub(" ", descriptor)):
        return False  # … and so does a heading with a number of its own ('1 Metered', a load, a serial)
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
    wheeling: arr_rates.Rate | None = None  # the per-unit wheeling charge printed in the same row
    block: str | None = None  # the rate block where the table's group heading gives it ('' = none)
    scope_extra: dict[str, Any] | None = None  # a band of contract demand or load the row is printed for
    qualifier: str | None = None  # a condition printed in brackets beside the voltage, in words
    extra: dict[str, Any] | None = None  # demand slabs, the rate above contract, energy bands (_clause_entry)


def _component_rows(t: SectionTable) -> tuple[Row, Row] | None:
    """The demand and the energy row of a table printed with its components down the side."""
    fixed = [r for r in t.rows if r.cells and _FIXED.search(r.cells[0])]
    energy = [r for r in t.rows if r.cells and _ENERGY.search(r.cells[0])]
    if len(fixed) == 1 and len(energy) == 1 and len(t.rows) == 2:
        return fixed[0], energy[0]
    return None


def _schedule_entries(src: Source, t: SectionTable, out: Mapped) -> list[_Entry]:
    cat = _category_name(t.category or "")
    entries: list[_Entry] = []
    skipped: list[Unmapped] = []
    clauses = _clause_entry(src, t, cat)
    if clauses is not None:
        return [clauses]
    if any(_SUBSIDY.search(c) for c in t.columns):
        raise _Skip("the tariff is printed with subsidy columns: several fixed and energy charges per row")
    if any(_SLABBED.search(c) for c in t.columns) and _unit_slabs(t):
        raise _Skip("energy charge in consumption slabs: the tariff_schedule family holds one rate per row")
    if any(_TIME_SLOT.search(c) for c in t.columns):
        raise _Skip("energy charge by time slot: the tariff_schedule family holds one rate per row")
    grouped = _grouped_entries(src, t)
    if grouped is not None:
        return _or_table_skip(grouped[0], grouped[1], out)
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
        w_col = _one_column(t.columns, _WHEELING_COLUMN, "wheeling-charge")
        charges |= {w_col} if w_col is not None else set()
        lead = [t.title] if _title_year(t.title) else []
        tod_rows = False
        own = _category_column(t.columns)
        named = ""
        for r in t.rows:
            others = [c for i, c in enumerate(r.cells) if i not in charges and i != own]
            descriptor = " ".join(c for c in others if c.strip())
            if own is not None and own < len(r.cells) and r.cells[own].strip():
                named = _plain(r.cells[own])
            try:
                tod_rows = tod_rows or bool(_TOD_MARKER.search(descriptor))
                if tod_rows:
                    raise _Skip("a time-of-day row printed inside the rate table")
                if max(f_col, e_col) >= len(r.cells):
                    raise _Skip("the row has fewer cells than the table has columns")
                _slab_guard(descriptor)
                category = named or cat
                e = _Entry(t, category, _own_descriptor(t, category, descriptor))
                e.band, e.qualifier = _band_and_qualifier(
                    _without_serial(t, r, charges | ({own} if own is not None else set()), descriptor)
                )
                e.fixed = _rate_and_ref(src, t, r, f_col, list(r.cells), lead)
                e.energy = _rate_and_ref(src, t, r, e_col, list(r.cells), lead)
                e.wheeling = _wheeling_of(t, r, w_col, e.energy[0])
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


# ------------------------------------------------------------------ schedules with group headings

# 'HT-I (A): Industry General', 'LT-II (B): Above 50 units/month': a heading that carries its category code
_HEAD_CODE = re.compile(
    r"^\s*((?:LT|HT|EHT|EHV|HV|LV|LMV)\s?-?\s?[IVX\d]+(?:\s?\([A-Za-z]{1,3}\))*(?:\s?\([ivx]+\))*)\s*:\s*(\S.*)$"
)
_SERIAL = re.compile(r"^(?:[A-Z]{1,5}\.?\s?)?\d+(?:\.\s?\d+)*\.?$")  # '3', 'SI.3.2', 'SVI. 3.6'
_FIXED_UNITS = {"", "kw", "kva", "hp", "bhp"}
_NOTE = re.compile(r"\b(shall|note)\b|^\W*\*", re.I)
_HEAD_CUT = re.compile(r"\s+(i\.e\.?|viz\.?|such as|covered under|in accordance with)\b.*$", re.I | re.S)
_PER_UNIT = re.compile(r"\bunit\b", re.I)
_RUNNING_TEXT = re.compile(
    r"[A-Za-z]{20,}|\b(shall|is applicable|are applicable|who wish|will provide)\b"
    r"|^\s*\d{1,2}\.\s+\w|[.,;]\s*$",
    re.I,
)
_NOT_A_CATEGORY = re.compile(
    r"\bterms\s+and\s+conditions\b|\bother\s+charges\b|\bto\s?d\b|\bpeak\b|[A-Za-z]{20,}|[.,;]\s*$", re.I
)
_NAMES_CATEGORY = re.compile(r"\b[A-Z]{1,5}\s?-\s?[IVX\d]+\b|(?i:\b(schedule|category)\b)")
# a heading that starts with a category's short name in capitals, which is not a bare supply level
# ('LT', 'HT consumers …') and not a numeral ('III INDUSTRY')
_SHORT_NAME = re.compile(r"^\s*([A-Z]{2,5})(?=\s|\(|$)")
_YEAR_HEAD = re.compile(r"\btariff\s+for\s+(?:FY\s*)?(\d{4})\s*-\s*(\d{2})\b", re.I)
_PART_OF_YEAR = "a rate for part of the year (a season): the row is not a tariff for every month"
_SLAB_ROW = "a consumption slab: the tariff_schedule family holds one energy rate per row"


def _heading_guard(t: SectionTable) -> None:
    """A table is taken as a category's schedule only when the heading it sits under can be one. The
    site reads headings from the page's lines, and in an order whose schedule is running text it
    takes the end of a sentence or a row label of the table before ('CompositeProtectedWaterSupply(PWS)',
    'Industry, ToD (LGD)', 'HT TARIFFS – TERMS AND CONDITIONS'): the rates under such a heading are
    real, the category is not known, so nothing is mapped."""
    cat = t.category or ""
    if _NOT_A_CATEGORY.search(cat):
        raise _Skip(
            f"printed under '{cat[:60]}': running text, a heading of terms or a time of day, "
            "not a tariff category"
        )
    if _NAMES_CATEGORY.search(cat) or _CODED_CATEGORY.match(cat):
        return  # also 'RTS 6: Mixed Load', 'RTS-4A: …': a code without a hyphen, or with a letter
    short = _SHORT_NAME.match(cat)
    if short and short.group(1).lower() not in _LEVELS and not re.fullmatch(r"[IVX]+", short.group(1)):
        return  # 'HTSS – (33 kV/11 kV)', 'LTIS – I (Demand based)', 'PWW (Demand Based)'
    if not t.label and t.title == cat:
        raise _Skip(
            f"no caption of its own and the heading above it ('{cat[:60]}') carries no category code: "
            "the category is not known"
        )
    if not _GENERIC_TITLE.search(t.title) and _RUNNING_TEXT.search(t.title):
        raise _Skip(
            f"captioned by running text under a heading without a category code ('{cat[:60]}'): "
            "the category is not known"
        )


def _merged_suffix(columns: tuple[str, ...], idx: list[int]) -> str:
    """The words every one of these column headings ends with: the table's first group heading,
    which the site joins to each heading ('Energy Charge (Rs./kVAh) General Industry')."""
    parts = [columns[i].split() for i in idx]
    n = 0
    while all(len(p) > n + 1 for p in parts) and len({p[-1 - n] for p in parts}) == 1:
        n += 1
    suffix = " ".join(parts[0][len(parts[0]) - n :]) if n else ""
    rest = [" ".join(p[: len(p) - n]) for p in parts]
    if not re.search(r"[A-Za-z]{4,}", suffix) or arr_rates.unit_of(suffix) is not None:
        return ""
    if not all(_FIXED.search(x) or _ENERGY.search(x) for x in rest):
        return ""
    return suffix


def _coded(heading: str) -> tuple[str, str] | None:
    m = _HEAD_CODE.match(heading)
    return (re.sub(r"\s+", "", m.group(1)), _plain(m.group(2))) if m else None


def _row_scope(e: _Entry, descriptor: str, base: str | None, voltage_text: str | None = None) -> None:
    """What a row is printed for, from its own first cell: a voltage, a band of contract demand or
    load, or a name (which joins the group heading as the rate block)."""
    if arr_rates.is_slab(descriptor):
        raise _Skip(_SLAB_ROW)
    if _months(descriptor):
        raise _Skip(_PART_OF_YEAR)
    e.band = _band(descriptor if voltage_text is None else voltage_text)
    load = None if e.band else arr_rates.load_band(descriptor)
    if load:
        e.scope_extra = {load[0]: load[1]}
    name = None if e.band or load else _plain(arr_rates._LIST_MARK.sub("", descriptor.strip()))
    e.block = " · ".join(x for x in (base, name) if x)


def _own_heading(category: str | None) -> bool:
    """Whether the heading a table is filed under is one category's own: its code, a colon, its name."""
    return bool(category) and bool(_coded(str(category)) or _CODED_CATEGORY.match(str(category)))


def _grouped_entries(src: Source, t: SectionTable) -> tuple[list[_Entry], list[Unmapped]] | None:
    """A schedule printed as one table with group headings: a row without rates names the group
    ('HT-I (A): Industry General', 'Power Intensive Units …') and the rows under it are its voltages,
    demand bands or sub-categories. The fixed charge may have its unit in a column of its own
    ('kVA' | '500'). None when the table is not of this shape (the plain shapes are read as before)."""
    fixed_cols = [i for i, c in enumerate(t.columns) if _FIXED.search(c)]
    energy_cols = [i for i, c in enumerate(t.columns) if _ENERGY.search(c)]
    if len(energy_cols) != 1 or len(fixed_cols) not in (1, 2):
        return None
    e_col, u_col = energy_cols[0], None
    if len(fixed_cols) == 2:
        both = _merged_suffix(t.columns, [*fixed_cols, e_col])
        heads = {i: t.columns[i][: len(t.columns[i]) - len(both)].strip() for i in fixed_cols}
        units = [i for i in fixed_cols if re.search(r"\bunit$", heads[i], re.I)]
        if len(units) != 1:
            return None
        u_col = units[0]
        if any(len(r.cells) > u_col and r.cells[u_col].strip().lower() not in _FIXED_UNITS for r in t.rows):
            return None
        fixed_cols.remove(u_col)
    if u_col is None and (_category_column(t.columns) is not None or _own_heading(t.category)):
        # every row names its own category in a first column headed 'Tariff', or the table sits
        # under the heading of one category ('LV-5: L.V. Industry', 'RTS-1: Domestic') and its rows
        # are that category's sub-categories: the plain shape reads both, row by row
        return None
    f_col = fixed_cols[0]
    for i in (f_col, e_col):
        if _NOT_APPROVED.search(t.columns[i]) and not _APPROVED.search(t.columns[i]):
            return None
    rate_cols = {c for c in (u_col, f_col, e_col) if c is not None}
    suffix = _merged_suffix(t.columns, sorted(rate_cols))
    e_head = t.columns[e_col][: len(t.columns[e_col]) - len(suffix)]
    f_head = t.columns[f_col][: len(t.columns[f_col]) - len(suffix)]

    def read(r: Row, col: int, heading: str) -> arr_rates.Rate:
        got = arr_rates.rate(r.cells[col], heading=heading)
        if got is None:
            raise _Skip(f"rate not read: '{r.cells[col][:60]}'")
        return got.in_rupees()

    def descr(r: Row) -> str:
        cells = [c.strip() for i, c in enumerate(r.cells) if i not in rate_cols]
        return " ".join(c for c in cells if c and not _SERIAL.match(c))

    def blank(r: Row) -> bool:
        return all(i >= len(r.cells) or arr_rates.is_blank(r.cells[i]) for i in (f_col, e_col))

    if u_col is None and not suffix and not any(blank(r) and descr(r) for r in t.rows):
        return None
    carried = t.category if t.category and _coded(t.category) and not suffix else None
    group: str | None = suffix or carried
    if group and (_NOTE.search(group) or len(group) > 300):
        group = None  # a note the site joined to the headings, not a group
    group_page: int | None = None
    entries: list[_Entry] = []
    skipped: list[Unmapped] = []
    for r in t.rows:
        d = descr(r)
        if blank(r):
            if d:
                group, group_page = (None if _NOTE.search(d) or len(d) > 300 else d), r.page
            continue
        try:
            if max(rate_cols) >= len(r.cells):
                raise _Skip("the row has fewer cells than the table has columns")
            own = _coded(d)
            head = _coded(group) if group and not own else None
            if own:
                e = _Entry(t, own[0], d, block=own[1])
                group = None  # a rate row with its own code is a category by itself
            elif head:
                e = _Entry(t, head[0], d)
                _row_scope(e, d, head[1])
            elif t.category and not _coded(t.category):
                e = _Entry(t, t.category, d)
                _row_scope(e, d, _plain(_HEAD_CUT.sub("", group)) if group else None)
            else:
                raise _Skip("the row is printed under no heading that names its category")
            quote = list(r.cells)
            lead = [group] if group and group_page == r.page else []
            if arr_rates.is_blank(r.cells[f_col]) or arr_rates.is_blank(r.cells[e_col]):
                raise _Skip("no fixed or no energy charge is printed for the row")
            ref = _ref(src, t, r.page, quote, lead)
            if u_col is None:
                e.fixed = read(r, f_col, f_head), ref
            else:
                fixed = arr_rates.rate(f"Rs. {r.cells[f_col]} / {r.cells[u_col]} / month")
                if fixed is None:
                    raise _Skip(f"rate not read: '{r.cells[f_col][:40]}' per '{r.cells[u_col][:10]}'")
                e.fixed = fixed, ref
            energy = read(r, e_col, e_head)
            if _PER_UNIT.search(e_head) and arr_rates.unit_of(r.cells[e_col]) is None:
                # 'Rs./Unit': a kWh or a kVAh, which the table does not say
                on_kva = e.fixed[0].unit == Unit.INR_PER_KVA_MONTH
                if not (on_kva and src.per_unit in ("kvah", "kwh")):
                    raise _Skip(
                        "energy charge 'per unit' and the table does not say kWh or kVAh"
                        + (
                            " (rows with a demand charge per kVA take the reading --per-unit)"
                            if on_kva
                            else ""
                        )
                    )
                if src.per_unit == "kvah":
                    energy = arr_rates.Rate(energy.value, Unit.INR_PER_KVAH)
            e.energy = energy, ref
            entries.append(e)
        except _Skip as why:
            skipped.append(
                Unmapped(f"{t.name()} · row '{_quote(r.cells)[:80]}'", "tariff_schedule", str(why))
            )
    return entries, skipped


def _year_entries(src: Source, t: SectionTable) -> tuple[list[_Entry], list[Unmapped]] | None:
    """A schedule set side by side for two tariff years ('Tariff for 2025-26 (W.E.F. …)' | 'Tariff
    for 2026-27 (W.E.F. …)'): only the columns of the order's own year are read, a name and a cell
    that prints the energy and the fixed charge together. A numbered row without rates is the
    category. None when the table is not of this shape."""
    years = {
        i: f"FY{m.group(1)}-{m.group(2)}" for i, c in enumerate(t.columns) if (m := _YEAR_HEAD.search(c))
    }
    if len(set(years.values())) < 2:
        return None
    _table_guard(t, src.order, _NOT_APPROVED_SCHEDULE)
    mine = [i for i, y in years.items() if y == src.order.arr_year]
    if len(mine) != 2 or not (_ENERGY.search(t.columns[mine[1]]) and _FIXED.search(t.columns[mine[1]])):
        raise _Skip(
            "set side by side for several years, and the columns of the order's own year are not a name "
            "and a rate"
        )
    d_col, v_col = mine
    group: str | None = None
    group_page: int | None = None
    entries: list[_Entry] = []
    skipped: list[Unmapped] = []
    for r in t.rows:
        if len(r.cells) <= v_col:
            continue
        d, v = r.cells[d_col].strip(), r.cells[v_col].strip()
        if not d:
            numbered = bool(_SERIAL.match(r.cells[0].strip()))
            if v:  # a numbered heading is a category; any other line ends it (a sub-heading, a broken row)
                rated = re.search(r"\d\s*/\s*(kwh|kvah|kw|kva|unit|bhp)", v, re.I)
                group, group_page = (v if numbered and not rated else None), r.page
            continue
        try:
            if group is None:
                raise _Skip("the row is printed under no numbered heading that names its category")
            if arr_rates.is_slab(d):
                raise _Skip(_SLAB_ROW)
            got = arr_rates.joined(v, t.columns[v_col])
            if got is None:
                raise _Skip(f"an energy and a fixed charge not read from one cell: '{v[:60]}'")
            e = _Entry(t, group, d)
            at = re.sub(r"(\d+)\s*(?:/|or)\s*(\d+)\s*kv\b", r"from \1 kV to \2 kV", d, flags=re.I)
            name = _plain(re.sub(r"\(?\s*supply\s+at\s+[^()]*k\s*v\s*\)?", " ", d, flags=re.I))
            _row_scope(e, name or d, None, at)
            if e.band:
                e.block = name  # '' for a plain 'Supply at 11 kV'; 'Arc furnaces …' where the row names more
            ref = _ref(src, t, r.page, [d, v], [group] if group_page == r.page else [])
            e.energy, e.fixed = (got[0].in_rupees(), ref), (got[1], ref)
            entries.append(e)
        except _Skip as why:
            skipped.append(
                Unmapped(f"{t.name()} · row '{_quote(r.cells)[:80]}'", "tariff_schedule", str(why))
            )
    return entries, skipped


def _rate_and_ref(
    src: Source, t: SectionTable, row: Row, col: int, quote: list[str], lead: tuple[str, ...] | list[str] = ()
) -> tuple[arr_rates.Rate, dict[str, Any]]:
    cell = row.cells[col]
    if arr_rates.is_blank(cell):
        raise _Skip("no fixed or no energy charge is printed for the row")
    heading = t.columns[col] if col < len(t.columns) else None
    r = arr_rates.rate(cell, heading=heading) or arr_rates.flat_rate(cell, heading)
    if r is None and heading is not None:
        r = arr_rates.rate_under(cell, heading)
    if r is None:
        raise _Skip(f"rate not read: '{cell[:60]}'")
    return r.in_rupees(), _ref(src, t, row.page, quote, lead)


def _rate_block(t: SectionTable, descriptor: str, band: dict[str, Any] | None) -> str | None:
    parts = []
    generic = (
        _GENERIC_TITLE.search(t.title)
        or _title_year(t.title)
        or _LEAD_IN_TITLE.search(_plain(t.title))
        or _NO_BLOCK_TITLE.search(t.title)
    )
    if not generic and _plain(t.title).lower() != (t.category or "").lower():
        parts.append(_plain(t.title))
    if band is None and not _is_generic(descriptor) and not _restates(descriptor, t.category or ""):
        parts.append(_plain(descriptor))
    return " · ".join(parts)[:200] or None


def _schedule_rows(src: Source, tables: list[SectionTable], out: Mapped) -> None:
    entries: list[_Entry] = []
    tables, undated, _ = _year_runs(tables)
    misfiled = _misfiled(tables)
    for t in tables:
        try:
            by_year = None if t.as_text else _year_entries(src, t)
            if by_year is not None:
                entries += _or_table_skip(by_year[0], by_year[1], out)
                continue
            if t.category is None:
                raise _Skip("not printed under a tariff category (a summary or billing-rate table)")
            if t.table_id in undated:
                raise _Skip(undated[t.table_id])
            if t.as_text:
                raise _Skip("rate lines read from running text, not from a ruled table")
            _heading_guard(t)
            _filing_guard(t, misfiled)
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
            other.extra = {**(other.extra or {}), **(e.extra or {})} or None
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
        block = _rate_block(e.table, e.descriptor, e.band) if e.block is None else (e.block[:200] or None)
        if e.qualifier:
            block = " · ".join(x for x in (block, e.qualifier) if x)[:200]
        names = _category_names(e.category)
        scope: dict[str, Any] = {
            "category": [*names, *(c for c in _category_codes(e.category) if c not in names)]
        }
        if block:
            scope["rate_block"] = block
        if e.band:
            scope["voltage_kv"] = e.band
        scope.update(e.scope_extra or {})
        same = fixed_ref == energy_ref
        row: dict[str, Any] = {
            "label": " · ".join(
                x for x in (e.category, block, _plain(e.descriptor) if e.band or e.scope_extra else None) if x
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
        if e.wheeling is not None:
            row["body"]["retail_wheeling_charge"] = e.wheeling.text()
        if e.extra:
            _apply_clauses(row, e.extra)
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
    if any(_NOT_APPROVED.search(c) and not _APPROVED.search(c) for c in t.columns):
        raise _Skip("claimed, existing or proposed figures, not the commission's approved rate")
    v_cols = _adjustment_columns(t, [i for i in range(len(t.columns)) if i != h_col])
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
            pct = arr_rates.of_normal_pct(cell)
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
        scope: dict[str, Any] = _tod_scope(str(t.category))
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
    figures = [i for i, c in enumerate(t.columns) if not _LABEL_HEAD.match(c.strip())]
    if len(figures) == 1:
        return figures[0]  # serial, particulars, reference and unit columns beside one column of figures
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
    hits = [
        r for r in t.rows if r.cells and re.search(pattern, _particulars(t, r), re.I) and len(r.cells) > col
    ]
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
        t = _year_view(t, src.order.arr_year) or t
        try:
            _table_guard(t, src.order)
            got: list[dict[str, Any]]
            approved = (
                bool(_APPROVED.search(title))
                or any(_APPROVED.search(c) for c in t.columns)
                or bool(_DETERMINATION.match(title))
            )
            if re.search(r"green\s+energy\s+charges?", title, re.I) and approved:
                got = [_headed_rate(src, t, r"green\s+energy\s+charges?", "green_tariff_premium")]
            elif re.search(r"green\s+(energy\s+)?tariff", title, re.I):
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
                got = _loss_rows_by_level(src, t, out) or _loss_rows(src, t)
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


# ------------------------------------------------------------------ orders that set several years


_WEF_YEAR = re.compile(r"\b1(?:st)?\s+april,?\s+(\d{4})\s+to\s+31(?:st)?\s+march,?\s+(\d{4})\b", re.I)
_FY_TOKEN = re.compile(r"\bFY\s*(\d{4}|\d{2})\s*[-–]\s*(\d{2})\b", re.I)
_WHEELING_COLUMN = re.compile(r"\bwheeling\s+charges?\b", re.I)
_TOD_MARKER = re.compile(r"\btod\s+tariff\b", re.I)
_LOAD_UNIT = re.compile(r"\b(kw|kva|hp|bhp|kv|mw|mva)\b", re.I)
_ALL_UNITS = re.compile(r"all\s+(units?|consumption)", re.I)
_CODE = re.compile(r"\b(E?HT|EHV|LT)\s*[-–]?\s*([IVX]{1,5})\b(?:\s*\(\s*([A-Za-z])\s*\))?")
_UNDATED = "the order prints one table for each tariff year and this table does not say which year it is for"


def _fy(start: int) -> str:
    return f"FY{start}-{(start + 1) % 100:02d}"


def _title_year(title: str) -> str | None:
    """'Tariff w.e.f. 1 April, 2025 to 31 March, 2026' → 'FY2025-26'; None for any other title."""
    m = _WEF_YEAR.search(title)
    if not m or int(m.group(2)) != int(m.group(1)) + 1:
        return None
    return _fy(int(m.group(1)))


def _years_named(text: str) -> set[str]:
    """The tariff years a heading names: 'FY 2025-26' → {FY2025-26}; 'FY 25-26' likewise;
    'FY 2027-28 to FY 2029-30' → the three years. Empty when it names none."""
    found: list[tuple[int, int, int]] = []
    for m in _FY_TOKEN.finditer(text):
        start = int(m.group(1)) + (2000 if len(m.group(1)) == 2 else 0)
        if (start + 1) % 100 == int(m.group(2)):
            found.append((m.start(), m.end(), start))
    years: set[int] = set()
    for i, (_, end, start) in enumerate(found):
        years.add(start)
        nxt = found[i + 1] if i + 1 < len(found) else None
        if nxt and re.fullmatch(r"\s*to\s*", text[end : nxt[0]], re.I) and nxt[2] > start:
            years.update(range(start, nxt[2] + 1))
    return {_fy(y) for y in years}


def _category_codes(heading: str) -> list[str]:
    """The category codes a heading starts with, in one spelling: 'EHT I and HT I: HT – Industry' →
    ['EHT I', 'HT I']; 'HT - II Commercial' → ['HT II']; 'LT III(B): Above 20kW' → ['LT III (B)'].
    Empty where the heading is not headed by such a code, or is nothing but the code ('HV-2', 'HT-I(A)')."""
    head = heading.split(":")[0] if ":" in heading else heading
    codes: list[str] = []
    for m in _CODE.finditer(head):
        code = f"{m.group(1)} {m.group(2)}" + (f" ({m.group(3).upper()})" if m.group(3) else "")
        if code not in codes and _key(code) != _key(heading):  # a heading that is the code adds nothing
            codes.append(code)
    return codes


def _key(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", text.lower())


@dataclass
class _Run:
    """The tables of one category in an order that sets several years: one table per tariff year,
    each titled with its year ('Tariff w.e.f. 1 April, 2026 to 31 March, 2027'), under one heading."""

    category: str | None
    columns: str
    year: int
    labels: set[str]
    first_page: int
    last_page: int

    def has_label(self, key: str) -> bool:
        """Whether a heading is in truth a row label of these tables (whole, or cut short)."""
        return bool(key) and any(lb == key or (len(key) >= 12 and lb.startswith(key)) for lb in self.labels)


def _year_runs(tables: list[SectionTable]) -> tuple[list[SectionTable], dict[str, str], list[_Run]]:
    """Tables titled with a tariff year get that year, and the tables of the later years get the
    category the first year's table is printed under (the heading is printed once, above the first
    table; the ARR Network files the later tables under the last row label it saw, such as 'HT').
    Returns the tables, the reason for every table whose year is not stated in an order that titles
    its tables by year, and the runs. An order without such titles comes back unchanged."""
    out: list[SectionTable] = []
    runs: list[_Run] = []
    run: _Run | None = None
    for t in tables:
        m = _WEF_YEAR.search(t.title)
        if not m or _title_year(t.title) is None:
            out.append(t)
            continue
        year, columns, own = int(m.group(1)), _key("|".join(t.columns)), _key(t.category or "")
        labels = {_key(r.cells[0]) for r in t.rows if r.cells and r.cells[0].strip()}
        pages = [t.page, *(r.page for r in t.rows)]
        row_label = run is not None and run.has_label(own) and own != _key(run.category or "")
        same_heading = run is not None and (row_label or (own != "" and own == _key(run.category or "")))
        if run is not None and same_heading and columns == run.columns and year > run.year:
            run.year, run.last_page = year, max(run.last_page, *pages)
            run.labels |= labels
        else:
            run = _Run(None if row_label else t.category, columns, year, labels, min(pages), max(pages))
            runs.append(run)
        out.append(replace(t, category=run.category, fiscal_year=t.fiscal_year or _fy(year)))
    undated = {t.table_id: _UNDATED for t in out if runs and t.fiscal_year is None}
    return out, undated, runs


def _unit_slabs(t: SectionTable) -> bool:
    """Whether a column headed 'Consumption Slab' really prints consumption slabs ('0-100 units',
    '101 – 300'): MERC heads the first column so in every table, also where the rows are supply
    levels ('EHV', 'HT'), 'All Units' or load ranges ('(B) >20 kW and ≤ 50 kW')."""
    col = next(i for i, c in enumerate(t.columns) if _SLABBED.search(c))
    for r in t.rows:
        cell = r.cells[col] if col < len(r.cells) else ""
        if _TOD_MARKER.search(cell):
            break  # time-of-day rows follow: their hours are not slabs
        if re.search(r"\d", cell) and not _LOAD_UNIT.search(cell):
            return True
    return False


def _own_descriptor(t: SectionTable, category: str, descriptor: str) -> str:
    """What a row's first cells add to the category: nothing for 'All Units', nothing where the
    table's only row repeats the category ('HT - I Industry' under 'HT I: Industry'). A row that
    names another category than the heading it is printed under is not mapped."""
    if _ALL_UNITS.fullmatch(_plain(descriptor)):
        return ""
    named, heading = _category_codes(descriptor), _category_codes(category)
    if named and heading:
        if not any(n == h or n.startswith(h + " (") for n in named for h in heading):
            raise _Skip(f"the row names another category ('{descriptor[:40]}') than its heading")
        if len(t.rows) == 1:
            return ""
    return descriptor


def _wheeling_of(t: SectionTable, r: Row, col: int | None, energy: arr_rates.Rate) -> arr_rates.Rate | None:
    """The wheeling charge printed beside the energy charge (MERC schedules), in the same unit."""
    if col is None or col >= len(r.cells) or arr_rates.is_blank(r.cells[col]):
        return None
    got = arr_rates.rate(r.cells[col], heading=t.columns[col])
    if got is None:
        raise _Skip(f"wheeling charge not read: '{r.cells[col][:60]}'")
    if got.in_rupees().unit != energy.unit:
        raise _Skip("the wheeling charge is printed in another unit than the energy charge")
    return got.in_rupees()


def _year_view(t: SectionTable, year: str) -> SectionTable | None:
    """A table with a column per tariff year ('Particulars | FY 2025-26 | FY 2026-27 | …') as the
    two-column table of one year: the row's name and that year's figure. A column headed as the
    petition's, claimed, projected or proposed is never taken. None when the table is not of this
    shape, or when the year has not exactly one column left (a rate printed per kWh and per kVAh)."""
    named = {i: _years_named(c) for i, c in enumerate(t.columns) if i > 0 and _years_named(c)}
    if len(named) < 2:
        return None
    cols = [i for i, years in named.items() if year in years]
    cols = [i for i in cols if not re.search(r"\bpetition\b", t.columns[i], re.I)]
    cols = [i for i in cols if _APPROVED.search(t.columns[i]) or not _NOT_APPROVED.search(t.columns[i])]
    if len(cols) != 1:
        return None
    col = cols[0]
    rows = tuple(Row((r.cells[0], r.cells[col]), r.page) for r in t.rows if len(r.cells) > col)
    return replace(t, columns=(t.columns[0], t.columns[col]), rows=rows, fiscal_year=None)


_LEVEL_LOSS = re.compile(r"\s*(EHV|EHT|HT|LT)\s+loss(es)?\s*", re.I)


def _loss_rows_by_level(src: Source, t: SectionTable, out: Mapped) -> list[dict[str, Any]]:
    """Wheeling losses a table states by supply level ('HT Loss | 1.44%', 'LT Loss | 5.36%'), the
    level kept as printed (which voltages are 'HT' is in the order's text, not in the table). Empty
    unless every row of the table is such a row; a row whose figure is not read is listed."""
    if not t.rows or not all(len(r.cells) >= 2 and _LEVEL_LOSS.fullmatch(r.cells[0]) for r in t.rows):
        return []
    rows = []
    for r in t.rows:
        got = arr_rates.rate(r.cells[-1], heading=t.columns[-1])
        if got is None or got.unit != Unit.PERCENT:
            why = f"loss not read as a percentage: '{r.cells[-1][:60]}'"
            out.unmapped.append(Unmapped(f"{t.name()} · row '{_quote(r.cells)[:80]}'", "oa_charge", why))
            continue
        m = _LEVEL_LOSS.fullmatch(r.cells[0])
        assert m is not None
        scope = {"connection": list(OA_CONNECTIONS), "rate_block": m.group(1).upper()}
        rows.append(_oa_row(src, t, r, "wheeling_losses", got, _plain(r.cells[0]), scope))
    return rows


def _tod_category(t: SectionTable, runs: list[_Run]) -> str:
    """The category a time-of-day table belongs to. Where the ARR Network filed it under a row label
    of the rate tables ('HT'), it follows the tables of one category directly: the category whose
    tables end on its page, else the one that ended on the page before."""
    own = _key(t.category or "")
    for run in runs:
        if run.category and own == _key(run.category):
            return run.category  # the heading as the rate tables carry it
    if not any(run.has_label(own) for run in runs):
        return str(t.category)
    here = [run for run in runs if run.last_page == t.page]
    if len(here) > 1:
        raise _Skip("filed under a row label and the rate tables of two categories end on its page")
    if here:
        owner = here[0]
    else:
        before = [run for run in runs if run.last_page < t.page]
        inside = [run for run in runs if run.first_page <= t.page <= run.last_page]
        if not before or inside or t.page - before[-1].last_page > 1:
            raise _Skip("filed under a row label and no category's rate tables end just before it")
        owner = before[-1]
    if owner.category is None:
        raise _Skip("not printed under a tariff category")
    return owner.category


def _tod_year_table(
    src: Source, t: SectionTable, h_col: int, year_cols: dict[int, set[str]], runs: list[_Run]
) -> list[dict[str, Any]]:
    """One row per season from a table that prints the adjustment of every slot in a column per
    tariff year ('FY 2025-26 | FY 2026-27 | FY 2027-28 to FY 2029-30'), a season inside the cell
    ('-15% (April to September) -25% (October to March)')."""
    if t.category is None:
        raise _Skip("not printed under a tariff category")
    _table_guard(replace(t, fiscal_year=None), src.order, _NOT_APPROVED_SCHEDULE)
    cols = [i for i, years in year_cols.items() if src.order.arr_year in years]
    if len(cols) != 1:
        raise _Skip(f"{len(cols)} columns are headed {src.order.arr_year}")
    col = cols[0]
    if _NOT_APPROVED.search(t.columns[col]) and not _APPROVED.search(t.columns[col]):
        raise _Skip("the column of the year is headed as claimed, existing or proposed")
    category = _tod_category(t, runs)
    read: list[tuple[Row, tuple[str, str], Decimal | None, list[tuple[Decimal, list[int]]] | None]] = []
    for r in t.rows:
        if len(r.cells) <= max(h_col, col):
            raise _Skip("a row has fewer cells than the table has columns")
        band = arr_rates.hours(r.cells[h_col])
        if band is None:
            raise _Skip(f"hours not read: '{r.cells[h_col][:60]}'")
        cell = r.cells[col]
        pct = arr_rates.adjustment_pct(cell)
        seasonal = None if pct is not None else arr_rates.seasonal_adjustments(cell)
        if pct is None and seasonal is None:
            raise _Skip(
                f"adjustment not read (no sign, not a percentage, or part of the cell missing): '{cell[:60]}'"
            )
        read.append((r, band, pct, seasonal))
    if not read:
        raise _Skip("no rows")
    seasons = {tuple(tuple(months) for _, months in s) for _, _, _, s in read if s}
    if len(seasons) > 1:
        raise _Skip("the slots of one table split the year into different seasons")
    rows: list[dict[str, Any]] = []
    for months in next(iter(seasons)) if seasons else [None]:
        slots: list[dict[str, Any]] = []
        for r, band, pct, seasonal in read:
            value = pct if seasonal is None else next(p for p, ms in seasonal if tuple(ms) == months)
            assert value is not None
            slot: dict[str, Any] = {"name": r.cells[h_col].strip(), "start": band[0], "end": band[1]}
            if value > 0:
                slot["surcharge_pct"] = format(value, "f")
            elif value < 0:
                slot["rebate_pct"] = format(-value, "f")
            slots.append(slot)
        if _span(slots) != 1440:
            hours = Decimal(_span(slots)) / Decimal(60)
            raise _Skip(
                f"the slots cover {hours.normalize():f} of 24 hours (a table split across pages, "
                "or an order that prints only some hours)"
            )
        scope: dict[str, Any] = {"category": [category, *_category_codes(category)]}
        body: dict[str, Any] = {"slots": slots}
        note = ""
        if months:
            scope["months"] = sorted(months)
            note = f"{calendar.month_name[months[0]]} to {calendar.month_name[months[-1]]}"
            body["season_note"] = note
        pages = sorted({r.page for r, _, _, _ in read})
        printed = [
            " ".join(c for c in (r.cells[0], r.cells[h_col], r.cells[col]) if c.strip("- ")) for r, *_ in read
        ]
        row: dict[str, Any] = {
            "label": " · ".join(x for x in (category, "ToD", src.order.arr_year, note) if x)[:200],
            "scope": scope,
            "body": body,
            "source_ref": _ref(src, t, pages[0], printed, [t.columns[col]]),
            "review": _review(src, t),
        }
        if len(pages) > 1:
            row["field_refs"] = {
                f"/slots/{i}": _ref(src, t, r.page, list(r.cells))
                for i, (r, *_) in enumerate(read)
                if r.page != pages[0]
            }
        rows.append(row)
    return rows


def _tod_year_shape(t: SectionTable) -> tuple[int, dict[int, set[str]]] | None:
    """The hours column and the columns per tariff year of a time-of-day table of that shape."""
    year_cols = {i: _years_named(c) for i, c in enumerate(t.columns) if _years_named(c)}
    h_col = next(
        (
            i
            for i, c in enumerate(t.columns)
            if i not in year_cols and re.search(r"\b(period|hours?|time)\b", c, re.I)
        ),
        None,
    )
    if len(year_cols) < 2 or h_col is None or t.as_text:
        return None
    return h_col, year_cols


def _over_the_page(a: SectionTable, b: SectionTable) -> SectionTable | None:
    """A table and its continuation on the next page as one table. Where the page break cut the
    last row, the ARR Network has the rest of each cut cell at the end of the continuation's column
    heading ('… FY 2025-26 -25% (October to March)', 'Period 09:00 hrs'): it is put back after the
    cell it was cut from. None unless b is on the next page, under the same heading, with the same
    columns (apart from such a rest). Whether the joined rows make sense is for the caller's reading:
    every cell must still read whole and the slots must make one day."""
    if b.page != a.page + 1 or b.category != a.category or len(a.columns) != len(b.columns) or not a.rows:
        return None
    rest: list[str] = []
    for ca, cb in zip(a.columns, b.columns, strict=True):
        na, nb = " ".join(ca.split()), " ".join(cb.split())
        if not nb.startswith(na):
            return None
        rest.append(nb[len(na) :].strip())
    last = a.rows[-1]
    if any(rest) and len(last.cells) != len(a.columns):
        return None
    cells = (
        tuple(f"{c} {r}".strip() if r else c for c, r in zip(last.cells, rest, strict=True))
        if any(rest)
        else last.cells
    )
    return replace(a, rows=(*a.rows[:-1], Row(cells, last.page), *b.rows))


def _tod_by_year_rows(
    src: Source, tables: list[SectionTable], schedule: list[SectionTable], out: Mapped
) -> list[SectionTable]:
    """Maps the time-of-day tables that have a column per tariff year and returns the others (for
    `_tod_rows`, which reads a table of one year). A table that does not read whole on its own is
    tried together with its continuation on the next page, which the ARR Network may have filed in
    the tariff-schedule section."""
    rest = [t for t in tables if _tod_year_shape(t) is None]
    own = {t.table_id for t in tables}
    parts = [t for t in [*tables, *schedule] if _tod_year_shape(t) is not None]
    parts.sort(key=lambda t: t.page)  # stable: the order of the section pages within a page
    rows: list[tuple[Any, dict[str, Any]]] = []
    runs = _year_runs(schedule)[2] if parts else []
    used: set[str] = set()
    for i, t in enumerate(parts):
        if t.table_id in used:
            continue
        shape = _tod_year_shape(t)
        assert shape is not None
        try:
            rows += [(t, row) for row in _tod_year_table(src, t, *shape, runs)]
            used.add(t.table_id)
            continue
        except _Skip as why:
            alone = str(why)
        for nxt in parts[i + 1 :]:
            joined = _over_the_page(t, nxt) if nxt.table_id not in used else None
            if joined is None:
                continue
            try:
                rows += [(t, row) for row in _tod_year_table(src, joined, *shape, runs)]
            except _Skip:
                continue
            used |= {t.table_id, nxt.table_id}
            break
        if t.table_id not in used and t.table_id in own:
            out.unmapped.append(Unmapped(t.name(), "tod_slots", alone))
    # a part filed in the tariff-schedule section and read here is no longer an unread table there
    names = {t.name() for t in parts if t.table_id in used and t.table_id not in own}
    out.unmapped[:] = [u for u in out.unmapped if not (u.family == "tariff_schedule" and u.fact_id in names)]
    _keep_unambiguous(rows, "tod_slots", out)
    return rest


# ------------------------------------------------------------------ what other schedules print
# (shapes met in the Gujarat, Madhya Pradesh and Bihar orders; nothing here names a state)

_RATE_PREFIX = re.compile(r"^\s*rate\s*:\s*", re.I)
_ITEM = re.compile(r"^\(?([a-z]|\d{1,2}|[ivx]{1,4})\)$", re.I)
_ITEM_LEAD = re.compile(r"^\(?([a-z]|\d{1,2}|[ivx]{1,4})\)\s+", re.I)
_SERIAL_HEAD = re.compile(r"^s[lr]?\.?\s*no\.?$", re.I)
_LABEL_HEAD = re.compile(r"^(s[lr]?\.?\s*no\.?|particulars?|description|reference|units?)$", re.I)
_DETERMINATION = re.compile(r"^\s*determination\s+of\b", re.I)
_SECTION_NO = re.compile(r"^\s*(\d{1,2})\.\d{1,2}\.?\s")
_GENERAL_TERMS = re.compile(
    r"^\s*(general\s+)?terms\s+and\s+conditions\s+of\s+(high|low)[- ]tension\s+supply", re.I
)
# a table heading that is not the name of a rate block: the order's running head, a clause number,
# a connective, or a sentence of the schedule's text that happens to stand above the table
_NO_BLOCK_TITLE = re.compile(
    r"^\s*(plus|(applicable\s+)?tariff(\s+rates?)?)\s*$|"
    r"^\s*\d{1,2}(\.\d{1,2})+\.?\s*(demand|energy|fixed)\s+charges?\s*:?\s*$|"
    r"\b(shall|will)\b|\bis\s+applicable\b|\bapplicable\s+(to|for)\b|^\s*character\s+of\s+service\b|"
    r"\btariff\s+order\s+for\s+FY\b|\bARR\s+for\s+FY\b|\btruing\s+up\s+for\s+FY\b",
    re.I,
)
_SLAB_FIRST = re.compile(r"^(?:for\s+)?(?:the\s+)?first\s+(\d+)\s*(kva|kw)\b")
_SLAB_NEXT = re.compile(r"^(?:for\s+)?(?:the\s+)?next\s+(\d+)\s*(kva|kw)\b")
_SLAB_REST = re.compile(r"^(?:for\s+billing\s+demand\s+)?(?:in\s+excess\s+of|above)\s+(\d+)\s*(kva|kw)\b")
_WITHIN_CONTRACT = re.compile(r"^for\s+billing\s+demand\s+up\s*to\s+(?:the\s+)?contract\s+demand$")
_OVER_CONTRACT = re.compile(
    r"^for\s+billing\s+demand\s+in\s+excess\s+(?:of|over)\s+(?:the\s+)?contract\s+demand$"
)
_BAND_BASE = re.compile(r"^up\s*to\s+(\d+)\s*kva\s+of\s+billing\s+demand$")
_BAND_ABOVE = re.compile(r"^for\s+billing\s+demand\s+above\s+(\d+)\s*kva(?:\s+and\s+up\s*to\s+(\d+)\s*kva)?$")
_CLOCK_HEAD = re.compile(r"\bpeak\s+periods?\b|\b\d{4}\s*hrs\b", re.I)


def _category_name(category: str) -> str:
    """The category as the order names it, without the word that introduces it ('RATE: HTP-II')."""
    return _RATE_PREFIX.sub("", category).strip()


def _listed_band(text: str) -> dict[str, Any] | None:
    """A descriptor that lists two supply voltages and no other number ('132 kV / 220 kV',
    '132kV/220kV'): the band from the lower to the higher of them."""
    m = re.search(r"(\d+(?:\.\d+)?)\s*kv\s*/\s*(\d+(?:\.\d+)?)\s*kv(?![a-z])", text, re.I)
    if not m or re.findall(r"\d+(?:\.\d+)?", text) != [m.group(1), m.group(2)]:
        return None
    if re.search(r"\b(above|below|up\s*to|upto|exceeding|less than|more than)\b", text, re.I):
        return None
    lo, hi = sorted((Decimal(m.group(1)), Decimal(m.group(2))))
    return {"min": float(lo) if lo % 1 else int(lo), "max": float(hi) if hi % 1 else int(hi)}


def _without_serial(t: SectionTable, r: Row, skip: set[int], descriptor: str) -> str:
    """What a row names for its voltage band: a serial-number column is not part of it."""
    serial = {i for i, c in enumerate(t.columns) if _SERIAL_HEAD.match(c.strip())}
    if not serial:
        return descriptor
    return " ".join(c for i, c in enumerate(r.cells) if i not in skip | serial and c.strip())


def _particulars(t: SectionTable, r: Row) -> str:
    """What a row is about: its first cell, or the next one when the first column is a serial number."""
    if len(r.cells) > 1 and t.columns and _SERIAL_HEAD.match(t.columns[0].strip()):
        return r.cells[1]
    return r.cells[0]


def _misfiled(tables: list[SectionTable]) -> set[str]:
    """Tables whose numbered headings ('16.1. DEMAND CHARGES', '17.1. DEMAND CHARGES') belong to more
    than one numbered section although they are filed under one category: the category printed above
    each of them is then not certain."""
    numbers: dict[str, dict[str, str]] = defaultdict(dict)
    for t in tables:
        m = _SECTION_NO.match(t.title)
        if t.category and m:
            numbers[t.category][t.table_id] = m.group(1)
    return {tid for ids in numbers.values() if len(set(ids.values())) > 1 for tid in ids}


def _filing_guard(t: SectionTable, misfiled: set[str]) -> None:
    if t.table_id in misfiled:
        raise _Skip(
            "filed under a category together with a table of another numbered section: "
            "the category of the table is not certain"
        )
    named = set(_KV_NUMBER.findall(t.category or ""))
    titled = set(_KV_NUMBER.findall(t.title))
    if named and titled and not named & titled:
        raise _Skip(
            f"the heading names {', '.join(sorted(titled, key=float))} kV but the table is filed under "
            f"'{t.category}': the category of the table is not certain"
        )


def _printed_rows(t: SectionTable) -> list[Row]:
    """The rows of a table, with the heading row when it is itself a clause ('(a) | For billing demand
    up to the contract demand | Rs. 180 per kVA per month')."""
    if t.columns and _ITEM.match(t.columns[0].strip()):
        return [Row(tuple(t.columns), t.page), *t.rows]
    return list(t.rows)


def _clause(r: Row) -> tuple[str, str]:
    """The words and the rate cell of a clause row: an item mark is dropped, the last cell is the rate
    ('' when the row prints none)."""
    cells = [c.strip() for c in r.cells]
    if cells and _ITEM.match(cells[0]):
        cells = cells[1:]
    while cells and not cells[0]:
        cells = cells[1:]
    if not cells:
        return "", ""
    if len(cells) == 1:
        return _ITEM_LEAD.sub("", arr_rates._norm(cells[0])), ""
    return _ITEM_LEAD.sub("", arr_rates._norm(" ".join(c for c in cells[:-1] if c))), cells[-1]


def _demand_clauses(src: Source, t: SectionTable, cat: str) -> _Entry | None:
    """A demand charge printed as clauses: telescopic slabs of billing demand ('For the first 500 kVA
    … / next 500 kVA … / in excess of 1000 kVA …'), or one rate up to the contract demand, and the
    rate on demand above the contract demand. None when the table is not of this shape."""
    if not _FIXED.search(t.title) or len(t.columns) not in (2, 3):
        return None
    if any(_FIXED.search(c) or _ENERGY.search(c) for c in t.columns):
        return None
    rows = [(r, *_clause(r)) for r in _printed_rows(t)]
    kinds = (_SLAB_FIRST, _SLAB_NEXT, _SLAB_REST, _WITHIN_CONTRACT, _OVER_CONTRACT)
    if not any(k.match(words) for _, words, _ in rows for k in kinds):
        return None
    slabs: list[tuple[str | None, arr_rates.Rate, dict[str, Any]]] = []
    flat: tuple[arr_rates.Rate, dict[str, Any]] | None = None
    over: tuple[arr_rates.Rate, dict[str, Any]] | None = None
    edge = Decimal(0)
    for r, words, cell in rows:
        if not cell and (not words or _WITHIN_CONTRACT.match(words)):
            continue  # an empty row, or the sub-heading above the slabs
        slab = _SLAB_FIRST.match(words) or _SLAB_NEXT.match(words) or _SLAB_REST.match(words)
        got = arr_rates.rate(cell) if cell else None
        if (slab and slab.group(2) != "kva") or (got and got.unit == Unit.INR_PER_KW_MONTH):
            raise _Skip("billing-demand clauses in kW: the family's slabs are in kVA")
        if got is None or got.unit != Unit.INR_PER_KVA_MONTH:
            raise _Skip(f"a demand clause not read as rupees per kVA per month: '{_quote(r.cells)[:80]}'")
        ref = _ref(src, t, r.page, list(r.cells), [t.title])
        closed = bool(slabs) and slabs[-1][0] is None
        if _SLAB_FIRST.match(words) and not slabs:
            edge = Decimal(slab.group(1)) if slab else edge
            slabs.append((format(edge, "f"), got, ref))
        elif _SLAB_NEXT.match(words) and slabs and not closed:
            edge += Decimal(slab.group(1)) if slab else Decimal(0)
            slabs.append((format(edge, "f"), got, ref))
        elif _SLAB_REST.match(words) and slabs and not closed and slab and Decimal(slab.group(1)) == edge:
            slabs.append((None, got, ref))
        elif _WITHIN_CONTRACT.match(words) and not slabs and flat is None:
            flat = (got, ref)
        elif _OVER_CONTRACT.match(words) and over is None:
            over = (got, ref)
        else:
            raise _Skip(f"a demand clause not understood: '{_quote(r.cells)[:80]}'")
    if slabs and (flat is not None or len(slabs) < 2 or slabs[-1][0] is not None):
        raise _Skip("the demand slabs do not run from the first kVA to an open last slab")
    if not slabs and flat is None:
        raise _Skip("no demand charge up to the contract demand is printed in the table")
    e = _Entry(t, cat, "")
    e.fixed = (slabs[-1][1], slabs[-1][2]) if slabs else flat
    e.extra = {"slabs": slabs, "over_contract": over}
    return e


def _energy_clauses(src: Source, t: SectionTable, cat: str) -> _Entry | None:
    """An energy charge for the whole consumption chosen by the month's billing demand, printed as
    clauses under one heading ('Up to 500 kVA of billing demand 400 Paise per unit / For billing
    demand above 500 kVA and up to 2500 kVA 420 Paise per Unit / …'). None when not of this shape."""
    if len(t.columns) != 1 or not t.rows or any(len(r.cells) != 1 for r in t.rows):
        return None
    head = t.columns[0]
    if _CLOCK_HEAD.search(head):
        raise _Skip(
            "a time-of-use charge printed as a clause of the schedule (the hours are in its heading), "
            "not a time-of-day table"
        )
    if not re.search(r"\bentire\s+consumption\b", head, re.I):
        return None
    base: tuple[arr_rates.Rate, dict[str, Any]] | None = None
    bands: list[tuple[str, arr_rates.Rate, dict[str, Any]]] = []
    edge: str | None = None
    for r in t.rows:
        read = arr_rates.trailing_rate(r.cells[0])
        if read is None:
            raise _Skip(f"an energy clause not read: '{r.cells[0][:80]}'")
        words, got = _ITEM_LEAD.sub("", read[0]), read[1].in_rupees()
        if got.unit not in ENERGY_BASIS:
            raise _Skip(f"an energy clause in a unit outside the family: '{r.cells[0][:80]}'")
        ref = _ref(src, t, r.page, [head, *r.cells], [t.title])
        low, above = _BAND_BASE.match(words), _BAND_ABOVE.match(words)
        if low and base is None:
            base, edge = (got, ref), low.group(1)
        elif above and base is not None and above.group(1) == edge:
            bands.append((above.group(1), got, ref))
            edge = above.group(2)
        else:
            raise _Skip(f"an energy clause not understood: '{r.cells[0][:80]}'")
    units = {b[1].unit for b in bands} | ({base[0].unit} if base else set())
    if base is None or not bands or edge is not None or len(units) != 1:
        raise _Skip("the energy bands do not run from the lowest billing demand to an open last band")
    e = _Entry(t, cat, "")
    e.energy = base
    e.extra = {"bands": bands}
    return e


def _clause_entry(src: Source, t: SectionTable, cat: str) -> _Entry | None:
    return _demand_clauses(src, t, cat) or _energy_clauses(src, t, cat)


def _apply_clauses(row: dict[str, Any], extra: dict[str, Any]) -> None:
    """Demand slabs, the rate above the contract demand and energy bands onto a schedule row, each
    value with the citation of its own printed line."""
    body, refs = row["body"], row.setdefault("field_refs", {})
    slabs = extra.get("slabs") or []
    if slabs:
        body["demand_charge"]["basis"] = "inr_per_kva_month_billing_demand"
        body["demand_charge"]["slabs"] = [
            {"value": rate.text(), **({"up_to_kva": upto} if upto else {})} for upto, rate, _ in slabs
        ]
        for i, (_, _, ref) in enumerate(slabs):
            refs[f"/demand_charge/slabs/{i}/value"] = ref
    over = extra.get("over_contract")
    if over:
        body["demand_charge"]["billing_demand_rule"] = {"excess_demand_rate": over[0].text()}
        refs["/demand_charge/billing_demand_rule/excess_demand_rate"] = over[1]
    for i, (above, rate, ref) in enumerate(extra.get("bands") or []):
        body["energy_charge"].setdefault("by_billing_demand", []).append(
            {"above_kva": above, "base": rate.text()}
        )
        refs[f"/energy_charge/by_billing_demand/{i}/base"] = ref


def _adjustment_columns(t: SectionTable, cols: list[int]) -> list[int]:
    """Of the columns beside the hours, the one that holds the adjustment: a demand-charge column
    that prints 'Normal Rate' in every row adjusts nothing and is set aside."""
    if len(cols) < 2 or not t.rows:
        return cols
    keep = [
        i
        for i in cols
        if not (
            _FIXED.search(t.columns[i])
            and all(i < len(r.cells) and arr_rates.of_normal_pct(r.cells[i]) == 0 for r in t.rows)
        )
    ]
    return keep if len(keep) == 1 else cols


def _tod_scope(category: str) -> dict[str, Any]:
    """Who a time-of-day table is for: the category it is printed under, or, when it stands in the
    general terms of high- or low-tension supply, every consumer of that kind."""
    m = _GENERAL_TERMS.match(category)
    if m:
        return {"consumer_kind": ["ht", "eht"] if m.group(2).lower() == "high" else ["lt"]}
    return {"category": [category]}


def _headed_rate(src: Source, t: SectionTable, pattern: str, component: str) -> dict[str, Any]:
    """A charge printed under its own column heading, the heading saying it is approved ('Approved
    Green Energy Charges Rs/Unit' | '0.30'), in a table with one row of figures under it."""
    cols = [
        i
        for i, c in enumerate(t.columns)
        if re.search(pattern, c, re.I) and _APPROVED.search(c) and not _NOT_APPROVED.search(c)
    ]
    if len(cols) != 1:
        raise _Skip("no single column headed as the approved charge")
    col = cols[0]
    rated = []
    for r in t.rows:
        if col < len(r.cells) and re.fullmatch(r"\s*\d[\d,]*(\.\d+)?\s*", r.cells[col]):
            got = arr_rates.rate(r.cells[col], heading=t.columns[col])
            if got is not None and got.unit != Unit.PERCENT:
                rated.append((r, got))
    if len(rated) != 1:
        raise _Skip(f"no single per-unit rate under the approved heading ({len(rated)} rows state one)")
    r, got = rated[0]
    row = _oa_row(src, t, r, component, got, _plain(t.columns[col]), {})
    lead = [f"{t.label} {t.title}".strip()]
    row["source_ref"] = _ref(src, t, r.page, [t.columns[col], r.cells[col]], lead)
    return row


# ------------------------------------------------------------------ the order


def map_order(src: Source, sections: dict[str, list[SectionTable]]) -> Mapped:
    """Draft rows per family for one order, and everything that was not mapped with its reason."""
    out = Mapped(facts_read=sum(len(v) for v in sections.values()))
    _schedule_rows(src, sections.get("tariff_schedule", []), out)
    tod = _tod_by_year_rows(src, sections.get("tod", []), sections.get("tariff_schedule", []), out)
    _tod_rows(src, tod, out)
    _oa_rows(src, [*sections.get("wheeling", []), *sections.get("re_portfolio", [])], out)
    for section, tables in sections.items():
        if section not in MAPPED_SECTIONS:
            out.unmapped += [
                Unmapped(t.name(), section, "no ergPLAN family for this section") for t in tables
            ]
    return out
