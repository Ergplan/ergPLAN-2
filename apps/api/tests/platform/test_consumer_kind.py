"""Plan evaluation sets the supply level (`consumer_kind`: lt, ht or eht, from the supply voltage) in
the facts a rule's scope is matched on, so a row scoped by it (Bihar's ToD draft) is picked."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy.orm import Session

from ergplan.db.base import session_scope
from ergplan.db.models import Connection
from ergplan.engine import scope as escope
from ergplan.platform.routers.cost import _facts, _rules_for
from ergplan.rulebook import repository
from ergplan.rulebook.models import RuleView
from ergplan.rulebook.service import jurisdiction_chain
from tests.platform.test_api_runs import make_site, msedcl_5mw


def _tod(rid: str, jid: str, kinds: list[str]) -> RuleView:
    return RuleView(
        id=rid,
        family="tod_slots",
        version_id="fixture",
        jurisdiction_id=jid,
        scope={"consumer_kind": kinds},
        body={"slots": [{"name": f"all day ({rid})", "from": "00:00", "to": "24:00"}]},
        source_ref={"kind": "assumption", "note": "fixture row"},
    )


def test_the_supply_level_follows_the_voltage(monkeypatch: pytest.MonkeyPatch) -> None:
    _site, conn_id = make_site(monkeypatch, msedcl_5mw(), {})
    with session_scope(actor="analyst@joulewise.in") as s:
        conn = s.get(Connection, conn_id)
        assert conn is not None
        for kv, kind in (("0.415", "lt"), ("11", "ht"), ("33", "ht"), ("66", "eht"), ("132", "eht")):
            conn.voltage_kv = Decimal(kv)
            assert _facts(s, conn).consumer_kind == kind, kv
        s.rollback()


def test_a_row_scoped_by_the_supply_level_is_picked(monkeypatch: pytest.MonkeyPatch) -> None:
    _site, conn_id = make_site(monkeypatch, msedcl_5mw(), {})
    with session_scope() as s:
        conn = s.get(Connection, conn_id)
        assert conn is not None
        facts = _facts(s, conn)
        jid = jurisdiction_chain(s, facts.discom)[0]
        rows = [_tod("lt-row", jid, ["lt"]), _tod("ht-row", jid, ["ht", "eht"])]

        def reader(sess: Session, family: str, chain: list[str], on: date) -> list[RuleView]:
            if family == "tod_slots":
                return rows
            return repository.approved_rules(sess, family, chain, on)

        got: Any = _rules_for(s, facts, date(2026, 6, 1), reader)(date(2026, 6, 1)).tod
        assert facts.consumer_kind in ("ht", "eht")
        assert got is not None and got.id == "ht-row"
        # without the supply level no row scoped by it matches (the strict scope), as before
        bare = escope.ConnectionFacts(**{**facts.__dict__, "consumer_kind": None})
        assert _rules_for(s, bare, date(2026, 6, 1), reader)(date(2026, 6, 1)).tod is None
