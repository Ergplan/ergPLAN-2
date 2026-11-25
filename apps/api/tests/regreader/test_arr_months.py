"""Hand-worked months (packages/rulebooks/golden/up/tariff-months.yaml) evaluated on the imported
drafts through the sandbox: the imported schedule and ToD rows produce the bill a person works out
from the order's pages."""

from __future__ import annotations

import calendar
from datetime import date
from decimal import Decimal as D
from typing import Any

import pytest
import yaml

from ergplan.core.settings import get_settings
from ergplan.core.sourced import RefKind, SourcedValue, SourceRef
from ergplan.core.units import Unit
from ergplan.db.base import session_scope
from ergplan.db.models import RulebookVersion
from ergplan.engine import scope as escope
from ergplan.engine.tariff_eval import MonthQuantities, evaluate
from ergplan.rulebook import sandbox, service

from .arr_helpers import import_up

CASES = yaml.safe_load((get_settings().rulebooks_dir / "golden" / "up" / "tariff-months.yaml").read_text())


def _sv(value: Any, unit: Unit) -> SourcedValue:
    return SourcedValue(
        value=D(str(value)), unit=unit, source_refs=(SourceRef(kind=RefKind.ASSUMPTION, note="worked case"),)
    )


@pytest.mark.usefixtures("jurisdictions")
def test_hand_worked_months_on_the_imported_drafts() -> None:
    import_up()
    assert len(CASES["cases"]) >= 6
    for c in CASES["cases"]:
        y, m = (int(x) for x in str(c["month"]).split("-"))
        when = date(y, m, calendar.monthrange(y, m)[1])
        facts = escope.ConnectionFacts(
            discom=c["discom"], state="UP", category=c["category"], voltage_kv=D(str(c["voltage_kv"])),
            connection_kind="ht", rate_block=c.get("rate_block"),
        )  # fmt: skip
        with session_scope() as s:
            chain = service.jurisdiction_chain(s, c["discom"])
            ctx = escope.context(facts, when)
            schedule = escope.first(
                sandbox.drafts_in_force(s, "tariff_schedule", chain, when, CASES["origin"]), ctx, chain
            )
            tod = escope.first(
                sandbox.drafts_in_force(s, "tod_slots", chain, when, CASES["origin"]), ctx, chain
            )
            assert schedule is not None, c["name"]
            v = s.get(RulebookVersion, schedule.version_id)
            assert v is not None and v.status == "draft" and c["expect"]["tariff_year"] in (v.title or "")
        unit = Unit.KVAH if schedule.body["billing_basis"] == "kvah" else Unit.KWH
        q = MonthQuantities(
            month=when.replace(day=1),
            units=_sv(c["units"], unit),
            by_slot={k: _sv(u, unit) for k, u in (c.get("by_slot") or {}).items()},
            max_kva=_sv(c["max_kva"], Unit.KVA),
            contract_kva=_sv(c["contract_kva"], Unit.KVA),
        )
        bill = evaluate(schedule, tod, q)
        exp = c["expect"]
        assert bill.sum("demand_charge") == D(exp["demand_charge"]), c["name"]
        assert bill.sum("energy_charge") == D(exp["energy_charge"]), c["name"]
        assert bill.sum("tod_adjustment") == D(exp["tod_adjustment"]), c["name"]
        assert bill.total.value == D(exp["total"]), c["name"]
        # every line cites the order page of the imported row (invariant 1)
        refs = [r for ln in bill.lines for r in ln.amount.source_refs if r.kind == RefKind.ORDER]
        assert refs and all(r.page and r.quote and r.document_id for r in refs)
