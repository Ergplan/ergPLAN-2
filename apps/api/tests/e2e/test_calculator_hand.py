"""Stage 19: for every state worked by hand (Stage 17's tests, and Uttar Pradesh here), the landed-cost
calculator's figures equal the hand-worked landed cost, read from tests/e2e/landed_hand/<state>.yaml.

The YAML shape (one file per state; keys this test reads, others are ignored):

    state, discom, category, rate_block (optional), voltage_kv, contract_demand_kva
    as_of: 'YYYY-MM-DD' (optional; default the first of the month)
    month: 11 or '2026-11' (the month priced)
    calculator: what the visitor states, in the calculator's input (report.calculator.CalculatorIn):
        solar / wind: {ppa_tariff_inr_per_kwh, capex_inr_per_kw, cuf_pct, om_pct_of_capex, life_years,
                       degradation_pct, capacity_mw, equity_cost_inr_per_kwh}, equity_share_pct,
        connection (intra_state | ists), iex_market (dam | gdam | rtm). REQUIRED: the hand figures
        rest on inputs, and without them the calculator would price ergPLAN's benchmarks instead.
    slot (or tod_slot): a ToD slot's name, or 'all ...' / absent for every slot (the default)
    routes: a list, each
        route: captive | group_captive | third_party | iex;  source: solar | wind | a source id
        slot (optional, as above)
        total: the landed cost per kWh at the meter WITHOUT the banking charge, as 'x.xxxx' or
               {value: 'x.xxxx'}; or approved_rulebook: {total_excluding_banking_upper_bound,
               banking_upper_bound} (the Maharashtra file's shape: its figures from the approved rules)
        banking (optional): the banking charge on a banked unit, 'x.xxxx' or {value, document_id, page},
               null where the rules state none
        lines (optional, a list): each {value, and document_id + page, or kind (client_stated,
               assumption)}; the calculator must show exactly these lines (equal to 4 places), each
               citing that page or carrying that kind. A dict of named lines (the Gujarat shape) is
               not compared line by line; its total is.
Figures are compared to 4 decimal places: the calculator's exact figure rounded half-up equals the
hand figure, or (a total) the sum of the calculator's lines each rounded to 4 places does."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient

from ergplan.platform import ratelimit
from ergplan.platform.app import app
from tests.engine.helpers import restore_committed
from tests.platform.test_api_plan import _truncate

HERE = Path(__file__).parent / "landed_hand"
FILES = sorted(HERE.glob("*.yaml"))
Q = Decimal("0.0001")
AN = {"X-Ergplan-Actor": "analyst@joulewise.in", "X-Ergplan-Role": "analyst"}
client = TestClient(app)


@pytest.fixture(scope="module", autouse=True)
def rules() -> Iterator[None]:
    _truncate()
    restore_committed()
    yield
    _truncate()


@pytest.fixture(autouse=True)
def clean_db() -> None:  # the restored rule books are shared by the module's tests
    ratelimit.reset()  # as conftest's clean_db does


def q(v: Any) -> Decimal:
    return Decimal(str(v)).quantize(Q, ROUND_HALF_UP)


def _value(v: Any) -> Decimal | None:
    if v is None:
        return None
    if isinstance(v, dict):
        return None if v.get("value") is None else q(v["value"])
    return q(v)


def _month(doc: dict[str, Any]) -> tuple[date, date]:
    as_of = date.fromisoformat(str(doc["as_of"])) if doc.get("as_of") else None
    m = doc["month"]
    if isinstance(m, int):
        first = date((as_of or date.today()).year, m, 1)
    else:
        first = date.fromisoformat(f"{str(m)[:7]}-01")
    return first, as_of or first


def consumer(doc: dict[str, Any]) -> dict[str, Any]:
    assert isinstance(doc.get("calculator"), dict), (
        "the hand file needs a `calculator:` block: the inputs its figures rest on (see this test's docstring)"
    )
    first, as_of = _month(doc)
    body: dict[str, Any] = {
        "discom": doc["discom"],
        "state": doc.get("state"),
        "category": doc.get("category"),
        "voltage_kv": str(doc["voltage_kv"]),
        "month": first.isoformat(),
        "as_of": as_of.isoformat(),
    }
    if doc.get("rate_block"):
        body["rate_block"] = doc["rate_block"]
    if doc.get("contract_demand_kva"):
        body["contract_demand_kva"] = str(doc["contract_demand_kva"])
    body.update(doc["calculator"])
    return body


def _slots(entry: dict[str, Any], doc: dict[str, Any], names: list[str]) -> list[str]:
    s = entry.get("slot") or doc.get("slot") or doc.get("tod_slot")
    if s is None or str(s).lower().startswith("all"):
        return names
    assert s in names, f"no ToD slot {s!r} in {names}"
    return [str(s)]


def _route(out: dict[str, Any], entry: dict[str, Any]) -> dict[str, Any]:
    if entry["route"] == "iex":
        return dict(out["exchange"]["route"])
    src = str(entry["source"])
    tech = "solar" if "solar" in src else "wind" if "wind" in src else src
    t = next(x for x in out["technologies"] if x["tech"] == tech)
    return dict(next(r for r in t["routes"] if r["route"] == entry["route"]))


def _hand_totals(entry: dict[str, Any]) -> tuple[Decimal | None, Decimal | None, bool]:
    """(landed without banking, banking on a banked unit, whether banking is stated)."""
    if "approved_rulebook" in entry:
        ab = entry["approved_rulebook"]
        bank = _value(ab.get("banking_upper_bound"))
        return _value(ab.get("total_excluding_banking_upper_bound")), bank or None, True
    return _value(entry.get("total")), _value(entry.get("banking")), "banking" in entry


def _refs_match(line: dict[str, Any], hand: dict[str, Any]) -> bool:
    if hand.get("document_id"):
        return any(
            r.get("document_id") == hand["document_id"] and r.get("page") == hand.get("page")
            for r in line["source_refs"]
        )
    if hand.get("kind"):
        return any(r["kind"] == hand["kind"] for r in line["source_refs"])
    return True


@pytest.mark.parametrize("path", FILES, ids=[p.stem for p in FILES])
def test_the_calculator_equals_the_hand_worked_landed_cost(path: Path) -> None:
    doc = yaml.safe_load(path.read_text())
    body = consumer(doc)
    r = client.post("/calculator", json=body, headers=AN)
    assert r.status_code == 200, r.text[:500]
    out = r.json()
    assert out["state"] == str(doc["state"]).upper() and out["discom"] == doc["discom"]
    names = [s["slot"] for s in out["slots"]]
    checked = 0
    for entry in doc["routes"]:
        route = _route(out, entry)
        where = f"{path.stem} {entry['route']} {entry.get('source', '')}"
        assert route["shown"] and route["slots"], f"{where}: not shown ({route['reason']})"
        landed, bank, bank_stated = _hand_totals(entry)
        by_slot = {s["slot"]: s for s in route["slots"]}
        for name in _slots(entry, doc, names):
            sl = by_slot[name]
            got = q(sl["landed"]["value"])
            summed = sum((q(x["value"]) for x in sl["lines"]), Decimal(0))
            assert landed in (got, summed), (
                f"{where} {name}: calculator {got} (lines {summed}), hand {landed}"
            )
            if bank_stated:
                cb = sl.get("banking")
                assert (q(cb["value"]) if cb else None) == bank, f"{where} {name}: banking {cb} vs {bank}"
                hb = entry.get("banking")
                if isinstance(hb, dict) and hb.get("document_id"):
                    assert _refs_match(cb, hb), f"{where}: banking does not cite p. {hb.get('page')}"
            hand_lines = entry.get("lines")
            if isinstance(hand_lines, list):
                calc = list(sl["lines"])
                assert len(calc) == len(hand_lines), f"{where} {name}: {[x['label'] for x in calc]}"
                for h in hand_lines:
                    hit = next(
                        (x for x in calc if q(x["value"]) == q(h["value"]) and _refs_match(x, h)), None
                    )
                    assert hit is not None, (
                        f"{where} {name}: no line {h} in {[(x['label'], x['value']) for x in calc]}"
                    )
                    calc.remove(hit)
            checked += 1
    assert checked > 0
