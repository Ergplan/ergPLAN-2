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
    if any(_SLABBED.search(c) for c in t.columns):
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
        for r in t.rows:
            others = [c for i, c in enumerate(r.cells) if i not in charges]
            descriptor = " ".join(c for c in others if c.strip())
            try:
                if max(f_col, e_col) >= len(r.cells):
                    raise _Skip("the row has fewer cells than the table has columns")
                e = _Entry(t, cat, descriptor)
                e.band = _row_band(t, r, charges, descriptor)
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
    heading = t.columns[col] if col < len(t.columns) else None
    r = arr_rates.rate(cell, heading=heading) or arr_rates.flat_rate(cell, heading)
    if r is None:
        raise _Skip(f"rate not read: '{cell[:60]}'")
    return r.in_rupees(), _ref(src, t, row.page, quote, lead)


def _rate_block(t: SectionTable, descriptor: str, band: dict[str, Any] | None) -> str | None:
    parts = []
    if (
        not _GENERIC_TITLE.search(t.title)
        and not _NO_BLOCK_TITLE.search(t.title)
        and _plain(t.title).lower() != (t.category or "").lower()
    ):
        parts.append(_plain(t.title))
    if band is None and not _is_generic(descriptor):
        parts.append(_plain(descriptor))
    return " · ".join(parts)[:200] or None


def _schedule_rows(src: Source, tables: list[SectionTable], out: Mapped) -> None:
    entries: list[_Entry] = []
    misfiled = _misfiled(tables)
    for t in tables:
        try:
            if t.category is None:
                raise _Skip("not printed under a tariff category (a summary or billing-rate table)")
            if t.as_text:
                raise _Skip("rate lines read from running text, not from a ruled table")
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
        block = _rate_block(e.table, e.descriptor, e.band)
        scope: dict[str, Any] = {"category": [e.category]}
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
        if e.extra:
            _apply_clauses(row, e.extra)
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


def _row_band(t: SectionTable, r: Row, charges: set[int], descriptor: str) -> dict[str, Any] | None:
    """The voltage band of a row; a serial-number column is not part of what the row names."""
    serial = {i for i, c in enumerate(t.columns) if _SERIAL_HEAD.match(c.strip())}
    if not serial:
        return _band(descriptor)
    return _band(" ".join(c for i, c in enumerate(r.cells) if i not in charges | serial and c.strip()))


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
    _tod_rows(src, sections.get("tod", []), out)
    _oa_rows(src, [*sections.get("wheeling", []), *sections.get("re_portfolio", [])], out)
    for section, tables in sections.items():
        if section not in MAPPED_SECTIONS:
            out.unmapped += [
                Unmapped(t.name(), section, "no ergPLAN family for this section") for t in tables
            ]
    return out
