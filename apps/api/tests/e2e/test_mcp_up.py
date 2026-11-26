"""Stage 16 end to end: the assumed Uttar Pradesh site of tests/e2e/test_up_assumed.py (PVVNL HV-2,
5,000 kVA at 33 kV, the same twelve assumed bills), driven through the MCP tools as Claude drives
them: the bills go to billx through read_bill, the values asked are confirmed, the site is built,
the questions are listed and one is answered, a solar profile is supplied, then today's cost, the
landed cost and the five-component captive sizing of test_up_assumed at 60 minutes.

The same inputs go through the screens' routes exactly as test_up_assumed sends them (its `make()`,
then the same answer, the same profile and the same scenario), on a clean database; every figure the
tools give equals the screens' to the paisa. The tools compute nothing of their own.

Slow: two hourly five-component sizings. Set UP_OUT to keep both sets of figures as JSON."""

from __future__ import annotations

import base64
import json
import os
import re
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from ergplan.intake import bill_reads
from ergplan.intake import bills as bill_store
from ergplan.mcp.tools import bills as bill_tools
from ergplan.mcp.tools import sizing as sizing_tools
from ergplan.platform import ratelimit, worker
from tests.e2e import test_up_assumed as up
from tests.engine.helpers import restore_committed
from tests.intake import gen_files
from tests.intake.helpers import FakeBillx
from tests.mcp import helpers as h
from tests.platform.test_api_plan import _truncate

pytestmark = pytest.mark.slow
OUT = os.environ.get("UP_OUT")
client, AN = up.client, up.AN
ANSWER = ("roof_sqm", "4000")  # the person's roof: the rooftop plant is bounded at 400 kWp
SOLAR = gen_files.shares("solar", date(2025, 11, 1), 60)
SOLAR_CSV = gen_files.csv_of(SOLAR, "kW", factor=Decimal(1000))  # for 1 MW, the plant's meter
SPEC = {"candidates": up.FIVE, "as_of": up.AS_OF, "resolution_min": 60}
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def _ids_out(node: Any) -> Any:
    """The same figures with the rows' ids (new on each database) taken out."""
    return json.loads(UUID.sub("<id>", json.dumps(node, sort_keys=True)))


def _figures(today: dict[str, Any], landed: dict[str, Any], run: dict[str, Any]) -> dict[str, Any]:
    return {"today": today, "landed": landed, "summary": run["summary"]}


def through_the_screens() -> dict[str, Any]:
    site = up.make()
    r = client.put(f"/sites/{site['site']}/questions/{ANSWER[0]}", json={"value": ANSWER[1]}, headers=AN)
    assert r.status_code == 200, r.text
    r = client.post(
        f"/sites/{site['site']}/generation-profiles",
        files={"file": ("solar-profile.csv", SOLAR_CSV, "text/csv")},
        data={"technology": "solar", "provenance": "measured", "basis": "per_mw"},
        headers=AN,
    )
    assert r.status_code == 201, r.text
    today = client.get(f"/connections/{site['conn']}/today", params={"as_of": up.AS_OF}, headers=AN).json()
    landed = client.get(
        f"/connections/{site['conn']}/landed-cost",
        params={"scenario": json.dumps({"as_of": up.AS_OF})},
        headers=AN,
    ).json()
    sc = client.post(
        f"/sites/{site['site']}/scenarios",
        json={"connection_id": site["conn"], "name": "Sized in Claude", "spec": SPEC},
        headers=AN,
    ).json()
    run = client.post(f"/scenarios/{sc['id']}/run", params={"wait": "true"}, headers=AN).json()
    assert run["status"] == "done", run["error"]
    return _figures(today, landed, run)


def through_the_tools(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    restore_committed()  # the approved rule books, as up.make() loads them
    who = h.configure(monkeypatch)
    monkeypatch.setattr(bill_tools, "QUICK_WAIT_S", 0.0)
    monkeypatch.setattr(sizing_tools, "QUICK_WAIT_S", 0.0)
    fake = FakeBillx()
    monkeypatch.setattr(bill_store, "BillxClient", fake.client)
    token = h.tokens(h.client(), who)["access_token"]

    # 1. the twelve bills, each read by billx and its open values confirmed as read
    read_ids = []
    for first, factor in up.MONTHS:
        tag = first.strftime("%Y%m")
        pdf = fake.register(tag, up.assumed_bill(first, factor))
        out = h.call(
            token, "read_bill", {"pdf_base64": base64.b64encode(pdf).decode(), "filename": f"{tag}.pdf"}
        )
        read_ids.append(out["read_id"])
    assert bill_reads.drain(fake.client()) == 12  # the worker's readings
    bill_ids = []
    for rid in read_ids:
        out = h.call(token, "read_bill", {"read_id": rid})
        assert out["status"] == "done", out
        for item in out["to_confirm"]:
            for f in item["fields"]:
                h.call(token, "confirm_field", {"bill_id": item["bill_id"], "path": f["path"]})
        bill_ids += [b["id"] for b in out["bills"]]
    assert fake.calls == 12

    # 2. the site, then the questions before any sizing; the person answers one
    site = h.call(token, "build_site", {"bill_ids": bill_ids, "overrides": {"voltage_kv": "33"}})
    asked = h.call(token, "list_questions", {"site_id": site["site_id"]})
    assert asked["outstanding"] and ANSWER[0] in asked["outstanding"]
    got = h.call(token, "answer", {"site_id": site["site_id"], "key": ANSWER[0], "value": ANSWER[1]})
    assert got["question"]["answer"]["source_ref"]["kind"] == "client_stated"

    # 3. the solar profile
    prof = h.call(
        token,
        "add_generation_profile",
        {
            "site_id": site["site_id"],
            "technology": "solar",
            "provenance": "measured",
            "basis": "per_mw",
            "csv_text": SOLAR_CSV.decode(),
        },
    )
    assert prof["status"] == "done" and prof["profile"]["in_use"]

    # 4. today's cost, the landed cost, the sizing
    conn = site["connection_id"]
    today = h.call(token, "todays_cost", {"connection_id": conn, "as_of": up.AS_OF})
    landed = h.call(token, "landed_cost", {"connection_id": conn, "scenario": {"as_of": up.AS_OF}})
    sized = h.call(token, "run_sizing", {"connection_id": conn, "spec": SPEC, "name": "Sized in Claude"})
    assert sized["status"] == "running"
    worker.drain()
    run = h.call(token, "sizing_status", {"run_id": sized["run_ids"][0]})
    assert run["status"] == "done", run["error"]
    assert run["pins"]["generation_profiles"], "the run pins the supplied profile"
    return _figures(today, landed, run)


def test_the_tools_give_the_screens_figures_for_the_same_inputs(monkeypatch: pytest.MonkeyPatch) -> None:
    if not OUT:
        monkeypatch.setattr(up, "save", lambda *a, **k: None)
    screens = through_the_screens()
    _truncate()
    ratelimit.reset()
    tools = through_the_tools(monkeypatch)
    if OUT:
        Path(OUT, "40_mcp_screens.json").write_text(json.dumps(screens, indent=1))
        Path(OUT, "41_mcp_tools.json").write_text(json.dumps(tools, indent=1))
    assert _ids_out(tools["today"]) == _ids_out(screens["today"])
    assert _ids_out(tools["landed"]) == _ids_out(screens["landed"])
    assert _ids_out(tools["summary"]) == _ids_out(screens["summary"])
    s = tools["summary"]
    print(
        "\nMCP figures (UP assumed site, roof 4000 m², measured solar profile, five components, 60 min):",
        f"\n  today: {tools['today']['annual']['value']} INR/yr, {tools['today']['average_inr_per_kwh']['value']} INR/kWh",
        f"\n  before {s['annual']['before']['value']}, after {s['annual']['after']['value']}",
        "\n  sizes: " + ", ".join(f"{z['kind']} {z['kw']['value']} kW" for z in s["sizes"]),
    )
