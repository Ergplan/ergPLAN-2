#!/usr/bin/env python3
"""Fail when a file git tracks (or is about to track) looks like it holds a secret: API keys, private
keys, tokens, or a filled-in .env. Rule 11: secrets by environment variable only. Run by the watcher
and CI; prints the file and line (never the value).

Stage 13: where the server's own `.env` holds the administrator's password (ERGPLAN_ADMIN_PASSWORD;
the file is the repository's `.env`, or the one ERGPLAN_SECRETS_ENV_FILE names; absent on CI and on a
laptop without one, and then there is nothing to look for), that exact value must not appear in any
file git tracks or would track, nor in the watcher's log and status. The value is read to be searched
for and is never printed; only the file's name is."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PATTERNS = {
    "OpenAI key": re.compile(r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{20,}"),
    "Anthropic key": re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}"),
    "AWS access key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "GitHub token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}"),
    "Slack token": re.compile(r"\bxox[abpr]-[A-Za-z0-9-]{20,}"),
    "Google API key": re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    "private key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
}
ASSIGN = re.compile(
    r"^(OPENAI_API_KEY|BILLX_TOKEN|ERGPLAN_SMTP_URL|TARIFF_ODER_TOKEN|BILLX_OPENAI_API_KEY|POSTGRES_PASSWORD"
    r"|ERGPLAN_ADMIN_PASSWORD)=(.+)$"
)
ALLOWED_VALUES = {"", "change-me", "change-me-long-random", "dev-billx-token", "stub"}
DB_URL = re.compile(r"^DATABASE_URL=\S+?://[^:/@]+:([^@]+)@")
SKIP_SUFFIXES = {".pdf", ".png", ".jpg", ".jpeg", ".gif", ".ico", ".woff", ".woff2", ".zip"}


def tracked() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.split("\n")
    return [ROOT / p for p in out if p]


ADMIN_PASSWORD = "ERGPLAN_ADMIN_PASSWORD"
ENV_FILE_VARIABLE = "ERGPLAN_SECRETS_ENV_FILE"
SHORTEST_SEARCHED = 6  # a shorter value would be "found" in ordinary text


def admin_password() -> str:
    """The administrator's password as the server's .env holds it, or "" (no file, no line, empty)."""
    path = Path(os.environ.get(ENV_FILE_VARIABLE) or ROOT / ".env")
    try:
        lines = path.read_text(errors="ignore").splitlines()
    except OSError:
        return ""
    value = ""
    for line in lines:
        name, sep, rest = line.strip().removeprefix("export ").partition("=")
        if sep and name.strip() == ADMIN_PASSWORD:
            value = rest.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                value = value[1:-1]
    return value


def password_leaks(password: str, files: list[Path]) -> list[str]:
    """The files that contain the password, by name only."""
    needle = password.encode()
    found = []
    for p in files:
        try:
            if p.is_file() and needle in p.read_bytes():
                found.append(f"{p.relative_to(ROOT)}: holds the administrator password of the server's .env")
        except OSError:
            continue
    return found


def main() -> int:
    bad = []
    password = admin_password()
    if len(password) >= SHORTEST_SEARCHED:
        watcher = [ROOT / "tools" / "watch" / "watch.log", ROOT / "tools" / "watch" / "status.json"]
        bad += password_leaks(password, [p for p in tracked() if p.name != ".env"] + watcher)
    elif password:
        print(f"secrets: the administrator password is under {SHORTEST_SEARCHED} characters; not searched for")
    for p in tracked():
        if p.suffix.lower() in SKIP_SUFFIXES or not p.is_file():
            continue
        try:
            text = p.read_text(errors="ignore")
        except OSError:
            continue
        rel = p.relative_to(ROOT)
        if rel.name == ".env":
            bad.append(f"{rel}: a .env file must not be committed")
            continue
        for n, line in enumerate(text.splitlines(), 1):
            for name, rx in PATTERNS.items():
                if rx.search(line) and "secrets-check: allow" not in line:
                    bad.append(f"{rel}:{n}: looks like an {name}")
            u = DB_URL.match(line.strip())
            if u and u.group(1) not in ALLOWED_VALUES | {"ergplan"} and rel.name.startswith(".env"):
                bad.append(f"{rel}:{n}: DATABASE_URL carries a password")
            m = ASSIGN.match(line.strip())
            if m and m.group(2).strip().strip('"') not in ALLOWED_VALUES and rel.name.startswith(".env"):
                bad.append(f"{rel}:{n}: {m.group(1)} has a value")
    for b in bad:
        print("secrets:", b)
    print("secrets: OK" if not bad else f"secrets: FAIL ({len(bad)})")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
