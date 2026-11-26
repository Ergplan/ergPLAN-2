"""The ergPLAN 2 MCP server (Stage 16): a bill dropped into Claude gets a savings analysis and a
sizing after a few questions.

- `server`: the MCP endpoint `/mcp` (streamable HTTP, the official SDK's transport, stateless, JSON
  responses) and the server instructions Claude reads.
- `tools/`: one module per group of tools; every module in the package is loaded, so a new tool is
  one new file (see `registry`). Each tool calls the function the screen's route calls and computes
  nothing of its own: billx is the only bill reader, no model decides a number, and every figure
  comes back as the SourcedValue the screen gets.
- `oauth`: OAuth 2.1 for the connector (metadata, dynamic registration, authorization code with PKCE,
  refresh, revocation), backed by the Stage 13 administrator account (`platform.admin`).
- `uploads`: one-time upload links, for a file Claude cannot hand to a tool.
"""
