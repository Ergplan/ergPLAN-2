#!/usr/bin/env python3
"""Checks the public deployment (Stage 11) from the VM itself, through its public address:

  python3 tools/dev/vm-check.py                     # every check
  python3 tools/dev/vm-check.py --generate          # also draft this month's rule books if none exist
  python3 tools/dev/vm-check.py --base http://…     # another address (default https://SITE_ADDRESS)
  python3 tools/dev/vm-check.py --address 34.…      # connect to this IP for the hostname (the name and
                                                    # its certificate are still checked): for the hours
                                                    # after a DNS change while a resolver holds the old answer

1. every compose service is running (healthy where it has a health check) and only Caddy is published;
2. the address answers over https with a certificate the system trusts, and sends a signed-out
   visitor of the application to sign in (the API answers 401);
3. an analyst signs in with a one-time link minted on the server (never printed) and is an analyst;
4. a monthly rule book has a figure cited to a stored page: the PDF opens and the quoted words are
   on that page; the documents approved rules cite that are not stored are counted.

Exit 0 when every check passes. Nothing secret is printed: not the link, not the cookie.
"""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import re
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ["docker", "compose", "--env-file", ".env", "-f", "docker/compose.yaml", "-f", "docker/compose.prod.yaml"]
FAILED: list[str] = []


def say(ok: bool, what: str) -> bool:
    print(("ok    " if ok else "FAIL  ") + what)
    if not ok:
        FAILED.append(what)
    return ok


def env(key: str) -> str:
    """One named, non-secret setting from .env (the address); nothing else is read."""
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        if line.startswith(key + "="):
            return line.split("=", 1)[1].strip()
    return ""


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


class Site:
    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def call(self, method: str, path: str, body: Any = None, timeout: int = 300) -> tuple[int, str, bytes]:
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with self.opener.open(req, timeout=timeout) as r:
                return r.status, r.headers.get("Content-Type", ""), r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers.get("Content-Type", ""), e.read()

    def json(self, method: str, path: str, body: Any = None) -> tuple[int, Any]:
        status, _, raw = self.call(method, path, body)
        try:
            return status, json.loads(raw)
        except ValueError:
            return status, None


def containers() -> None:
    out = subprocess.run([*COMPOSE, "ps", "--all", "--format", "json"], cwd=ROOT, capture_output=True, text=True, check=True)
    rows = [json.loads(line) for line in out.stdout.splitlines() if line.strip()]
    want = {"postgres", "api", "worker", "web", "billx", "docling", "caddy"}
    seen = {r["Service"] for r in rows}
    say(want <= seen, f"all seven services exist ({', '.join(sorted(want - seen)) or 'none missing'})")
    for r in sorted(rows, key=lambda r: r["Service"]):
        good = r["State"] == "running" and r.get("Health", "") in ("", "healthy")
        say(good, f"{r['Service']}: {r['State']}{' · ' + r['Health'] if r.get('Health') else ''}")
    published = sorted({(r["Service"], p["PublishedPort"]) for r in rows for p in r.get("Publishers") or [] if p.get("PublishedPort")})
    say(
        bool(published) and {s for s, _ in published} == {"caddy"} and {p for _, p in published} == {80, 443},
        "only Caddy is published, on 80 and 443 (" + ", ".join(f"{s}:{p}" for s, p in published) + ")",
    )


def book_with_a_stored_page(site: Site, generate: bool) -> None:
    month = date.today().replace(day=1).isoformat()
    status, books = site.json("GET", f"/api/statebooks?month={month}")
    if status == 200 and not books and generate:
        status, made = site.json("POST", "/api/statebooks/generate-month", {"month": month})
        say(status == 201, f"the month's rule books generate ({len(made or [])} drafted for {month})")
        status, books = site.json("GET", f"/api/statebooks?month={month}")
    if not say(status == 200 and bool(books), f"rule books exist for {month} ({len(books or [])})"):
        return
    for b in books:
        _, detail = site.json("GET", f"/api/statebooks/{b['id']}")
        gone = set(detail.get("documents_missing") or [])
        for g in detail["content"]["grid"]:
            refs = [r for r in g["rate"]["source_refs"] if r["kind"] in ("order", "regulation")]
            ref = next((r for r in refs if r["document_id"] not in gone), None)
            if ref is None:
                continue
            what = f"{detail['title']} · grid tariff {g['slot']} = {g['rate']['value']} {g['rate']['unit']}"
            status, ctype, raw = site.call("GET", f"/api/documents/{ref['document_id']}")
            say(
                status == 200 and ctype.startswith("application/pdf") and raw.startswith(b"%PDF"),
                f"{what} opens {ref.get('document_title') or ref['document_id']}, page {ref['page']} (a PDF, {len(raw):,} bytes)",
            )
            status, page = site.json("GET", f"/api/documents/{ref['document_id']}/pages/{ref['page']}/text")
            found = status == 200 and norm(ref["quote"])[:60] in norm(page["text"])
            say(found, f"the quoted words are on that page: “{norm(ref['quote'])[:70]}…”")
            return
    say(False, "no rule book has a grid tariff cited to a stored page")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="https://" + env("SITE_ADDRESS"))
    ap.add_argument("--email", default="vm-check@" + (env("ERGPLAN_ANALYST_DOMAIN") or "joulewise.in"))
    ap.add_argument("--generate", action="store_true")
    ap.add_argument("--address")
    a = ap.parse_args()
    if a.address:
        host, real = urllib.request.urlparse(a.base).hostname, socket.getaddrinfo

        def pinned(name: Any, *rest: Any, **kw: Any) -> Any:
            return real(a.address if name == host else name, *rest, **kw)

        socket.getaddrinfo = pinned  # type: ignore[assignment]
    print(f"checking {a.base}\n")
    containers()

    site = Site(a.base)
    try:
        status, _, raw = site.call("GET", "/login", timeout=30)
    except (urllib.error.URLError, OSError) as e:
        say(False, f"{a.base} answers with a trusted certificate ({getattr(e, 'reason', e)})")
        print("\nstopped: the address does not answer")
        return 1
    say(status == 200 and b"ergPLAN" in raw, f"{a.base}/login serves the sign-in page ({status}, trusted certificate)")
    status, _ = site.json("GET", "/api/statebooks")
    say(status == 401, f"signed out, the API refuses ({status})")

    link = subprocess.run(
        [*COMPOSE, "exec", "-T", "api", "python", "-m", "ergplan.platform.signin", a.email, "--next", "/statebooks"],
        cwd=ROOT, capture_output=True, text=True,
    ).stdout.strip()
    token = re.search(r"token=([^&\s]+)", link)
    if say(token is not None, "a one-time sign-in link is minted on the server"):
        assert token is not None
        status, me = site.json("POST", "/api/auth/verify", {"token": token.group(1)})
        say(status == 200 and me["signed_in"] and me["role"] == "analyst", f"the link signs {a.email} in as an analyst ({status})")
        status, _ = site.json("POST", "/api/auth/verify", {"token": token.group(1)})
        say(status == 401, f"the same link does not work twice ({status})")
        book_with_a_stored_page(site, a.generate)
        status, missing = site.json("GET", "/api/documents/missing")
        status2, docs = site.json("GET", "/api/documents")
        if say(status == 200 and status2 == 200, "the regulation library answers"):
            print(f"      {len(docs)} documents stored; {len(missing)} cited by approved rules are not on this server")
        site.json("POST", "/api/auth/logout")

    print("\n" + ("every check passed" if not FAILED else f"{len(FAILED)} failed: " + "; ".join(FAILED)))
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
