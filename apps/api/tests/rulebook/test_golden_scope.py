"""Golden cases that say which versions they are for (a tariff year, an importer's drafts), and the
approval gate counting only those: two tariff years of one DISCOM, each with its own cases."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError, IntegrityError

from ergplan.db.base import session_scope
from ergplan.db.models import RulebookVersion, RuleTest
from ergplan.rulebook import golden, repository, sandbox, service
from tests.rulebook.helpers import schedule_row

pytestmark = pytest.mark.usefixtures("jurisdictions")

FILES = {
    "npcl-tariff_schedule.yaml": """
jurisdiction: npcl
family: tariff_schedule
effective_on: '2026-08-01'
tests:
  - {name: "FY27 energy", given: {context: {category: HV-2, voltage_kv: 11}, field: /energy_charge/base}, expect: {value: "8.32"}}
""",
    "npcl-tariff_schedule-fy2025-26.yaml": """
jurisdiction: npcl
family: tariff_schedule
effective_on: '2026-01-15'
tests:
  - {name: "FY26 energy", given: {context: {category: HV-2, voltage_kv: 11}, field: /energy_charge/base}, expect: {value: "7.90"}}
""",
    "npcl-tariff_schedule-import.yaml": """
jurisdiction: npcl
family: tariff_schedule
effective_on: '2026-08-01'
origin: arr_network
tests:
  - {name: "FY27 import · HV-1", given: {context: {category: HV-1, voltage_kv: 11}, field: /energy_charge/base}, expect: {value: "9.00"}}
""",
}


def _sync(tmp_path: Path, files: dict[str, str] = FILES) -> int:
    for name, text in files.items():
        (tmp_path / name).write_text(text)
    with session_scope(actor="analyst@jw") as s:
        return golden.sync_tests(s, tmp_path)


def _propose(
    energy: str, eff: date, to: date | None = None, origin: str = "manual", extra: bool = False
) -> str:
    rows = [schedule_row("HV-2", 11, 11, energy, "430.00")]
    if extra:
        rows.append(schedule_row("HV-1", 11, 11, "9.00", "400.00"))
    with session_scope(actor="analyst@jw") as s:
        return service.propose(
            s, family="tariff_schedule", jurisdiction_id="npcl", rows=rows, actor="analyst@jw",
            effective_from=eff, effective_to=to, origin=origin,
        ).id  # fmt: skip


def _run(vid: str) -> tuple[int, int, list[str]]:
    with session_scope(actor="analyst@jw") as s:
        r = service.run_golden(s, vid)
        return r.total, r.passed, sorted(o.name for o in r.outcomes)


def test_several_files_for_one_family_keep_all_their_tests(tmp_path: Path) -> None:
    assert _sync(tmp_path) == 3
    with session_scope() as s:
        tests = {t.name: t.given for t in s.scalars(select(RuleTest))}
    assert set(tests) == {"FY27 energy", "FY26 energy", "FY27 import · HV-1"}
    assert tests["FY26 energy"]["effective_on"] == "2026-01-15" and "origin" not in tests["FY26 energy"]
    assert tests["FY27 import · HV-1"]["origin"] == "arr_network"
    # a test removed from its file goes; a second test of the same name anywhere is refused
    (tmp_path / "npcl-tariff_schedule-import.yaml").unlink()
    assert _sync(tmp_path, {}) == 2
    dup = {"again.yaml": FILES["npcl-tariff_schedule.yaml"]}
    with pytest.raises(ValueError, match="a second golden test named 'FY27 energy'"):
        _sync(tmp_path, dup)


def test_each_tariff_year_is_tested_on_its_own_cases_and_approved_on_them(tmp_path: Path) -> None:
    _sync(tmp_path)
    fy27 = _propose("8.32", date(2026, 7, 2))
    fy26 = _propose("7.90", date(2025, 11, 22), date(2026, 7, 1))
    assert _run(fy27) == (1, 1, ["FY27 energy"])  # not the other year's case, not the importer's
    assert _run(fy26) == (1, 1, ["FY26 energy"])
    for vid in (fy27, fy26):
        with session_scope(actor="analyst@jw", role="analyst") as s:
            assert service.approve(s, vid, actor="analyst@jw", role="analyst").status == "approved"
    with session_scope() as s:  # both years are now in force, each in its window
        got = {
            on: [
                r.body["energy_charge"]["base"]
                for r in repository.approved_rules(s, "tariff_schedule", ["npcl"], on)
            ]
            for on in (date(2026, 1, 15), date(2026, 8, 1))
        }
    # approving the earlier year did not supersede the later one (their windows do not overlap)
    assert got == {date(2026, 1, 15): ["7.90"], date(2026, 8, 1): ["8.32"]}
    # a later version for an overlapping window still supersedes
    newer = _propose("8.32", date(2026, 7, 2))
    assert _run(newer)[1] == 1
    with session_scope(actor="analyst@jw", role="analyst") as s:
        service.approve(s, newer, actor="analyst@jw", role="analyst")
    with session_scope() as s:
        status = {v.id: v.status for v in s.scalars(select(RulebookVersion))}
    assert status == {fy27: "superseded", fy26: "approved", newer: "approved"}


def test_an_importers_draft_also_answers_the_cases_written_for_it(tmp_path: Path) -> None:
    _sync(tmp_path)
    thin = _propose("8.32", date(2026, 7, 2), origin="arr_network")
    assert _run(thin) == (2, 1, ["FY27 energy", "FY27 import · HV-1"])
    with (
        pytest.raises(service.ApprovalRefused, match="1 of 2"),
        session_scope(actor="a", role="analyst") as s,
    ):
        service.approve(s, thin, actor="a", role="analyst")
    full = _propose("8.32", date(2026, 7, 2), origin="arr_network", extra=True)
    assert _run(full) == (2, 2, ["FY27 energy", "FY27 import · HV-1"])


def test_the_database_gate_counts_the_applicable_tests_only(tmp_path: Path) -> None:
    _sync(tmp_path)
    wrong = _propose("9.99", date(2025, 11, 22), date(2026, 7, 1))
    assert _run(wrong) == (1, 0, ["FY26 energy"])
    with (
        pytest.raises((IntegrityError, DBAPIError), match="0 of 1 golden tests"),
        session_scope(actor="a", role="analyst") as s,
    ):
        v = s.get(RulebookVersion, wrong)
        assert v is not None
        v.status = "approved"  # past the service: the trigger still refuses
    # a version no case applies to cannot be approved at all
    none = _propose("8.00", date(2027, 7, 1))
    assert _run(none)[0] == 0
    with pytest.raises(service.ApprovalRefused), session_scope(actor="a", role="analyst") as s:
        service.approve(s, none, actor="a", role="analyst")
    with (
        pytest.raises((IntegrityError, DBAPIError), match="0 of 0 golden tests"),
        session_scope(actor="a", role="analyst") as s,
    ):
        v = s.get(RulebookVersion, none)
        assert v is not None
        v.status = "approved"


def test_a_draft_window_can_be_moved_and_drafts_in_force_are_read_by_date(tmp_path: Path) -> None:
    _sync(tmp_path)
    vid = _propose("7.90", date(2025, 4, 1))
    with session_scope(actor="analyst@jw") as s:
        v = service.reschedule(s, vid, date(2025, 11, 22), date(2026, 7, 1))
        assert (v.effective_from, v.effective_to, v.golden_status) == (
            date(2025, 11, 22),
            date(2026, 7, 1),
            "not_run",
        )
    later = _propose("8.32", date(2026, 7, 2), origin="arr_network")
    with session_scope() as s:

        def base(on: date, origin: str | None = None) -> list[str]:
            return [
                r.body["energy_charge"]["base"]
                for r in sandbox.drafts_in_force(s, "tariff_schedule", ["npcl"], on, origin)
            ]

        assert base(date(2026, 1, 15)) == ["7.90"] and base(date(2026, 8, 1)) == ["8.32"]
        assert base(date(2025, 6, 1)) == [] and base(date(2026, 1, 15), "arr_network") == []
        assert (
            repository.approved_rules(s, "tariff_schedule", ["npcl"], date(2026, 8, 1)) == []
        )  # drafts only
    with session_scope(actor="analyst@jw", role="analyst") as s:
        service.approve(s, later, actor="analyst@jw", role="analyst") if _run(later)[1] == 2 else None
    with session_scope(actor="analyst@jw") as s:
        s.get(RulebookVersion, vid).status = "rejected"  # type: ignore[union-attr]
    with pytest.raises(service.NotADraft), session_scope(actor="analyst@jw") as s:
        service.reschedule(s, vid, date(2025, 4, 1), None)
