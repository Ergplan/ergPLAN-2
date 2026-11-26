"""Bills: read one through billx, confirm what it asks, and build the site from the bills.

The functions called are the upload screen's (`POST /bill-reads`, `GET /bill-reads/{id}`,
`POST /bills/{id}/confirm`, `POST /intake/from-bills`, `POST /sites`): billx reads the file, ergPLAN
stores its reading with every field's proof (invariant 7)."""

from __future__ import annotations

import base64
import binascii
import io
from datetime import date, datetime
from typing import Literal

import anyio
import anyio.to_thread
from fastapi import HTTPException, UploadFile
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import BaseModel, Field, JsonValue

from ...core.settings import get_settings
from ...intake import site_intake
from ...platform import ratelimit
from ...platform.routers import intake as screen
from ...platform.routers import questions as questions_screen
from .. import uploads
from ..registry import call, call_async, signed_in, tool

# how long read_bill waits for the worker's billx reading before answering "reading" (billx takes
# one to three minutes a bill; the tests set it to 0)
QUICK_WAIT_S = 10.0
MAX_PDF_BYTES = 20 * 1024 * 1024

ReadStatus = Literal["upload_needed", "waiting_for_upload", "queued", "reading", "done", "failed"]


class ToConfirm(BaseModel):
    bill_id: str
    bill_month: date | None
    fields: list[screen.FieldReviewOut]  # each value to check against the bill (its page is given)


class ReadBillOut(BaseModel):
    status: ReadStatus
    read_id: str | None = None
    upload_id: str | None = None
    upload_url: str | None = None  # a one-time link: one file, fifteen minutes
    upload_expires_at: datetime | None = None
    error: str | None = None
    bills: list[screen.BillOut] = Field(default_factory=list)
    to_confirm: list[ToConfirm] = Field(default_factory=list)
    next_step: str


def _upload_words(url: str) -> str:
    return (
        "Give the person this one-time link to upload the bill PDF in their browser (one file, fifteen "
        f"minutes): {url} . In Claude Code you may upload a local file yourself: "
        f"curl -sS -F file=@/path/to/bill.pdf '{url}' . Then call read_bill again with upload_id."
    )


def _from_read(out: screen.BillReadOut) -> ReadBillOut:
    to_confirm = [
        ToConfirm(bill_id=b.id, bill_month=b.bill_month, fields=[f for f in b.fields if f.needs_confirmation])
        for b in out.bills
        if b.outstanding
    ]
    if out.status in ("queued", "reading"):
        step = (
            "billx is reading the file (one to three minutes a bill). Call read_bill again with this "
            "read_id in a minute; meanwhile ask the person nothing that needs the bill's values."
        )
    elif out.status == "failed":
        step = "billx could not read the file; tell the person why (error) and ask for a clearer PDF."
    elif to_confirm:
        step = (
            "Ask the person to check each value in to_confirm against their bill, one or two at a "
            "time (say the page), then call confirm_field for each. When no bill has anything left to "
            "confirm, call build_site with the ids of all the connection's bills."
        )
    else:
        step = (
            "Every value is confirmed. Read every bill of the connection, then call build_site with "
            "their ids."
        )
    status: ReadStatus = out.status if out.status in ("queued", "reading", "done", "failed") else "reading"  # type: ignore[assignment]
    return ReadBillOut(
        status=status, read_id=out.id, error=out.error, bills=out.bills, to_confirm=to_confirm, next_step=step
    )


async def _read(read_id: str) -> ReadBillOut:
    who = signed_in()
    waited = 0.0
    while True:
        out = await call(lambda: screen.get_bill_read(read_id, who))
        if out.status in ("done", "failed") or waited >= QUICK_WAIT_S:
            return _from_read(out)
        await anyio.sleep(1.0)
        waited += 1.0


@tool(title="Read a bill")
async def read_bill(
    pdf_base64: str | None = None,
    filename: str | None = None,
    connection_id: str | None = None,
    read_id: str | None = None,
    upload_id: str | None = None,
) -> ReadBillOut:
    """Read an electricity bill with billx (ergPLAN's only bill reader) and say what was read and what
    the person must confirm. Give the PDF as pdf_base64 if you have its bytes; with no file, the
    answer is a one-time upload link (upload_url) and an upload_id. Call again with upload_id after
    the upload, or with read_id while billx is reading (it takes one to three minutes a bill).
    connection_id adds the bill to an existing connection."""
    who = signed_in()
    if upload_id:
        try:
            got = await anyio.to_thread.run_sync(uploads.state, upload_id, who.actor)
        except LookupError as e:
            raise ToolError(
                "there is no such upload link; call read_bill without arguments for a new one"
            ) from e
        if got.status == "waiting_for_upload":
            return ReadBillOut(
                status="waiting_for_upload",
                upload_id=upload_id,
                upload_expires_at=got.expires_at,
                error=got.error,
                next_step=(
                    "The file has not arrived yet. Ask the person to upload it with the link, then call "
                    "again."
                ),
            )
        if got.status == "expired":
            return ReadBillOut(
                status="failed",
                upload_id=upload_id,
                error="the upload link expired before a file was sent",
                next_step="Call read_bill without arguments for a new link.",
            )
        out = await _read(str(got.result["read_id"]))
        return out.model_copy(update={"upload_id": upload_id})
    if read_id:
        return await _read(read_id)
    if pdf_base64:
        try:
            data = base64.b64decode(pdf_base64, validate=False)
        except (binascii.Error, ValueError) as e:
            raise ToolError("pdf_base64 is not base64") from e
        if len(data) > MAX_PDF_BYTES:
            raise ToolError("the file is larger than 20 MB")
        st = get_settings()
        try:
            ratelimit.check(
                "upload", f"{who.actor}|mcp", st.ergplan_upload_rate_limit, st.ergplan_upload_rate_window_s
            )
        except HTTPException as e:  # the screen's 429, in its words
            raise ToolError(str(e.detail)) from e
        file = UploadFile(io.BytesIO(data), filename=filename or "bill.pdf", size=len(data))
        started = await call_async(lambda: screen.start_bill_read(file, who, connection_id))
        return await _read(started.id)
    link = await anyio.to_thread.run_sync(
        uploads.issue, "bill", who.actor, {"connection_id": connection_id} if connection_id else {}
    )
    return ReadBillOut(
        status="upload_needed",
        upload_id=link.upload_id,
        upload_url=link.url,
        upload_expires_at=link.expires_at,
        next_step=_upload_words(link.url),
    )


@tool(title="Confirm a value read from a bill")
async def confirm_field(bill_id: str, path: str, value: str | None = None) -> screen.BillOut:
    """Record the person's confirmation of one value read from a bill (path as read_bill listed it).
    Give value only when the person corrects what billx read; leave it out to confirm it as read."""
    who = signed_in()
    if value is None:
        bill = await call(lambda: screen.get_bill(bill_id))
        value = next((f.value for f in bill.fields if f.path == path), None)
    body = screen.ConfirmIn(path=path, value=value)
    return await call(lambda: screen.confirm_field(bill_id, body, who))


class BuildSiteOut(BaseModel):
    client_id: str
    site_id: str
    connection_id: str
    load_profile_id: str
    load_profile_version: int
    warnings: list[str]
    intake: site_intake.SiteIntake  # what was taken from the bills, stated or assumed, with sources
    outstanding_questions: list[str]  # list_questions gives the wording, in the order to ask
    next_step: str


@tool(title="Build the site from its bills")
async def build_site(bill_ids: list[str], overrides: dict[str, JsonValue] | None = None) -> BuildSiteOut:
    """Make the client, site and connection from every confirmed bill of one connection (up to 24),
    and synthesise its load profile from them. overrides: what the person states that the bills do
    not (for example {"voltage_kv": "33"}); each becomes a client-stated value."""
    who = signed_in()
    ask = screen.FromBillsIn(bill_ids=bill_ids, overrides=dict(overrides or {}))
    intake = await call(lambda: screen.intake_from_bills(ask, who))
    built = await call(lambda: screen.build_site(screen.BuildIn(intake=intake), who))
    qs = await call(lambda: questions_screen.list_questions(built.site_id))
    return BuildSiteOut(
        **built.model_dump(),
        intake=intake,
        outstanding_questions=qs.outstanding,
        next_step=(
            "Call list_questions and ask the open questions one or two at a time before any sizing."
            if qs.outstanding
            else "Nothing is left to ask: call todays_cost, then landed_cost and run_sizing."
        ),
    )
