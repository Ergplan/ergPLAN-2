"""The tools answer at once in the tests: no worker runs beside them, the test drains the queues."""

from __future__ import annotations

import pytest

from ergplan.mcp.server import server
from ergplan.mcp.tools import bills, sizing


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch: pytest.MonkeyPatch) -> None:
    server()  # every tool module loaded
    monkeypatch.setattr(bills, "QUICK_WAIT_S", 0.0)
    monkeypatch.setattr(sizing, "QUICK_WAIT_S", 0.0)
