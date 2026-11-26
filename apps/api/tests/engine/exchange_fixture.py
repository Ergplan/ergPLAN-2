"""Exchange-purchase rules for tests (Stage 18).

No state's open access regulations are stored in this repository yet, so the committed drafts state
no schedule and no permission and every real verdict is "not yet sourced". The tests therefore use
FIXTURE rules, written here in the family's own shape and approved inside the test database only.
They are test inputs, not readings of any regulation: the fixture named after Rajasthan has the
shape the brief describes for it (a uniform schedule over the day and a barrier), the other two the
shape of a state where the exchange is workable. Their citations point at a fixture document.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from ergplan.db.base import session_scope
from ergplan.db.models import RuleTest
from ergplan.rulebook import service
from ergplan.rulebook.models import RuleView

DOC = "fixture-exchange-regulation"
EFFECTIVE = date(2025, 4, 1)


def ref(page: int, quote: str) -> dict[str, Any]:
    return {"kind": "regulation", "document_id": DOC, "page": page, "quote": quote}


def _ns(what: str) -> dict[str, Any]:
    return {"status": "not_stated", "obtain": f"the fixture's {what}"}


def body(**fields: dict[str, Any]) -> dict[str, Any]:
    """A body with every field not stated except those given (each given field is `stated`)."""
    names = (
        "permitted", "eligibility", "sldc_clearance", "schedule", "notice", "contract_demand", "standby",
        "deviation_under", "deviation_over", "cross_subsidy_surcharge", "additional_surcharge", "banking",
        "lock_in",
    )  # fmt: skip
    out: dict[str, Any] = {n: _ns(n) for n in names}
    for name, f in fields.items():
        out[name] = f if name == "barriers" else {"status": "stated", **f}
    return out


def refs_for(b: dict[str, Any]) -> dict[str, Any]:
    out = {
        f"/{n}": ref(i + 1, f"the fixture regulation's words on {n.replace('_', ' ')}")
        for i, (n, f) in enumerate(b.items())
        if isinstance(f, dict) and f.get("status") == "stated"
    }
    for i, _ in enumerate(b.get("barriers") or []):
        out[f"/barriers/{i}"] = ref(40 + i, "the fixture regulation's words on the barrier")
    return out


# a uniform schedule over the whole day, the contract demand reduced, and a cited barrier: the shape
# the brief gives for Rajasthan ("hard in practice")
UNIFORM_WITH_BARRIER = body(
    permitted={"value": "yes"},
    eligibility={"min_contract_demand_kva": "1000", "condition": "Contract demand of 1 MVA and above."},
    sldc_clearance={"required": "yes", "kind": "no_objection", "validity_days": "30"},
    schedule={"shape": "uniform_day", "condition": "The same quantity must be scheduled for all 24 hours."},
    contract_demand={"treatment": "reduced"},
    deviation_under={"treatment": "not_compensated"},
    deviation_over={"treatment": "multiple_of_energy_charge", "multiple": "1.25"},
    cross_subsidy_surcharge={"payable": "yes"},
    additional_surcharge={"payable": "yes"},
    banking={"allowed": "no"},
    barriers=[
        {
            "text": "The day's schedule must be given to the DISCOM before the exchange's results are "
            "known, and power not cleared may not be drawn from the DISCOM within the reduced contract "
            "demand."
        }
    ],
)
# a state where the exchange is workable: the schedule may vary block by block
BY_BLOCK = body(
    permitted={"value": "yes"},
    eligibility={"min_contract_demand_kva": "1000", "condition": "Contract demand of 1 MVA and above."},
    sldc_clearance={"required": "yes", "kind": "standing_clearance", "validity_days": "90"},
    schedule={"shape": "by_block"},
    contract_demand={"treatment": "stays"},
    deviation_under={"treatment": "not_compensated"},
    deviation_over={"treatment": "grid_tariff"},
    cross_subsidy_surcharge={"payable": "yes"},
    additional_surcharge={"payable": "no"},
    banking={"allowed": "no"},
)
# a second workable state: by block, drawal above the schedule at 1.5 times the energy charge, and
# the settlement of under-drawal not stated
BY_BLOCK_OVER_AT_MULTIPLE = body(
    permitted={"value": "yes"},
    schedule={"shape": "by_block"},
    deviation_over={"treatment": "multiple_of_energy_charge", "multiple": "1.5"},
    cross_subsidy_surcharge={
        "payable": "no",
        "condition": "Not levied on green power bought on the exchange.",
    },
)
# workable, with a uniform schedule and nothing barring it
UNIFORM = body(
    permitted={"value": "yes"},
    schedule={"shape": "uniform_day"},
    deviation_under={"treatment": "not_compensated"},
    deviation_over={"treatment": "grid_tariff"},
)


def view(b: dict[str, Any], jurisdiction: str = "RJ", scope: dict[str, Any] | None = None) -> RuleView:
    """The body as an engine sees it (no database)."""
    return RuleView(
        id=f"rule-{jurisdiction}",
        family="exchange_purchase",
        version_id=f"version-{jurisdiction}",
        jurisdiction_id=jurisdiction,
        scope=scope or {"source": ["iex"]},
        body=b,
        source_ref=ref(1, "the fixture regulation"),
        field_refs=refs_for(b),
    )


def row(b: dict[str, Any]) -> dict[str, Any]:
    return {
        "label": "fixture · exchange purchase",
        "scope": {"source": ["iex"]},
        "body": b,
        "source_ref": ref(1, "the fixture regulation"),
        "field_refs": refs_for(b),
    }


def propose(jurisdiction: str, b: dict[str, Any]) -> str:
    """A DRAFT of the fixture rule for a state (jurisdictions must be synced)."""
    with session_scope(actor="analyst@jw") as s:
        v = service.propose(
            s,
            family="exchange_purchase",
            jurisdiction_id=jurisdiction,
            rows=[row(b)],
            actor="analyst@jw",
            effective_from=EFFECTIVE,
            title=f"fixture exchange rule · {jurisdiction}",
        )
        return v.id


def approve(jurisdiction: str, b: dict[str, Any]) -> str:
    """The fixture rule approved in the TEST database, as an analyst would (with its golden case)."""
    name = f"fixture: {jurisdiction} schedule"
    with session_scope(actor="analyst@jw") as s:
        s.add(
            RuleTest(
                jurisdiction_id=jurisdiction,
                family="exchange_purchase",
                name=name,
                given={"context": {"source": "iex"}, "field": "/schedule/status", "origin": "manual"},
                expect={"value": b["schedule"]["status"]},
            )
        )
    vid = propose(jurisdiction, b)
    with session_scope(actor="analyst@jw", role="analyst") as s:
        service.approve(s, vid, actor="analyst@jw", role="analyst")
    return vid
