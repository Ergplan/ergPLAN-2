"""The transition plan, the state's rule book and the share link: the screens'
`POST /connections/{id}/transition-plan`, `GET /connections/{id}/statebook` and
`POST /connections/{id}/share`."""

from __future__ import annotations

from mcp.server.fastmcp.exceptions import ToolError

from ...db.base import session_scope
from ...platform.routers import plan as plan_screen
from ...platform.routers import statebooks as book_screen
from ...platform.routers import transition as transition_screen
from ...report import statebook
from ...report.transition import Target
from ..registry import call, signed_in, tool


@tool(title="The transition plan")
async def transition_plan(
    connection_id: str, target: Target | None = None, fresh: bool = False
) -> transition_screen.TransitionOut:
    """The phased transition from today's bill to a target, as the plan page shows it: rooftop, then
    open access, then storage, each a settled and pinned run with its capacities, capital or tariff,
    the year's cost and saving, the rule that holds it and what must be signed or applied for.
    target: {"kind": "lowest_cost"} (default) or {"kind": "share", "share_pct": "50"}, with the
    person's own figures per source. The runs are solved by the worker: while status is "running",
    call again with the same target (it returns the same plan, further on). fresh=true solves again."""
    who = signed_in()
    body = target or Target()
    return await call(
        lambda: transition_screen.start_transition_plan(connection_id, body, who, wait=False, fresh=fresh)
    )


@tool(title="The state's rule book", read_only=True)
async def state_rule_book(
    connection_id: str | None = None, discom: str | None = None
) -> book_screen.BookDetail:
    """The approved monthly rule book in force: for a connection (its own issue, else its DISCOM's
    reference consumer), or for a DISCOM id (its reference consumer). Landed cost by source and
    route, the saving on the grid tariff, how open-access energy and exchange purchases are settled
    (banking, ToD and 15-minute set-off, caps, lapse), every figure footnoted to its source. Only
    approved issues are given."""
    who = signed_in()
    if connection_id:
        return await call(lambda: book_screen.connection_book(connection_id, who))
    if not discom:
        raise ToolError("give connection_id or discom")

    def read() -> book_screen.BookDetail:
        with session_scope() as s:
            b = statebook.last_approved(s, discom.strip().lower(), None)
            if b is None:
                raise ToolError(f"no approved rule book for {discom} yet")
            return book_screen.BookDetail.of(b)

    return await call(read)


@tool(title="Share the plan")
async def share_link(connection_id: str) -> plan_screen.ShareOut:
    """A link to the live plan the client can open without signing in (it shows only this plan)."""
    who = signed_in()
    return await call(lambda: plan_screen.share_plan(connection_id, who))
