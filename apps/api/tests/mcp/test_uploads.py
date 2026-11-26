"""Stage 16: the one-time upload link, for a file Claude cannot hand to a tool. It shows a page with a
file field, takes one file (a refused file does not spend it), lasts fifteen minutes, is bound to
the administrator, and passes the file to the upload screen's own function."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from pydantic import SecretStr
from sqlalchemy import select

from ergplan.core.settings import get_settings
from ergplan.db.base import session_scope
from ergplan.db.models import BillRead, McpUpload
from ergplan.intake import bill_reads
from ergplan.intake import bills as bill_store
from tests.intake import gen_files
from tests.intake.helpers import FakeBillx, result
from tests.mcp import helpers as h
from tests.platform.test_api_runs import make_site


@pytest.fixture
def ctx(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    who = h.configure(monkeypatch)
    fake = FakeBillx()
    monkeypatch.setattr(bill_store, "BillxClient", fake.client)
    return {"who": who, "fake": fake, "token": h.tokens(h.client(), who)["access_token"]}


def _path(url: str) -> str:
    assert url.startswith(h.BASE)
    return url[len(h.BASE) :]


def test_the_bill_link_works_once(ctx: dict[str, Any]) -> None:
    out = h.call(ctx["token"], "read_bill", {})
    assert out["status"] == "upload_needed"
    path = _path(out["upload_url"])
    c = h.client()
    page = c.get(path)
    assert page.status_code == 200 and "type='file'" in page.text and "application/pdf" in page.text
    # a file that is not a PDF is refused by the upload screen, and does not spend the link
    bad = c.post(path, files={"file": ("x.pdf", b"not a pdf", "application/pdf")})
    assert bad.status_code == 415 and bad.json()["received"] is False
    pdf = ctx["fake"].register("linked", result("pvvnl_passed"))
    first = c.post(path, files={"file": ("bill.pdf", pdf, "application/pdf")})
    assert first.status_code == 201, first.text
    assert first.json()["received"] is True and first.json()["read_id"]
    # once: the second file and the page are refused
    again = c.post(path, files={"file": ("bill.pdf", pdf, "application/pdf")})
    assert again.status_code == 410 and "used already" in again.json()["words"]
    assert c.get(path).status_code == 410
    # the read is the administrator's, made by the bill-read screen's function
    with session_scope() as s:
        read = s.get(BillRead, first.json()["read_id"])
        assert read is not None and read.uploaded_by == ctx["who"].username
        row = s.scalars(select(McpUpload)).one()
        assert row.token_sha256 not in path and path.rsplit("/", 1)[1] not in str(row.__dict__)
    queued = h.call(ctx["token"], "read_bill", {"upload_id": out["upload_id"]})
    assert queued["status"] == "queued" and queued["read_id"] == first.json()["read_id"]
    bill_reads.drain(ctx["fake"].client())
    done = h.call(ctx["token"], "read_bill", {"upload_id": out["upload_id"]})
    assert done["status"] == "done" and done["bills"][0]["discom"] == "pvvnl"


def test_a_browser_gets_a_page_back(ctx: dict[str, Any]) -> None:
    path = _path(h.call(ctx["token"], "read_bill", {})["upload_url"])
    pdf = ctx["fake"].register("page", result("pvvnl_passed"))
    r = h.client().post(
        path, files={"file": ("b.pdf", pdf, "application/pdf")}, headers={"Accept": "text/html"}
    )
    assert r.status_code == 201 and "Go back to Claude" in r.text


def test_a_link_expires_and_is_bound_to_the_administrator(
    ctx: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    c = h.client()
    pdf = ctx["fake"].register("late", result("pvvnl_passed"))
    out = h.call(ctx["token"], "read_bill", {})
    with session_scope() as s:
        row = s.get(McpUpload, out["upload_id"])
        assert row is not None
        row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    r = c.post(_path(out["upload_url"]), files={"file": ("b.pdf", pdf, "application/pdf")})
    assert r.status_code == 410 and "expired" in r.json()["words"]
    gone = h.call(ctx["token"], "read_bill", {"upload_id": out["upload_id"]})
    assert gone["status"] == "failed" and "expired" in gone["error"]

    out = h.call(ctx["token"], "read_bill", {})
    monkeypatch.setattr(get_settings(), "ergplan_admin_user", "someone-else")
    r = c.post(_path(out["upload_url"]), files={"file": ("b.pdf", pdf, "application/pdf")})
    assert r.status_code == 410 and "no longer works" in r.json()["words"]
    monkeypatch.setattr(get_settings(), "ergplan_admin_password", SecretStr(""))
    assert c.get(_path(out["upload_url"])).status_code == 410
    assert c.get("/mcp/upload/made-up").status_code == 410


def test_the_generation_link_takes_the_csv(ctx: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    site, _ = make_site(monkeypatch, result("pvvnl_passed"), {"voltage_kv": "33"})
    args = {
        "site_id": site,
        "technology": "solar",
        "provenance": "measured",
        "basis": "capacity",
        "capacity_mw": "2",
    }
    out = h.call(ctx["token"], "add_generation_profile", args)
    assert out["status"] == "upload_needed"
    path = _path(out["upload_url"])
    assert "text/csv" in h.client().get(path).text
    rows = gen_files.shares("solar", date(2025, 4, 1), 60)
    data = gen_files.csv_of(rows, "MW", factor=Decimal(2))
    r = h.client().post(path, files={"file": ("plant.csv", data, "text/csv")})
    assert r.status_code == 201, r.text
    done = h.call(ctx["token"], "add_generation_profile", args | {"upload_id": out["upload_id"]})
    p = done["profile"]
    assert done["status"] == "done" and p["provenance"] == "measured" and p["capacity_mw"] == "2"
    assert p["annual_cuf"]["source_refs"][0]["kind"] == "measured"
    assert h.client().post(path, files={"file": ("plant.csv", data, "text/csv")}).status_code == 410
