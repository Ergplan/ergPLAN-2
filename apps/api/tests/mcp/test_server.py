"""Stage 16: the MCP server's shape. It computes nothing and asks no model (it imports neither the
model providers nor an engine), and a new tool is one new file in ergplan/mcp/tools/."""

from __future__ import annotations

import sys
from pathlib import Path

import anyio
import pytest

from ergplan.mcp import registry, server, tools
from tests.test_architecture import PKG, _imports

FORBIDDEN = ("providers", "voice", "explain", "audit_chat", "engine", "settlement", "optimizer.model")


def test_the_mcp_package_imports_no_model_and_no_engine() -> None:
    offenders = []
    for f in (PKG / "mcp").rglob("*.py"):
        for i in _imports(f):
            if any(part in i for part in FORBIDDEN) or i.split(".")[0] in ("openai", "anthropic"):
                offenders.append(f"{f.relative_to(PKG)}: {i}")
    assert offenders == []


def test_a_new_tool_is_one_new_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "extra_for_the_test.py").write_text(
        "from ergplan.mcp.registry import signed_in, tool\n"
        "from pydantic import BaseModel\n\n\n"
        "class Out(BaseModel):\n    words: str\n\n\n"
        "@tool(title='A test tool', read_only=True)\n"
        "async def tool_added_by_one_file(name: str) -> Out:\n"
        "    '''Says hello.'''\n"
        "    return Out(words=f'hello {name}')\n"
    )
    monkeypatch.setattr(tools, "__path__", [*tools.__path__, str(tmp_path)])
    server.server.cache_clear()
    try:
        names = [t.name for t in anyio.run(server.server().list_tools)]
        assert "tool_added_by_one_file" in names and "read_bill" in names
    finally:
        monkeypatch.undo()
        registry.TOOLS.pop("tool_added_by_one_file", None)
        sys.modules.pop("ergplan.mcp.tools.extra_for_the_test", None)
        server.server.cache_clear()
    assert "tool_added_by_one_file" not in [t.name for t in anyio.run(server.server().list_tools)]


def test_two_tools_may_not_share_a_name() -> None:
    with pytest.raises(ValueError, match="two MCP tools"):
        registry.tool(title="again")(registry.TOOLS["read_bill"].fn)
