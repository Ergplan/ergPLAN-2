"""Where configuration comes from: the environment first, then the repository's git-ignored `.env`.

Rule 11: secrets only by environment variable, read here and nowhere else.

On a server the repository's `.env` is the deployment's (production address, keys). The tests and
the watcher must not read it: `ERGPLAN_ENV_FILE` names another file, and set empty it reads the
environment only (tests/conftest.py sets it; tools/dev/vm-watch.sh does for the watcher).
"""

from __future__ import annotations

import os
from decimal import Decimal
from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[4]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(REPO_ROOT / ".env"), env_file_encoding="utf-8", extra="ignore"
    )

    ergplan_env: str = "dev"
    database_url: str = "postgresql+psycopg://ergplan:ergplan@localhost:5432/ergplan2"
    ergplan_data_dir: Path = Field(default=REPO_ROOT / "data")
    ergplan_public_url: str = "http://localhost:3000"
    ergplan_web_internal_url: str = "http://localhost:3000"  # where the API reaches the web app (PDF)
    ergplan_analyst_domain: str = "joulewise.in"  # emails here sign in as analysts
    ergplan_analyst_emails: str = ""  # comma-separated, also analysts
    ergplan_smtp_url: str = ""  # smtp[s]://user:password@host:port; empty: links go to the log
    ergplan_mail_from: str = "ergPLAN <no-reply@joulewise.in>"
    # Stage 13: the one administrator account. Typed by the person into .env; both empty leaves
    # password sign-in switched off (the emailed link stays). Never defaulted, logged or printed.
    ergplan_admin_user: str = ""
    ergplan_admin_password: SecretStr = SecretStr("")
    # The proxies whose X-Forwarded-For is believed (Caddy and the web server on the compose network,
    # or this machine): addresses or networks, comma-separated. A caller from anywhere else is
    # known by the address it connected from, whatever header it sends.
    ergplan_trusted_proxies: str = "127.0.0.0/8,::1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16"
    # Rate limits (platform/ratelimit.py): requests per window, per address on the sign-in routes and
    # per signed-in account and address on bill upload. 0 switches a limit off.
    ergplan_signin_rate_limit: int = 20
    ergplan_signin_rate_window_s: int = 60
    ergplan_upload_rate_limit: int = 60
    ergplan_upload_rate_window_s: int = 600
    # Stage 16: the MCP connector's address (what the person adds in Claude). Empty: the public
    # address followed by /mcp. Its scheme and host are also the OAuth issuer's.
    ergplan_mcp_url: str = ""
    # hosts an MCP connector may send the sign-in back to over https (comma-separated); http only to
    # this machine. Claude's own hosts by default, so a client someone else registers cannot
    # collect the administrator's grant at an address of their choosing
    ergplan_mcp_redirect_hosts: str = "claude.ai,claude.com"
    ergplan_resettle_tolerance_kwh: Decimal = Decimal("1")
    # the solve's budget (sizing takes half; each month's 15-minute dispatch at least 30 s): runs
    # are solved on the worker, and a mix of several sources needs minutes, not seconds
    ergplan_optimizer_time_limit_s: float = 300.0
    ergplan_optimizer_mip_gap: float = 0.001
    # exchange purchases (Stage 18): how far the consumer's drawal is taken to differ from its
    # day-ahead schedule, each way, as a percentage of the schedule; an assumption shown as one and
    # pinned by every run, used to price the deviation risk the state's rule creates
    ergplan_exchange_deviation_pct: Decimal = Decimal("5")
    ergplan_worker_concurrency: int = 1  # runs solved at once by `python -m ergplan.platform.worker`
    # Stage 19: the landed-cost calculator (/calculator) open to visitors without signing in. Off by
    # default: opening the app to visitors is the person's decision, never the builder's; when off,
    # the calculator needs sign-in like every other screen. Per-address limit on its routes.
    ergplan_public_calculator: bool = False
    # the person's decision of 26 Nov 2026: no sign-in at all. Every visitor of the web app and of
    # the MCP connector is taken as the analyst `OPEN_ACTOR`; only approving or superseding a rule
    # still needs a named analyst's sign-in (invariant 3). Off by default
    ergplan_open_access: bool = False
    ergplan_calculator_rate_limit: int = 30
    ergplan_calculator_rate_window_s: int = 60

    billx_url: str = "http://localhost:8080"
    billx_token: str = ""
    # what one file's reading may spend on model readers (Model A, LlamaParse), in US dollars,
    # and how long ergPLAN waits for billx (Docling and two model readers take minutes)
    billx_max_cost_usd: str = "2.00"
    billx_timeout_s: int = 1500

    # Models: OpenAI only (decision 31 Oct 2026). Read by ergplan.providers and nothing else.
    openai_api_key: str = ""
    openai_model_regreader: str = "gpt-5.1"
    openai_model_explain: str = "gpt-5-mini"
    openai_realtime_model: str = "gpt-realtime"

    tariff_oder_url: str = ""
    tariff_oder_token: str = ""

    @property
    def documents_dir(self) -> Path:
        return self.ergplan_data_dir / "documents"

    @property
    def bill_files_dir(self) -> Path:
        """The client's bill PDFs as uploaded (gitignored), so a bill value links to its page."""
        return self.ergplan_data_dir / "bill_files"

    @property
    def schemas_dir(self) -> Path:
        return REPO_ROOT / "packages" / "rulebooks" / "schemas"

    @property
    def corpus_index(self) -> Path:
        return REPO_ROOT / "packages" / "rulebooks" / "corpus" / "tariff-library.json"

    @property
    def rulebooks_dir(self) -> Path:
        return REPO_ROOT / "packages" / "rulebooks"


ENV_FILE_VARIABLE = "ERGPLAN_ENV_FILE"


@lru_cache
def get_settings() -> Settings:
    env_file = os.environ.get(ENV_FILE_VARIABLE)
    if env_file is None:
        return Settings()
    return Settings(_env_file=env_file or None)
