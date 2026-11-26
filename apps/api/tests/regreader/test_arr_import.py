"""The ARR Network import into the database: drafts only, through rulebook.service, idempotent,
with the order's PDF stored when its SHA-256 matches and a reference row when it cannot be fetched."""

from __future__ import annotations

import dataclasses
import hashlib
import re
from datetime import date
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select

from ergplan.core.ids import document_id
from ergplan.db.base import session_scope
from ergplan.db.models import AuditLog, Rule, RulebookVersion, RuleTestRun, SourceDocument
from ergplan.documents import service as documents
from ergplan.regreader import arr_import, arr_map
from ergplan.regreader.arr_network import ArrNetworkClient
from ergplan.regreader.tariff_oder import Mapped
from ergplan.rulebook import repository, sandbox, service
from tests.engine.helpers import restore_committed
from tests.fixtures.pdfgen import make_pdf

from .arr_helpers import FIXTURES, UP_EFFECTIVE, UP_ORDERS, import_up, offline_client, read

pytestmark = pytest.mark.usefixtures("jurisdictions")


def _versions() -> list[tuple[str, str, str, str, int]]:
    with session_scope() as s:
        return [
            (v.jurisdiction_id, v.family, v.status, v.origin, v.version)
            for v in s.scalars(
                select(RulebookVersion).order_by(
                    RulebookVersion.jurisdiction_id, RulebookVersion.family, RulebookVersion.version
                )
            )
        ]


def test_four_orders_become_twelve_drafts_and_nothing_is_approved() -> None:
    out = import_up()
    assert [(o.order.order_id, o.jurisdiction_id) for o in out] == [
        (67, "uppcl"),
        (66, "npcl"),
        (65, "uppcl"),
        (64, "npcl"),
    ]
    got = _versions()
    assert len(got) == 12 and {(j, f) for j, f, *_ in got} == {
        (j, f) for j in ("uppcl", "npcl") for f in ("tariff_schedule", "tod_slots", "oa_charge")
    }
    assert all(status == "draft" and origin == "arr_network" for _, _, status, origin, _ in got)
    by = {o.order.order_id: o for o in out}
    # the tariff of an order runs until the next imported order of the same jurisdiction
    assert (by[65].effective_from, by[65].effective_to) == (date(2025, 11, 22), date(2026, 7, 1))
    assert (by[67].effective_from, by[67].effective_to) == (date(2026, 7, 2), None)
    assert by[67].effective_basis == "the order's date printed in its title"  # 'dated 02/07/26'
    assert by[65].effective_basis == "given on the command line"
    assert by[67].mapped == {"oa_charge": 17, "tariff_schedule": 13, "tod_slots": 5}
    assert by[64].mapped == {"oa_charge": 8, "tariff_schedule": 11, "tod_slots": 4}
    with session_scope() as s:
        v = s.scalar(
            select(RulebookVersion).where(
                RulebookVersion.family == "tod_slots",
                RulebookVersion.jurisdiction_id == "uppcl",
                RulebookVersion.effective_from == date(2025, 11, 22),
            )
        )
        assert v is not None and v.effective_to == date(2026, 7, 1) and v.approved_by is None
        assert (
            v.title
            == "UPERC tariff order FY2025-26 (DVVNL, KESCO, MVVNL, PUVVNL, PVVNL) · ARR Network order 65"
        )
        assert v.created_by == "arr-network-import"
        for part in (
            "ARR Network order 65",
            "section-reader 3",
            "fetched 20",
            "not reviewed by a person",
            "NOT stored",
        ):
            assert part in (v.notes or "")
        row = sandbox.rules(s, v.id)[0]
        assert row.source_ref["kind"] == "order" and row.source_ref["page"] == 513
        assert row.source_ref["document_id"] == document_id(by[65].order.sha256)
        r = s.scalar(select(Rule).where(Rule.rulebook_version_id == v.id))
        assert r is not None and r.review["reader"] == "arr_network" and r.review["order_id"] == 65
        # every write went through the service under an actor and is in the audit log
        audited = s.scalar(
            select(func.count()).select_from(AuditLog).where(AuditLog.table_name == "rulebook_versions")
        )
        assert audited and audited >= 12


def test_running_it_again_changes_nothing_and_a_changed_source_revises_the_draft() -> None:
    import_up()
    before = _versions()
    with session_scope() as s:
        rules_before = s.scalar(select(func.count()).select_from(Rule))
        ids_before = sorted(s.scalars(select(Rule.id)))
    again = import_up()
    assert _versions() == before
    assert {v.action for o in again for v in o.versions} == {"unchanged"}
    with session_scope() as s:
        assert s.scalar(select(func.count()).select_from(Rule)) == rules_before
        assert sorted(s.scalars(select(Rule.id))) == ids_before  # not even rewritten
    # the source changes (here: another effective date for order 65): same versions, revised in place
    moved = arr_import.run(
        offline_client(), "up", UP_ORDERS, effective={**UP_EFFECTIVE, 65: date(2025, 12, 1)}
    )
    assert _versions() == before
    assert {v.action for o in moved if o.order.order_id == 65 for v in o.versions} == {"revised"}
    assert {v.action for o in moved if o.order.order_id == 67 for v in o.versions} == {"unchanged"}
    with session_scope() as s:
        assert s.scalar(select(func.count()).select_from(Rule)) == rules_before


def test_a_dry_run_writes_nothing() -> None:
    out = import_up(dry_run=True)
    assert _versions() == []
    with session_scope() as s:
        assert s.scalar(select(func.count()).select_from(SourceDocument)) == 0
    assert {v.action for o in out for v in o.versions} == {"would propose"}
    assert all(o.pdf == "not fetched (dry run)" for o in out)
    text = arr_import.summary(out)
    assert "order 67 · UPERC · FY2026-27 · DVVNL, KESCO, MVVNL, PUVVNL, PVVNL → uppcl" in text
    assert "tariff_schedule  uppcl      —    would propose   13 rows" in text
    assert "× claimed, existing or proposed figures, not the commission's approved rate" in text
    listed = arr_import.summary(out, verbose=True)
    assert "voltage not read with confidence: 'For supply above 66 V kV and up to 132 k BASE RATE'" in listed


def test_no_imported_draft_reaches_what_a_client_is_served() -> None:
    """Invariant 2 with the real import: the approved rules a client endpoint reads are the same
    rows before and after, in the year the drafts overlap and in the year only drafts cover."""
    restore_committed()

    def approved() -> dict[tuple[str, str, date], list[str]]:
        with session_scope() as s:
            return {
                (d, f, on): [
                    r.id for r in repository.approved_rules(s, f, service.jurisdiction_chain(s, d), on)
                ]
                for d in ("pvvnl", "npcl")
                for f in ("tariff_schedule", "tod_slots", "oa_charge")
                for on in (date(2026, 1, 15), date(2026, 8, 1))
            }

    before = approved()
    with session_scope() as s:
        status_before = {v.id: v.status for v in s.scalars(select(RulebookVersion))}
    out = import_up()
    assert approved() == before
    assert before[("pvvnl", "tariff_schedule", date(2026, 1, 15))] == []  # still no tariff for a client
    with session_scope() as s:
        for v in s.scalars(select(RulebookVersion)):
            if v.id in status_before:
                assert v.status == status_before[v.id]  # nothing superseded
            else:
                assert v.status == "draft" and v.origin == "arr_network" and v.approved_at is None
    # the builder cannot approve one either: the import's own session carries no analyst role
    vid = out[0].versions[0].version_id
    assert vid is not None
    with pytest.raises(service.ApprovalRefused), session_scope(actor="arr-network-import") as s:
        service.approve(s, vid, actor="arr-network-import", role="builder")


def test_golden_cases_of_every_imported_version() -> None:
    """Each imported version is evaluated (through the sandbox) on the cases written from its order's
    pages. The FY 2025-26 drafts are green. The FY 2026-27 drafts pass every case written for the
    import and fail exactly those of the approved rulebook that rest on what no table prints."""
    restore_committed()
    out = import_up()
    seen = {}
    with session_scope(actor="test") as s:
        for o in out:
            for v in o.versions:
                assert v.version_id is not None
                res = service.run_golden(s, v.version_id)
                assert res.total > 0, (o.order.order_id, v.family)
                seen[(o.order.order_id, v.family)] = (
                    res.total,
                    sorted(x.name for x in res.outcomes if not x.passed),
                )
                runs = s.scalar(
                    select(func.count())
                    .select_from(RuleTestRun)
                    .where(RuleTestRun.rulebook_version_id == v.version_id)
                )
                assert runs == res.total
    for order_id in (65, 64):  # FY 2025-26: only that year's cases apply, and all pass
        for family in ("tariff_schedule", "tod_slots", "oa_charge"):
            assert seen[(order_id, family)][1] == [], (order_id, family)
    assert seen[(65, "tariff_schedule")][0] == 26 and seen[(65, "oa_charge")][0] == 17
    assert seen[(64, "tod_slots")][0] == 20 and seen[(64, "oa_charge")][0] == 10
    # FY 2026-27: the approved rulebook's cases apply too; what fails is what the tables do not hold
    assert seen[(67, "tod_slots")] == (14, []) and seen[(66, "tod_slots")] == (17, [])
    prose = [
        "HV-2 billable demand floor 75% of contract",
        "HV-2 excess demand at 200%",
        "HV-2 urban 132 kV · energy",
    ]
    assert seen[(67, "tariff_schedule")] == (28, prose) and seen[(66, "tariff_schedule")] == (28, prose)
    assert seen[(67, "oa_charge")] == (18, ["CSS waived for captive use", "CSS waived for group captive"])
    assert seen[(66, "oa_charge")] == (
        15,
        ["CSS HV-2 at 220 kV", "CSS waived for captive use", "CSS waived for group captive",
         "additional surcharge zero", "no CSS below 11 kV for HV-2"],
    )  # fmt: skip
    with session_scope() as s:
        status = {
            (v.jurisdiction_id, v.family, v.version): v.golden_status
            for v in s.scalars(select(RulebookVersion).where(RulebookVersion.origin == "arr_network"))
        }
    assert sorted(status.values()).count("green") == 8 and sorted(status.values()).count("red") == 4


def test_orders_without_a_jurisdiction_are_refused_with_what_to_add() -> None:
    order, _ = read(67)
    with session_scope() as s:
        assert arr_import.jurisdiction_for(s, order, "up") == "uppcl"  # five DISCOMs, one group
        assert arr_import.jurisdiction_for(s, read(66)[0], "UP") == "npcl"
        assert arr_import.jurisdiction_for(s, order, "up", override="pvvnl") == "pvvnl"
        with pytest.raises(arr_import.ImportRefused, match="not all in MH"):
            arr_import.jurisdiction_for(s, order, "mh")
        with pytest.raises(arr_import.ImportRefused, match="not one of UP"):
            arr_import.jurisdiction_for(s, order, "up", override="msedcl")
        unknown = dataclasses.replace(order, utilities=("NO-SUCH-DISCOM",))
        with pytest.raises(arr_import.ImportRefused, match=r"jurisdictions\.yaml.*--jurisdiction 67="):
            arr_import.jurisdiction_for(s, unknown, "up")
        mixed = dataclasses.replace(order, utilities=("PVVNL", "NPCL"))
        with pytest.raises(arr_import.ImportRefused, match="share no group"):
            arr_import.jurisdiction_for(s, mixed, "up")


def test_effective_date_given_printed_or_said_to_be_assumed() -> None:
    o65 = read(65)[0]
    assert arr_import.effective_from_for(o65, date(2025, 11, 22)) == (
        date(2025, 11, 22),
        "given on the command line",
    )
    assert arr_import.effective_from_for(read(67)[0])[0] == date(2026, 7, 2)
    d, basis = arr_import.effective_from_for(o65)
    assert d == date(2025, 4, 1) and basis.startswith("ASSUMED: 1 April of the tariff year")
    dated = dataclasses.replace(o65, order_date=date(2025, 11, 22))
    assert arr_import.effective_from_for(dated) == (date(2025, 11, 22), "the order's date in the ARR Network")


PAGES = [
    ["RATE SCHEDULE HV-2", "For supply up to 11 kV", "Demand Charges Rs. 300.00 / kVA / month", "Energy Charges Rs. 7.10 / kVAh"],
    ["TABLE 10-3: WHEELING CHARGE APPROVED BY THE COMMISSION FOR FY 2026-27", "Average Wheeling charge Rs. /kWh 0.9318"],
    [],
]  # fmt: skip


def _row(page: int, quote: str, value: str = "1") -> dict[str, Any]:
    return {
        "label": quote[:40],
        "scope": {},
        "body": {"component": "wheeling", "basis": "inr_per_kwh", "value": value},
        "source_ref": {"kind": "order", "document_id": "d", "page": page, "quote": quote},
    }


def test_grounding_drops_rows_whose_numbers_are_not_on_the_stored_page() -> None:
    with session_scope(actor="test") as s:
        doc, _ = documents.register(
            s, make_pdf(PAGES), documents.DocumentMeta(title="order"), uploaded_by="test"
        )
        m = Mapped()
        m.by_family["oa_charge"] = [
            _row(
                2,
                "Table 10-3 WHEELING CHARGE APPROVED BY THE COMMISSION FOR FY 2026-27 | Average Wheeling charge | Rs. /kWh | 0.9318",
            ),
            _row(2, "Average Wheeling charge | Rs. /kWh | 0.9381"),  # digits transposed by a reader
            _row(1, "For supply up to 11 kV | Demand Charges | Rs. 300.00 / kVA / month"),
            _row(3, "Distribution Loss at 33 kV | 3.00%"),  # a page without text: kept, reported unchecked
            _row(9, "Distribution Loss at 11 kV | 8.00%"),  # a page the PDF does not have
        ]
        failed, passed, unchecked = arr_import.ground(doc, m)
    assert passed == 2 and unchecked == [3]
    assert [(u.fact_id[:24], u.reason) for u in failed] == [
        ("Average Wheeling charge ", "grounding: 0.9381 not printed on page 2"),
        ("Distribution Loss at 11 ", "grounding: page 9 is outside the PDF's 3 pages"),
    ]
    assert [r["source_ref"]["page"] for r in m.by_family["oa_charge"]] == [2, 1, 3]


def test_the_pdf_is_stored_when_its_sha256_matches_and_referenced_when_it_cannot_be_fetched() -> None:
    pdf = make_pdf([*PAGES, ["a copy of the order no other test stores"]])
    order = dataclasses.replace(read(67)[0], sha256=hashlib.sha256(pdf).hexdigest(), pages=4)
    serving = ArrNetworkClient(
        cache_dir=FIXTURES, min_interval_s=0,
        http=httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, content=pdf))),
    )  # fmt: skip
    with session_scope(actor="test") as s:
        # 1. the regulator's link cannot be reached: a reference row keeps the link and the pages
        doc, said = arr_import.store_pdf(s, offline_client(), order, "up", date(2026, 7, 2), "test", True)
        assert said.startswith("NOT stored (") and "HTTP 503" in said and "link and the page are kept" in said
        assert doc.id == document_id(order.sha256) and doc.file_bytes == 0 and doc.pages == 4
        assert doc.url_public == order.url and doc.jurisdiction == "UP" and doc.regulator == "UPERC"
        assert not documents.is_stored(doc) and doc.id not in documents.stored_ids(s)
        with pytest.raises(documents.DocumentNotFound):
            documents.file_path(doc)
        # 2. the PDF arrives: the same row is completed and its pages open
        doc2, said2 = arr_import.store_pdf(s, serving, order, "up", date(2026, 7, 2), "test", True)
        assert said2 == "stored" and doc2.id == doc.id and doc2.file_bytes == len(pdf)
        assert documents.is_stored(doc2) and "0.9318" in documents.page_text(doc2, 2)
        # 3. and is not fetched again
        assert (
            arr_import.store_pdf(s, offline_client(), order, "up", date(2026, 7, 2), "test", True)[1]
            == "already stored"
        )
    # a PDF whose hash is not the one the ARR Network read is not stored
    other = dataclasses.replace(order, sha256="0" * 64)
    with session_scope(actor="test") as s:
        doc3, said3 = arr_import.store_pdf(s, serving, other, "up", date(2026, 7, 2), "test", True)
        assert "replaced since" in said3 and not documents.is_stored(doc3)
        _, said4 = arr_import.store_pdf(
            s, serving, dataclasses.replace(order, sha256="1" * 64), "up", date(2026, 7, 2), "test", False
        )
        assert "--no-pdf" in said4


def test_a_citation_to_an_order_that_is_not_stored_says_so_and_keeps_the_regulator_link() -> None:
    from fastapi.testclient import TestClient

    from ergplan.platform.app import app

    out = import_up()
    doc_id = out[0].document_id
    client = TestClient(app)
    r = client.get(f"/documents/{doc_id}")
    assert r.status_code == 404 and "not yet stored" in r.text.lower()
    with session_scope() as s:
        doc = documents.get(s, doc_id)
        assert doc.url_public == "https://www.uperc.org/App_File/UPPCLTariffOrder-pdf72202630151PM.pdf"
        v = s.scalar(
            select(RulebookVersion).where(
                RulebookVersion.origin == "arr_network",
                RulebookVersion.family == "oa_charge",
                RulebookVersion.jurisdiction_id == "uppcl",
                RulebookVersion.effective_to.is_(None),
            )
        )
        assert v is not None
        ref = sandbox.rules(s, v.id)[0].source_ref
    assert ref["document_id"] == doc_id and ref["page"] == 379
    assert ref["note"].endswith(
        "regulator's copy: https://www.uperc.org/App_File/UPPCLTariffOrder-pdf72202630151PM.pdf#page=379"
    )


def test_command_line(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(arr_import, "ArrNetworkClient", lambda refresh=False: offline_client())
    args = ["import", "--state", "up", "--orders", "67,66,65,64", "--effective-from", "65=2025-11-22",
            "--effective-from", "64=2025-11-22", "--effective-from", "66=2026-07-02"]  # fmt: skip
    assert arr_import.main([*args, "--dry-run"]) == 0
    said = capsys.readouterr().out
    assert "dry run: nothing was written" in said and "order 64 · UPERC · FY2025-26 · NPCL → npcl" in said
    assert _versions() == []
    assert arr_import.main(args) == 0
    said = capsys.readouterr().out
    assert len(re.findall(r" v\d+ +proposed ", said)) == 12 and "PDF: NOT stored" in said
    assert "grounding: not run (the PDF is not stored)" in said
    assert arr_import.main(args) == 0
    assert len(re.findall(r" v\d+ +unchanged ", capsys.readouterr().out)) == 12 and len(_versions()) == 12
    assert arr_import.main(["import", "--state", "mh", "--orders", "67"]) == 2
    assert "refused: order 67: its utilities are not all in MH" in capsys.readouterr().err


def test_importer_and_mapper_use_no_model() -> None:
    import ast
    from pathlib import Path

    root = Path(arr_map.__file__).parent
    for name in ("arr_network.py", "arr_rates.py", "arr_map.py", "arr_import.py"):
        tree = ast.parse((root / name).read_text())
        mods = {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)} | {
            a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names
        }
        assert not [m for m in mods if "providers" in m or "openai" in m or "anthropic" in m], name
