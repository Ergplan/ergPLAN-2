"""Stage 11: the documents approved rules cite that are not in this machine's store, listed for the
operator (python -m ergplan.documents.missing) and the analyst (GET /documents/missing); a citation
to one opens a page that says so, never a raw error."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from ergplan.db.base import session_scope
from ergplan.documents import missing, service
from ergplan.platform.app import app

from ..engine.helpers import restore_committed
from ..fixtures.pdfgen import make_pdf

client = TestClient(app)
AN = {"X-Ergplan-Actor": "analyst@joulewise.in", "X-Ergplan-Role": "analyst"}
CL = {"X-Ergplan-Actor": "client@acme.in", "X-Ergplan-Role": "client"}


def test_document_refs_finds_order_and_regulation_refs_at_any_depth() -> None:
    node = {
        "source_ref": {"kind": "order", "document_id": "a", "page": 3},
        "field_refs": {"x": {"kind": "regulation", "document_id": "b", "page": 1}},
        "other": [
            {"kind": "register", "note": "by name"},
            {"kind": "order"},
            {"kind": "bill", "document_id": "c"},
        ],
    }
    assert sorted(r["document_id"] for r in missing.document_refs(node)) == ["a", "b"]


def test_cited_documents_are_missing_until_their_pdf_is_stored(monkeypatch: pytest.MonkeyPatch) -> None:
    restore_committed()
    with session_scope() as s:
        found = missing.cited_missing(s)
    ids = {m.document_id for m in found}
    npcl = next(m for m in found if "npcl" in m.jurisdictions and "tariff_schedule" in m.families)
    assert len(found) >= 10 and npcl.rules > 0 and npcl.pages and npcl.quote and npcl.cited_in
    # a stored file of other bytes does not stand in for a cited one (the id is the file's SHA-256)
    with session_scope(actor="t") as s:
        doc, _ = service.register(s, make_pdf([["Another order"]]), service.DocumentMeta(title="x"), "t")
        assert service.stored_ids(s) == {doc.id}
        assert {m.document_id for m in missing.cited_missing(s)} == ids
        # a row whose file has gone from the store is not stored
        service.path_for(doc.file_sha256).unlink()
        assert service.stored_ids(s) == set()
    # once the cited PDF is in the store its citations are no longer listed
    monkeypatch.setattr(service, "stored_ids", lambda s: {npcl.document_id})
    with session_scope() as s:
        assert {m.document_id for m in missing.cited_missing(s)} == ids - {npcl.document_id}


def test_the_list_is_the_analysts_and_the_operators(capsys: pytest.CaptureFixture[str]) -> None:
    restore_committed()
    r = client.get("/documents/missing", headers=AN)
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) >= 10 and all(x["rules"] > 0 and x["pages"] and x["jurisdictions"] for x in rows)
    assert set(rows[0]) == {"document_id", "jurisdictions", "families", "cited_in", "pages", "rules", "quote"}
    assert client.get("/documents/missing", headers=CL).status_code == 403
    assert missing.main() == 0
    out = capsys.readouterr().out
    assert f"{len(rows)} cited by approved rules are missing" in out and rows[0]["document_id"] in out


def test_a_citation_to_a_pdf_that_is_not_stored_says_so() -> None:
    restore_committed()
    doc = client.get("/documents/missing", headers=AN).json()[0]["document_id"]
    r = client.get(f"/documents/{doc}")
    assert r.status_code == 404 and r.headers["content-type"].startswith("text/html")
    assert "not yet stored here" in r.text and doc in r.text
    # the id is echoed only as letters, digits and hyphens
    r = client.get("/documents/%3Cscript%3Ealert(1)%3C%2Fscript%3E")
    assert r.status_code == 404 and "<script>" not in r.text
    assert client.get(f"/documents/{doc}/meta").status_code == 404
