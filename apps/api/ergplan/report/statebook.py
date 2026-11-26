"""The state rule book: one month's issue for a DISCOM, for a reference industrial consumer or for
one client's connection. It sets out, from the approved rules in force:

- what the grid costs, per ToD slot;
- the landed cost at the meter of open-access solar and wind by route (third party; captive with the
  26% equity of a group-captive company; captive from the consumer's own plant) and of exchange
  purchases (IEX), each line with its source;
- the saving on the grid tariff, per slot and averaged over the day;
- how open-access solar and wind are settled after banking, and how an exchange purchase is settled.

`build()` computes the book (nothing is stored); `generate()` stores it as a draft; `approve()` and
`reject()` are the analyst's decision. An issue is frozen when generated: its `content` is what was
reviewed. Next month, or when a rule changes, a new issue is generated and what changed since the
last approved issue is listed for the reviewer. This module is the only writer of `state_rulebooks`.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..core.settings import get_settings
from ..core.sourced import RefKind, SourcedValue, SourceRef, derive
from ..core.units import Unit
from ..db.models import (
    Asset,
    Client,
    Connection,
    Jurisdiction,
    Rule,
    RulebookVersion,
    Site,
    SourceDocument,
    StateRulebook,
)
from ..documents import service as documents
from ..documents.missing import document_refs
from ..engine import landed_cost
from ..engine import scope as escope
from ..intake import defaults, site_intake
from ..platform.routers import landed as landed_api
from ..platform.routers.cost import _facts, _kvah_factor, bill_levies
from ..rulebook import repository
from ..rulebook.models import RuleView
from ..rulebook.scope import matches
from ..rulebook.service import jurisdiction_chain

ZERO = Decimal(0)
# the routes a renewable plant's energy can reach the consumer by
ROUTES: tuple[tuple[str, str, str], ...] = (
    (
        "third_party",
        "Third party",
        "Bought from a generator under a PPA; cross-subsidy and additional surcharge apply.",
    ),
    (
        "group_captive",
        "Captive, 26% equity (group captive)",
        "The consumer holds at least 26% of the generating company's equity and takes at least 51% of its "
        "output (Electricity Rules 2005, rule 3): the plant is captive, so the cross-subsidy surcharge and "
        "the additional surcharge do not apply (Electricity Act 2003, section 42(2) and 42(4)). The "
        "exemption is a line of its own below. The consumer's equity outlay, taken as 26% of the plant's "
        "capital cost (an assumption; the plant's own debt and equity split is not modelled), is carried "
        "over the plant's life as a cost per kWh beside the tariff.",
    ),
    (
        "captive",
        "Captive, own plant",
        "The consumer owns the plant outright: the cross-subsidy surcharge and the additional surcharge do "
        "not apply (Electricity Act 2003, section 42(2) and 42(4)); the exemption is a line of its own below. "
        "No tariff is paid: the price is the plant's levelised cost, from its capital cost, O&M, CUF and life "
        "(each an assumption), and a tariff quoted for the source is not used on this route.",
    ),
)
RE_SOURCES = ("oa_solar_intra", "oa_wind_intra", "oa_solar_ists", "oa_wind_ists")
IEX_SOURCES = ("iex_dam", "iex_gdam", "iex_rtm")
CAT_WORDS = {"peak": "peak", "normal": "normal", "off_peak": "off-peak"}


class BookError(ValueError):
    pass


class BookNotFound(BookError):
    """No such issue."""


class BookConflict(BookError):
    """The issue is not in a state that allows the action."""


# ---------------------------------------------------------------- the consumer


def reference_category(s: Session, discom: str, on: date) -> tuple[str | None, str | None]:
    """The category a DISCOM's reference industrial consumer is on (the first category of its approved
    schedule that reads as HT industrial, else the first) and, where the schedule splits urban and
    rural, the first rate block it names for that category."""
    rows = repository.approved_rules(s, "tariff_schedule", jurisdiction_chain(s, discom), on)
    cats = list(dict.fromkeys(str(r.body.get("category")) for r in rows if r.body.get("category")))
    labels = {str(r.body.get("category")): (r.label or "") for r in rows}
    pick = next(
        (
            c
            for c in cats
            if ("ht" in f"{c} {labels[c]}".lower() or "hv" in c.lower())
            and "ind" in f"{c} {labels[c]}".lower()
        ),
        None,
    ) or next((c for c in cats if c.lower().startswith(("ht", "hv"))), cats[0] if cats else None)
    block = next(
        (
            str(r.scope["rate_block"])
            for r in rows
            if str(r.body.get("category")) == pick and r.scope.get("rate_block")
        ),
        None,
    )
    return pick, block


def _reference_factor() -> SourcedValue:
    a = SourceRef(kind=RefKind.ASSUMPTION, note="reference consumer: a power factor of 0.95 assumed")
    return SourcedValue(value=1 / Decimal("0.95"), unit=Unit.RATIO, source_refs=(a,), label="kVAh per kWh")


# ---------------------------------------------------------------- settlement, in words


UNCITED = SourceRef(
    kind=RefKind.ASSUMPTION,
    note="ergPLAN's own statement: no approved rule row carries a citation for this sentence",
)


def _pt(text: str, ref: SourceRef | None = None) -> dict[str, Any]:
    """A sentence of the book with its source (invariant 1): the rule row's field ref, or, where the
    row carries none for that field, a ref that says the sentence is ergPLAN's own."""
    return {"text": text, "ref": (ref or UNCITED).to_json()}


def _model_ref(note: str) -> SourceRef:
    return SourceRef(kind=RefKind.ASSUMPTION, note=note)


def _cats(names: list[str]) -> str:
    words = [CAT_WORDS.get(n, n) for n in names]
    return words[0] if len(words) == 1 else ", ".join(words[:-1]) + " and " + words[-1]


def settlement_points(rule: RuleView | None) -> list[dict[str, Any]]:
    """How open-access energy is settled under a settlement rule, in sentences, each citing the field
    of the rule it restates. Nothing is added to the rule: a field it does not state is not described."""
    if rule is None:
        return [
            _pt(
                "No banking and settlement rule is approved for this source here: energy is set off only "
                "against consumption in the same 15-minute block, and what is not consumed then is not banked.",
                _model_ref("ergPLAN's treatment where no settlement rule is approved"),
            )
        ]
    body, b = rule.body, rule.body.get("banking") or {}
    out: list[dict[str, Any]] = []
    cycle = body.get("method") == "cycle_pooled"
    gran = str(body.get("granularity", "block_15min"))
    first = {
        "block_15min": "First, in every 15-minute block, the energy delivered at your meter is set off against "
        "what you consume in that block.",
        "hourly": "First, in every hour, the energy delivered at your meter is set off against what you consume "
        "in that hour.",
        "tod_slot_monthly": "First, for each month, the energy delivered in a time-of-day slot is set off against "
        "what you consume in that slot over the month.",
        "monthly": "First, the month's energy delivered is set off against the month's consumption.",
    }.get(gran, f"Energy is set off on the basis: {gran}.")
    out.append(_pt(first, rule.ref("/granularity")))
    if not b.get("allowed"):
        out.append(
            _pt(
                "Banking is not allowed: energy not set off as above is not carried.",
                rule.ref("/banking/allowed"),
            )
        )
        return out
    period = str(b.get("period", "monthly"))
    span = {
        "monthly": "month",
        "quarterly": "quarter",
        "financial_year": "financial year",
        "calendar_year": "year",
    }.get(period, period)
    out.append(
        _pt(
            f"What is left over is banked with the DISCOM and may be drawn within the same {span}.",
            rule.ref("/banking/period"),
        )
    )
    ch = b.get("charge") or {}
    if ch.get("value") not in (None, ""):
        if ch.get("kind") == "in_kind_percent":
            out.append(
                _pt(
                    f"The DISCOM keeps {ch['value']}% of the energy banked as the banking charge (in kind): "
                    f"of every 100 units banked, {Decimal(100) - Decimal(str(ch['value']))} can be drawn.",
                    rule.ref("/banking/charge/value"),
                )
            )
        else:
            out.append(
                _pt(
                    f"A banking charge of {ch['value']} ({ch.get('kind')}) applies.",
                    rule.ref("/banking/charge/value"),
                )
            )
    lim = b.get("limit") or {}
    if cycle and lim:
        legs = []
        if lim.get("drawal_pct"):
            legs.append(f"{lim['drawal_pct']}% of the grid drawal left after the first set-off")
        if lim.get("generation_pct") and lim.get("combine") != "drawal":
            legs.append(f"{lim['generation_pct']}% of the energy generated")
        if legs:
            how = {"max": "the higher of ", "min": "the lower of "}.get(str(lim.get("combine", "max")), "")
            text = legs[0] if len(legs) == 1 else how + " and ".join(legs)
            extra = (
                f", and never more than {lim['consumption_pct']}% of the consumption"
                if lim.get("consumption_pct")
                else ""
            )
            out.append(
                _pt(
                    f"The most that can be banked in a {span} is {text}{extra}. Surplus beyond it is not banked.",
                    rule.ref("/banking/limit"),
                )
            )
    cap = b.get("cap") or {}
    if not cycle and cap:
        words = ", ".join(f"{k.replace('_', ' ')}: {v}" for k, v in cap.items() if v not in (None, "", []))
        if words:
            out.append(_pt(f"A ceiling applies to the energy banked ({words}).", rule.ref("/banking/cap")))
    if cycle:
        dr = str(b.get("drawal_rule", "higher_to_lower"))
        if dr == "same_tod":
            text = "Banked energy can be drawn only in the time-of-day category it was banked in."
        elif dr == "higher_to_lower":
            text = (
                "Banked energy can be drawn in the time-of-day category it was banked in or in a lower-value "
                "one (peak, then normal, then off-peak), never in a higher one."
            )
        else:
            m = b.get("matrix") or {}
            parts = [
                f"energy banked at {CAT_WORDS[c]} can be drawn at {_cats(list(m.get(c) or [c]))}"
                for c in ("peak", "normal", "off_peak")
            ]
            text = "Where banked energy can be drawn: " + "; ".join(parts) + "."
        out.append(_pt(text, rule.ref("/banking/drawal_rule")))
        out.append(
            _pt(
                "Within those limits banked energy is drawn first where the grid costs most, so it replaces the "
                "dearest grid units.",
                rule.ref("/method"),
            )
        )
    else:
        wins = b.get("drawal_windows") or []
        if wins:
            out.append(
                _pt(
                    "Banked energy is drawn in time order (first banked, first drawn), in the slots the rule's "
                    "drawal windows allow.",
                    rule.ref("/banking/drawal_windows"),
                )
            )
        if b.get("carry_over"):
            out.append(
                _pt(
                    "Unused banked energy carries forward within the period.", rule.ref("/banking/carry_over")
                )
            )
    lapse = b.get("lapse") or {}
    treat = str(lapse.get("treatment", "lapses"))
    if treat.startswith("paid"):
        text = f"Banked energy not drawn by the end of the {span} is paid for by the DISCOM at the rate the rule sets."
    else:
        text = f"Banked energy not drawn by the end of the {span} lapses: it is not paid for."
    out.append(_pt(text, rule.ref("/banking/lapse") if lapse else rule.ref("/banking/period")))
    return out


def iex_points(order: list[str] | None, rule: RuleView | None) -> list[dict[str, Any]]:
    """How an exchange purchase is settled. This is how ergPLAN models it (general exchange practice);
    it is not restated from a stored page, and is shown as such."""
    ref = _model_ref(
        "how ergPLAN models exchange purchases (general practice on the exchange; not cited to a page)"
    )
    pts = [
        _pt(
            "A bid is placed for each 15-minute block: a day ahead on the day-ahead and green day-ahead markets "
            "(DAM, G-DAM), about an hour ahead on the real-time market (RTM). The price paid is the exchange's "
            "area clearing price for that block.",
            ref,
        ),
        _pt(
            "The energy cleared is scheduled to your drawal point as short-term open access. It bears the "
            "inter-state and state transmission charges and losses, the wheeling charge and loss, and the "
            "cross-subsidy and additional surcharges, as set out in its landed-cost table.",
            ref,
        ),
        _pt(
            "It is set off against your consumption in the same 15-minute block only. Exchange energy cannot be "
            "banked: what you do not consume in the block is not carried to any other block.",
            ref,
        ),
        _pt(
            "The exchange is paid through your trading member around delivery (pay-in before, settlement after); "
            "the DISCOM bills the open-access charges with the monthly bill.",
            ref,
        ),
    ]
    if order:
        words = {"captive_generation": "captive energy", "bilateral": "contracted (third-party) energy",
                 "exchange": "exchange energy", "banked_energy": "banked energy"}  # fmt: skip
        seq = ", then ".join(words.get(x, x) for x in order)
        pts.insert(
            3,
            _pt(
                f"Order of set-off in a block: {seq}; the grid supplies the rest.",
                rule.ref("/set_off_order") if rule is not None else ref,
            ),
        )
    return pts


# ---------------------------------------------------------------- the book


def _slot_hours(start: str, end: str) -> Decimal:
    def mins(t: str) -> int:
        h, m = t.split(":")
        return int(h) * 60 + int(m)

    d = (mins(end) - mins(start)) % (24 * 60)
    return Decimal(d or 24 * 60) / 60


def _j(v: SourcedValue) -> dict[str, Any]:
    return v.model_dump(mode="json")


def _route_table(
    out: landed_api.LandedOut, source_id: str, hours: dict[str, Decimal], grid: dict[str, SourcedValue]
) -> dict[str, Any]:
    src = next(x for x in out.sources if x.id == source_id)
    slots = []
    tot_w: list[tuple[Decimal, SourcedValue]] = []
    sav_w: list[tuple[Decimal, SourcedValue]] = []
    for w in src.by_slot:
        # the banking charge is levied only on the units banked, so the book states it beside the
        # landed cost instead of adding it to every unit (the plan's settlement decides how many bank)
        bank = [x for x in w.lines if (x.label or "").startswith("Banking charge")]
        lines = [x for x in w.lines if x not in bank]
        total = (
            derive(
                w.total.value - sum((x.value for x in bank), ZERO),
                Unit.INR_PER_KWH,
                lines,
                label="Landed cost before banking",
            )
            if bank
            else w.total
        )
        row: dict[str, Any] = {"slot": w.slot, "lines": [_j(x) for x in lines], "landed": _j(total)}
        if bank:
            banking = derive(
                sum((x.value for x in bank), ZERO),
                Unit.INR_PER_KWH,
                bank,
                label="Banking charge on a banked unit",
            )
            row["banking"] = _j(banking)
            # beside them, the landed-cost screen's figure: the banking charge on every unit, the most
            # banking can add (the same total, so the book and the screen agree)
            row["landed_upper"] = _j(
                derive(
                    total.value + banking.value,
                    Unit.INR_PER_KWH,
                    [total, banking],
                    label="Landed cost if every unit is banked (upper bound)",
                )
            )
        tot_w.append((hours.get(w.slot, ZERO), total))
        if w.slot in grid:
            g = grid[w.slot]
            saving = derive(
                g.value - total.value, Unit.INR_PER_KWH, [g, total], label="Saving on the grid tariff"
            )
            row["grid"] = _j(g)
            row["saving"] = _j(saving)
            sav_w.append((hours.get(w.slot, ZERO), saving))
        slots.append(row)

    def avg(pairs: list[tuple[Decimal, SourcedValue]], label: str) -> dict[str, Any] | None:
        h = sum((p[0] for p in pairs), ZERO)
        if not pairs or h <= 0:
            return None
        v = sum((p[0] * p[1].value for p in pairs), ZERO) / h
        return _j(derive(v.quantize(Decimal("0.0001")), Unit.INR_PER_KWH, [p[1] for p in pairs], label=label))

    return {
        "source": source_id,
        "label": src.label,
        "available": src.available,
        "reason": src.reason,
        "slots": slots,
        "landed_avg": avg(tot_w, "Landed cost, average over the day's hours"),
        "saving_avg": avg(sav_w, "Saving on the grid tariff, average over the day's hours")
        if len(sav_w) == len(tot_w)
        else None,
        "gaps": src.gaps,
        "notes": [n for n in src.notes if not n.startswith("the banking charge is shown")],
        "rules_used": src.rules_used,
        # what a price that is built up, not quoted, rests on: the plant's capital cost, O&M, CUF,
        # degradation, life and discount rate (captive), the equity share (26% equity)
        "inputs": [_j(x) for x in src.inputs],
    }


def build(
    s: Session,
    *,
    discom: str,
    month: date,
    connection_id: str | None = None,
    category: str | None = None,
    voltage_kv: Decimal | None = None,
    contract_demand_kva: Decimal | None = None,
    prices: dict[str, Decimal] | None = None,
    as_of: date | None = None,
) -> dict[str, Any]:
    """The rule book's content for a DISCOM and a month. With `connection_id`, for that client's
    connection (its category, voltage, contract demand and bills); otherwise for a reference
    industrial consumer (`category`, `voltage_kv`, `contract_demand_kva`; defaults: the schedule's
    HT industrial category, 33 kV, 5,000 kVA). `prices`: the PPA tariff to quote per source id
    (INR/kWh); a source left out takes ergPLAN's benchmark, shown as an assumption."""
    month = month.replace(day=1)
    if as_of is None:
        # the rules in force: today for the current month, the 15th of the month for any other
        today = date.today()
        as_of = today if (today.year, today.month) == (month.year, month.month) else month.replace(day=15)
    j = s.get(Jurisdiction, discom)
    if j is None or discom not in defaults.DISCOM_STATE:
        raise BookError(f"unknown DISCOM {discom!r}")
    state = defaults.DISCOM_STATE[discom]
    sj = s.get(Jurisdiction, state)
    if connection_id is not None:
        conn = s.get(Connection, connection_id)
        if conn is None or conn.discom_id != discom:
            raise BookError("the connection is not on this DISCOM")
        try:
            facts = _facts(s, conn)
        except Exception as e:  # the connection lacks a category or a voltage
            raise BookError(str(getattr(e, "detail", e))) from e
        factor, _pf = _kvah_factor(s, conn)
        levies = bill_levies(s, conn)  # FPPCA and duty at the client's own bills' rates
        assets = list(s.scalars(select(Asset).where(Asset.site_id == conn.site_id)))
        recorded = site_intake.recorded_ownership(conn)
        site = s.get(Site, conn.site_id)
        client = s.get(Client, site.client_id) if site else None
        consumer = {
            "kind": "client",
            "connection_id": conn.id,
            "client": client.name if client else None,
            "site": site.name if site else None,
        }
    else:
        ref_cat, ref_block = reference_category(s, discom, as_of)
        cat = category or ref_cat or "HT industrial"
        facts = escope.ConnectionFacts(
            discom=discom,
            state=state,
            category=cat,
            voltage_kv=voltage_kv or Decimal(33),
            connection_kind="ht",
            consumer_kind=defaults.connection_kind(voltage_kv or Decimal(33)),
            contract_demand_kva=contract_demand_kva or Decimal(5000),
            rate_block=ref_block if cat == ref_cat else None,
        )
        factor, assets, recorded = _reference_factor(), [], None
        levies = ()  # a reference consumer has no bills: the levies stay named gaps
        consumer = {"kind": "reference", "connection_id": None, "client": None, "site": None}
    consumer.update(
        rate_block=facts.rate_block,
        category=facts.category,
        voltage_kv=str(facts.voltage_kv),
        contract_demand_kva=str(facts.contract_demand_kva) if facts.contract_demand_kva is not None else None,
    )
    overrides = {
        k: landed_api.SourceScenario(price_inr_per_kwh=v)
        for k, v in (prices or {}).items()
        if k in RE_SOURCES
    }
    by_route: dict[str, landed_api.LandedOut] = {}
    for route, _label, _what in ROUTES:
        sc = landed_api.ScenarioIn(month=month.month, as_of=as_of, sources=overrides)
        by_route[route] = landed_api.landed(
            s,
            facts=facts,
            factor=factor,
            contract_kva=facts.contract_demand_kva,
            assets=assets,
            recorded=recorded,
            sc_in=sc,
            connection_id=connection_id or "",
            ownership_all=route,
            levies=levies,
        )
    base = by_route["third_party"]
    hours = {x.name: _slot_hours(x.start, x.end) for x in base.slots}
    grid = {a.slot: a.rate for a in base.avoided}
    renewable = []
    for sid in RE_SOURCES:
        routes = []
        for route, label, what in ROUTES:
            t = _route_table(by_route[route], sid, hours, grid)
            routes.append({"route": route, "route_label": label, "route_note": what, **t})
        spec = landed_cost.SOURCE_BY_ID[sid]
        renewable.append(
            {
                "source": sid,
                "label": spec.label,
                "kind": spec.source,
                "connection": spec.connection,
                "routes": routes,
            }
        )
    exchange = [_route_table(base, sid, hours, grid) for sid in IEX_SOURCES]
    # settlement: the rule that governs intra-state solar and wind for this consumer
    chain = jurisdiction_chain(s, discom)
    ctx = escope.context(
        facts, date(as_of.year if month.month >= as_of.month else as_of.year + 1, month.month, 15)
    )
    settlements = repository.approved_rules(s, "settlement", chain, as_of)
    settle: list[dict[str, Any]] = []
    order: list[str] | None = None
    first_rule: RuleView | None = None
    seen: set[str] = set()
    for src_kind in ("solar", "wind"):
        for own in ("third_party", "group_captive", "captive"):
            r = next(
                (
                    x
                    for x in settlements
                    if matches(
                        x.scope, {**ctx, "connection": "intra_state", "source": src_kind, "ownership": own}
                    )
                ),
                None,
            )
            key = f"{r.id if r else 'none'}"
            if r is not None and first_rule is None:
                first_rule, order = r, list(r.body.get("set_off_order") or []) or None
            if key in seen:
                next(x for x in settle if x["key"] == key)["applies_to"].append(
                    f"{src_kind}, {own.replace('_', ' ')}"
                )
                continue
            seen.add(key)
            settle.append(
                {
                    "key": key,
                    "rule": r.label if r else None,
                    "rule_ref": r.source_ref if r else None,
                    "method": (r.body.get("method") or "chronological") if r else None,
                    "applies_to": [f"{src_kind}, {own.replace('_', ' ')}"],
                    "points": settlement_points(r),
                }
            )
    # the versions the book rests on
    rule_ids = {rid for o in by_route.values() for x in o.sources for rid in x.rules_used}
    rule_ids |= {r.id for r in settlements}
    versions = []
    if rule_ids:
        q = (
            select(RulebookVersion)
            .join(Rule, Rule.rulebook_version_id == RulebookVersion.id)
            .where(Rule.id.in_(rule_ids))
            .distinct()
        )
        for v in sorted(s.scalars(q), key=lambda v: (v.family, v.jurisdiction_id)):
            versions.append(
                {"id": v.id, "family": v.family, "jurisdiction": v.jurisdiction_id, "version": v.version,
                 "title": v.title, "origin": v.origin, "effective_from": v.effective_from.isoformat()}
            )  # fmt: skip
    gaps = dict(base.gaps)
    # a standby rate in the rulebook is not a landed-cost line; the book says so instead of hiding it
    standby = [
        r
        for r in repository.approved_rules(s, "oa_charge", jurisdiction_chain(s, discom), as_of)
        if r.body.get("component") == "standby_charge" and Decimal(str(r.body.get("value") or 0)) > 0
    ]
    if standby:
        gaps["standby_supply"] = (
            f"The rulebook holds a standby rate of ₹{standby[0].body['value']}/kWh for supply drawn from the "
            "DISCOM when the open-access source is down. It is not in the landed cost: it is paid only on "
            "standby units, which ergPLAN does not model yet."
        )
    if not grid:
        gaps["grid_tariff"] = (
            "No approved retail tariff for this consumer: the saving cannot be stated until the DISCOM's "
            "schedule is approved or, for a client, their bill gives the tariff."
        )
    content = {
        "title": f"{sj.name if sj else state} · {j.name} · {month.strftime('%B %Y')}",
        "state": state,
        "state_name": sj.name if sj else state,
        "discom": discom,
        "discom_name": j.name,
        "month": month.isoformat(),
        "as_of": as_of.isoformat(),
        "consumer": consumer,
        "slots": [
            {"name": x.name, "start": x.start, "end": x.end, "hours": str(hours[x.name])} for x in base.slots
        ],
        "grid": [{"slot": a.slot, "rate": _j(a.rate)} for a in base.avoided],
        "renewable": renewable,
        "exchange": exchange,
        "settlement": [{k: v for k, v in x.items() if k != "key"} for x in settle],
        "iex_settlement": iex_points(order, first_rule),
        "limits": [{"limit": x.limit, "label": x.label, "value": _j(x.value)} for x in base.limits],
        "versions": versions,
        "gaps": gaps,
    }
    _name_documents(s, content)
    content["trace"] = trace(content, documents.stored_ids(s))
    return content


def trace(content: dict[str, Any], stored: set[str] | None = None) -> dict[str, int]:
    """How far the book's own figures (the lines, not the totals computed from them) are traced:
    `page` opens a stored order or regulation page; `unstored` cites an order's page whose PDF is not
    in this server's library (given `stored`, the ids that are); `register` cites an order by name in
    the State Regulation Master; `assumption` is ergPLAN's benchmark; `other` is a bill or the
    client's word."""
    out = {"page": 0, "unstored": 0, "register": 0, "assumption": 0, "other": 0}
    vals = [g["rate"] for g in content["grid"]] + [x["value"] for x in content["limits"]]
    for r in [r for src in content["renewable"] for r in src["routes"]] + content["exchange"]:
        for sl in r["slots"]:
            vals += sl["lines"] + ([sl["banking"]] if sl.get("banking") else [])
    for v in vals:
        refs = [r for r in v["source_refs"] if r["kind"] != "derived"]
        kinds = {r["kind"] for r in refs}
        if "assumption" in kinds:
            out["assumption"] += 1
        elif "register" in kinds:
            out["register"] += 1
        elif kinds and kinds <= {"order", "regulation"}:
            here = stored is None or all(r.get("document_id") in stored for r in refs)
            out["page" if here else "unstored"] += 1
        else:
            out["other"] += 1
    return out


def unstored_documents(s: Session, content: dict[str, Any]) -> list[str]:
    """The documents a book cites whose PDF is not in the store now (asked each time the book is
    shown: the issue is frozen, the library is not)."""
    stored = documents.stored_ids(s)
    return sorted({str(r["document_id"]) for r in document_refs(content)} - stored)


def _name_documents(s: Session, node: Any, titles: dict[str, tuple[str, str | None]] | None = None) -> None:
    """Every ref to a stored page gains the document's title and the regulator's own link, so the
    book (on screen and as a file) can say which order a number opens."""
    if titles is None:
        stored = documents.stored_ids(s)
        titles = {d.id: (d.title, d.url_public) for d in s.scalars(select(SourceDocument)) if d.id in stored}
    if isinstance(node, dict):
        doc = node.get("document_id")
        if doc and node.get("kind") in ("order", "regulation") and doc in titles:
            node["document_title"], node["url_public"] = titles[doc]
        for v in node.values():
            _name_documents(s, v, titles)
    elif isinstance(node, list):
        for v in node:
            _name_documents(s, v, titles)


# ---------------------------------------------------------------- what changed


def figures(content: dict[str, Any]) -> dict[str, str]:
    """Every headline figure of a book by a stable name, for comparing two issues."""
    out: dict[str, str] = {}
    for g in content.get("grid", []):
        out[f"Grid tariff · {g['slot']}"] = g["rate"]["value"]
    for src in content.get("renewable", []):
        for r in src["routes"]:
            for sl in r["slots"]:
                out[f"{src['label']} · {r['route_label']} · {sl['slot']} · landed cost"] = sl["landed"][
                    "value"
                ]
                if sl.get("banking"):
                    out[f"{src['label']} · {r['route_label']} · {sl['slot']} · banking charge"] = sl[
                        "banking"
                    ]["value"]
                for ln in sl["lines"]:
                    out[f"{src['label']} · {r['route_label']} · {sl['slot']} · {ln.get('label')}"] = ln[
                        "value"
                    ]
    for x in content.get("exchange", []):
        for sl in x["slots"]:
            out[f"{x['label']} · {sl['slot']} · landed cost"] = sl["landed"]["value"]
    for i, block in enumerate(content.get("settlement", [])):
        for k, p in enumerate(block["points"]):
            out[f"Settlement {i + 1}.{k + 1}"] = p["text"]
    for lim in content.get("limits", []):
        out[f"Limit · {lim['label'] or lim['limit']}"] = lim["value"]["value"]
    return out


def changes(before: dict[str, Any] | None, after: dict[str, Any]) -> list[dict[str, str | None]]:
    """What differs between the last approved issue and this one: figures changed, added or gone."""
    if before is None:
        return []
    a, b = figures(before), figures(after)
    out: list[dict[str, str | None]] = []
    for k in sorted(set(a) | set(b)):
        x, y = a.get(k), b.get(k)
        same = x == y
        if not same and x is not None and y is not None:
            try:
                same = Decimal(x) == Decimal(y)
            except ArithmeticError:
                same = False
        if not same:
            out.append({"what": k, "before": x, "after": y})
    return out


# ---------------------------------------------------------------- stored issues


def _sha(content: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(content, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def last_approved(
    s: Session, discom: str, connection_id: str | None, before: date | None = None
) -> StateRulebook | None:
    q = select(StateRulebook).where(
        StateRulebook.discom_id == discom,
        StateRulebook.status == "approved",
        StateRulebook.connection_id.is_(None)
        if connection_id is None
        else StateRulebook.connection_id == connection_id,
    )
    if before is not None:
        q = q.where(StateRulebook.month <= before)
    return s.scalars(q.order_by(StateRulebook.month.desc(), StateRulebook.issue.desc())).first()


def generate(s: Session, *, actor: str, content: dict[str, Any] | None = None, **kw: Any) -> StateRulebook:
    """Compute a book (or take one already computed from the same arguments) and store it as a draft
    issue for review. The caller's session must carry the actor (session_scope(actor=...)): the audit
    trigger records who generated it."""
    if content is None:
        content = build(s, **kw)
    month = date.fromisoformat(content["month"])
    conn_id = kw.get("connection_id")
    prev = last_approved(s, kw["discom"], conn_id, month)
    content["changes"] = changes(prev.content if prev else None, content)
    content["compared_with"] = (
        {"id": prev.id, "month": prev.month.isoformat(), "issue": prev.issue, "title": prev.title}
        if prev
        else None
    )
    n = s.scalar(
        select(func.max(StateRulebook.issue)).where(
            StateRulebook.discom_id == kw["discom"],
            StateRulebook.month == month,
            StateRulebook.connection_id.is_(None)
            if conn_id is None
            else StateRulebook.connection_id == conn_id,
        )
    )
    inputs = {
        k: (
            str(v)
            if isinstance(v, Decimal | date)
            else {a: str(b) for a, b in v.items()}
            if isinstance(v, dict)
            else v
        )
        for k, v in kw.items()
        if v is not None
    }
    book = StateRulebook(
        state=content["state"],
        discom_id=kw["discom"],
        connection_id=conn_id,
        month=month,
        issue=(n or 0) + 1,
        status="draft",
        title=content["title"],
        inputs=inputs,
        content=content,
        content_sha256=_sha(content),
        created_by=actor,
    )
    s.add(book)
    s.flush()
    return book


def approve(s: Session, book_id: str, *, actor: str, role: str, note: str | None = None) -> StateRulebook:
    """An analyst approves a draft: it becomes the issue clients see, and the month's earlier approved
    issue for the same consumer is superseded. The content reviewed is the content approved: a book
    whose stored content no longer matches its hash is refused."""
    if role != "analyst":
        raise BookError("only an analyst approves a rule book")
    book = s.get(StateRulebook, book_id)
    if book is None:
        raise BookNotFound("rule book not found")
    if book.status != "draft":
        raise BookConflict(f"only a draft is approved; this issue is {book.status}")
    if _sha(book.content) != book.content_sha256:
        raise BookConflict("the stored content does not match what was generated; generate a new issue")
    for old in s.scalars(
        select(StateRulebook).where(
            StateRulebook.discom_id == book.discom_id,
            StateRulebook.month == book.month,
            StateRulebook.status == "approved",
            StateRulebook.connection_id.is_(None)
            if book.connection_id is None
            else StateRulebook.connection_id == book.connection_id,
        )
    ):
        old.status = "superseded"
    book.status, book.reviewed_by, book.reviewed_at, book.review_note = (
        "approved",
        actor,
        datetime.now(UTC),
        note,
    )
    s.flush()
    return book


def reject(s: Session, book_id: str, *, actor: str, role: str, note: str) -> StateRulebook:
    if role != "analyst":
        raise BookError("only an analyst rejects a rule book")
    book = s.get(StateRulebook, book_id)
    if book is None:
        raise BookNotFound("rule book not found")
    if book.status != "draft":
        raise BookConflict(f"only a draft is rejected; this issue is {book.status}")
    if not note.strip():
        raise BookError("say why the issue is rejected")
    book.status, book.reviewed_by, book.reviewed_at, book.review_note = (
        "rejected",
        actor,
        datetime.now(UTC),
        note,
    )
    s.flush()
    return book


def generate_month(s: Session, *, month: date, actor: str, clients: bool = True) -> list[StateRulebook]:
    """The month's drafts: a reference book for every DISCOM with an approved open-access charge or
    settlement rule, and (with `clients`) one for every client connection that can be priced. A
    consumer whose latest issue for the month is still a draft, or whose book would be identical to
    its approved issue for the month, is skipped."""
    out: list[StateRulebook] = []
    todo: list[tuple[str, str | None]] = []
    for discom in sorted(defaults.DISCOM_STATE):
        if s.get(Jurisdiction, discom) is None:
            continue
        chain = jurisdiction_chain(s, discom)
        if repository.approved_rules(s, "oa_charge", chain, month) or repository.approved_rules(
            s, "settlement", chain, month
        ):
            todo.append((discom, None))
    if clients:
        for conn in s.scalars(select(Connection)):
            if (
                conn.discom_id in defaults.DISCOM_STATE
                and conn.tariff_category
                and conn.voltage_kv is not None
            ):
                todo.append((conn.discom_id, conn.id))
    # DISCOMs of one state whose reference books carry the same figures share one book: the first
    # DISCOM's, naming the others it covers (Karnataka's five ESCOMs read one tariff order)
    built: list[tuple[str, str | None, dict[str, Any]]] = []
    lead: dict[tuple[str, str], dict[str, Any]] = {}
    for discom, conn_id in todo:
        try:
            content = build(s, discom=discom, month=month, connection_id=conn_id)
        except BookError:
            continue
        if conn_id is None:
            key = (content["state"], _sha(figures(content)))
            if key in lead:
                lead[key]["covers"].append({"discom": discom, "name": content["discom_name"]})
                continue
            content["covers"] = []
            lead[key] = content
        built.append((discom, conn_id, content))
    for discom, conn_id, content in built:
        same = (
            StateRulebook.connection_id.is_(None)
            if conn_id is None
            else StateRulebook.connection_id == conn_id
        )
        latest = s.scalars(
            select(StateRulebook)
            .where(StateRulebook.discom_id == discom, StateRulebook.month == month.replace(day=1), same)
            .order_by(StateRulebook.issue.desc())
        ).first()
        if latest is not None and latest.status == "draft":
            continue
        if latest is not None and latest.status == "approved":
            old = {k: v for k, v in latest.content.items() if k not in ("changes", "compared_with")}
            if _sha(old) == _sha(content):
                continue
        out.append(
            generate(s, actor=actor, content=content, discom=discom, month=month, connection_id=conn_id)
        )
    return out


# ---------------------------------------------------------------- the file


def _money(v: dict[str, Any] | None) -> str:
    return "—" if v is None else f"{Decimal(v['value']):.2f}"


def quantity(value: object) -> str:
    """A quantity as it is printed: no trailing zeros the source never had ("33.000" is 33 kV) and
    Indian digit grouping ("5000.000" is 5,000 kVA). Rendering only: a book's stored content is frozen
    once approved and is not rewritten. A value that is not a number is printed as it stands."""
    try:
        d = Decimal(str(value))
    except ArithmeticError:
        return str(value)
    if not d.is_finite():
        return str(value)
    text = format(d.normalize(), "f")
    sign = "-" if text.startswith("-") else ""
    whole, _, frac = text.lstrip("-").partition(".")
    head, tail = whole[:-3], whole[-3:]
    groups: list[str] = []
    while head:
        groups.insert(0, head[-2:])
        head = head[:-2]
    return sign + ",".join([*groups, tail]) + (f".{frac}" if frac else "")


_INPUT_UNIT = {"percent": "%", "INR": " ₹", "count": " years"}


def _plain(value: str) -> str:
    """A stored decimal without trailing zeros, with Indian digit grouping left to the reader."""
    d = Decimal(value)
    return format(d.quantize(Decimal(1)) if d == d.to_integral_value() else d.normalize(), "f")


def _cite(refs: list[dict[str, Any]], notes: list[str], stored: set[str] | None = None) -> str:
    """Footnote marks for a figure's sources; the notes list collects each distinct source once.
    Given `stored` (the documents in this server's library), a page that is not there is cited in
    words and not linked."""
    marks = []
    for r in refs:
        kind = r.get("kind")
        if kind in ("regulation", "order") and stored is not None and r.get("document_id") not in stored:
            text = (
                f"{r.get('document_title') or 'Order'}, page {r.get('page')} "
                "(its PDF is not yet in this server's library, so no page opens)"
                + (f": “{str(r.get('quote'))[:160]}”" if r.get("quote") else "")
            )
        elif kind in ("regulation", "order"):
            link = f"{get_settings().ergplan_public_url}/api/documents/{r.get('document_id')}#page={r.get('page')}"
            text = f"{r.get('document_title') or 'Order'}, [page {r.get('page')}]({link})" + (
                f": “{str(r.get('quote'))[:160]}”" if r.get("quote") else ""
            )
        elif kind == "register":
            text = f"Register: {r.get('note')}"
        elif kind == "derived":
            continue
        else:
            text = f"{str(kind).replace('_', ' ').capitalize()}: {r.get('note') or r.get('field_path') or ''}".strip()
        if text not in notes:
            notes.append(text)
        marks.append(str(notes.index(text) + 1))
    marks = list(dict.fromkeys(marks))[:6]
    return f" [{','.join(marks)}]" if marks else ""


def to_markdown(book: StateRulebook, stored: set[str] | None = None) -> str:
    """The rule book as a file: Markdown with every figure footnoted to its source. `stored` is the
    library as it is now (documents.stored_ids): a cited page that is not in it is not linked."""
    c = book.content
    traced = trace(c, stored) if stored is not None else {"unstored": 0, **(c.get("trace") or {})}
    notes: list[str] = []
    cons = c["consumer"]
    who = (
        f"{cons['client']} — {cons['site']}" if cons["kind"] == "client" else "Reference industrial consumer"
    )
    L: list[str] = [
        f"# State rule book — {c['title']}",
        "",
        f"Issue {book.issue} · status: **{book.status}**"
        + (
            f" · approved by {book.reviewed_by} on {book.reviewed_at:%d %b %Y}"
            if book.status == "approved" and book.reviewed_at
            else ""
        )
        + f" · rules in force on {c['as_of']}",
        "",
        f"Consumer: {who} · category {cons['category']} · {quantity(cons['voltage_kv'])} kV"
        + (
            f" · contract demand {quantity(cons['contract_demand_kva'])} kVA"
            if cons.get("contract_demand_kva")
            else ""
        ),
        *(
            ["", "Also covers (same figures): " + ", ".join(x["name"] for x in c["covers"]) + "."]
            if c.get("covers")
            else []
        ),
        *(
            [
                "",
                f"Where the numbers come from: {traced['page']} open a stored order or regulation page, "
                + (
                    f"{traced['unstored']} cite a page of an order whose PDF is not yet in this server's library, "
                    if traced["unstored"]
                    else ""
                )
                + f"{traced['register']} cite an order by name (State Regulation Master, no stored page yet), "
                f"{traced['assumption']} are assumed.",
            ]
            if c.get("trace")
            else []
        ),
        "",
        "All figures in ₹ per kWh at the consumer's meter. Numbers in brackets refer to the sources at the end. "
        "A “register” source is jouleWise's State Regulation Master (the analyst's citation; no stored page); an "
        "“assumption” is ergPLAN's own benchmark and should be replaced by your quote.",
        "",
        "## 1. What the grid costs",
        "",
    ]
    if c["grid"]:
        L += ["| ToD slot | Hours | Grid tariff |", "| --- | --- | ---: |"]
        hrs = {x["name"]: f"{x['start']}–{x['end']}" for x in c["slots"]}
        for g in c["grid"]:
            L.append(
                f"| {g['slot']} | {hrs.get(g['slot'], '')} | {_money(g['rate'])}{_cite(g['rate']['source_refs'], notes, stored)} |"
            )
    else:
        L.append(f"_{c['gaps'].get('grid_tariff', 'Not yet sourced.')}_")
    L += ["", "## 2. Landed cost and saving, by source and route", ""]
    for src in c["renewable"]:
        L += [f"### {src['label']}", ""]
        L += [
            "| Route | Landed cost (day average) | Saving on the grid tariff (day average) |",
            "| --- | ---: | ---: |",
        ]
        for r in src["routes"]:
            if r["available"]:
                L.append(f"| {r['route_label']} | {_money(r['landed_avg'])} | {_money(r['saving_avg'])} |")
            else:
                L.append(f"| {r['route_label']} | not available: {r['reason'] or 'no price'} | — |")
        L.append("")
        for r in src["routes"]:
            if not r["available"] or not r["slots"]:
                continue
            L += [f"**{r['route_label']}.** {r['route_note']}", ""]
            if r.get("inputs"):  # absent in a book generated before 25 Nov 2026
                L += [
                    "Built from (each an assumption, to be replaced by your own figure): "
                    + "; ".join(
                        f"{x.get('label')} {_plain(x['value'])}{_INPUT_UNIT.get(x['unit'], '')}"
                        f"{_cite(x['source_refs'], notes, stored)}"
                        for x in r["inputs"]
                    )
                    + ".",
                    "",
                ]
            names = [sl["slot"] for sl in r["slots"]]
            L += ["| Line | " + " | ".join(names) + " |", "| --- |" + " ---: |" * len(names)]
            labels = list(dict.fromkeys(ln.get("label") or "" for sl in r["slots"] for ln in sl["lines"]))
            for lab in labels:
                cells = []
                for sl in r["slots"]:
                    ln = next((x for x in sl["lines"] if (x.get("label") or "") == lab), None)
                    cells.append(
                        "—" if ln is None else f"{_money(ln)}{_cite(ln['source_refs'], notes, stored)}"
                    )
                L.append(f"| {lab} | " + " | ".join(cells) + " |")
            L.append(
                "| **Landed cost** | " + " | ".join(f"**{_money(sl['landed'])}**" for sl in r["slots"]) + " |"
            )
            L.append("| Grid tariff | " + " | ".join(_money(sl.get("grid")) for sl in r["slots"]) + " |")
            L.append(
                "| **Saving** | " + " | ".join(f"**{_money(sl.get('saving'))}**" for sl in r["slots"]) + " |"
            )
            if any(sl.get("banking") for sl in r["slots"]):
                L.append(
                    "| Banking charge, on banked units only | "
                    + " | ".join(
                        "—"
                        if not sl.get("banking")
                        else f"{_money(sl['banking'])}{_cite(sl['banking']['source_refs'], notes, stored)}"
                        for sl in r["slots"]
                    )
                    + " |"
                )
                L.append(
                    "| Landed cost if every unit is banked (upper bound) | "
                    + " | ".join(_money(sl.get("landed_upper")) for sl in r["slots"])
                    + " |"
                )
            for g in r["gaps"]:
                L.append(f"\n_Not yet sourced: {c['gaps'].get(g, g)}_")
            L.append("")
    L += ["### Exchange purchases (IEX)", ""]
    L += [
        "| Market | Landed cost (day average) | Saving on the grid tariff (day average) |",
        "| --- | ---: | ---: |",
    ]
    for x in c["exchange"]:
        if x["available"]:
            L.append(f"| {x['label']} | {_money(x['landed_avg'])} | {_money(x['saving_avg'])} |")
        else:
            L.append(f"| {x['label']} | not available: {x['reason'] or 'no prices'} | — |")
    L += ["", "## 3. How open-access solar and wind are settled, after banking", ""]
    for block in c["settlement"]:
        L.append(f"**{block['rule'] or 'No approved rule'}** (applies to: {'; '.join(block['applies_to'])})")
        L.append("")
        for i, p in enumerate(block["points"], 1):
            L.append(f"{i}. {p['text']}{_cite([p['ref']] if p.get('ref') else [], notes, stored)}")
        L.append("")
    L += ["## 4. How an exchange (IEX) purchase is settled", ""]
    for i, p in enumerate(c["iex_settlement"], 1):
        L.append(f"{i}. {p['text']}{_cite([p['ref']] if p.get('ref') else [], notes, stored)}")
    if c["limits"]:
        L += ["", "## 5. Limits", ""]
        for lim in c["limits"]:
            v = lim["value"]
            L.append(
                f"- {lim['label'] or lim['limit']}: {quantity(v['value'])} {v['unit']}"
                f"{_cite(v['source_refs'], notes, stored)}"
            )
    if c.get("changes"):
        cw = c.get("compared_with") or {}
        L += [
            "",
            f"## Changes since the last approved issue ({cw.get('title')}, issue {cw.get('issue')})",
            "",
        ]
        L += ["| What | Before | Now |", "| --- | ---: | ---: |"]
        for ch in c["changes"][:200]:
            L.append(f"| {ch['what']} | {ch['before'] or '—'} | {ch['after'] or '—'} |")
    L += ["", "## Rule versions this issue rests on", ""]
    for v in c["versions"]:
        L.append(
            f"- {v['family']} · {v['jurisdiction']} · v{v['version']} · {v['title'] or ''} ({v['origin']})"
        )
    L += ["", "## Sources", ""]
    for i, n in enumerate(notes, 1):
        L.append(f"{i}. {n}")
    return "\n".join(L) + "\n"
