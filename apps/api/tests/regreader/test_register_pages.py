"""Stage 12: register figures cited to the order's page. The committed catalogue covers every order
the register cites; on trimmed real pages of the Bihar order, the approved register version is
re-issued as a DRAFT whose figures open the page that prints them (the figure itself never
changes); a figure the page does not print unambiguously keeps its register ref with the reason;
nothing is approved and no client reads the draft (invariant 2); a second run says unchanged."""

from __future__ import annotations

import dataclasses
import hashlib
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from ergplan.core.ids import document_id
from ergplan.core.settings import get_settings
from ergplan.db.base import session_scope
from ergplan.db.models import AuditLog, RulebookVersion
from ergplan.documents import service as documents
from ergplan.regreader import register_pages as rp
from ergplan.report import statebook
from ergplan.rulebook import golden, repository, sandbox, service, yaml_io
from tests.fixtures.pdfgen import make_pdf

FIXTURE = Path(__file__).parent / "fixtures" / "register_pages" / "br_fy27.yaml"
ON = date(2026, 11, 15)


def fixture_pdf() -> bytes:
    d = yaml.safe_load(FIXTURE.read_text())
    return make_pdf(
        [[f"Bihar Electricity Regulatory Commission Page| {p['printed']}", *p["lines"]] for p in d["pages"]]
    )


# ---------------------------------------------------------------- the catalogue and its parsing


def test_the_committed_catalogue_names_every_order_the_rule_books_register_refs_cite() -> None:
    cat = rp.load_catalogue()
    for o in cat.orders:
        assert o.cites and o.title
        assert all(len(sha) == 64 and set(sha) <= set("0123456789abcdef") for sha in o.documents), o.key
        assert o.obtained == bool(o.documents), o.key
        if not o.obtained:
            assert o.why, o.key  # what is missing, said
    unmatched = []
    for p in sorted((get_settings().rulebooks_dir / "states").rglob("*.yaml")):
        d = yaml.safe_load(p.read_text())
        state = p.parts[-3].upper()
        for row in d["rows"]:
            cite = (
                rp.citation(row["source_ref"].get("note"))
                if row["source_ref"]["kind"] == "register"
                else None
            )
            if cite and cat.match(state, cite) is None:
                unmatched.append((p.name, cite))
    assert unmatched == []


def test_citation_pages_and_printed_page_numbers() -> None:
    note = "BERC TO FY27 (18-03-2026) s.8.7 pp.492-494 - AS not levied — as entered in the jouleWise State Regulation Master"
    assert rp.citation(note) == "BERC TO FY27 (18-03-2026) s.8.7 pp.492-494 - AS not levied"
    assert rp.citation("jouleWise State Regulation Master · Bihar: entered without a citation") is None
    assert rp.cited_pages("BERC TO FY27 (18-03-2026) s.8.7 pp.492-494") == [492, 493, 494]
    assert rp.cited_pages("Table 12.4 p.236 - APSPDCL") == [236]
    assert rp.cited_pages("tariff schedule s.1.3 - 33 kV 0.2605") == []
    assert rp.printed_number("Chapter-XII\nrows\nPage 236 of 440") == 236
    assert (
        rp.printed_number(
            "NBPDCL & SBPDCL Tariff Order\nBihar Electricity Regulatory Commission Page| 484\nx"
        )
        == 484
    )
    assert (
        rp.printed_number(
            "5. Tariff Rationalisation\nUttarakhand Electricity Regulatory Commission 253\nbody"
        )
        == 253
    )
    assert rp.printed_number("no number here\nat all") is None


def test_the_longest_citation_prefix_names_the_order() -> None:
    cat = rp.load_catalogue()
    o = cat.match("TN", "TNERC Order 6 of 2024 / TO 6 of 2025 - single HT rate (all HT voltages)")
    assert o is not None and o.key == "tn-to-6-2025"
    o = cat.match("BR", "BERC TO NBPDCL/SBPDCL FY27 Case 50&51/2025 (18-03-2026) s.8.3.1 Table 8.9 p.486")
    assert o is not None and o.key == "br-to-fy27"
    assert cat.match("UP", "BERC TO FY27") is None  # by state


def test_a_rate_per_kwh_may_be_printed_in_paise_or_per_mwh() -> None:
    forms = rp.printed_forms(Decimal("0.0068"), "inr_per_kwh")
    assert "0.68" in forms[1][0] and forms[1][2] == " paise/kWh"
    assert "240" in rp.printed_forms(Decimal("0.24"), "inr_per_kwh")[2][0]
    assert rp._numbers("Rs.5760.00/MW/day or Rs.240.00/MWh") >= {"5760", "240"}


# ---------------------------------------------------------------- on the trimmed Bihar pages


@pytest.fixture
def bihar() -> rp.Catalogue:
    """The approved Bihar rule book as committed (register refs), the fixture order stored, and the
    committed catalogue with the Bihar order's PDF swapped for the fixture's."""
    pdf = fixture_pdf()
    sha = hashlib.sha256(pdf).hexdigest()
    with session_scope(actor="t") as s:
        service.sync_jurisdictions(s)
        golden.sync_tests(s)
        documents.register(
            s, pdf, documents.DocumentMeta(title="BERC tariff order FY 2026-27 (trimmed)"), "t"
        )
    for p in sorted((get_settings().rulebooks_dir / "states" / "br").rglob("*.yaml")):
        d = yaml_io.load(p)
        with session_scope(actor=d.get("approved_by") or "t", role="analyst") as s:
            yaml_io.restore(s, p)
    cat = rp.load_catalogue()
    orders = tuple(
        dataclasses.replace(o, documents=(sha,)) if o.key == "br-to-fy27" else o for o in cat.orders
    )
    return rp.Catalogue(orders, cat.figures)


def _figures(result: dict) -> dict[tuple[str, str], dict]:
    return {(f["label"].split(" · ")[0], f["pointer"]): f for f in result["figures"] if f["pointer"]}


def test_bihar_figures_open_their_page_as_a_draft(bihar: rp.Catalogue) -> None:
    with session_scope(actor=rp.ACTOR) as s:
        result = rp.run(s, states=["BR"], catalogue=bihar, month=ON)
    figs = _figures(result)
    fixture_id = document_id(bihar.order("br-to-fy27").documents[0])  # type: ignore[union-attr]
    grounded = {k for k, f in figs.items() if f["grounded"]}
    assert grounded == {
        ("Cross-subsidy surcharge", "/value"),
        ("Additional surcharge", "/value"),
        ("SLDC scheduling and operating charge", "/value"),
        ("State transmission charge", "/value"),
        ("State transmission loss", "/value"),
        ("Wheeling charge", "/value_by_voltage/33"),
        ("Wheeling loss", "/value_by_voltage/33"),
        ("Wheeling loss", "/value_by_voltage/11"),
    }
    css = figs[("Cross-subsidy surcharge", "/value")]
    assert css["document_id"] == fixture_id and css["page"] == 8 and css["printed_page"] == 491
    assert "For 33kV consumers 1.77 1.68" in css["quote"] and "1.77/kWh for NBPDCL" in css["differs"]
    sldc = figs[("SLDC scheduling and operating charge", "/value")]
    assert sldc["page"] == 6 and "0.68 Paise/kWh" in sldc["quote"]
    # the register's 11 kV wheeling is a sum the order does not print; EHV nil is not printed
    w11 = figs[("Wheeling charge", "/value_by_voltage/11")]
    assert not w11["grounded"] and "0.53 + 0.57" in w11["reason"]
    assert not figs[("Wheeling charge", "/value_by_voltage/132")]["grounded"]
    [v] = [o for o in result["versions"] if o["family"] == "oa_charge"]
    assert v["status"] == "proposed" and v["golden"].split(" of ")[0] == v["golden"].split(" of ")[1]
    assert all(o["status"] == "nothing_grounded" for o in result["versions"] if o["family"] != "oa_charge")
    with session_scope() as s:
        draft = s.get(RulebookVersion, v["draft_id"])
        base = s.get(RulebookVersion, v["base_id"])
        assert draft is not None and base is not None
        assert draft.status == "draft" and draft.origin == rp.ORIGIN and draft.approved_by is None
        assert draft.golden_status == "green" and base.status == "approved"  # nothing approved
        assert s.query(AuditLog).filter(AuditLog.table_name == "rulebook_versions").count() >= 1
        rows = {r.label.split(" · ")[0]: r for r in sandbox.rules(s, draft.id)}
        old = {r.label.split(" · ")[0]: r for r in sandbox.rules(s, base.id)}
        for name, r in rows.items():  # the figures are the register's, unchanged
            assert r.body == old[name].body and r.scope == old[name].scope
        css_ref = rows["Cross-subsidy surcharge"].ref("/value")
        assert css_ref.kind == "order" and css_ref.url == f"/documents/{fixture_id}#page=8"
        # the statutory captive exemption keeps the register's ref it had
        assert rows["Cross-subsidy surcharge"].ref("/exemptions/0/waived_pct").kind == "register"
        wheel = rows["Wheeling charge"]
        assert (
            wheel.ref("/value_by_voltage/33").kind == "order" and wheel.ref("/value_by_voltage/33").page == 1
        )
        assert wheel.ref("/value_by_voltage/11").kind == "register"  # not grounded: as it was
        # invariant 2: what a client reads is still the approved register version
        chain = service.jurisdiction_chain(s, "sbpdcl")
        served = repository.approved_rules(s, "oa_charge", chain, ON)
        assert served and {r.version_id for r in served} == {base.id}
        assert all(r.source_ref["kind"] == "register" for r in served)
    # the rule book's trace: more figures open a stored page with the draft in place
    before, after = result["trace_before"]["BR"], result["trace_after"]["BR"]
    assert before["page"] == 0 and after["page"] > 0 and after["register"] < before["register"]
    with session_scope(actor=rp.ACTOR) as s:  # idempotent
        again = rp.run(s, states=["BR"], catalogue=bihar, month=ON, trace=False)
    [v2] = [o for o in again["versions"] if o["family"] == "oa_charge"]
    assert v2["status"] == "unchanged" and v2["draft_id"] == v["draft_id"]


def test_the_projection_is_undone_and_a_dry_run_writes_nothing(bihar: rp.Catalogue) -> None:
    with session_scope(actor=rp.ACTOR) as s:
        result = rp.run(s, states=["BR"], catalogue=bihar, month=ON, dry_run=True, trace=False)
    [v] = [o for o in result["versions"] if o["family"] == "oa_charge"]
    assert v["status"] == "proposed" and v["draft_id"] is None and v["grounded"] == 8
    with session_scope() as s:
        assert s.query(RulebookVersion).filter(RulebookVersion.status == "draft").count() == 0
    with session_scope(actor=rp.ACTOR) as s:
        result = rp.run(s, states=["BR"], catalogue=bihar, month=ON, trace=False)
    [v] = [o for o in result["versions"] if o["family"] == "oa_charge"]
    with session_scope() as s:
        chain = service.jurisdiction_chain(s, "sbpdcl")
        with rp.drafts_in_place({v["base_id"]: v["draft_id"]}):
            inside = repository.approved_rules(s, "oa_charge", chain, ON)
        outside = repository.approved_rules(s, "oa_charge", chain, ON)
        assert {r.version_id for r in inside} == {v["draft_id"]}
        assert {r.version_id for r in outside} == {v["base_id"]}
        assert statebook.build(s, discom="sbpdcl", month=date(2026, 11, 1))["trace"]["page"] == 0


def test_a_figure_printed_on_two_lines_is_not_guessed(bihar: rp.Catalogue) -> None:
    with session_scope() as s:
        order = rp.StoredOrder(s, bihar.order("br-to-fy27"))  # type: ignore[arg-type]
        [doc] = order.docs
        pages = {doc.id: order.window(doc, [491])}
        got = rp.find_figure(
            order, pages, value=Decimal("1.68"), component="cross_subsidy_surcharge", basis="inr_per_kwh"
        )
        assert isinstance(got, rp.NotFound) and "printed on 3 lines" in got.reason
        got = rp.find_figure(
            order, pages, value=Decimal("1.69"), component="cross_subsidy_surcharge", basis="inr_per_kwh"
        )
        assert isinstance(got, rp.NotFound) and "not printed" in got.reason and got.seen
        got = rp.find_figure(
            order, pages, value=Decimal("1.68"), component="cross_subsidy_surcharge", basis="inr_per_kwh",
            anchor="For 11kV consumers",
        )  # fmt: skip
        assert (
            isinstance(got, rp.Found)
            and got.page == 8
            and got.quote.endswith("3 For 11kV consumers 1.77 1.68")
        )


def test_an_order_not_stored_keeps_every_register_ref(bihar: rp.Catalogue) -> None:
    orders = tuple(
        dataclasses.replace(o, documents=("0" * 64,)) if o.key == "br-to-fy27" else o for o in bihar.orders
    )
    with session_scope(actor=rp.ACTOR) as s:
        result = rp.run(
            s, states=["BR"], catalogue=rp.Catalogue(orders, bihar.figures), month=ON, trace=False
        )
    assert all(not f["grounded"] for f in result["figures"])
    assert all(o["status"] == "nothing_grounded" for o in result["versions"])
    assert any(m["order"] == "br-to-fy27" for m in result["missing_orders"])
    assert any("not stored on this server" in (f["reason"] or "") for f in result["figures"])


def test_fetch_keeps_only_the_catalogue_file(tmp_path: Path) -> None:
    import httpx

    good, other = fixture_pdf(), make_pdf([["a re-issued copy"]])
    sha = hashlib.sha256(good).hexdigest()
    base = rp.Order(
        "k", "BR", ("X",), "Order", url_public="https://regulator.example/order.pdf", documents=(sha,)
    )
    bad = dataclasses.replace(
        base, key="k2", url_public="https://regulator.example/copy.pdf", documents=("1" * 64,)
    )
    cat = rp.Catalogue((base, bad), ())

    def answer(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=good if req.url.path == "/order.pdf" else other)

    with session_scope() as s:
        lines = rp.fetch(s, cat, tmp_path, httpx.Client(transport=httpx.MockTransport(answer)))
    assert (tmp_path / f"{sha}.pdf").read_bytes() == good
    assert [ln.split()[0] for ln in lines] == ["fetched", "not"] and "SHA-256 differs" in lines[1]
    assert len(list(tmp_path.glob("*.pdf"))) == 1
    with session_scope(actor=rp.ACTOR) as s:
        assert rp.store_from(s, cat, [tmp_path]) == [f"stored k {document_id(sha)}"]
