"""The landed-cost calculator's routes (Stage 19): the choices, one consumer's answer, every state at
once (also as CSV) and an approved state rule book as a file. The arithmetic is report.calculator's,
which is platform.routers.landed.landed() on approved rules.

Who may call. The routes are in auth.PUBLIC so the guard lets a signed-out visitor through, and
`access` then decides: with `ERGPLAN_PUBLIC_CALCULATOR` off (the default) a signed-out caller in
production is refused like on any other screen; with it on, a visitor may use the calculator. Every
route is rate-limited per address. Nothing is stored: each request reads in a READ ONLY transaction,
so the database refuses any write a visitor's use could cause."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from ...core.settings import get_settings
from ...db.base import session_scope
from ...db.models import StateRulebook
from ...documents import service as documents
from ...report import calculator as calc
from ...report import statebook
from .. import ratelimit
from ..auth import Caller, caller, production

router = APIRouter(tags=["calculator"])


def access(request: Request, who: Annotated[Caller, Depends(caller)]) -> Caller:
    """Sign-in unless the person has opened the calculator to visitors; then the address's limit."""
    if production() and not who.signed_in and not get_settings().ergplan_public_calculator:
        raise HTTPException(401, "sign in first")
    st = get_settings()
    ratelimit.check(
        "calculator",
        ratelimit.client_address(request),
        st.ergplan_calculator_rate_limit,
        st.ergplan_calculator_rate_window_s,
    )
    return who


Who = Annotated[Caller, Depends(access)]


def is_analyst(who: Caller) -> bool:
    return who.role == "analyst" and who.signed_in


@contextmanager
def reading() -> Iterator[Session]:
    """A transaction that can only read: a visitor's use of the calculator stores nothing."""
    with session_scope() as s:
        s.execute(text("SET TRANSACTION READ ONLY"))
        yield s


@router.get("/calculator/options", response_model=calc.OptionsOut)
def options(who: Who, on: date | None = None) -> calc.OptionsOut:
    """The states, DISCOMs and categories the calculator can price, and the exchange markets."""
    with reading() as s:
        return calc.options(s, on or date.today())


def run(inp: calc.CalculatorIn, who: Caller) -> calc.CalculatorOut:
    """One consumer's answer (the screen's endpoint and the MCP tool call this)."""
    with reading() as s:
        try:
            return calc.calculate(s, inp, analyst=is_analyst(who))
        except calc.CalculatorError as e:
            raise HTTPException(422, str(e)) from e


@router.post("/calculator", response_model=calc.CalculatorOut)
def calculate(body: calc.CalculatorIn, who: Who) -> calc.CalculatorOut:
    """The landed cost of solar and wind by route (captive own plant, captive with 26% equity, third
    party) and of the exchange, line by line and per ToD slot, beside the grid tariff and the
    saving, for the consumer described. Answers at once; nothing is solved or stored."""
    return run(body, who)


def states_of(inp: calc.CalculatorIn, who: Caller) -> calc.StatesOut:
    with reading() as s:
        return calc.all_states(s, inp, analyst=is_analyst(who))


@router.post("/calculator/states", response_model=calc.StatesOut)
def states(body: calc.CalculatorIn, who: Who) -> calc.StatesOut:
    """Every state at once for one consumer definition (`discom` is ignored: each DISCOM is priced
    on its own reference HT industrial category)."""
    return states_of(body, who)


@router.post("/calculator/states.csv")
def states_csv(body: calc.CalculatorIn, who: Who) -> Response:
    """The all-states table as a CSV file, each figure with the pages it opens."""
    t = states_of(body, who)
    return Response(
        calc.to_csv(t),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="landed-cost-all-states-{t.month:%Y-%m}.csv"'},
    )


@router.get("/calculator/statebooks/{book_id}/file")
def statebook_file(book_id: str, who: Who) -> Response:
    """An approved reference state rule book (no client's) as a file; a draft is never served here."""
    with reading() as s:
        b = s.scalars(
            select(StateRulebook).where(
                StateRulebook.id == book_id,
                StateRulebook.status == "approved",
                StateRulebook.connection_id.is_(None),
            )
        ).first()
        if b is None:
            raise HTTPException(404, "rule book not found")
        name = f"rule-book-{b.state}-{b.discom_id}-{b.month:%Y-%m}-issue-{b.issue}.md"
        return Response(
            statebook.to_markdown(b, documents.stored_ids(s)),
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{name}"'},
        )
