"""The MCP endpoint `/mcp` (Stage 16): streamable HTTP through the official MCP Python SDK.

The SDK's session manager runs stateless with JSON responses: every POST is one JSON-RPC exchange,
so the endpoint is an ordinary FastAPI route behind the same app (no second server, no lifespan of
its own). A long solve is never held in a request: `run_sizing` and `transition_plan` queue runs for
the worker and answer "running" with the ids that `sizing_status` and `transition_plan` read.

Every request needs an access token from `oauth`; without one the answer is 401 with the pointer to
the resource metadata, and while password sign-in is switched off on the server nobody gets one.
"""

from __future__ import annotations

import importlib
import pkgutil
from functools import cache

from fastapi import APIRouter, Request
from fastapi.responses import Response
from mcp.server.fastmcp import FastMCP
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.types import ToolAnnotations
from starlette.concurrency import run_in_threadpool
from starlette.types import Receive, Scope, Send

from ..core.settings import get_settings
from ..platform.auth import OPEN_ACTOR, Caller
from . import oauth, tools
from .registry import CALLER, TOOLS

router = APIRouter(include_in_schema=False)

MAX_BODY = 32 * 1024 * 1024  # a bill passed as base64 (up to 20 MB of PDF)

INSTRUCTIONS = """\
ergPLAN (jouleWise) plans an industrial electricity consumer's move to renewable energy in India: \
from its electricity bills to today's cost, the landed cost of each source, a sizing that gives the \
least-cost grid replacement under the state's own settlement rules, and a phased transition plan.

How to work with it:
1. Bills first. Pass each bill to read_bill (billx reads it; nothing else does). If you cannot pass \
the file's bytes, read_bill gives a one-time upload link: give it to the person (or, in Claude \
Code, upload with curl as read_bill says) and call read_bill again with the upload_id. Ask the \
person to check each value read_bill lists as needing confirmation, then call confirm_field.
2. When every bill of the connection is read and confirmed, call build_site with all their ids.
3. Before any sizing, call list_questions and ask the open questions ONE OR TWO AT A TIME, in the \
order given, in plain words, saying what ergPLAN uses until it is answered. Store each answer with \
answer. The person may skip a question: the stated default is then used. Offer add_generation_profile \
if they have a solar or wind generation profile.
4. Then todays_cost and landed_cost, then run_sizing (it queues the solve and answers "running"; \
call sizing_status until it is done), then get_plan or transition_plan, and share_link if asked.

Rules for what you say:
- State numbers ONLY from tool results, with their units. Never estimate, round into new figures \
or compute a figure of your own; if a figure is not in a result, say ergPLAN has not worked it out.
- Every figure carries source_refs. Say which figures are ASSUMED (source kind "assumption") or \
stated by the client ("client_stated"), as against those sourced to a tariff order, regulation, \
bill or market price, and offer to replace an assumption with the person's own figure.
- A run whose notes say it ended on its time limit, or is not proven cheapest, is never \
presented as the optimum: say it is the best plan found in the time, with its gap.
- Where a result lists gaps or "not yet sourced", say so plainly rather than filling them in.
"""


def _load_tools() -> None:
    """Import every module of ergplan.mcp.tools: each declares its tools with @tool."""
    for info in pkgutil.iter_modules(tools.__path__):
        importlib.import_module(f"{tools.__name__}.{info.name}")


@cache
def server() -> FastMCP:
    _load_tools()
    mcp = FastMCP(name="ergPLAN", instructions=INSTRUCTIONS, website_url=oauth.issuer())
    for t in TOOLS.values():
        mcp.add_tool(
            t.fn,
            name=t.name,
            title=t.title,
            description=t.description,
            annotations=ToolAnnotations(
                title=t.title, readOnlyHint=t.read_only, destructiveHint=False, openWorldHint=False
            ),
            structured_output=True,
        )
        # The SDK derives the output schema in validation mode; a result goes out serialised (a
        # SourceRef carries its computed `url`, money is a string), and the server checks each result
        # against the schema it lists, so the schema is the serialised form's.
        made = mcp._tool_manager.get_tool(t.name)
        assert made is not None and made.fn_metadata.output_model is not None
        made.fn_metadata.output_schema = made.fn_metadata.output_model.model_json_schema(mode="serialization")
    return mcp


class _Exchange(Response):
    """Hands the request to the SDK's transport, as the administrator the token was issued to."""

    def __init__(self, who: Caller) -> None:
        super().__init__()
        self.who = who

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        mark = CALLER.set(self.who)
        try:
            # one manager per request: stateless, so nothing outlives the exchange
            manager = StreamableHTTPSessionManager(
                app=server()._mcp_server,  # the SDK's low-level server behind FastMCP
                stateless=True,
                json_response=True,
                max_request_body_size=MAX_BODY,
            )
            async with manager.run():
                await manager.handle_request(scope, receive, send)
        finally:
            CALLER.reset(mark)


@router.api_route("/mcp", methods=["GET", "POST", "DELETE"])
async def endpoint(request: Request) -> Response:
    if get_settings().ergplan_open_access and not request.headers.get("authorization"):
        # the person's decision (ERGPLAN_OPEN_ACCESS): the connector needs no sign-in
        return _Exchange(Caller(actor=OPEN_ACTOR, role="analyst"))
    grant = await run_in_threadpool(oauth.bearer, request.headers.get("authorization"))
    if isinstance(grant, oauth.Refusal):
        return oauth.unauthorized(grant)
    return _Exchange(Caller(actor=grant.actor, role="analyst"))
