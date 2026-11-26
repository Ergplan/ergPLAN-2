"""Stage 16: every MCP tool, through the real /mcp endpoint with a good token (the SDK's client),
against the fixtures, billx faked. Each tool answers what the screen's route answers for the same
request, figure for figure (the tool computes nothing of its own), and every figure is a
SourcedValue with its source_refs."""

from __future__ import annotations

import base64
from collections.abc import Iterator
from datetime import date
from typing import Any

import pytest
from fastapi.testclient import TestClient

from ergplan.core.sourced import SourcedValue
from ergplan.intake import bill_reads
from ergplan.intake import bills as bill_store
from ergplan.mcp.tools import bills as bill_tools
from ergplan.mcp.tools import sizing as sizing_tools
from ergplan.platform import worker
from ergplan.platform.app import app
from tests.intake import gen_files
from tests.intake.helpers import FakeBillx, result
from tests.mcp import helpers as h
from tests.platform.test_api_plan import _truncate

AN = {"X-Ergplan-Actor": "analyst@joulewise.in", "X-Ergplan-Role": "analyst"}
rest = TestClient(app)


def sourced(node: Any, found: list[SourcedValue]) -> None:
    """Collect every SourcedValue in a result (each validated: a figure without a source fails)."""
    if isinstance(node, dict):
        if "source_refs" in node and "value" in node and "unit" in node:
            found.append(SourcedValue.model_validate(node))
            return
        for v in node.values():
            sourced(v, found)
    elif isinstance(node, list):
        for v in node:
            sourced(v, found)


@pytest.fixture(scope="module")
def made() -> Iterator[dict[str, Any]]:
    """A PVVNL HV-2 site at 33 kV from one synthetic bill, made through the tools as Claude would."""
    from tests.engine.helpers import restore_committed

    _truncate()
    mp = pytest.MonkeyPatch()
    try:
        who = h.configure(mp)
        mp.setattr(bill_tools, "QUICK_WAIT_S", 0.0)
        mp.setattr(sizing_tools, "QUICK_WAIT_S", 0.0)
        restore_committed()
        fake = FakeBillx()
        mp.setattr(bill_store, "BillxClient", fake.client)
        token = h.tokens(h.client(), who)["access_token"]
        pdf = fake.register("mcp", result("pvvnl_passed"))
        started = h.call(
            token, "read_bill", {"pdf_base64": base64.b64encode(pdf).decode(), "filename": "b.pdf"}
        )
        bill_reads.drain(fake.client())  # the worker's billx reading
        read = h.call(token, "read_bill", {"read_id": started["read_id"]})
        yield {"token": token, "who": who, "fake": fake, "started": started, "read": read}
    finally:
        mp.undo()
        _truncate()


@pytest.fixture(autouse=True)
def clean_db() -> None:  # the module's site is shared by its tests
    return None


@pytest.fixture
def site(made: dict[str, Any]) -> dict[str, Any]:
    """Confirmed and built once, for the tests after the bill's."""
    if "site" not in made:
        bill = made["read"]["bills"][0]
        for f in made["read"]["to_confirm"][0]["fields"] if made["read"]["to_confirm"] else []:
            h.call(made["token"], "confirm_field", {"bill_id": bill["id"], "path": f["path"]})
        made["site"] = h.call(
            made["token"], "build_site", {"bill_ids": [bill["id"]], "overrides": {"voltage_kv": "33"}}
        )
    return dict(made["site"])


def test_read_bill_sends_the_file_to_billx_and_lists_what_to_confirm(made: dict[str, Any]) -> None:
    assert made["started"]["status"] == "queued" and "billx is reading" in made["started"]["next_step"]
    read = made["read"]
    assert read["status"] == "done" and len(read["bills"]) == 1
    assert made["fake"].calls == 1  # billx read it, once
    bill = read["bills"][0]
    assert bill["discom"] == "pvvnl"
    # the reading is the screen's
    screen = rest.get(f"/bill-reads/{read['read_id']}", headers=AN).json()
    assert screen["bills"] == read["bills"]
    assert [t["bill_id"] for t in read["to_confirm"]] == ([bill["id"]] if bill["outstanding"] else [])
    for t in read["to_confirm"]:
        assert {f["path"] for f in t["fields"]} == set(bill["outstanding"])


def test_read_bill_without_a_file_gives_a_one_time_upload_link(made: dict[str, Any]) -> None:
    out = h.call(made["token"], "read_bill", {})
    assert out["status"] == "upload_needed"
    assert out["upload_url"].startswith(f"{h.BASE}/mcp/upload/") and "curl" in out["next_step"]
    wait = h.call(made["token"], "read_bill", {"upload_id": out["upload_id"]})
    assert wait["status"] == "waiting_for_upload"
    assert "no such upload link" in h.call_error(made["token"], "read_bill", {"upload_id": "nope"})
    assert "uploaded as a PDF" in h.call_error(made["token"], "read_bill", {"pdf_base64": "bm90IGEgcGRm"})


def test_confirm_field_records_the_confirmation(made: dict[str, Any], site: dict[str, Any]) -> None:
    bill_id = made["read"]["bills"][0]["id"]
    bill = rest.get(f"/bills/{bill_id}", headers=AN).json()
    assert bill["outstanding"] == [] and bill["accepted"] is True
    field = bill["fields"][0]["path"]
    out = h.call(made["token"], "confirm_field", {"bill_id": bill_id, "path": field})
    assert out == rest.get(f"/bills/{bill_id}", headers=AN).json()
    assert "no such field" in h.call_error(
        made["token"], "confirm_field", {"bill_id": bill_id, "path": "x.y"}
    )


def test_build_site_makes_the_connection_and_names_the_questions(site: dict[str, Any]) -> None:
    conn = rest.get(f"/connections/{site['connection_id']}", headers=AN).json()
    assert conn["discom_id"] == "pvvnl" and conn["voltage_kv"] == "33.000"
    assert site["load_profile_version"] == 1
    assert site["outstanding_questions"] and "list_questions" in site["next_step"]


def test_list_questions_and_answer(made: dict[str, Any], site: dict[str, Any]) -> None:
    sid = site["site_id"]
    out = h.call(made["token"], "list_questions", {"site_id": sid})
    assert out == rest.get(f"/sites/{sid}/questions").json()
    assert out["outstanding"] == site["outstanding_questions"]
    a = h.call(made["token"], "answer", {"site_id": sid, "key": "roof_sqm", "value": "4000"})
    assert a["changed"] is True and "roof_sqm" not in a["outstanding"]
    q = a["question"]
    assert q["answer"]["source_ref"]["kind"] == "client_stated"
    assert q["answer"]["stated_by"] == made["who"].username
    w = h.call(made["token"], "answer", {"site_id": sid, "key": "roof_sqm", "withdraw": True})
    assert "roof_sqm" in w["outstanding"]
    assert "no such question" in h.call_error(
        made["token"], "answer", {"site_id": sid, "key": "nope", "value": 1}
    )


def test_add_generation_profile_from_text(made: dict[str, Any], site: dict[str, Any]) -> None:
    rows = gen_files.shares("solar", date(2025, 4, 1), 60)
    csv_text = gen_files.csv_of(rows, "kW", factor=gen_files.Decimal(1000)).decode()
    out = h.call(
        made["token"],
        "add_generation_profile",
        {
            "site_id": site["site_id"],
            "technology": "solar",
            "provenance": "stated",
            "basis": "per_mw",
            "csv_text": csv_text,
        },
    )
    assert out["status"] == "done"
    p = out["profile"]
    assert p["technology"] == "solar" and p["in_use"] is True and p["created_by"] == made["who"].username
    assert p == rest.get(f"/generation-profiles/{p['id']}", headers=AN).json()
    assert p["annual_cuf"]["value"] == format(
        gen_files.cuf_pct(rows).quantize(gen_files.Decimal("0.01")), "f"
    )
    bad = h.call_error(
        made["token"],
        "add_generation_profile",
        {"site_id": site["site_id"], "technology": "solar", "provenance": "stated", "csv_text": "kW\n1\n2\n"},
    )
    assert "HTTP 422" in bad


def test_todays_cost_and_landed_cost_are_the_screens(made: dict[str, Any], site: dict[str, Any]) -> None:
    conn = site["connection_id"]
    today = h.call(made["token"], "todays_cost", {"connection_id": conn, "as_of": "2026-09-01"})
    assert today == rest.get(f"/connections/{conn}/today", params={"as_of": "2026-09-01"}, headers=AN).json()
    found: list[SourcedValue] = []
    sourced(today, found)
    assert today["annual"] and found and all(v.source_refs for v in found)
    landed = h.call(
        made["token"],
        "landed_cost",
        {"connection_id": conn, "scenario": {"as_of": "2026-09-01"}, "sources": ["oa_solar_intra"]},
    )
    import json

    screen = rest.get(
        f"/connections/{conn}/landed-cost",
        params={"scenario": json.dumps({"as_of": "2026-09-01"})},
        headers=AN,
    ).json()
    assert [s["id"] for s in landed["sources"]] == ["oa_solar_intra"]
    assert landed["sources"][0] == next(s for s in screen["sources"] if s["id"] == "oa_solar_intra")
    assert landed["avoided"] == screen["avoided"] and landed["limits"] == screen["limits"]


def test_run_sizing_answers_running_and_sizing_status_reads_the_run(
    made: dict[str, Any], site: dict[str, Any]
) -> None:
    spec = {
        "candidates": [{"kind": "oa_solar_intra", "ownership": "captive", "max_mw": "10"}],
        "resolution_min": 60,
        "as_of": "2026-09-01",
    }
    out = h.call(
        made["token"],
        "run_sizing",
        {"connection_id": site["connection_id"], "spec": spec, "name": "Captive solar"},
    )
    assert out["status"] == "running" and len(out["run_ids"]) == 1 and "sizing_status" in out["next_step"]
    assert out["runs"][0]["status"] == "queued"
    worker.drain()  # the worker solves it
    run_id = out["run_ids"][0]
    run = h.call(made["token"], "sizing_status", {"run_id": run_id})
    assert run["status"] == "done", run["error"]
    assert run == rest.get(f"/runs/{run_id}", headers=AN).json()
    found: list[SourcedValue] = []
    sourced(run["summary"], found)
    assert found and all(v.source_refs for v in found)
    made["run_id"] = run_id


def test_get_plan_transition_plan_and_share_link(made: dict[str, Any], site: dict[str, Any]) -> None:
    conn = site["connection_id"]
    plan = h.call(made["token"], "get_plan", {"connection_id": conn})
    assert plan == rest.get(f"/connections/{conn}/plan", headers=AN).json()
    assert plan["discom"] == "pvvnl"
    # the standard plan: queued for the worker, "running"
    std = h.call(made["token"], "run_sizing", {"connection_id": conn, "as_of": "2026-09-01"})
    assert std["status"] == "running" and len(std["run_ids"]) >= 1
    tp = h.call(
        made["token"],
        "transition_plan",
        {"connection_id": conn, "target": {"kind": "lowest_cost", "as_of": "2026-09-01"}},
    )
    assert tp["status"] == "running" and tp["pending_runs"] >= 1
    again = h.call(
        made["token"],
        "transition_plan",
        {"connection_id": conn, "target": {"kind": "lowest_cost", "as_of": "2026-09-01"}},
    )
    assert again["id"] == tp["id"]  # asking again returns the same plan
    assert tp == rest.get(f"/transition-plans/{tp['id']}", headers=AN).json() | {"status": tp["status"]}
    share = h.call(made["token"], "share_link", {"connection_id": conn})
    assert share["path"] == f"/p/{share['token']}"
    assert rest.get(share["path"]).status_code == 200


def test_state_rule_book_gives_only_an_approved_issue(made: dict[str, Any], site: dict[str, Any]) -> None:
    conn = site["connection_id"]
    assert "no approved rule book" in h.call_error(made["token"], "state_rule_book", {"connection_id": conn})
    assert "no approved rule book" in h.call_error(made["token"], "state_rule_book", {"discom": "pvvnl"})
    assert "give connection_id or discom" in h.call_error(made["token"], "state_rule_book", {})
    b = rest.post(
        "/statebooks/generate",
        json={"discom": "pvvnl", "month": "2026-11-01", "connection_id": conn},
        headers=AN,
    ).json()
    assert "no approved rule book" in h.call_error(made["token"], "state_rule_book", {"connection_id": conn})
    rest.post(f"/statebooks/{b['id']}/approve", json={"note": "checked"}, headers=AN)
    book = h.call(made["token"], "state_rule_book", {"connection_id": conn})
    assert book == rest.get(f"/connections/{conn}/statebook", headers=AN).json()
    assert book["status"] == "approved"


def test_the_server_lists_every_tool_typed_with_its_instructions(made: dict[str, Any]) -> None:
    init, tools = h.initialize_and_list(made["token"])
    text = init.instructions
    for words in ("ONE OR TWO AT A TIME", "ONLY from tool results", "ASSUMED", "time limit"):
        assert words in text
    names = {t.name for t in tools}
    assert names >= {
        "read_bill", "confirm_field", "list_questions", "answer", "add_generation_profile", "todays_cost",
        "landed_cost", "run_sizing", "sizing_status", "get_plan", "transition_plan", "state_rule_book",
        "share_link", "build_site",
    }  # fmt: skip
    for t in tools:
        assert t.inputSchema["type"] == "object" and t.outputSchema and t.description
        assert t.annotations is not None and t.annotations.destructiveHint is False
