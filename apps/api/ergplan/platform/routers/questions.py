"""The questions a site's bills do not answer (Stage 15): list them, answer one, change or withdraw
an answer. Every answer is a `client_stated` value and makes a new load-profile version, so the
profile on the screen is redrawn from it. The functions behind these routes (`intake.questions`) are
the ones the Stage 16 MCP tools `list_questions` and `answer` call."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict

from ...db.base import session_scope
from ...intake import answers as store
from ...intake import questions as svc
from ..auth import Caller, caller

router = APIRouter(tags=["questions"])


class QuestionsOut(BaseModel):
    site_id: str
    measured: bool  # a measured 15-minute file is in use: the profile is not an estimate
    questions: list[svc.Question]
    outstanding: list[str]  # the keys still to ask, in the order to ask them


class AnswerIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: Any


class ProfileNowOut(BaseModel):
    connection_id: str
    load_profile_id: str
    version: int
    provenance: str


class AnswerResult(BaseModel):
    question: svc.Question
    changed: bool
    profiles: list[ProfileNowOut]  # the load profile now in use for each connection of the site
    warnings: list[str]
    outstanding: list[str]


def _result(s: Any, site_id: str, out: svc.Outcome) -> AnswerResult:
    return AnswerResult(
        question=out.question,
        changed=out.changed,
        profiles=[ProfileNowOut(**vars(p)) for p in out.profiles],
        warnings=out.warnings,
        outstanding=[q.key for q in svc.outstanding(s, site_id)],
    )


@router.get("/sites/{site_id}/questions", response_model=QuestionsOut)
def list_questions(site_id: str) -> QuestionsOut:
    with session_scope() as s:
        try:
            qs = svc.listing(s, site_id)
        except svc.SiteNotFound as e:
            raise HTTPException(404, "site not found") from e
        return QuestionsOut(
            site_id=site_id,
            measured=svc.measured(s, site_id),
            questions=qs,
            outstanding=[q.key for q in svc.outstanding(s, site_id)],
        )


@router.put("/sites/{site_id}/questions/{key}", response_model=AnswerResult)
def answer_question(
    site_id: str, key: str, body: AnswerIn, who: Annotated[Caller, Depends(caller)]
) -> AnswerResult:
    """State an answer, or change the one in force."""
    with session_scope(actor=who.actor, role=who.role) as s:
        try:
            return _result(s, site_id, svc.answer(s, site_id, key, body.value, who.actor))
        except svc.SiteNotFound as e:
            raise HTTPException(404, "site not found") from e
        except store.UnknownQuestion as e:
            raise HTTPException(404, "there is no such question") from e
        except store.InvalidAnswer as e:
            raise HTTPException(422, str(e)) from e


@router.delete("/sites/{site_id}/questions/{key}", response_model=AnswerResult)
def withdraw_answer(site_id: str, key: str, who: Annotated[Caller, Depends(caller)]) -> AnswerResult:
    """Withdraw an answer: ergPLAN goes back to what it used before the question was answered."""
    with session_scope(actor=who.actor, role=who.role) as s:
        try:
            return _result(s, site_id, svc.withdraw(s, site_id, key, who.actor))
        except svc.SiteNotFound as e:
            raise HTTPException(404, "site not found") from e
        except store.UnknownQuestion as e:
            raise HTTPException(404, "there is no such question") from e
