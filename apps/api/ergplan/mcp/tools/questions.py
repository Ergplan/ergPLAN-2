"""What the bills do not say (Stage 15): the questions, and the answers. The functions are the
questions screen's (`GET /sites/{id}/questions`, `PUT` and `DELETE /sites/{id}/questions/{key}`).
Every answer is a `client_stated` value and makes a new load-profile version."""

from __future__ import annotations

from pydantic import JsonValue

from ...platform.routers import questions as screen
from ..registry import call, signed_in, tool


@tool(title="The open questions", read_only=True)
async def list_questions(site_id: str) -> screen.QuestionsOut:
    """Every question the site's bills do not answer: its wording, unit, why it is asked, what ergPLAN
    uses until it is answered, and the answer in force. `outstanding` lists those still to ask, in
    the order to ask them. Ask them one or two at a time, before any sizing."""
    signed_in()
    return await call(lambda: screen.list_questions(site_id))


@tool(title="Answer a question")
async def answer(
    site_id: str, key: str, value: JsonValue = None, withdraw: bool = False
) -> screen.AnswerResult:
    """Store the person's answer to one question (key from list_questions; value in the question's
    form: a number as a string, a percent, a list of shifts such as [{"start": "06:00", "end":
    "18:00"}], weekdays, shutdowns or large loads). The answer is the client's own statement, shown as
    such. withdraw=true takes the answer back (ergPLAN returns to its default)."""
    who = signed_in()
    if withdraw:
        return await call(lambda: screen.withdraw_answer(site_id, key, who))
    body = screen.AnswerIn(value=value)
    return await call(lambda: screen.answer_question(site_id, key, body, who))
