"""A solar or wind generation profile supplied for a site (Stage 15), through the generation screen's
own upload (`POST /sites/{id}/generation-profiles`): the file is checked there and refused, with the
reason, when it is not a whole, possible year."""

from __future__ import annotations

import io
from datetime import date, datetime
from decimal import Decimal
from typing import Literal

import anyio.to_thread
from fastapi import UploadFile
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import BaseModel

from ...platform.routers import generation as screen
from .. import uploads
from ..registry import call, call_async, signed_in, tool


class GenerationOut(BaseModel):
    status: Literal["upload_needed", "waiting_for_upload", "done", "failed"]
    upload_id: str | None = None
    upload_url: str | None = None
    upload_expires_at: datetime | None = None
    error: str | None = None
    profile: screen.GenerationProfileOut | None = None
    next_step: str


@tool(title="Add a generation profile")
async def add_generation_profile(
    site_id: str,
    technology: Literal["solar", "wind"],
    provenance: Literal["measured", "stated"],
    applies_to: Literal["open_access", "rooftop"] = "open_access",
    basis: Literal["per_mw", "capacity"] | None = None,
    capacity_mw: Decimal | None = None,
    first_day: date | None = None,
    csv_text: str | None = None,
    upload_id: str | None = None,
) -> GenerationOut:
    """Use a year of solar or wind generation in place of ergPLAN's library curve for the site's new
    runs. The CSV has a header naming its unit (kW, MW, kWh, MWh, CUF, pu or CUF %), one row per hour
    or 15 minutes for a whole year, and a timestamp column or first_day. provenance: measured (the
    plant's meter) or stated (the developer's estimate). kW/MW/kWh/MWh values are per_mw (for 1 MW) or
    for capacity_mw. Give the file as csv_text if you have it; otherwise the answer is a one-time
    upload link, and you call again with upload_id after the upload."""
    who = signed_in()
    if upload_id:
        try:
            got = await anyio.to_thread.run_sync(uploads.state, upload_id, who.actor)
        except LookupError as e:
            raise ToolError("there is no such upload link; call again without csv_text for a new one") from e
        if got.status == "waiting_for_upload":
            return GenerationOut(
                status="waiting_for_upload",
                upload_id=upload_id,
                upload_expires_at=got.expires_at,
                error=got.error,
                next_step=(
                    "The file has not arrived yet (or was refused: see error). Ask the person to upload it."
                ),
            )
        if got.status == "expired":
            return GenerationOut(
                status="failed",
                upload_id=upload_id,
                error="the upload link expired",
                next_step="Ask for a new link.",
            )
        pid = str(got.result["profile_id"])
        profile = await call(lambda: screen.get_generation_profile(pid, who))
        return GenerationOut(status="done", upload_id=upload_id, profile=profile, next_step=_done(profile))
    if csv_text:
        data = csv_text.encode()
        if len(data) > screen.MAX_BYTES:
            raise ToolError("the file is larger than 20 MB")
        file = UploadFile(io.BytesIO(data), filename=f"{technology}-profile.csv", size=len(data))
        profile = await call_async(
            lambda: screen.upload_generation_profile(
                site_id, who, file, technology, provenance, applies_to, basis, capacity_mw, first_day
            )
        )
        return GenerationOut(status="done", profile=profile, next_step=_done(profile))
    await call(lambda: screen.list_generation_profiles(site_id))  # the site exists (404 otherwise)
    params = {
        "site_id": site_id,
        "technology": technology,
        "provenance": provenance,
        "applies_to": applies_to,
        "basis": basis,
        "capacity_mw": None if capacity_mw is None else format(capacity_mw, "f"),
        "first_day": None if first_day is None else first_day.isoformat(),
    }
    link = await anyio.to_thread.run_sync(uploads.issue, "generation", who.actor, params)
    return GenerationOut(
        status="upload_needed",
        upload_id=link.upload_id,
        upload_url=link.url,
        upload_expires_at=link.expires_at,
        next_step=(
            f"Give the person this one-time link to upload the CSV (one file, fifteen minutes): {link.url} . "
            f"In Claude Code: curl -sS -F file=@/path/to/profile.csv '{link.url}' . "
            "Then call add_generation_profile again with upload_id (and the same site_id and technology)."
        ),
    )


def _done(profile: screen.GenerationProfileOut) -> str:
    return (
        f"The {profile.technology} profile is stored (version {profile.version}) and new runs for the "
        "site use it. Say its annual CUF beside the library curve's, both as the tool gives them."
    )
