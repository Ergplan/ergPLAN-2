"""The questions a site's bills do not answer (Stage 15), and what happens when one is answered.

`listing(session, site_id)` gives every question with its plain wording, its unit, why it is asked,
what ergPLAN uses until it is answered, and the answer in force; `outstanding(session, site_id)`
gives those still to ask. `answer(session, site_id, key, value, actor)` stores the answer as a
`client_stated` value (who, when) and makes a new load-profile version for each of the site's
connections, so a run made afterwards pins the new answers and a run made before replays on the
profile it pinned. `withdraw` takes an answer back the same way. The screens call these functions,
and the Stage 16 MCP tools `list_questions` and `answer` call the same ones.

A site whose connection has a measured 15-minute file is not asked what only shapes the synthesis
(shifts, weekly off, seasonal shutdowns, large loads): the measured profile stays in use.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field, JsonValue
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.sourced import SourcedValue, SourceRef
from ..db.models import Connection, LoadProfile, Site, SiteAnswer
from . import answers as store
from . import measured_upload, profile_synthesis, site_intake

KINDS: list[JsonValue] = ["client_stated"]
FormKind = Literal["shifts", "weekdays", "shutdowns", "loads", "number", "percent"]


class SiteNotFound(LookupError):
    pass


class AnswerOut(BaseModel):
    """An answer in force: what the client stated, by whom and when. Never a sourced fact."""

    value: Any
    words: str
    figures: list[SourcedValue] = Field(json_schema_extra={"owner": "site_answers.value", "ref_kinds": KINDS})
    stated_by: str
    stated_at: datetime
    source_ref: SourceRef


class Question(BaseModel):
    key: str
    label: str
    question: str
    unit: str
    why: str
    form: FormKind
    # what the answer changes: the load profile's shape, the sizing, or (yet) nothing in the engine
    used_for: str
    shapes_profile_only: bool
    asked: bool  # False: not asked for this site (not_asked says why)
    not_asked: str | None = None
    until_answered: str  # what ergPLAN uses until the question is answered
    known_from_inputs: str | None = None  # already typed with the site's inputs
    answer: AnswerOut | None = None


@dataclass(frozen=True)
class _Q:
    key: str
    question: str
    unit: str
    why: str
    form: FormKind
    used_for: str
    shapes_profile_only: bool = False


CATALOGUE: tuple[_Q, ...] = (
    _Q(
        "shifts",
        "What are the shifts, and at what hours does each start and end?",
        "hours of the day (HH:MM)",
        "The bills give energy by month and by time-of-day slot, not by hour. The shift hours say "
        "when the plant draws its power, which decides how much solar or wind lands in working hours.",
        "shifts",
        "The day's shape of the load profile: the working level in the shift hours, the base load "
        "outside them.",
        True,
    ),
    _Q(
        "weekly_off",
        "Which days of the week is the plant closed?",
        "days of the week",
        "Energy generated on a closed day is not used in the plant; it must be banked or it lapses.",
        "weekdays",
        "The week's shape of the load profile: base load on the closed days, working days otherwise.",
        True,
    ),
    _Q(
        "seasonal_shutdowns",
        "Does the plant shut down for part of the year (maintenance, a season, festivals)? From which "
        "day to which day?",
        "days of the year (from, to)",
        "A shutdown moves the month's energy onto its working days, and lowers the estimate for a "
        "month that has no bill.",
        "shutdowns",
        "The months of the load profile: base load on the shutdown days.",
        True,
    ),
    _Q(
        "large_loads",
        "Which large loads run at set hours (a furnace, compressors, a chiller plant), how many kW "
        "is each, and when does it run?",
        "kW and hours of the day (HH:MM)",
        "The bill records the month's highest demand but not when it happens. A large load says when "
        "the peak falls.",
        "loads",
        "The peaks of the load profile: demand is raised in the hours each load runs.",
        True,
    ),
    _Q(
        "power_factor",
        "What power factor does the plant run at?",
        "ratio (0.30 to 1.00)",
        "Recorded demand is in kVA and the load profile is in kW; where energy is billed in kVAh the "
        "power factor also converts units to kWh. It is asked where the bills do not print it.",
        "number",
        "The profile's peak (recorded kVA × power factor) and the kVAh conversion, in months whose "
        "bill prints no power factor; and the grid's kVAh rate in the sizing when no bill gives one.",
    ),
    _Q(
        "expansion",
        "Is an expansion (or a reduction) planned? By what percentage will consumption change?",
        "percent of today's consumption",
        "The plan is sized for the load the plant will have, not only the load on the past bills.",
        "percent",
        "The year the sizing is run for: every block of the load profile is scaled by the growth. "
        "Today's cost screens keep the year the bills cover.",
    ),
    _Q(
        "roof_sqm",
        "How much roof is free for solar panels?",
        "m²",
        "Rooftop solar cannot be larger than the roof allows.",
        "number",
        "The largest rooftop solar offered to the sizing (10 m² of roof per kWp).",
    ),
    _Q(
        "land_acres",
        "How much free land is there at the site?",
        "acres",
        "Free land could carry ground-mounted solar or a battery yard.",
        "number",
        "Recorded and shown on the plan. The sizing has no ground-mounted plant at the site yet, so "
        "it bounds nothing today.",
    ),
    _Q(
        "contract_demand_kva",
        "What contract demand do you intend to hold with the DISCOM?",
        "kVA",
        "The bill shows the contract demand held today. If a different one is intended (after an "
        "expansion, or after a surrender), demand charges and the open-access limits follow it.",
        "number",
        "The contract demand the sizing starts from, in place of the bill's. Today's cost screens "
        "keep the bill's.",
    ),
)
_BY_KEY = {q.key: q for q in CATALOGUE}

MEASURED_REASON = (
    "A measured 15-minute file is in use for this site, so its load profile needs no estimate of this."
)


def _site(session: Session, site_id: str) -> Site:
    site = session.get(Site, site_id)
    if site is None:
        raise SiteNotFound(site_id)
    return site


def _connections(session: Session, site_id: str) -> list[Connection]:
    return list(session.scalars(select(Connection).where(Connection.site_id == site_id)))


def measured(session: Session, site_id: str) -> bool:
    """True when every connection of the site that has a load profile has a measured one."""
    has = [
        measured_upload.latest(session, c.id) is not None
        for c in _connections(session, site_id)
        if session.scalars(select(LoadProfile.id).where(LoadProfile.connection_id == c.id)).first()
    ]
    return bool(has) and all(has)


def _until_answered(key: str, site: Site, conns: list[Connection]) -> str:
    if key == "shifts":
        try:
            _, d = profile_synthesis.shape(site.industry_shape)
            return f"The operating pattern chosen with the inputs ({d.get('name', site.industry_shape)})."
        except profile_synthesis.ShapeNotFound:
            return "The operating pattern chosen with the inputs."
    if key == "weekly_off":
        return f"{store.words('weekly_off', list(site.weekly_off or []))} (from the inputs)."
    if key == "seasonal_shutdowns":
        return "No shutdown: every month runs as its bill shows."
    if key == "large_loads":
        return "None named: the peak falls where the operating pattern is busiest."
    if key == "power_factor":
        return "The power factor each bill prints; 0.95 where a bill prints none."
    if key == "expansion":
        return "No change: the plan is sized for the year the bills cover."
    if key == "roof_sqm":
        return "Not stated: rooftop solar is offered up to the contract demand in kW."
    if key == "land_acres":
        return "Not stated."
    cds = [c.contract_demand_kva for c in conns if c.contract_demand_kva is not None]
    return (
        f"The contract demand held today ({store.words('contract_demand_kva', cds[0])})."
        if cds
        else "The contract demand held today."
    )


def _every_bill_prints_power_factor(session: Session, conns: list[Connection]) -> bool:
    """True when every month the site's synthesised profiles were built from carries a power factor
    of its own (the bill's), so a stated one would be used nowhere."""
    found = False
    for c in conns:
        last = session.scalars(
            select(LoadProfile)
            .where(LoadProfile.connection_id == c.id, LoadProfile.provenance == "synthesised")
            .order_by(LoadProfile.version.desc())
        ).first()
        months = (((last.method or {}).get("inputs") or {}).get("months") or []) if last else []
        if any("power_factor" not in m for m in months):
            return False
        found = found or bool(months)
    return found


def _known(key: str, site: Site, power_factor_on_bills: bool = False) -> str | None:
    """A value already typed with the site's inputs (or printed on every bill), which need not be
    asked again."""
    prov = (site.provenance or {}).get(key) or {}
    if key == "power_factor" and power_factor_on_bills:
        return "printed on every bill"
    if key == "roof_sqm" and site.roof_sqm is not None:
        return store.words(key, site.roof_sqm)
    if key == "land_acres" and site.land_acres is not None:
        return store.words(key, site.land_acres)
    if key == "weekly_off" and prov.get("provenance") == "client_stated":
        return store.words(key, list(site.weekly_off or []))
    return None


def _answer_out(key: str, row: SiteAnswer) -> AnswerOut:
    return AnswerOut(
        value=row.value,
        words=store.words(key, row.value),
        figures=store.figures(key, row),
        stated_by=row.stated_by,
        stated_at=row.stated_at,
        source_ref=store.stated_ref(key, row),
    )


def listing(session: Session, site_id: str) -> list[Question]:
    """Every question for the site, answered or not."""
    site = _site(session, site_id)
    conns = _connections(session, site_id)
    have = store.in_force(session, site_id)
    is_measured = measured(session, site_id)
    pf_on_bills = _every_bill_prints_power_factor(session, conns)
    out = []
    for q in CATALOGUE:
        moot = is_measured and q.shapes_profile_only
        row = have.get(q.key)
        out.append(
            Question(
                key=q.key,
                label=store.LABELS[q.key],
                question=q.question,
                unit=q.unit,
                why=q.why,
                form=q.form,
                used_for=q.used_for,
                shapes_profile_only=q.shapes_profile_only,
                asked=not moot,
                not_asked=MEASURED_REASON if moot else None,
                until_answered=_until_answered(q.key, site, conns),
                known_from_inputs=_known(q.key, site, pf_on_bills),
                answer=_answer_out(q.key, row) if row is not None else None,
            )
        )
    return out


def outstanding(session: Session, site_id: str) -> list[Question]:
    """What is still missing: asked for this site, not answered, and not already typed with the inputs."""
    return [
        q for q in listing(session, site_id) if q.asked and q.answer is None and q.known_from_inputs is None
    ]


@dataclass
class ProfileNow:
    connection_id: str
    load_profile_id: str
    version: int
    provenance: str


@dataclass
class Outcome:
    question: Question
    changed: bool
    profiles: list[ProfileNow] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def refresh_profiles(session: Session, site_id: str) -> tuple[list[ProfileNow], list[str]]:
    """A new load-profile version for each connection of the site, made with the answers in force:
    the measured profile carried forward where there is one, else the synthesis run again from the
    intake the last synthesised profile recorded. A connection with no profile yet gets none."""
    out: list[ProfileNow] = []
    warnings: list[str] = []
    for c in _connections(session, site_id):
        lp = measured_upload.carry_forward(session, c.id)
        if lp is None:
            last = session.scalars(
                select(LoadProfile)
                .where(LoadProfile.connection_id == c.id, LoadProfile.provenance == "synthesised")
                .order_by(LoadProfile.version.desc())
            ).first()
            if last is None or "intake" not in (last.method or {}):
                continue
            intake = site_intake.SiteIntake.model_validate(last.method["intake"])
            lp = site_intake.synthesise(session, c.id, intake)
            warnings += site_intake.profile_warnings(lp.method)
        out.append(ProfileNow(c.id, lp.id, lp.version, lp.provenance))
    return out, warnings


def _check(session: Session, site_id: str, key: str, stored: Any) -> list[str]:
    """What is worth saying about an answer that is accepted all the same."""
    notes = []
    if key == "large_loads":
        cds = [c.contract_demand_kva for c in _connections(session, site_id) if c.contract_demand_kva]
        total = sum((Decimal(str(x["kw"])) for x in stored), Decimal(0))
        if cds and total > max(cds):
            notes.append(
                f"The large loads add up to {store.plain(total)} kW, "
                f"more than the contract demand ({store.words('contract_demand_kva', max(cds))}). The "
                "profile still peaks at the demand the bills record."
            )
    return notes


def _one(session: Session, site_id: str, key: str) -> Question:
    return next(q for q in listing(session, site_id) if q.key == key)


def answer(session: Session, site_id: str, key: str, value: Any, actor: str) -> Outcome:
    """State or change the answer to one question. Raises `SiteNotFound`, `answers.UnknownQuestion`
    or `answers.InvalidAnswer` (its message is for the person)."""
    _site(session, site_id)
    if key not in _BY_KEY:
        raise store.UnknownQuestion(key)
    row, changed = store.put(session, site_id, key, value, actor)
    profiles, warnings = refresh_profiles(session, site_id) if changed else ([], [])
    return Outcome(
        _one(session, site_id, key), changed, profiles, _check(session, site_id, key, row.value) + warnings
    )


def withdraw(session: Session, site_id: str, key: str, actor: str) -> Outcome:
    """Take an answer back: ergPLAN returns to what it used before the question was answered."""
    _site(session, site_id)
    if key not in _BY_KEY:
        raise store.UnknownQuestion(key)
    changed = store.withdraw(session, site_id, key, actor)
    profiles, warnings = refresh_profiles(session, site_id) if changed else ([], [])
    return Outcome(_one(session, site_id, key), changed, profiles, warnings)


def for_report(session: Session, site_id: str, method: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """The answers in force for the plan page, each as a client-stated chip: its label, its words,
    its figures, who stated it and when, what it is used for, and (with `method`, the load profile a
    plan's run pinned) whether that plan was sized with this answer."""
    used = store.pinned(method) if method is not None else None
    out = []
    for q in listing(session, site_id):
        if q.answer is None:
            continue
        out.append(
            {
                "key": q.key,
                "label": q.label,
                "words": q.answer.words,
                "figures": [f.model_dump(mode="json") for f in q.answer.figures],
                "stated_by": q.answer.stated_by,
                "stated_at": q.answer.stated_at,
                "source_ref": q.answer.source_ref.model_dump(mode="json"),
                "used_for": q.used_for,
                "in_use": q.asked,
                "in_this_plan": None if used is None else used.get(q.key) == q.answer.value,
            }
        )
    return out
