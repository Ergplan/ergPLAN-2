"""Rows of any version, drafts included. Used only by analyst endpoints and golden runs; never by
an engine serving a client (invariant 2)."""

from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.models import Rule, RulebookVersion
from .models import RuleView


def rules(session: Session, version_id: str) -> list[RuleView]:
    v = session.get(RulebookVersion, version_id)
    if v is None:
        raise LookupError(version_id)
    return [
        RuleView(
            id=r.id,
            family=r.family,
            version_id=v.id,
            jurisdiction_id=v.jurisdiction_id,
            scope=r.scope,
            body=r.body,
            source_ref=r.source_ref,
            field_refs=r.field_refs or {},
            label=r.label,
            position=r.position,
            effective_from=v.effective_from,
            effective_to=v.effective_to,
        )
        for r in session.scalars(select(Rule).where(Rule.rulebook_version_id == v.id).order_by(Rule.position))
    ]


def drafts_in_force(
    session: Session, family: str, jurisdiction_ids: list[str], on: date, origin: str | None = None
) -> list[RuleView]:
    """Rows of the DRAFT versions of a family that would be in force on a date for a jurisdiction
    chain (optionally of one origin, e.g. an importer's): the newest such draft per jurisdiction.
    For an analyst's trial of drafts (a sandbox bill replay); never for a client."""
    q = (
        select(RulebookVersion)
        .where(
            RulebookVersion.family == family,
            RulebookVersion.jurisdiction_id.in_(jurisdiction_ids),
            RulebookVersion.status == "draft",
            RulebookVersion.effective_from <= on,
        )
        .order_by(RulebookVersion.jurisdiction_id, RulebookVersion.version.desc())
    )
    out: list[RuleView] = []
    seen: set[str] = set()
    for v in session.scalars(q):
        if v.effective_to is not None and v.effective_to < on:
            continue
        if origin is not None and v.origin != origin:
            continue
        if v.jurisdiction_id not in seen:
            seen.add(v.jurisdiction_id)
            out += rules(session, v.id)
    return out
