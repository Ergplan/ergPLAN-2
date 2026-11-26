"""A DISCOM's tariff as the rule books hold it, for the "DISCOM tariffs" screen: the retail schedule,
the ToD slots, the open-access charges and banking, row by row, each figure a SourcedValue citing the
rule field it restates, with the version it comes from and that version's window.

Nothing is computed: every figure is a value a rule row states, read as the regulation states it
(its unit taken from the row's own basis). By default only approved versions in force on the date
are read, through `rulebook.repository.approved_rules` (invariant 2). An analyst may ask for drafts
as well: those are read through `rulebook.sandbox.drafts_in_force` (the newest draft per
jurisdiction in force on the date) and every one is labelled a draft. Nothing is written.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.sourced import RefKind, SourcedValue, SourceRef
from ..core.units import Unit
from ..db.models import Jurisdiction, RulebookVersion, RuleTest, RuleTestRun, SourceDocument
from ..documents import service as documents
from ..documents.missing import document_refs
from ..rulebook import repository, sandbox
from ..rulebook.models import RuleView
from ..rulebook.service import jurisdiction_chain
from .statebook import settlement_points

# the families the screen shows, in the order it shows them
FAMILIES = ("tariff_schedule", "tod_slots", "oa_charge", "settlement")

UNREADABLE = SourceRef(
    kind=RefKind.ASSUMPTION,
    note="the rule row's citation could not be read; treat this figure as not yet sourced",
)

DEMAND_UNIT = {
    "inr_per_kva_month": Unit.INR_PER_KVA_MONTH,
    "inr_per_kw_month": Unit.INR_PER_KW_MONTH,
    "inr_per_kva_month_billing_demand": Unit.INR_PER_KVA_MONTH,
}
ENERGY_UNIT = {"kwh": Unit.INR_PER_KWH, "kvah": Unit.INR_PER_KVAH}
OA_UNIT = {
    "inr_per_kwh": Unit.INR_PER_KWH,
    "inr_per_kvah": Unit.INR_PER_KVAH,
    "inr_per_kw_month": Unit.INR_PER_KW_MONTH,
    "inr_per_mw_month": Unit.INR_PER_MW_MONTH,
    "inr_per_mw_day": Unit.INR_PER_MW_DAY,
    "inr_per_mwh": Unit.INR_PER_MWH,
    "percent_in_kind": Unit.PERCENT,
    "percent_of_price": Unit.PERCENT,
    "percent_of_acos": Unit.PERCENT,
    "inr_per_year": Unit.INR_PER_YEAR,
}
OA_BASIS_WORDS = {
    "percent_in_kind": "in kind",
    "percent_of_price": "of the price",
    "percent_of_acos": "of the average cost of supply",
}


EXEMPTION_WORDS = {
    "captive_26_51": "a captive user (26% of the equity, 51% of the output)",
    "group_captive": "a group captive user",
    "geoa_contract_demand_not_reduced": "green energy open access that keeps its contract demand",
    "re_source_waiver": "a renewable source under a waiver",
    "behind_meter": "a plant behind the meter",
    "consumer_voltage_at_or_above_kv": "supply at or above {param} kV",
    "commissioned_in_band": "a plant commissioned in {param}",
}


def exemption_words(ex: dict[str, Any]) -> str:
    """Who an open-access exemption is for, in words (the row's own note for a condition of its own)."""
    cond = str(ex.get("condition", "other"))
    words = EXEMPTION_WORDS.get(cond)
    if words is None:
        note = str(ex.get("note") or "").strip()
        return f"the condition the order states: {note[:160]}" if note else "a condition the order states"
    return words.format(param=ex.get("param") or "the stated")


@dataclass
class Figure:
    key: str  # the JSON pointer into the row's body it restates (stable across versions, for comparing)
    label: str
    value: SourcedValue


@dataclass
class Slot:
    name: str
    start: str
    end: str
    months: list[int]
    category: str | None
    ref: SourceRef
    figures: list[Figure] = field(default_factory=list)


@dataclass
class Row:
    family: str
    rule_id: str
    version_id: str
    status: str  # approved | draft
    jurisdiction_id: str
    label: str | None
    scope: dict[str, Any]
    heading: str | None  # the category (schedule), the component (open access), else None
    basis: str | None  # billing basis (schedule) or charge basis (open access), as the row states it
    match_key: str  # family + heading + scope: the same row across versions, for the comparison
    figures: list[Figure] = field(default_factory=list)
    slots: list[Slot] = field(default_factory=list)
    points: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    nearer: str | None = None  # an approved row of a nearer jurisdiction with the same key replaces it


@dataclass
class VersionCard:
    id: str
    family: str
    jurisdiction_id: str
    jurisdiction_name: str
    version: int
    status: str
    origin: str
    title: str | None
    effective_from: date
    effective_to: date | None
    approved_by: str | None
    golden_passed: int
    golden_total: int


@dataclass
class Book:
    discom_id: str
    discom_name: str
    state_code: str | None
    chain: list[tuple[str, str, str]]  # (id, kind, name), nearest first
    on: date
    drafts: bool
    rows: list[Row]
    versions: list[VersionCard]
    documents: dict[str, dict[str, Any]]
    not_sourced: list[str]  # families with no row in force


class NotADiscom(LookupError):
    pass


def _ref(rule: RuleView, pointer: str) -> SourceRef:
    try:
        return rule.ref(pointer)
    except (ValidationError, TypeError):
        return UNREADABLE


def _dec(v: Any) -> Decimal | None:
    if v is None or v == "" or isinstance(v, bool):
        return None
    try:
        return Decimal(str(v))
    except InvalidOperation:
        return None


def _fig(rule: RuleView, pointer: str, unit: Unit, label: str) -> Figure | None:
    """The value the row states at a body pointer, as a SourcedValue citing that field (or the row)."""
    d = _dec(rule.get(pointer))
    if d is None:
        return None
    return Figure(
        key=pointer,
        label=label,
        value=SourcedValue(value=d, unit=unit, source_refs=(_ref(rule, pointer),), label=label),
    )


def _add(out: list[Figure], f: Figure | None) -> None:
    if f is not None:
        out.append(f)


def schedule_figures(rule: RuleView) -> list[Figure]:
    b = rule.body
    out: list[Figure] = []
    dc = b.get("demand_charge") or {}
    basis = str(dc.get("basis", ""))
    d_unit = DEMAND_UNIT.get(basis, Unit.INR_PER_KVA_MONTH)
    on_bd = " on billing demand" if basis == "inr_per_kva_month_billing_demand" else ""
    word = "Fixed charge" if basis == "inr_per_kw_month" else "Demand charge"
    slabs = dc.get("slabs") or []
    if slabs:
        lo = "0"
        for i, sl in enumerate(slabs):
            hi = sl.get("up_to_kva")
            span = f"{lo} to {hi} kVA" if hi else f"above {lo} kVA"
            _add(out, _fig(rule, f"/demand_charge/slabs/{i}/value", d_unit, f"{word}{on_bd}, {span}"))
            lo = str(hi) if hi else lo
    else:
        _add(out, _fig(rule, "/demand_charge/value", d_unit, f"{word}{on_bd}"))
    bdr = dc.get("billing_demand_rule") or {}
    if bdr:
        _add(
            out,
            _fig(
                rule,
                "/demand_charge/billing_demand_rule/min_pct_of_contract_demand",
                Unit.PERCENT,
                "Billing demand is at least this share of contract demand",
            ),
        )
        _add(
            out,
            _fig(
                rule, "/demand_charge/billing_demand_rule/min_kva", Unit.KVA, "Billing demand is never below"
            ),
        )
        _add(
            out,
            _fig(
                rule,
                "/demand_charge/billing_demand_rule/excess_demand_penalty_multiplier",
                Unit.RATIO,
                "Demand above contract demand is charged at this multiple",
            ),
        )
        _add(
            out,
            _fig(
                rule,
                "/demand_charge/billing_demand_rule/excess_demand_rate",
                Unit.INR_PER_KVA_MONTH,
                "Rate on billing demand above contract demand",
            ),
        )
    e_unit = ENERGY_UNIT.get(str(b.get("billing_basis", "kwh")), Unit.INR_PER_KWH)
    ec = b.get("energy_charge") or {}
    _add(out, _fig(rule, "/energy_charge/base", e_unit, "Energy charge"))
    for slot in sorted((ec.get("by_slot") or {}).keys()):
        _add(
            out,
            _fig(
                rule,
                f"/energy_charge/by_slot/{slot}",
                e_unit,
                f"Energy charge in the {slot.replace('_', ' ')} slot",
            ),
        )
    for i, band in enumerate(ec.get("by_billing_demand") or []):
        _add(
            out,
            _fig(
                rule,
                f"/energy_charge/by_billing_demand/{i}/base",
                e_unit,
                f"Energy charge when billing demand is above {band.get('above_kva')} kVA",
            ),
        )
    _add(out, _fig(rule, "/energy_charge/rebate_pct", Unit.PERCENT, "Rebate on the energy charge"))
    _add(out, _fig(rule, "/retail_wheeling_charge", e_unit, "Wheeling charge billed with the energy charge"))
    _add(out, _fig(rule, "/fppca_inr_per_unit", e_unit, "Fuel and power purchase cost adjustment"))
    _add(out, _fig(rule, "/green_tariff_premium_inr_per_unit", e_unit, "Green tariff premium"))
    mc = b.get("minimum_charge") or {}
    if mc:
        _add(out, _fig(rule, "/minimum_charge/inr_per_kva_month", Unit.INR_PER_KVA_MONTH, "Minimum charge"))
        _add(
            out,
            _fig(
                rule,
                "/minimum_charge/min_units_per_kva_month",
                Unit.KWH,
                "Minimum units billed per kVA per month",
            ),
        )
    ed = b.get("electricity_duty") or {}
    if ed:
        _add(
            out,
            _fig(rule, "/electricity_duty/pct", Unit.PERCENT, "Electricity duty, share of the energy charge"),
        )
        _add(out, _fig(rule, "/electricity_duty/inr_per_unit", e_unit, "Electricity duty per unit"))
    for i, band in enumerate(b.get("pf_incentive") or []):
        _add(
            out,
            _fig(
                rule,
                f"/pf_incentive/{i}/pct",
                Unit.PERCENT,
                f"Power factor {band.get('pf_min')} to {band.get('pf_max')}: incentive (+) or penalty (−)",
            ),
        )
    for i, band in enumerate(b.get("lf_incentive") or []):
        _add(
            out,
            _fig(
                rule,
                f"/lf_incentive/{i}/rebate_inr_per_unit",
                e_unit,
                f"Load factor {band.get('lf_min_pct')}% to {band.get('lf_max_pct')}%: rebate",
            ),
        )
    _add(out, _fig(rule, "/prompt_payment_rebate_pct", Unit.PERCENT, "Prompt payment rebate"))
    _add(out, _fig(rule, "/taxes_pct", Unit.PERCENT, "Other taxes and cess"))
    return out


def tod_slots(rule: RuleView) -> list[Slot]:
    out: list[Slot] = []
    for i, s in enumerate(rule.body.get("slots") or []):
        p = f"/slots/{i}"
        figs: list[Figure] = []
        _add(figs, _fig(rule, f"{p}/surcharge_pct", Unit.PERCENT, "Surcharge on the energy charge"))
        _add(figs, _fig(rule, f"{p}/rebate_pct", Unit.PERCENT, "Rebate on the energy charge"))
        _add(figs, _fig(rule, f"{p}/rate_delta_inr_per_kwh", Unit.INR_PER_KWH, "Change to the energy charge"))
        for j, band in enumerate(s.get("rate_delta_by_billing_demand") or []):
            _add(
                figs,
                _fig(
                    rule,
                    f"{p}/rate_delta_by_billing_demand/{j}/rate_delta_inr_per_kwh",
                    Unit.INR_PER_KWH,
                    f"Change to the energy charge when billing demand is above {band.get('above_kva')} kVA",
                ),
            )
        for f in figs:
            f.key = f"{s.get('name')}:{f.key.split('/', 3)[-1]}"  # by slot name, not position
        out.append(
            Slot(
                name=str(s.get("name")),
                start=str(s.get("start")),
                end=str(s.get("end")),
                months=[int(m) for m in s.get("months") or []],
                category=s.get("category"),
                ref=_ref(rule, p),
                figures=figs,
            )
        )
    return out


def oa_figures(rule: RuleView) -> list[Figure]:
    b = rule.body
    basis = str(b.get("basis", ""))
    unit = OA_UNIT.get(basis, Unit.INR_PER_KWH)
    extra = OA_BASIS_WORDS.get(basis)
    word = "Charge" + (f" ({extra})" if extra else "")
    out: list[Figure] = []
    _add(out, _fig(rule, "/value", unit, word))
    for kv in sorted((b.get("value_by_voltage") or {}).keys(), key=lambda k: _dec(k) or Decimal(0)):
        _add(out, _fig(rule, f"/value_by_voltage/{kv}", unit, f"{word} at {kv} kV"))
    cap = b.get("cap") or {}
    if cap:
        _add(
            out,
            _fig(
                rule,
                "/cap/percent_of_acos",
                Unit.PERCENT,
                "Capped at this share of the average cost of supply",
            ),
        )
        _add(out, _fig(rule, "/cap/inr_per_kwh", Unit.INR_PER_KWH, "Capped at"))
    for i, ex in enumerate(b.get("exemptions") or []):
        _add(
            out,
            _fig(
                rule, f"/exemptions/{i}/waived_pct", Unit.PERCENT, f"Share waived for {exemption_words(ex)}"
            ),
        )
    return out


def banking_figures(rule: RuleView) -> list[Figure]:
    b = rule.body.get("banking") or {}
    out: list[Figure] = []
    ch = b.get("charge") or {}
    if ch:
        unit = Unit.PERCENT if ch.get("kind") == "in_kind_percent" else Unit.INR_PER_KWH
        word = "Banking charge" + (" (in kind)" if unit == Unit.PERCENT else "")
        _add(out, _fig(rule, "/banking/charge/value", unit, word))
    _add(
        out,
        _fig(
            rule,
            "/banking/cap_pct_of_generation",
            Unit.PERCENT,
            "Most that may be banked, share of generation",
        ),
    )
    _add(
        out,
        _fig(
            rule, "/banking/cap/pct_of_injection", Unit.PERCENT, "Ceiling carried forward, share of injection"
        ),
    )
    _add(
        out,
        _fig(
            rule,
            "/banking/cap/pct_of_consumption",
            Unit.PERCENT,
            "Ceiling carried forward, share of consumption",
        ),
    )
    _add(
        out,
        _fig(
            rule, "/banking/limit/drawal_pct", Unit.PERCENT, "Most that may be banked, share of grid drawal"
        ),
    )
    _add(
        out,
        _fig(
            rule,
            "/banking/limit/generation_pct",
            Unit.PERCENT,
            "Most that may be banked, share of generation",
        ),
    )
    _add(
        out,
        _fig(
            rule,
            "/banking/limit/consumption_pct",
            Unit.PERCENT,
            "Most that may be banked, share of consumption",
        ),
    )
    _add(out, _fig(rule, "/banking/in_block_banking_pct", Unit.PERCENT, "Share bankable within the block"))
    return out


def _heading(rule: RuleView) -> str | None:
    if rule.family == "tariff_schedule":
        return str(rule.body.get("category") or "") or None
    if rule.family == "oa_charge":
        return str(rule.body.get("component") or "") or None
    return None


def match_key(rule: RuleView) -> str:
    return json.dumps([rule.family, _heading(rule), rule.scope], sort_keys=True, default=str)


def row_of(rule: RuleView, status: str) -> Row:
    fam = rule.family
    r = Row(
        family=fam,
        rule_id=rule.id,
        version_id=rule.version_id,
        status=status,
        jurisdiction_id=rule.jurisdiction_id,
        label=rule.label,
        scope=rule.scope,
        heading=_heading(rule),
        basis=str(rule.body.get("billing_basis") or rule.body.get("basis") or "") or None,
        match_key=match_key(rule),
    )
    if fam == "tariff_schedule":
        r.figures = schedule_figures(rule)
    elif fam == "tod_slots":
        r.slots = tod_slots(rule)
        if rule.body.get("season_note"):
            r.notes.append(str(rule.body["season_note"]))
    elif fam == "oa_charge":
        r.figures = oa_figures(rule)
    elif fam == "settlement":
        r.figures = banking_figures(rule)
        r.points = settlement_points(rule)
    if rule.body.get("notes"):
        r.notes.append(str(rule.body["notes"]))
    return r


def _card(s: Session, vid: str, names: dict[str, str]) -> VersionCard | None:
    v = s.get(RulebookVersion, vid)
    if v is None:
        return None
    total = len(
        list(
            s.scalars(
                select(RuleTest.id).where(
                    RuleTest.jurisdiction_id == v.jurisdiction_id, RuleTest.family == v.family
                )
            )
        )
    )
    passed = len(
        list(
            s.scalars(
                select(RuleTestRun.id).where(
                    RuleTestRun.rulebook_version_id == vid, RuleTestRun.passed.is_(True)
                )
            )
        )
    )
    return VersionCard(
        id=v.id,
        family=v.family,
        jurisdiction_id=v.jurisdiction_id,
        jurisdiction_name=names.get(v.jurisdiction_id, v.jurisdiction_id),
        version=v.version,
        status=v.status,
        origin=v.origin,
        title=v.title,
        effective_from=v.effective_from,
        effective_to=v.effective_to,
        approved_by=v.approved_by,
        golden_passed=passed,
        golden_total=total,
    )


def build(s: Session, discom_id: str, on: date, drafts: bool = False) -> Book:
    """The DISCOM's tariff in force on a date: approved rows only unless `drafts` (analysts only; the
    router checks the role)."""
    j = s.get(Jurisdiction, discom_id)
    if j is None or j.kind != "discom":
        raise NotADiscom(discom_id)
    chain = jurisdiction_chain(s, discom_id)
    rank = {jid: i for i, jid in enumerate(chain)}
    juris = {x.id: x for x in s.scalars(select(Jurisdiction).where(Jurisdiction.id.in_(chain)))}
    names = {jid: x.name for jid, x in juris.items()}
    rows: list[Row] = []
    not_sourced: list[str] = []
    for fam in FAMILIES:
        approved = [row_of(r, "approved") for r in repository.approved_rules(s, fam, chain, on)]
        # an approved row a nearer jurisdiction restates with the same scope is not the one that applies
        nearest: dict[str, str] = {}
        for r in sorted(approved, key=lambda r: rank.get(r.jurisdiction_id, 99)):
            nearest.setdefault(r.match_key, r.jurisdiction_id)
        for r in approved:
            if nearest[r.match_key] != r.jurisdiction_id:
                r.nearer = names.get(nearest[r.match_key], nearest[r.match_key])
        draft_rows = (
            [row_of(r, "draft") for r in sandbox.drafts_in_force(s, fam, chain, on)] if drafts else []
        )
        if not approved:
            not_sourced.append(fam)
        fam_rows = approved + draft_rows
        fam_rows.sort(key=lambda r: (rank.get(r.jurisdiction_id, 99), r.status != "approved"))
        rows += fam_rows
    cards = [
        c for vid in dict.fromkeys(r.version_id for r in rows) if (c := _card(s, vid, names)) is not None
    ]
    cited = {
        str(ref["document_id"])
        for r in rows
        for ref in document_refs(
            [f.value.model_dump(mode="json") for f in r.figures]
            + [f.value.model_dump(mode="json") for sl in r.slots for f in sl.figures]
            + [sl.ref.to_json() for sl in r.slots]
            + [p["ref"] for p in r.points]
        )
    }
    stored = documents.stored_ids(s) if cited else set()
    docs: dict[str, dict[str, Any]] = {}
    for d in s.scalars(select(SourceDocument).where(SourceDocument.id.in_(cited))) if cited else []:
        docs[d.id] = {"title": d.title, "url_public": d.url_public, "stored": d.id in stored}
    for doc_id in cited - set(docs):
        docs[doc_id] = {"title": None, "url_public": None, "stored": False}
    return Book(
        discom_id=j.id,
        discom_name=j.name,
        state_code=j.state_code,
        chain=[(jid, juris[jid].kind, juris[jid].name) for jid in chain if jid in juris],
        on=on,
        drafts=drafts,
        rows=rows,
        versions=cards,
        documents=docs,
        not_sourced=not_sourced,
    )
