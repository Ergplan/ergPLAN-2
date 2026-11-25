import pytest

from ergplan.db.base import session_scope
from ergplan.documents import service
from tests.fixtures.pdfgen import make_pdf

PDF = make_pdf([["Cover page"], ["ANNEXURE-I RATE SCHEDULE"], ["HV-2 Energy Charge 7.10 per kVAh"]])


def test_register_dedupes_by_sha256() -> None:
    meta = service.DocumentMeta(title="Test order", regulator="UPERC", jurisdiction="UP")
    with session_scope(actor="test") as s:
        a, created_a = service.register(s, PDF, meta, uploaded_by="test")
        b, created_b = service.register(s, PDF, meta, uploaded_by="test")
    assert created_a and not created_b
    assert a.id == b.id and a.pages == 3
    assert service.path_for(a.file_sha256).exists()


def test_not_a_pdf_is_refused() -> None:
    with session_scope(actor="test") as s, pytest.raises(service.NotAPdf):
        service.register(s, b"hello", service.DocumentMeta(title="x"), uploaded_by="test")


def test_page_text_and_render() -> None:
    with session_scope(actor="test") as s:
        doc, _ = service.register(s, PDF, service.DocumentMeta(title="t"), uploaded_by="test")
        assert "7.10" in service.page_text(doc, 3)
        png = service.render_page_png(doc, 2)
        assert png.startswith(b"\x89PNG")
        with pytest.raises(service.PageOutOfRange):
            service.page_text(doc, 4)


def test_a_reference_row_is_not_stored_until_the_pdf_itself_is_registered() -> None:
    import hashlib

    from ergplan.core.ids import document_id

    pdf = make_pdf([["An order this machine does not hold yet"], ["HV-2 Energy Charge 6.80 per kVAh"]])
    sha = hashlib.sha256(pdf).hexdigest()
    meta = service.DocumentMeta(
        title="Order by reference", regulator="UPERC", url_public="https://r.example/o.pdf"
    )
    with session_scope(actor="test") as s:
        ref, created = service.register_reference(s, sha.upper(), 2, meta, uploaded_by="test")
        again, created_again = service.register_reference(s, sha, 2, meta, uploaded_by="test")
        assert created and not created_again and again.id == ref.id == document_id(sha)
        assert ref.file_bytes == 0 and ref.pages == 2 and ref.url_public == "https://r.example/o.pdf"
        assert not service.is_stored(ref) and ref.id not in service.stored_ids(s)
        with pytest.raises(service.DocumentNotFound):
            service.page_text(ref, 2)
        with pytest.raises(ValueError, match="not a SHA-256"):
            service.register_reference(s, "abc", 2, meta, uploaded_by="test")
        doc, created_now = service.register(s, pdf, service.DocumentMeta(title="ignored"), uploaded_by="test")
        assert (
            not created_now
            and doc.id == ref.id
            and doc.file_bytes == len(pdf)
            and doc.title == "Order by reference"
        )
        assert service.is_stored(doc) and doc.id in service.stored_ids(s)
        assert "6.80" in service.page_text(doc, 2)
