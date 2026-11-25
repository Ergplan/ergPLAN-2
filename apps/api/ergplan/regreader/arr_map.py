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
    if any(_SLABBED.search(c) for c in t.columns) and _unit_slabs(t):
        raise _Skip("energy charge in consumption slabs: the tariff_schedule family holds one rate per row")
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
        for r in t.rows:
            others = [c for i, c in enumerate(r.cells) if i not in charges]
            descriptor = " ".join(c for c in others if c.strip())
            try:
                tod_rows = tod_rows or bool(_TOD_MARKER.search(descriptor))
                if tod_rows:
                    raise _Skip("a time-of-day row printed inside the rate table")
                if max(f_col, e_col) >= len(r.cells):
                    raise _Skip("the row has fewer cells than the table has columns")
                e = _Entry(t, cat, _own_descriptor(t, cat, descriptor))
                e.band = _band(descriptor)
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


def _rate_and_ref(
    src: Source, t: SectionTable, row: Row, col: int, quote: list[str], lead: tuple[str, ...] | list[str] = ()
) -> tuple[arr_rates.Rate, dict[str, Any]]:
    cell = row.cells[col]
    if arr_rates.is_blank(cell):
        raise _Skip("no fixed or no energy charge is printed for the row")
    r = arr_rates.rate(cell, heading=t.columns[col] if col < len(t.columns) else None)
    if r is None and col < len(t.columns):
        r = arr_rates.rate_under(cell, t.columns[col])
    if r is None:
        raise _Skip(f"rate not read: '{cell[:60]}'")
    return r.in_rupees(), _ref(src, t, row.page, quote, lead)


def _rate_block(t: SectionTable, descriptor: str, band: dict[str, Any] | None) -> str | None:
    parts = []
    generic = _GENERIC_TITLE.search(t.title) or _title_year(t.title)
    if not generic and _plain(t.title).lower() != (t.category or "").lower():
        parts.append(_plain(t.title))
    if band is None and not _is_generic(descriptor):
        parts.append(_plain(descriptor))
    return " · ".join(parts)[:200] or None


def _schedule_rows(src: Source, tables: list[SectionTable], out: Mapped) -> None:
    entries: list[_Entry] = []
    tables, undated, _ = _year_runs(tables)
    for t in tables:
        try:
            if t.category is None:
                raise _Skip("not printed under a tariff category (a summary or billing-rate table)")
            if t.table_id in undated:
                raise _Skip(undated[t.table_id])
            if t.category.rstrip().endswith((";", ",")):
                raise _Skip("filed under a clause of the order's text, not under a category heading")
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
        scope: dict[str, Any] = {"category": [e.category, *_category_codes(e.category)]}
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
        if e.wheeling is not None:
            row["body"]["retail_wheeling_charge"] = e.wheeling.text()
        rows.append((e, row))
    _keep_unambiguous(rows, "tariff_schedule", out)


def _keep_unambiguous(rows: list[tuple[Any, dict[str, Any]]], family: str, out: Mapped) -> None:
    """Rows whose scope is the same as another's cannot both apply: none of them is kept."""
    seen: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for _, row in rows:
        seen[json.dumps([row["scope"], row["body"].get("component")], sort_keys=True)].append(row)
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
    Empty where the heading is not headed by such a code ('HV-2' is a code already)."""
    head = heading.split(":")[0] if ":" in heading else heading
    codes: list[str] = []
    for m in _CODE.finditer(head):
        code = f"{m.group(1)} {m.group(2)}" + (f" ({m.group(3).upper()})" if m.group(3) else "")
        if code not in codes and code != heading:
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
