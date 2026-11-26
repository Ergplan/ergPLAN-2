"""How a tool is declared, and how it calls the screen's function.

A tool is an `async def` in a module of `ergplan.mcp.tools`, decorated with `@tool(...)`: its typed
parameters are the tool's input schema and its return annotation (a Pydantic model) its output
schema. `server` imports every module of the package, so adding a tool is adding one file there:

    from ..registry import call, signed_in, tool
    from ...platform.routers import landed

    @tool(title="Landed-cost calculator", read_only=True)
    async def landed_cost_calculator(connection_id: str) -> landed.LandedOut:
        \"\"\"What the description tells Claude.\"\"\"
        who = signed_in()
        return await call(lambda: landed.connection_landed_cost(connection_id, None))

`call` runs the function off the event loop and turns its refusal (the HTTPException the screen
would get) into the tool's error, in the same words. A tool computes nothing of its own.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, TypeVar

import anyio.to_thread
from fastapi import HTTPException
from mcp.server.fastmcp.exceptions import ToolError

from ..platform.auth import Caller

F = TypeVar("F", bound=Callable[..., Awaitable[Any]])

# the administrator behind the request being served (set by server for each /mcp request)
CALLER: ContextVar[Caller | None] = ContextVar("ergplan_mcp_caller", default=None)


@dataclass(frozen=True)
class ToolDef:
    fn: Callable[..., Awaitable[Any]]
    name: str
    title: str
    description: str
    read_only: bool


TOOLS: dict[str, ToolDef] = {}


def tool(*, title: str, read_only: bool = False) -> Callable[[F], F]:
    """Declare an MCP tool. The function's name is the tool's name, its docstring the description."""

    def deco(fn: F) -> F:
        if fn.__name__ in TOOLS:
            raise ValueError(f"two MCP tools are named {fn.__name__}")
        TOOLS[fn.__name__] = ToolDef(fn, fn.__name__, title, inspect.getdoc(fn) or title, read_only)
        return fn

    return deco


def signed_in() -> Caller:
    """The administrator the request's token was issued to."""
    who = CALLER.get()
    if who is None:  # only /mcp sets it, after the token is checked
        raise ToolError("not signed in")
    return who


def _refusal(e: HTTPException) -> ToolError:
    return ToolError(f"{e.detail} (HTTP {e.status_code})")


async def call[T](fn: Callable[[], T]) -> T:
    """Run a screen's function in a worker thread; its HTTPException becomes the tool's error."""
    try:
        return await anyio.to_thread.run_sync(fn)
    except HTTPException as e:
        raise _refusal(e) from e


async def call_async[T](fn: Callable[[], Awaitable[T]]) -> T:
    """The same for a screen's async function (an upload route)."""
    try:
        return await fn()
    except HTTPException as e:
        raise _refusal(e) from e
