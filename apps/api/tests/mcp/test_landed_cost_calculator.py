"""Stage 19: the MCP tool `landed_cost_calculator` through the real /mcp endpoint returns the same
figures as the calculator screen's endpoint (POST /calculator) for the same consumer, and refuses
what the screen refuses, in the same words."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from ergplan.platform.app import app
from tests.engine.helpers import restore_committed
from tests.mcp import helpers as h
from tests.platform.test_api_plan import _truncate

AN = {"X-Ergplan-Actor": "analyst@joulewise.in", "X-Ergplan-Role": "analyst"}
UP = {"discom": "pvvnl", "category": "HV-2", "voltage_kv": "33", "contract_demand_kva": "5000",
      "month": "2026-11-01", "as_of": "2026-11-01", "solar": {"ppa_tariff_inr_per_kwh": "3.05"}}  # fmt: skip


@pytest.fixture(scope="module")
def token() -> Iterator[str]:
    _truncate()
    mp = pytest.MonkeyPatch()
    try:
        who = h.configure(mp)
        restore_committed()
        yield str(h.tokens(h.client(), who)["access_token"])
    finally:
        mp.undo()
        _truncate()


@pytest.fixture(autouse=True)
def clean_db() -> None:
    return None


def test_the_tool_returns_the_screens_figures(token: str) -> None:
    got: dict[str, Any] = h.call(token, "landed_cost_calculator", {"consumer": UP})
    screen = TestClient(app).post("/calculator", json=UP, headers=AN)
    assert screen.status_code == 200
    assert got == screen.json()
    assert got["technologies"][0]["routes"][2]["slots"][0]["lines"][0]["value"] == "3.05"


def test_the_tool_refuses_what_the_screen_refuses(token: str) -> None:
    bad = {**UP, "category": "HV-9"}
    err = h.call_error(token, "landed_cost_calculator", {"consumer": bad})
    screen = TestClient(app).post("/calculator", json=bad, headers=AN)
    assert screen.status_code == 422 and screen.json()["detail"] in err and "(HTTP 422)" in err


def test_the_tool_is_listed_read_only(token: str) -> None:
    _init, tools = h.initialize_and_list(token)
    t = next(x for x in tools if x.name == "landed_cost_calculator")
    assert t.annotations is not None and t.annotations.readOnlyHint is True
    assert "consumer" in t.inputSchema["properties"] and t.outputSchema
