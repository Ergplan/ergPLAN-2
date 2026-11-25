# Stage 13 — Sign-in with a username and password

Built on 25 Nov 2026 on the VM, from `stage-13-start` (ca8e704). Brief of 25 Nov 2026, part B.

**Status: built, tagged `stage-13-built`.** Password sign-in is **switched off on the live server**: `ERGPLAN_ADMIN_USER` and `ERGPLAN_ADMIN_PASSWORD` are not in `.env`, and by the person's reading no password is invented. The reviewer could not run (no OpenAI key).

## What was built

- **One administrator account** (`platform/admin.py`): username and password from the server's environment; a row in `users` with role analyst and a salted scrypt hash, written at the API's start and replaced only when the configured password changes (which also revokes that account's sessions). `POST /auth/login` takes username and password and is for this account only.
- **Switched off when not configured**: with either variable empty, or in production over plain http, `POST /auth/login` answers 403 in words and the sign-in page shows only the emailed-link form.
- **Lockout by address** (table `signin_attempts`, migration 0014): the fifth wrong attempt in fifteen minutes locks the address for fifteen minutes, whatever the sixth carries. Every attempt is a row and a log line (`ergplan.signin`), never with the password; the username is recorded only when it is the administrator's.
- **Cookie**: `Secure` (on https), `HttpOnly`, `SameSite=Lax`.
- **Weak-password warning**: shorter than twelve characters, or the username with a suffix: a warning in the log at start-up and a banner on every analyst screen. The server still starts.
- **Rate limits** (`platform/ratelimit.py`): 20 a minute per address on each of `/auth/login`, `/auth/request`, `/auth/verify`; 60 uploads in ten minutes per account and address on `POST /bills` and `POST /bill-reads`. 429 with words and `Retry-After`.
- **`tools/dev/secrets-check.py`**: fails if the administrator password from `.env` is in any tracked file, the watcher's log or its status file; prints only the file's name.
- **Retired**: the per-person passwords of 1 Nov 2026 (`POST /auth/password`, `POST /auth/users`, the web `/account` page).
- The emailed link stays for clients; a share link opens only its own plan.

## Checked (done when)

| Check | Result |
| --- | --- |
| The administrator signs in on the VM's address and reaches the analyst screens | **Not met on the live server**: the two variables are not in `.env`. Met in tests with a throwaway value (`tests/platform/test_admin.py`) |
| A sixth wrong attempt from one address is refused for fifteen minutes | Test: refused even with the right password, still locked at 14 minutes, free after 15; per address; a forged `X-Forwarded-For` does not escape it |
| Each attempt is in the log without the password | Test |
| No plaintext password in the database, a log or the repository | Test (every table, every log line, the settings' printed form) and `secrets-check.py` |
| The weak-password warning shows in the log and on screen | Test for the log and for `GET /auth/me`; the banner's logic by vitest; no browser test (known gap) |
| A client signs in by emailed link; a share link opens only its plan | Tests |
| Bill upload and sign-in are rate-limited | Tests |
| Watcher green | Green at 21:14 UTC on 25 Nov on the code committed |
| `pytest -m slow` and the last CI run | The two 15-minute sites, the mix journey and both browser journeys passed on the final code (the Uttar Pradesh full test was last run on Stage 11's code; the stage does not touch what it exercises); the real-bill tests skip (no real bills on the VM). CI on `f6b8c03` passed; the run on this stage's commit is read at the start of Stage 14 |
| Reviewer clean | Not run: no OpenAI key (`docs/questions-for-review.md`, 1) |

## What was skipped, and why

- Live check after the redeploy: every `vm-check.py` check passed and `GET /api/auth/me` reports `password_sign_in: false`, as intended with the variables absent.
- Nothing MCP-specific: Stage 16 builds on this account. The session from `/auth/login` already works as a bearer token (tested).

## Deviations from the master prompt

- The older per-person password routes were removed rather than kept beside the administrator's (decision of 25 Nov 2026).
- Their stored hashes were not deleted: it cannot be undone (left undone, questions 17).
- Tagged `stage-13-built`, not `-done`, while the reviewer cannot run.

## Existing tests changed

- `tests/platform/test_auth.py`: `/auth/me` now carries `password_sign_in` and `warnings` in place of `has_password`; the two tests of per-person passwords are replaced by one asserting those routes are gone and an old hash opens nothing.
- `tests/conftest.py`: the new table is truncated between tests and the rate-limit counts are reset.

## Reviewer findings

Not run.

## Open questions

`docs/questions-for-review.md`, 16 to 18.

## Cost

- Application model calls: none. Reviewer: none. Builder: Claude Code on the subscription, one subagent.
