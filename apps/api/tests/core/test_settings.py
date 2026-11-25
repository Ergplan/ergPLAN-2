"""Settings: where they are read from. On a server the repository's .env is the deployment's, and
the tests and the watcher must not pick it up (ERGPLAN_ENV_FILE)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from ergplan.core.settings import ENV_FILE_VARIABLE, get_settings


@pytest.fixture
def fresh_settings() -> Iterator[None]:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_the_test_session_reads_no_env_file() -> None:
    import os

    assert os.environ[ENV_FILE_VARIABLE] == ""


@pytest.mark.usefixtures("fresh_settings")
def test_a_named_env_file_is_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    f = tmp_path / "deployment.env"
    f.write_text("ERGPLAN_ENV=prod\nERGPLAN_PUBLIC_URL=https://plan.example\n", encoding="utf-8")
    monkeypatch.delenv("ERGPLAN_ENV", raising=False)
    monkeypatch.delenv("ERGPLAN_PUBLIC_URL", raising=False)
    monkeypatch.setenv(ENV_FILE_VARIABLE, str(f))
    assert get_settings().ergplan_env == "prod"
    assert get_settings().ergplan_public_url == "https://plan.example"


@pytest.mark.usefixtures("fresh_settings")
def test_an_empty_name_reads_the_environment_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ERGPLAN_ENV", raising=False)
    monkeypatch.setenv(ENV_FILE_VARIABLE, "")
    assert get_settings().ergplan_env == "dev"
    monkeypatch.setenv("ERGPLAN_ENV", "prod")
    get_settings.cache_clear()
    assert get_settings().ergplan_env == "prod"
