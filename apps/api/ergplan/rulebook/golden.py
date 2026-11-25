"""Golden tests: in this context, the applicable rule's field equals this printed value.

Files: packages/rulebooks/golden/<state>/<jurisdiction>-<family>.yaml

    jurisdiction: npcl
    family: tariff_schedule
    tests:
      - name: HV-2 at 11 kV energy charge
        given: {context: {category: HV-2, voltage_kv: 11}, field: /energy_charge/base}
        expect: {value: "8.32"}
        source_ref: {kind: order, document: <sha256 prefix or id>, page: 384, quote: "..."}

`given.where` optionally filters rows on body fields ({/component: cross_subsidy_surcharge}).
A file may say which versions its tests are for, so that two tariff years of one DISCOM each have
their own cases (a test may also say so itself, inside `given`):

    effective_on: '2026-01-15'   # only versions whose effective window holds this date
    origin: arr_network          # only versions of this origin (an importer's drafts and what an
                                 # analyst approves of them)

A jurisdiction and family may have several files (one per tariff year); test names are unique
across them.

`expect` is one of {value: "..."} (one row applies and its field equals it, compared as decimals
when both are numbers), {absent: true} (no row applies), {count: n} (n rows apply).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..core.settings import get_settings
from ..db.models import RulebookVersion, RuleTest, RuleTestRun
from . import sandbox
from .models import RuleView
from .scope import matches, specificity


@dataclass
class TestOutcome:
    name: str
    passed: bool
    actual: dict[str, Any]


@dataclass
class GoldenResult:
    total: int
    passed: int
    outcomes: list[TestOutcome] = field(default_factory=list)

    @property
    def green(self) -> bool:
        return self.total > 0 and self.passed == self.total


SCOPE_KEYS = ("effective_on", "origin")  # which versions a file's (or a test's) cases are for


def golden_dir() -> Path:
    return get_settings().rulebooks_dir / "golden"


def load_files(root: Path | None = None) -> list[dict[str, Any]]:
    out = []
    for f in sorted((root or golden_dir()).rglob("*.yaml")):
        d = yaml.safe_load(f.read_text())
        if isinstance(d, dict) and "tests" in d:
            d["_file"] = str(f)
            out.append(d)
    return out


def sync_tests(session: Session, root: Path | None = None) -> int:
    """Upsert golden tests from files (by jurisdiction, family and name). Returns the count."""
    wanted: dict[tuple[str, str], dict[str, tuple[dict[str, Any], dict[str, Any], Any]]] = {}
    for d in load_files(root):
        cases = wanted.setdefault((d["jurisdiction"], d["family"]), {})
        for t in d["tests"]:
            if t["name"] in cases:
                raise ValueError(f"{d['_file']}: a second golden test named '{t['name']}'")
            given = dict(t["given"])
            for key in SCOPE_KEYS:
                if key in d and key not in given:
                    given[key] = str(d[key])
            cases[t["name"]] = (given, t["expect"], t.get("source_ref"))
    n = 0
    for (jid, fam), cases in wanted.items():
        existing = {
            t.name: t
            for t in session.scalars(
                select(RuleTest).where(RuleTest.jurisdiction_id == jid, RuleTest.family == fam)
            )
        }
        for name, (given, expect, ref) in cases.items():
            row = existing.get(name) or RuleTest(jurisdiction_id=jid, family=fam, name=name)
            row.given, row.expect, row.source_ref = given, expect, ref
            session.add(row)
            n += 1
        for name, row in existing.items():
            if name not in cases:
                session.delete(row)
    session.flush()
    return n


def _equal(a: Any, b: Any) -> bool:
    try:
        return Decimal(str(a)) == Decimal(str(b))
    except (InvalidOperation, ValueError):
        return str(a).strip() == str(b).strip()


def evaluate(
    rows: list[RuleView], given: dict[str, Any], expect: dict[str, Any]
) -> tuple[bool, dict[str, Any]]:
    ctx = given.get("context", {})
    where = given.get("where", {})
    hits = sorted(
        (r for r in rows if matches(r.scope, ctx) and all(_equal(r.get(k), v) for k, v in where.items())),
        key=lambda r: -specificity(r.scope),
    )
    if "absent" in expect:
        return (not hits) == bool(expect["absent"]), {"count": len(hits)}
    if "count" in expect:
        return len(hits) == int(expect["count"]), {"count": len(hits)}
    if not hits:
        return False, {"error": "no row applies"}
    top = [r for r in hits if specificity(r.scope) == specificity(hits[0].scope)]
    if len(top) > 1:
        return False, {"error": "ambiguous", "rows": [r.label or r.id for r in top]}
    value = top[0].get(given["field"])
    return _equal(value, expect["value"]), {"value": value, "row": top[0].label or top[0].id}


def applies(given: dict[str, Any], v: RulebookVersion) -> bool:
    """Whether a golden case is for this version (its `effective_on` date and `origin`, when stated)."""
    on = given.get("effective_on")
    if on is not None:
        d = date.fromisoformat(str(on))
        if d < v.effective_from or (v.effective_to is not None and d > v.effective_to):
            return False
    return given.get("origin") in (None, v.origin)


def run(session: Session, v: RulebookVersion) -> GoldenResult:
    """Evaluate the version's golden cases on its rows as the sandbox gives them (drafts included)."""
    tests = [
        t
        for t in session.scalars(
            select(RuleTest)
            .where(RuleTest.jurisdiction_id == v.jurisdiction_id, RuleTest.family == v.family)
            .order_by(RuleTest.name)
        )
        if applies(t.given, v)
    ]
    rows = sandbox.rules(session, v.id)
    session.execute(delete(RuleTestRun).where(RuleTestRun.rulebook_version_id == v.id))
    res = GoldenResult(total=len(tests), passed=0)
    for t in tests:
        ok, actual = evaluate(rows, t.given, t.expect)
        session.add(RuleTestRun(rulebook_version_id=v.id, test_id=t.id, passed=ok, actual=actual))
        res.outcomes.append(TestOutcome(t.name, ok, actual))
        res.passed += int(ok)
    session.flush()
    return res
