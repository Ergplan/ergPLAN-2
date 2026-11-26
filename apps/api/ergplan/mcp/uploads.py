"""One-time upload links (Stage 16), for a file Claude cannot hand to a tool.

claude.ai cannot pass an attached file to a connector's tool (MCP has no file argument; a tool sees
only what the model writes into its arguments), and a PDF written out as base64 by the model is
neither reliable nor cheap. So `read_bill` and `add_generation_profile` can answer with a link
instead: `GET` shows a page with a file field (the person opens it in the browser), `POST` takes the
file (the page's form, or `curl -F file=@bill.pdf <link>` from Claude Code), and the file goes to the
same function the upload screen calls (`POST /bill-reads`, `POST /sites/{id}/generation-profiles`).

A link is bound to the administrator the tool was called by and works while that account is the
configured one; it lasts fifteen minutes and takes one file (a refused file does not spend it);
only the SHA-256 of its secret is stored (`mcp_uploads`). The tool is called again with the link's
`upload_id` to see what became of the file.
"""

from __future__ import annotations

import html
import io
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, Response
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile as StarletteUpload

from ..core.settings import get_settings
from ..db.base import session_scope
from ..db.models import McpUpload
from ..platform import admin, ratelimit
from ..platform.auth import Caller, sha
from ..platform.routers import generation as generation_routes
from ..platform.routers import intake as intake_routes
from . import oauth

router = APIRouter(include_in_schema=False)

LIFE = timedelta(minutes=15)
MAX_BYTES = 20 * 1024 * 1024
ACCEPT = {"bill": ".pdf,application/pdf", "generation": ".csv,text/csv"}
WHAT = {"bill": "the electricity bill (PDF)", "generation": "the generation profile (CSV, a year)"}


@dataclass(frozen=True)
class Link:
    upload_id: str
    url: str
    expires_at: datetime


def issue(kind: str, actor: str, params: dict[str, Any]) -> Link:
    """A new link for one file of `kind` ('bill' or 'generation'), bound to `actor`."""
    secret = secrets.token_urlsafe(32)
    with session_scope(actor=actor) as s:
        row = McpUpload(
            token_sha256=sha(secret),
            kind=kind,
            actor=actor,
            params=params,
            expires_at=datetime.now(UTC) + LIFE,
        )
        s.add(row)
        s.flush()
        return Link(row.id, f"{oauth.issuer()}/mcp/upload/{secret}", row.expires_at)


@dataclass(frozen=True)
class State:
    status: str  # waiting_for_upload | received | expired
    result: dict[str, Any]
    expires_at: datetime
    error: str | None


def state(upload_id: str, actor: str) -> State:
    """What became of a link, for its administrator. LookupError: no such link of theirs."""
    with session_scope() as s:
        row = s.get(McpUpload, upload_id)
        if row is None or row.actor != actor:
            raise LookupError(upload_id)
        if row.used_at is not None:
            status = "received"
        elif row.expires_at <= datetime.now(UTC):
            status = "expired"
        else:
            status = "waiting_for_upload"
        return State(status, dict(row.result or {}), row.expires_at, row.error)


# ---------------------------------------------------------------- the link itself


def _page(title: str, body: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        "<meta name='robots' content='noindex'>"
        f"<title>{html.escape(title)} | jouleWise ergPLAN</title>"
        "<style>body{font:15px/1.5 system-ui,sans-serif;max-width:30rem;margin:3rem auto;padding:0 1rem;"
        "color:#1d2a33}h1{font-size:1.25rem}button{margin-top:1rem;padding:.5rem 1rem;font:inherit}"
        ".note{color:#5b6b75}.error{color:#a4262c}</style></head><body>"
        f"<p class='note'>jouleWise | ergPLAN</p><h1>{html.escape(title)}</h1>{body}</body></html>",
        status_code=status,
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )


def _usable(row: McpUpload | None, now: datetime) -> str | None:
    if row is None:
        return "This upload link is not known. Ask Claude for a new one."
    if row.used_at is not None:
        return "This upload link was used already; each link takes one file. Ask Claude for a new one."
    if row.expires_at <= now:
        return "This upload link has expired (links last fifteen minutes). Ask Claude for a new one."
    if oauth.closed_reason() is not None or row.actor != admin.username():
        return "This upload link no longer works: the administrator's sign-in has changed."
    return None


def _peek(secret: str) -> tuple[str | None, str]:
    with session_scope() as s:
        row = s.query(McpUpload).filter(McpUpload.token_sha256 == sha(secret)).one_or_none()
        return _usable(row, datetime.now(UTC)), (row.kind if row is not None else "bill")


@router.get("/mcp/upload/{secret}")
async def upload_page(secret: str) -> HTMLResponse:
    why, kind = await run_in_threadpool(_peek, secret)
    if why is not None:
        return _page("Upload link", f"<p class='error'>{html.escape(why)}</p>", 410)
    return _page(
        "Upload a file for ergPLAN",
        f"<p>Choose {html.escape(WHAT[kind])} and send it. The link takes one file.</p>"
        "<form method='post' enctype='multipart/form-data'>"
        f"<input type='file' name='file' accept='{ACCEPT[kind]}' required> "
        "<button type='submit'>Send</button></form>",
    )


def _claim(secret: str) -> tuple[McpUpload | None, str | None]:
    """Take the link for this upload (so a second file sent at the same time is refused)."""
    with session_scope() as s:
        row = s.query(McpUpload).filter(McpUpload.token_sha256 == sha(secret)).with_for_update().one_or_none()
        why = _usable(row, datetime.now(UTC))
        if row is None or why is not None:
            return None, why
        row.used_at = datetime.now(UTC)
        s.flush()
        s.expunge(row)
        return row, None


def _finish(upload_id: str, result: dict[str, Any] | None, error: str | None) -> None:
    """Record what became of the file; a refused file gives the link back (unspent)."""
    with session_scope() as s:
        row = s.get(McpUpload, upload_id)
        assert row is not None
        if result is None:
            row.used_at = None
            row.error = error
        else:
            row.result = result
            row.error = None


async def _bill(row: McpUpload, file: UploadFile, who: Caller) -> dict[str, Any]:
    out = await intake_routes.start_bill_read(file, who, row.params.get("connection_id"))
    return {"read_id": out.id}


async def _generation(row: McpUpload, file: UploadFile, who: Caller) -> dict[str, Any]:
    p = row.params
    out = await generation_routes.upload_generation_profile(
        p["site_id"],
        who,
        file,
        p["technology"],
        p["provenance"],
        p.get("applies_to") or "open_access",
        p.get("basis"),
        Decimal(p["capacity_mw"]) if p.get("capacity_mw") else None,
        date.fromisoformat(p["first_day"]) if p.get("first_day") else None,
    )
    return {"profile_id": out.id}


HANDLERS: dict[str, Callable[[McpUpload, UploadFile, Caller], Awaitable[dict[str, Any]]]] = {
    "bill": _bill,
    "generation": _generation,
}


def _answer(request: Request, status: int, words: str, body: dict[str, Any]) -> Response:
    if "text/html" in request.headers.get("accept", ""):
        title = "Received" if status < 400 else "Not received"
        cls = "" if status < 400 else " class='error'"
        return _page(title, f"<p{cls}>{html.escape(words)}</p>", status)
    return JSONResponse(body | {"words": words}, status_code=status, headers={"Cache-Control": "no-store"})


@router.post("/mcp/upload/{secret}")
async def upload(secret: str, request: Request) -> Response:
    form = await request.form()
    file = form.get("file")
    if not isinstance(file, StarletteUpload):
        return _answer(request, 422, "Send the file in a form field named file.", {"received": False})
    data = await file.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        return _answer(request, 413, "The file is larger than 20 MB.", {"received": False})
    row, why = await run_in_threadpool(_claim, secret)
    if row is None:
        return _answer(request, 410, why or "This upload link does not work.", {"received": False})
    st = get_settings()
    try:
        ratelimit.check(
            "upload",
            f"{row.actor}|{ratelimit.client_address(request)}",
            st.ergplan_upload_rate_limit,
            st.ergplan_upload_rate_window_s,
        )
        who = Caller(actor=row.actor, role="analyst")
        upload_file = UploadFile(io.BytesIO(data), filename=file.filename or "upload", size=len(data))
        result = await HANDLERS[row.kind](row, upload_file, who)
    except HTTPException as e:
        await run_in_threadpool(_finish, row.id, None, str(e.detail))
        return _answer(
            request,
            e.status_code,
            f"The file was not taken: {e.detail}",
            {"received": False, "error": e.detail},
        )
    await run_in_threadpool(_finish, row.id, result, None)
    return _answer(
        request,
        201,
        "Received. Go back to Claude and say the file is uploaded.",
        {"received": True, "upload_id": row.id, "kind": row.kind, **result},
    )
