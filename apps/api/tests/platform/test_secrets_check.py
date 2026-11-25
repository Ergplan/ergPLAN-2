"""Stage 13: tools/dev/secrets-check.py fails when the administrator password of the server's .env
appears in a file git tracks (or would track) or in the watcher's log, names the file, and never
prints the value. The .env here is a fixture's, in a temporary folder."""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parents[4] / "tools" / "dev" / "secrets-check.py"
PASSWORD = "fixture-only-Zq7-violet-harbour"


@pytest.fixture
def check(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """The script, pointed at a small repository of its own and a .env outside it."""
    spec = importlib.util.spec_from_file_location("secrets_check", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    repo = tmp_path / "repo"
    (repo / "tools" / "watch").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / ".gitignore").write_text("*.log\n")
    (repo / "README.md").write_text("nothing to see\n")
    env = tmp_path / "server.env"
    env.write_text(f'ERGPLAN_ADMIN_USER=plantadmin\nERGPLAN_ADMIN_PASSWORD="{PASSWORD}"\n')
    monkeypatch.setattr(module, "ROOT", repo)
    monkeypatch.setenv(module.ENV_FILE_VARIABLE, str(env))
    return module


def test_a_clean_repository_passes(check: ModuleType, capsys: pytest.CaptureFixture[str]) -> None:
    assert check.admin_password() == PASSWORD
    assert check.main() == 0
    assert capsys.readouterr().out.strip() == "secrets: OK"


@pytest.mark.parametrize("where", ["notes/report.md", "tools/watch/watch.log", "tools/watch/status.json"])
def test_the_password_in_a_tracked_file_or_the_watchers_log_fails_without_printing_it(
    check: ModuleType, capsys: pytest.CaptureFixture[str], where: str
) -> None:
    target = check.ROOT / where
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(f"signed in with {PASSWORD} today\n")
    assert check.main() == 1
    out = capsys.readouterr()
    assert where in out.out and "administrator password" in out.out and "FAIL" in out.out
    assert PASSWORD not in out.out and PASSWORD not in out.err


def test_no_env_file_or_no_password_is_nothing_to_look_for(
    check: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (check.ROOT / "notes.md").write_text(f"{PASSWORD}\n")
    monkeypatch.setenv(check.ENV_FILE_VARIABLE, str(tmp_path / "absent.env"))
    assert check.admin_password() == "" and check.main() == 0
    empty = tmp_path / "empty.env"
    empty.write_text("ERGPLAN_ADMIN_USER=\nERGPLAN_ADMIN_PASSWORD=\n")
    monkeypatch.setenv(check.ENV_FILE_VARIABLE, str(empty))
    assert check.admin_password() == "" and check.main() == 0
    assert PASSWORD not in capsys.readouterr().out


def test_a_filled_in_password_in_env_example_fails(
    check: ModuleType, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(check.ENV_FILE_VARIABLE, str(tmp_path / "absent.env"))
    (check.ROOT / ".env.example").write_text(
        "ERGPLAN_ADMIN_USER=someone\nERGPLAN_ADMIN_PASSWORD=some-value\n"
    )
    assert check.main() == 1
    out = capsys.readouterr().out
    assert ".env.example:2: ERGPLAN_ADMIN_PASSWORD has a value" in out and "some-value" not in out
