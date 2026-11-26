"""The exchange-purchase drafts (Stage 18): the committed proposals are well formed (every stated
field cited to a document page, every other field 'not stated' with the regulation to obtain), and
the loader proposes a file as a DRAFT only when every quote is on its stored page; it never
approves, and a draft never reaches an engine (invariant 2)."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pytest
import yaml

from ergplan.core.ids import document_id
from ergplan.db.base import session_scope
from ergplan.db.models import AuditLog, RulebookVersion
from ergplan.documents import service as documents
from ergplan.engine import exchange
from ergplan.regreader import exchange_drafts
from ergplan.rulebook import repository, sandbox, service
from tests.engine import exchange_fixture as fx
from tests.fixtures.pdfgen import make_pdf

STATES = ("AP", "BR", "CG", "GJ", "HR", "KA", "MH", "MP", "OD", "PB", "RJ", "TN", "TS", "UK", "UP")
QUOTE = "Standby drawal by an open access consumer shall be charged at Rs. 11.93 per kWh."


def committed() -> dict[str, dict[str, Any]]:
    return {
        p.stem: yaml.safe_load(p.read_text()) for p in sorted(exchange_drafts.proposals_dir().glob("*.yaml"))
    }


def test_one_proposal_per_state_well_formed_and_cited() -> None:
    files = committed()
    assert tuple(sorted(files)) == STATES
    cited = 0
    for code, d in files.items():
        assert d["family"] == "exchange_purchase" and d["jurisdiction"] == code
        assert d["origin"] == "exchange_reader" and "status" not in d  # a proposal, never an approval
        assert service.validate_rows("exchange_purchase", d["rows"]) == {}
        [row] = d["rows"]
        assert exchange_drafts.structure_errors(row) == []
        for pointer, _, ref in exchange_drafts.cited_fields(row):
            cited += 1
            assert ref is not None and ref["kind"] == "order" and ref["page"] >= 1 and ref["quote"], pointer
            doc = d["documents"][ref["document_id"]]  # the stored PDF it names, by SHA-256
            assert ref["document_id"] == document_id(doc["sha256"]) and doc["url_public"].startswith(
                "https://"
            )
        for name in exchange.FIELDS:
            f = row["body"][name]
            assert f["status"] == "stated" or len(f["obtain"]) > 40, (code, name)
    assert cited == 10


def test_rajasthan_states_nothing_and_names_what_to_obtain() -> None:
    [row] = committed()["RJ"]["rows"]
    assert all(row["body"][n]["status"] == "not_stated" for n in exchange.FIELDS)
    assert row["source_ref"]["kind"] == "assumption" and "field_refs" not in row
    assert "RERC's intra-state open access regulations" in row["body"]["schedule"]["obtain"]
    assert "Rajasthan SLDC's procedure" in row["body"]["sldc_clearance"]["obtain"]
    # as it stands (even approved) the verdict is "not yet sourced": nothing is offered on a guess
    v = exchange.verdict("Rajasthan", fx.view(row["body"]))
    assert v.verdict == exchange.NOT_YET_SOURCED and not v.offers


def test_no_committed_proposal_decides_a_verdict() -> None:
    """The stored tariff orders state surcharges, standby and (Gujarat) eligibility, but not whether
    the purchase is permitted or how it is scheduled: every state stays "not yet sourced"."""
    for code, d in committed().items():
        v = exchange.verdict(code, fx.view(d["rows"][0]["body"], code))
        assert v.verdict == exchange.NOT_YET_SOURCED, code


def _proposal(tmp_path: Path, doc: str, quote: str, value: str = "11.93", page: int = 2) -> Path:
    b = fx.body(standby={"basis": "inr_per_kwh", "value": value, "condition": "On standby drawal."})
    d = {
        "family": "exchange_purchase",
        "jurisdiction": "CG",
        "title": "fixture proposal",
        "effective_from": "2026-04-01",
        "origin": "exchange_reader",
        "rows": [
            {
                "label": "fixture",
                "scope": {"source": ["iex"]},
                "body": b,
                "source_ref": {"kind": "order", "document_id": doc, "page": page, "quote": quote},
                "field_refs": {
                    "/standby": {"kind": "order", "document_id": doc, "page": page, "quote": quote}
                },
            }
        ],
    }
    p = tmp_path / "CG.yaml"
    p.write_text(yaml.safe_dump(d, sort_keys=False))
    return p


@pytest.fixture
def stored() -> str:
    pdf = make_pdf([["Tariff order, page one."], ["8. Standby Charges", QUOTE]])
    with session_scope(actor="t") as s:
        service.sync_jurisdictions(s)
        doc, _ = documents.register(s, pdf, documents.DocumentMeta(title="Fixture order"), "t")
        return doc.id


def test_a_grounded_proposal_becomes_a_draft_and_is_never_approved(stored: str, tmp_path: Path) -> None:
    path = _proposal(tmp_path, stored, QUOTE)
    with session_scope(actor=exchange_drafts.ACTOR) as s:
        out = exchange_drafts.propose_file(s, path)
        assert out.status == "proposed" and out.failures == [] and out.stated == ["standby"]
        assert len(out.not_stated) == 12
        vid = out.version_id
    with session_scope() as s:
        v = s.get(RulebookVersion, vid)
        assert v is not None and v.status == "draft" and v.origin == "exchange_reader"
        assert v.approved_by is None and v.created_by == exchange_drafts.ACTOR
        # written through rulebook.service, so the trigger audited it (invariant 3)
        assert s.query(AuditLog).filter(AuditLog.table_name == "rulebook_versions").count() >= 1
        # a draft reaches no engine (invariant 2); the analyst's sandbox reads it
        chain = service.jurisdiction_chain(s, "CG")
        assert repository.approved_rules(s, "exchange_purchase", chain, date(2026, 6, 1)) == []
        [row] = sandbox.rules(s, v.id)
        assert row.body["standby"]["value"] == "11.93" and row.ref("/standby").page == 2
    with session_scope(actor=exchange_drafts.ACTOR) as s:  # idempotent
        again = exchange_drafts.propose_file(s, path)
        assert again.status == "present" and again.version_id == vid


@pytest.mark.parametrize(
    ("quote", "value", "page", "why"),
    [
        ("Standby drawal shall be charged at twice the tariff.", "11.93", 2, "the quote is not on page 2"),
        (QUOTE, "15.90", 2, "15.90 not printed on page 2"),
        (QUOTE, "11.93", 1, "the quote is not on page 1"),
        (QUOTE, "11.93", 9, "page 9 is outside"),
    ],
)
def test_a_citation_that_is_not_on_the_stored_page_refuses_the_draft(
    stored: str, tmp_path: Path, quote: str, value: str, page: int, why: str
) -> None:
    with session_scope(actor=exchange_drafts.ACTOR) as s:
        out = exchange_drafts.propose_file(s, _proposal(tmp_path, stored, quote, value, page))
        assert out.status == "refused" and out.version_id is None
        assert any(why in f for f in out.failures), out.failures
        assert s.query(RulebookVersion).count() == 0


def test_a_document_that_is_not_stored_and_a_stated_field_without_a_citation_are_refused(
    stored: str, tmp_path: Path
) -> None:
    with session_scope(actor=exchange_drafts.ACTOR) as s:
        out = exchange_drafts.propose_file(s, _proposal(tmp_path, "no-such-document", QUOTE))
        assert out.status == "refused" and "is not stored on this server" in out.failures[0]
    path = _proposal(tmp_path, stored, QUOTE)
    d = yaml.safe_load(path.read_text())
    del d["rows"][0]["field_refs"]
    path.write_text(yaml.safe_dump(d, sort_keys=False))
    with session_scope(actor=exchange_drafts.ACTOR) as s:
        out = exchange_drafts.propose_file(s, path)
        assert out.status == "refused" and "stated but not cited" in out.failures[0]
        assert s.query(RulebookVersion).count() == 0


def test_the_committed_proposals_are_refused_where_their_pdfs_are_not_stored() -> None:
    """On a server without the cited tariff orders the cited drafts are refused (never proposed on
    trust) and the states with nothing cited are still recorded as 'not stated' drafts."""
    with session_scope(actor=exchange_drafts.ACTOR) as s:
        by = {o.jurisdiction: o for o in exchange_drafts.propose_all(s)}
    assert tuple(sorted(by)) == STATES
    assert by["RJ"].status == "proposed" and by["RJ"].golden == "2 of 2" and by["RJ"].stated == []
    assert by["CG"].status == "refused" and "is not stored on this server" in by["CG"].failures[0]
    assert {c for c, o in by.items() if o.status == "proposed"} == {"KA", "MH", "PB", "RJ", "TN", "UK", "UP"}
