"""Tariff orders from the ARR Network into DRAFT rulebook versions.

    python -m ergplan.regreader.arr_network import --state up --orders 67,66,65,64 [--dry-run]
        [--effective-from 65=2025-11-22] [--jurisdiction 67=uppcl] [--no-pdf] [--refresh] [--verbose]
        [--effective-to 178=2026-03-31] [--per-unit 187=kvah]

For each order: its metadata and section tables are read (`arr_network`), the order PDF is fetched
from the regulator's link and stored when its SHA-256 is the one the ARR Network read (otherwise a
reference row keeps the link and the pages, and every citation says the page is not stored), the
tables are mapped (`arr_map`), each mapped row's quoted numbers are checked against the stored page,
and one draft version per family is proposed through `rulebook.service`: never approved here, and
visible to no client until an analyst approves it in the app. Running it again changes nothing
unless the source changed (then the draft is revised in place).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.ids import document_id
from ..db.base import session_scope
from ..db.models import Jurisdiction, Rule, RulebookVersion, SourceDocument
from ..documents import ocr
from ..documents import service as documents
from ..rulebook import golden, service
from . import arr_map, grounding
from .arr_network import ArrNetworkClient, ArrNetworkError, Order, SectionTable
from .tariff_oder import Mapped, Unmapped

ORIGIN = "arr_network"
ACTOR = "arr-network-import"


class ImportRefused(ValueError):
    pass


@dataclass
class VersionOutcome:
    family: str
    jurisdiction_id: str
    rows: int
    action: str  # proposed | revised | unchanged | would propose (dry run) | kept: <status>
    version: int | None = None
    version_id: str | None = None
    golden: str = "not run"
    golden_failing: list[str] = field(default_factory=list)


@dataclass
class OrderOutcome:
    order: Order
    jurisdiction_id: str
    effective_from: date
    effective_to: date | None
    effective_basis: str
    document_id: str
    pdf: str = "not fetched"
    tables_read: int = 0
    reader_version: str = ""
    mapped: dict[str, int] = field(default_factory=dict)
    unmapped: list[Unmapped] = field(default_factory=list)
    ungrounded: list[Unmapped] = field(default_factory=list)
    rows_grounded: int = 0
    pages_unchecked: list[int] = field(default_factory=list)
    versions: list[VersionOutcome] = field(default_factory=list)

    def title(self) -> str:
        o = self.order
        return (
            f"{o.commission} tariff order {o.arr_year} ({', '.join(o.utilities)}) · "
            f"ARR Network order {o.order_id}"
        )


# ------------------------------------------------------------------ what an order is for


def jurisdiction_for(session: Session, order: Order, state: str, override: str | None = None) -> str:
    """The jurisdiction an order's rules belong to: the utility itself, or the group its utilities
    share (one tariff order for several DISCOMs). Refused, with what to add, when the jurisdictions
    file does not hold it."""
    st = state.upper()
    if override:
        j = session.get(Jurisdiction, override)
        if j is None or j.state_code != st:
            raise ImportRefused(f"order {order.order_id}: jurisdiction {override} is not one of {st}")
        return j.id
    found = [session.get(Jurisdiction, u.lower()) for u in order.utilities]
    missing = [u for u, j in zip(order.utilities, found, strict=True) if j is None]
    if missing or not found:
        raise ImportRefused(
            f"order {order.order_id}: no jurisdiction for "
            f"{', '.join(missing) or 'an order without utilities'}. "
            "Add the DISCOM (and its state and regulator) to packages/rulebooks/jurisdictions.yaml, run "
            "`python -m ergplan.rulebook.cli sync-jurisdictions`, or pass --jurisdiction "
            f"{order.order_id}=<id>."
        )
    js = [j for j in found if j is not None]
    if any(j.state_code != st for j in js):
        raise ImportRefused(f"order {order.order_id}: its utilities are not all in {st}")
    if len(js) == 1:
        return js[0].id
    parents = {j.parent_id for j in js}
    parent = session.get(Jurisdiction, next(iter(parents)) or "") if len(parents) == 1 else None
    if parent is None or parent.kind != "group":
        raise ImportRefused(
            f"order {order.order_id}: {', '.join(order.utilities)} share no group in "
            "packages/rulebooks/jurisdictions.yaml (one order for several DISCOMs needs a `groups` row "
            f"that is their `parent`), or pass --jurisdiction {order.order_id}=<id>."
        )
    return parent.id


_TITLE_DATE = re.compile(r"\bdated\s+(\d{1,2})[/.-](\d{1,2})[/.-](\d{2,4})\b", re.I)


def effective_from_for(order: Order, given: date | None = None) -> tuple[date, str]:
    """When the order's tariff starts, and on what basis. The ARR Network states no effective date:
    a date given by the person running the import wins, then the order's own date (from the
    metadata or printed in its title, day first), else 1 April of the tariff year, said to be assumed."""
    if given:
        return given, "given on the command line"
    if order.order_date:
        return order.order_date, "the order's date in the ARR Network"
    m = _TITLE_DATE.search(order.title)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            return date(y + 2000 if y < 100 else y, mo, d), "the order's date printed in its title"
        except ValueError:
            pass
    fy = re.search(r"(\d{4})-\d{2}", order.arr_year)
    if not fy:
        raise ImportRefused(f"order {order.order_id}: no tariff year; pass --effective-from")
    return date(int(fy.group(1)), 4, 1), "ASSUMED: 1 April of the tariff year (no order date at the source)"


# ------------------------------------------------------------------ the order's PDF and grounding


def _meta(order: Order, state: str, eff: date) -> documents.DocumentMeta:
    name = PurePosixPath(urlparse(order.url).path).name or None
    return documents.DocumentMeta(
        title=order.title,
        regulator=order.commission or None,
        jurisdiction=state.upper(),
        document_kind="order",
        utilities=order.utilities,
        order_date=order.order_date,
        effective_from=eff,
        url_public=order.url,
        notes=(f"registered from ARR Network order {order.order_id}",),
        original_filename=name,
    )


def store_pdf(
    session: Session, client: ArrNetworkClient, order: Order, state: str, eff: date, actor: str, fetch: bool
) -> tuple[SourceDocument, str]:
    doc = session.get(SourceDocument, document_id(order.sha256))
    if doc is not None and documents.is_stored(doc):
        return doc, "already stored"
    held = documents.path_for(order.sha256)
    if held.exists() and hashlib.sha256(held.read_bytes()).hexdigest() == order.sha256:
        # the file is in the library already (copied there): register it, nothing to fetch
        doc, _ = documents.register(session, held.read_bytes(), _meta(order, state, eff), uploaded_by=actor)
        return doc, "stored (the file was in the library)"
    if fetch:
        try:
            data = client.pdf(order)
        except ArrNetworkError as e:
            why = str(e)
        else:
            doc, _ = documents.register(session, data, _meta(order, state, eff), uploaded_by=actor)
            return doc, "stored"
    else:
        why = "not fetched (--no-pdf)"
    doc, _ = documents.register_reference(
        session, order.sha256, order.pages, _meta(order, state, eff), uploaded_by=actor
    )
    return doc, f"NOT stored ({why}); the regulator's link and the page are kept"


def ground(doc: SourceDocument, mapped: Mapped) -> tuple[list[Unmapped], int, list[int]]:
    """Drop every mapped row whose quoted numbers are not printed on its cited pages of the stored
    PDF. Returns (the rows dropped, with the numbers; rows that passed; pages that have no text to
    check against, whose rows are kept and reported)."""
    texts: dict[int, str] = {}
    failures: list[Unmapped] = []
    unchecked: set[int] = set()
    passed = 0
    for family in list(mapped.by_family):
        keep = []
        for row in mapped.by_family[family]:
            refs = [row["source_ref"], *(row.get("field_refs") or {}).values()]
            problems: list[str] = []
            readable: list[str] = []
            for page in sorted({int(r["page"]) for r in refs}):
                if page not in texts:
                    try:
                        texts[page] = documents.page_text(doc, page)
                    except documents.PageOutOfRange:
                        texts[page] = ""
                if page < 1 or page > doc.pages:
                    problems.append(f"page {page} is outside the PDF's {doc.pages} pages")
                elif len(texts[page].strip()) < ocr.MIN_TEXT_CHARS:
                    unchecked.add(page)
                else:
                    readable.append(texts[page])
            if readable and not problems:
                for r in refs:
                    missing = grounding.quote_numbers_missing(str(r["quote"]), "\n".join(readable))
                    if missing:
                        problems.append(f"{', '.join(missing)} not printed on page {r['page']}")
            if problems:
                failures.append(
                    Unmapped(str(row.get("label")), family, "grounding: " + "; ".join(sorted(set(problems))))
                )
            else:
                keep.append(row)
                passed += int(bool(readable))
        mapped.by_family[family] = keep
    return failures, passed, sorted(unchecked)


# ------------------------------------------------------------------ drafts, idempotently


def _comparable(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "label": r.get("label"),
            "scope": r.get("scope") or {},
            "body": r["body"],
            "source_ref": r["source_ref"],
            "field_refs": r.get("field_refs") or {},
        }
        for r in rows
    ]


def _stored_rows(session: Session, v: RulebookVersion) -> list[dict[str, Any]]:
    return [
        {
            "label": r.label,
            "scope": r.scope,
            "body": r.body,
            "source_ref": r.source_ref,
            "field_refs": r.field_refs,
        }
        for r in session.scalars(select(Rule).where(Rule.rulebook_version_id == v.id).order_by(Rule.position))
    ]


def propose_draft(
    session: Session, out: OrderOutcome, family: str, rows: list[dict[str, Any]], actor: str, notes: str
) -> VersionOutcome:
    """One draft version of one family for the order, or the one already there: unchanged when its
    rows are the same, revised in place when the source changed and it is still a draft. A version
    an analyst has approved or rejected is never touched; a changed source then becomes a new draft."""
    latest = session.scalar(
        select(RulebookVersion)
        .where(
            RulebookVersion.family == family,
            RulebookVersion.jurisdiction_id == out.jurisdiction_id,
            RulebookVersion.origin == ORIGIN,
            RulebookVersion.title == out.title(),
        )
        .order_by(RulebookVersion.version.desc())
    )
    res = VersionOutcome(family, out.jurisdiction_id, len(rows), "proposed")
    same = latest is not None and _comparable(_stored_rows(session, latest)) == _comparable(rows)
    if latest is not None and same and latest.status != "draft":
        res.action, v = f"kept: {latest.status} by an analyst", latest
    elif latest is not None and latest.status == "draft":
        v = latest
        res.action = "unchanged"
        if not same:
            v = service.revise(session, latest.id, rows)
            res.action = "revised"
        if (v.effective_from, v.effective_to) != (out.effective_from, out.effective_to):
            v = service.reschedule(session, v.id, out.effective_from, out.effective_to)
            res.action = "revised" if res.action == "revised" else "rescheduled"
    else:
        v = service.propose(
            session,
            family=family,
            jurisdiction_id=out.jurisdiction_id,
            rows=rows,
            actor=actor,
            effective_from=out.effective_from,
            effective_to=out.effective_to,
            title=out.title(),
            origin=ORIGIN,
            notes=notes,
        )
    res.version, res.version_id = v.version, v.id
    if v.status == "draft":
        g = service.run_golden(session, v.id)
        res.golden = f"{g.passed}/{g.total}" if g.total else "no golden cases yet"
        res.golden_failing = [o.name for o in g.outcomes if not o.passed]
    else:
        res.golden = v.golden_status
    return res


# ------------------------------------------------------------------ the run


def _read(client: ArrNetworkClient, order_id: int) -> tuple[Order, dict[str, list[SectionTable]]]:
    order = client.order(order_id)
    return order, {s: client.sections(order_id, s) for s in arr_map.MAPPED_SECTIONS}


def run(
    client: ArrNetworkClient,
    state: str,
    order_ids: list[int],
    *,
    actor: str = ACTOR,
    dry_run: bool = False,
    fetch_pdf: bool = True,
    effective: dict[int, date] | None = None,
    jurisdictions: dict[int, str] | None = None,
    per_unit: dict[int, str] | None = None,
    until: dict[int, date] | None = None,
) -> list[OrderOutcome]:
    read = [_read(client, i) for i in order_ids]
    outcomes: list[OrderOutcome] = []
    with session_scope() as s:
        for order, _ in read:
            eff, basis = effective_from_for(order, (effective or {}).get(order.order_id))
            jid = jurisdiction_for(s, order, state, (jurisdictions or {}).get(order.order_id))
            outcomes.append(OrderOutcome(order, jid, eff, None, basis, document_id(order.sha256)))
    # an order's tariff runs until the next imported order of the same jurisdiction takes effect
    for o in outcomes:
        later = sorted(
            x.effective_from
            for x in outcomes
            if x.jurisdiction_id == o.jurisdiction_id and x.effective_from > o.effective_from
        )
        o.effective_to = later[0] - timedelta(days=1) if later else None
        # ... or until the day the order itself says (its successor may not be among the orders imported)
        given_to = (until or {}).get(o.order.order_id)
        if given_to is not None:
            if given_to < o.effective_from or (o.effective_to is not None and given_to > o.effective_to):
                raise ImportRefused(
                    f"order {o.order.order_id}: --effective-to {given_to} is before its start or after the "
                    "next imported order takes effect"
                )
            o.effective_to = given_to
            o.effective_basis += "; last day given on the command line"
    if not dry_run:
        with session_scope(actor=actor) as s:
            golden.sync_tests(s)
    for out, (order, sections) in zip(outcomes, read, strict=True):
        tables = [t for ts in sections.values() for t in ts]
        out.tables_read = len(tables)
        out.reader_version = ", ".join(sorted({t.reader_version for t in tables}))
        fetched = max((t.fetched_on for t in tables), default=order.fetched_on)
        unit = (per_unit or {}).get(order.order_id)
        mapped = arr_map.map_order(
            arr_map.Source(order, out.document_id, out.effective_from, per_unit=unit), sections
        )
        with session_scope(actor=actor) as s:
            if dry_run:
                doc = s.get(SourceDocument, out.document_id)
                stored = doc is not None and documents.is_stored(doc)
                out.pdf = "already stored" if stored else "not fetched (dry run)"
                doc = doc if stored else None
            else:
                doc, out.pdf = store_pdf(s, client, order, state, out.effective_from, actor, fetch_pdf)
                doc = doc if documents.is_stored(doc) else None
            if doc is not None:
                out.ungrounded, out.rows_grounded, out.pages_unchecked = ground(doc, mapped)
        out.unmapped = list(mapped.unmapped)
        out.mapped = {f: len(rows) for f, rows in mapped.by_family.items() if rows}
        notes = (
            f"ARR Network order {order.order_id} ({order.url}); reader {out.reader_version}; "
            f"fetched {fetched.isoformat()}; effective date: {out.effective_basis}; "
            f"PDF {out.pdf}; not reviewed by a person at the source"
            + (
                f"; reading given at import: 'per unit' on rows with a kVA demand charge = {unit}"
                if unit
                else ""
            )
        )
        for family in sorted(mapped.by_family):
            rows = mapped.by_family[family]
            if not rows:
                continue
            if dry_run:
                out.versions.append(VersionOutcome(family, out.jurisdiction_id, len(rows), "would propose"))
                continue
            with session_scope(actor=actor) as s:
                out.versions.append(propose_draft(s, out, family, rows, actor, notes))
    return outcomes


def summary(outcomes: list[OrderOutcome], verbose: bool = False) -> str:
    lines: list[str] = []
    for o in outcomes:
        od = o.order
        lines.append(
            f"order {od.order_id} · {od.commission} · {od.arr_year} · {', '.join(od.utilities)} "
            f"→ {o.jurisdiction_id}"
        )
        to = o.effective_to.isoformat() if o.effective_to else "open"
        lines.append(f"  effective {o.effective_from.isoformat()} → {to} ({o.effective_basis})")
        lines.append(f"  PDF: {o.pdf} · {od.pages} pages · document {o.document_id}")
        if o.rows_grounded or o.ungrounded or o.pages_unchecked:
            lines.append(
                f"  grounding: {o.rows_grounded} rows checked on their pages, {len(o.ungrounded)} failed and "
                f"were not imported"
                + (
                    f"; pages without text, rows kept unchecked: {o.pages_unchecked}"
                    if o.pages_unchecked
                    else ""
                )
            )
        else:
            lines.append("  grounding: not run (the PDF is not stored)")
        lines.append(f"  {o.tables_read} tables read ({o.reader_version})")
        for v in o.versions:
            ver = f"v{v.version}" if v.version else "—"
            failing = f" · failing: {'; '.join(v.golden_failing)}" if v.golden_failing else ""
            lines.append(
                f"  {v.family:16} {v.jurisdiction_id:10} {ver:4} {v.action:14} {v.rows:3} rows · "
                f"golden {v.golden}{failing}"
            )
        if not o.versions:
            lines.append("  nothing mapped: no version proposed")
        every = [*o.ungrounded, *o.unmapped]
        lines.append(f"  unmapped: {len(every)} tables or rows")
        reasons = Counter(re.sub(r"'.*?'|\(.*\)|\d+", "…", u.reason)[:110] for u in every)
        for reason, n in reasons.most_common():
            lines.append(f"    {n:4} × {reason}")
        if verbose:
            lines += [f"    - [{u.family}] {u.fact_id}: {u.reason}" for u in every]
        lines.append("")
    return "\n".join(lines)


def as_json(outcomes: list[OrderOutcome]) -> list[dict[str, Any]]:
    return [
        {
            "order_id": o.order.order_id,
            "tariff_year": o.order.arr_year,
            "utilities": list(o.order.utilities),
            "jurisdiction": o.jurisdiction_id,
            "effective_from": o.effective_from.isoformat(),
            "effective_to": o.effective_to.isoformat() if o.effective_to else None,
            "effective_basis": o.effective_basis,
            "document_id": o.document_id,
            "pdf": o.pdf,
            "tables_read": o.tables_read,
            "rows_grounded": o.rows_grounded,
            "pages_unchecked": o.pages_unchecked,
            "versions": [vars(v) for v in o.versions],
            "unmapped": [vars(u) for u in [*o.ungrounded, *o.unmapped]],
        }
        for o in outcomes
    ]


def _pairs(values: list[str], what: str) -> dict[int, str]:
    out = {}
    for v in values:
        k, sep, val = v.partition("=")
        if not sep or not k.strip().isdigit() or not val.strip():
            raise SystemExit(f"--{what} takes ORDER=VALUE, not '{v}'")
        out[int(k)] = val.strip()
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ergplan.regreader.arr_network")
    ap.add_argument("command", choices=["import"])
    ap.add_argument("--state", required=True, help="state code of the jurisdictions file, e.g. up")
    ap.add_argument("--orders", required=True, help="ARR Network order ids, comma-separated")
    ap.add_argument("--dry-run", action="store_true", help="map and report; write nothing, fetch no PDF")
    ap.add_argument("--effective-from", action="append", default=[], metavar="ORDER=YYYY-MM-DD")
    ap.add_argument(
        "--effective-to",
        action="append",
        default=[],
        metavar="ORDER=YYYY-MM-DD",
        help="the last day of the order's tariff where the order states it and its successor is not imported",
    )
    ap.add_argument("--jurisdiction", action="append", default=[], metavar="ORDER=ID")
    ap.add_argument(
        "--per-unit",
        action="append",
        default=[],
        metavar="ORDER=kvah|kwh",
        help="what a 'unit' is where the order's schedule prices energy per unit on rows with a kVA "
        "demand charge (read it from the order's terms; without it such rows are not mapped)",
    )
    ap.add_argument("--no-pdf", action="store_true", help="do not fetch the order PDFs")
    ap.add_argument("--refresh", action="store_true", help="read the site again instead of the disk cache")
    ap.add_argument("--actor", default=ACTOR)
    ap.add_argument("--verbose", action="store_true", help="list every unmapped table and row")
    ap.add_argument("--json", metavar="PATH", help="also write the full result as JSON")
    a = ap.parse_args(argv)
    try:
        ids = [int(x) for x in a.orders.split(",") if x.strip()]
        effective = {k: date.fromisoformat(v) for k, v in _pairs(a.effective_from, "effective-from").items()}
        until = {k: date.fromisoformat(v) for k, v in _pairs(a.effective_to, "effective-to").items()}
        per_unit = {k: v.lower() for k, v in _pairs(a.per_unit, "per-unit").items()}
        if any(v not in ("kvah", "kwh") for v in per_unit.values()):
            raise ValueError("--per-unit takes kvah or kwh")
    except ValueError as e:
        raise SystemExit(f"bad argument: {e}") from e
    try:
        outcomes = run(
            ArrNetworkClient(refresh=a.refresh),
            a.state,
            ids,
            actor=a.actor,
            dry_run=a.dry_run,
            fetch_pdf=not a.no_pdf,
            effective=effective,
            jurisdictions=_pairs(a.jurisdiction, "jurisdiction"),
            per_unit=per_unit,
            until=until,
        )
    except (ArrNetworkError, ImportRefused) as e:
        print(f"refused: {e}", file=sys.stderr)
        return 2
    print(summary(outcomes, a.verbose))
    if a.dry_run:
        print("dry run: nothing was written")
    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump(as_json(outcomes), f, indent=1)
    return 0
