# Stage 16 — The ergPLAN 2 MCP server

Built on 26 Nov 2026 on the VM, from `stage-16-start` (4ee83e8), by one worker in its own worktree; I reviewed and merged it (`2260b4f`) and tightened the redirect check (below). Brief of 25 Nov 2026, part E.

**Status: built, tagged `stage-16-built`.** Not `-done`: the connector has not been tried in claude.ai, because password sign-in is switched off on the live server until the administrator's two variables are in `.env` (questions 16, 31), and the reviewer could not run (no OpenAI key).

**The address to add as a connector: `https://ergplan.joulewise.tech/mcp`.** In Claude Code: `claude mcp add --transport http ergplan https://ergplan.joulewise.tech/mcp`. First type `ERGPLAN_ADMIN_USER` and `ERGPLAN_ADMIN_PASSWORD` into `/work/ergPLAN-2/.env` and restart the api.

## What was built

- **`/mcp`**, streamable HTTP, served by the API (`ergplan/mcp/server.py`) through the official MCP SDK's session manager (`mcp==1.30.0`; 2.x needs `httpx2`, a second HTTP client), stateless, JSON responses.
- **Fourteen typed tools**, each calling the screen's own route function (`mcp/registry.call()`), so a tool computes nothing and returns the same SourcedValues the screen gets:

  | Group | Tools |
  | --- | --- |
  | Bills | `read_bill` (to billx; base64, a one-time upload link, or a reading id to poll), `confirm_field`, `build_site` (confirmed bills to a site and connection; added) |
  | Answers | `list_questions`, `answer` (and withdraw) |
  | Profiles | `add_generation_profile` (CSV text, or an upload link) |
  | Cost | `todays_cost`, `landed_cost` |
  | Sizing | `run_sizing` (queued for the worker; "running" with the run ids after 8 s), `sizing_status` (added), `get_plan` |
  | Plan | `transition_plan`, `state_rule_book` (approved issues only), `share_link` |

- **Server instructions**: ask the open questions one or two at a time before sizing; state numbers only from tool results; say which figures are assumed or client-stated; never present a run that ended on its time limit as the optimum; say where something is not yet sourced.
- **Files**: MCP cannot pass an attachment to a tool, so `read_bill` and `add_generation_profile` return a one-time upload link (`/mcp/upload/{secret}`: 15 minutes, one file, bound to the administrator, rate-limited like bill upload; a refused file does not spend it). In claude.ai the person opens the link and chooses the PDF; in Claude Code, Claude can upload a local file with curl. Read from the protocol and SDK documentation; not tried live.
- **Access** (`mcp/oauth.py`): OAuth 2.1 backed by the Stage 13 administrator account — authorization-server and protected-resource metadata, dynamic client registration, authorize with the administrator's password (the same check, lockout by address and log as `/auth/login`) and PKCE S256, token, refresh, revoke. Codes last 5 minutes and work once; access tokens 1 hour; refresh tokens 30 days, rotating; reusing a spent code or refresh token ends the grant. Every secret is stored as its SHA-256 only. A token stops working when password sign-in is off, the administrator changes, or the password changes. Every `/mcp` refusal is a 401 with `WWW-Authenticate: Bearer resource_metadata=…`.
- **Tightened in review**: the worker's registration accepted an https redirect to any host. With open registration, anyone could register a client whose redirect is their own address and send the administrator a sign-in link; the grant would go to them. Redirects over https are now allowed only to `ERGPLAN_MCP_REDIRECT_HOSTS` (claude.ai and claude.com by default), and http only to localhost (test added).
- Tables `mcp_clients`, `mcp_tokens`, `mcp_uploads` (migration 0020). Caddy sends `/mcp`, `/oauth/*` and `/.well-known/oauth-*` to the API (validated with the deployed Caddy image).

## The assumed Uttar Pradesh site through the MCP (`tests/e2e/test_mcp_up.py`, slow, 180 s)

The twelve assumed PVVNL HV-2 bills of `tests/e2e/test_up_assumed.py` go in through `read_bill` and are confirmed; `build_site` (33 kV); `list_questions`; `answer` roof 4,000 m²; `add_generation_profile` with a measured solar CSV; `todays_cost`; `landed_cost`; `run_sizing` with the UP test's five captive components at hourly blocks; `sizing_status`. The same inputs through the screens' routes on a clean database give the same today's cost, landed-cost page and run summary to the paisa.

| Figure | Value |
| --- | --- |
| Today's cost | ₹19.15 cr a year (₹191,476,311), 22,606,226 kWh, ₹8.47/kWh |
| Cost with the plan | ₹14.10 cr (₹140,998,000.54) |
| Saving | ₹5.05 cr a year |
| Capital | ₹39.86 cr |
| Sizes | rooftop 400 kW (bounded by the roof answer), captive solar 5,471 kW, captive wind 2,248 kW, batteries 0 |
| Grid energy | 5,402,966 kWh; 58,592 kWh of banked energy lapses |

`tests/e2e/test_up_assumed.py` asserts no figures and takes no solar profile, so "equals its figures for the same inputs" is read as: equal to the screens fed its inputs plus the same answer and profile.

## Checked (done when)

| Check | Result |
| --- | --- |
| With the connector added in claude.ai, one assumed UP bill goes in, the questions are asked, a solar profile is supplied, and the answer equals the UP test's figures | Through the MCP endpoint with the SDK's own client, yes (above). In claude.ai: not tried, password sign-in is off on the live server |
| Every tool has a test | `tests/mcp/test_tools.py` (11), each tool's answer equal to the screen route's JSON for the same request; `test_the_server_lists_every_tool_typed_with_its_instructions` |
| A request without the sign-in is refused | `tests/mcp/test_oauth.py::test_a_request_without_sign_in_is_refused` (no token, a made-up token, the web session's token: 401); `test_with_password_sign_in_switched_off_nobody_gets_in`; `test_in_production_the_cookie_guard_lets_the_token_check_answer` |
| OAuth end to end | `test_the_oauth_flow_end_to_end_with_the_sdk_client` (the SDK's `OAuthClientProvider`: discovery, registration, authorize with a password generated in the test, PKCE, tools; then refresh, revoke, and no token in the database) and five more |
| The upload link works once | `tests/mcp/test_uploads.py` |
| The person is told the address | Above, and questions 31 |
| Watcher green | Green on the merge (`2260b4f`, pass ended 12:05 UTC); the full fast suite also passed after the redirect fix (`c2a43c0`) |
| `pytest -m slow` and the last CI run | `tests/e2e/test_mcp_up.py` passed on the merged tree; the whole slow suite passed again with Stages 17 and 19 merged; CI: CI_RESULT |
| Reviewer clean | Not run: no OpenAI key |

## On the live server

Redeployed at `0367971` (migration 0020), Caddy restarted so it reads the new Caddyfile. Without a token, `POST https://ergplan.joulewise.tech/mcp` answers 401 with `WWW-Authenticate: Bearer resource_metadata="https://ergplan.joulewise.tech/.well-known/oauth-protected-resource/mcp"`; the metadata is served; registering an `https://evil.example` redirect is refused (400). `tools/dev/vm-check.py`: every check passed. Password sign-in is off there, so nobody can sign in yet.

## Deviations from the master prompt

- Two tools beyond the brief's twelve: `build_site` (the brief has no step from confirmed bills to a site) and `sizing_status` (the status tool `run_sizing` names).

## Existing tests changed

- `tests/platform/test_auth.py::test_every_public_template_is_a_real_route` also reads the MCP routers' paths (kept out of the OpenAPI schema, which was the test's only source).
- `tests/conftest.py` empties the three MCP tables and `bill_reads` (never emptied before; a leftover queued reading broke a later test).

## Reviewer findings

Not run: no OpenAI key (`docs/questions-for-review.md`, 1). My review found the open-redirect registration above and fixed it.

## Open questions

`docs/questions-for-review.md`, 31.

## Cost

- Application model calls: none. Reviewer: none. Builder: Claude Code on the subscription; one worker subagent and my review.
