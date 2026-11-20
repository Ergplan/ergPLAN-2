# ergPLAN 2

jouleWise | ergPLAN 2 replaces costly grid power with cheaper green power for industrial consumers in India. Drop an electricity bill: billx reads it, and ergPLAN 2 shows the state, the DISCOM, the tariff category, the time-of-day slots and the energy and fixed charges. Give targets, costs and CUF for rooftop solar, open-access solar and wind, a battery behind the meter and a battery at the open-access plant. The plan is drawn as an isometric view of the site and the plant; the optimizer (Pyomo + HiGHS) sizes each component at least cost over a year; the result is settled again under the state's banking rules and projected over 25 years, each figure with its source.

It merges two applications: the flow and engines of the first ergPLAN (`Ergplan/Enterprise-RE-Planner`) and the settlement logic and State Regulation Master of erg_plan 2.0. See Stage 9 in `docs/MASTER-PROMPT.md` and the entry of 20 Nov 2026 in `docs/decisions.md`.

- Settlement: `apps/api/ergplan/settlement/cycle.py`, held to the original erg_plan 2.0 engine by `apps/api/tests/settlement/test_cycle_vs_erg2.py`.
- Regulation data: thirteen states from the State Regulation Master (`packages/rulebooks/register/`, imported by `tools/dev/import-register.py`) beside the states cited to stored order pages. Register values are shown as "register", not as cited pages.
- Remote access: `tools/dev/remote.sh on` on the server gives an https address through an outbound tunnel; sign-in is still required.

## How this repository is built

The build is driven stage by stage by Claude Code from one document:

- `docs/MASTER-PROMPT.md` — operating rules, invariants, locked decisions, folder layout, the nine stage prompts, reference data and the definition of done.
- `CLAUDE.md` — sections 2–5 of the master prompt, verbatim, read in every session (`tools/dev/sync-claude-md.sh` regenerates it).
- `docs/development-plan.md` — the approved plan this prompt implements.

One stage per session, on the build machine inside `tmux`:

```
claude
> Run Stage 0 of docs/MASTER-PROMPT.md
```

Read the stage report in `docs/stage-reports/`, spot-check one invariant by hand, answer the open questions numbered in one message, then start the next stage.

## Layout

See section 5.1 of `docs/MASTER-PROMPT.md`. In short: `apps/web` (Next.js, ergOS Harmony), `apps/api` (FastAPI engines, Pyomo + HiGHS optimizer), `packages/rulebooks` (JSON Schemas, exported state rulebooks, golden tests), `packages/profiles` (load shapes, CUF curves), `docker/`, `tools/`, `docs/`.

## Rulebook schemas

`packages/rulebooks/schemas/` holds the seven rule families (`tod_slots`, `tariff_schedule`, `oa_charge`, `settlement`, `regulatory_limit`, `ists_waiver`, `net_metering`) and `source_ref.schema.json`. `packages/rulebooks/golden/up/example-settlement.yaml` shows a rule row as an analyst sees it. Values in that file are illustrative, not a state's actual rule.

## Run it on the Mac mini

```bash
git clone https://github.com/Ergplan/Enterprise-RE-Planner.git && cd Enterprise-RE-Planner
cp .env.example .env                       # add OPENAI_API_KEY and BILLX_TOKEN
docker compose -f docker/compose.yaml up --build   # billx builds from billx/ in this repo
# register the regulation corpus (PDFs from ~/Downloads/India_DISCOM_Tariff_Orders_Final_2026)
mkdir -p data/corpus && cp -R ~/Downloads/India_DISCOM_Tariff_Orders_Final_2026 data/corpus/
docker compose -f docker/compose.yaml exec api python -m ergplan.documents.corpus --root /data/corpus/India_DISCOM_Tariff_Orders_Final_2026
```

Web http://localhost:3000 · API http://localhost:8000/docs.

Without Docker: Postgres 16 on :5432 with role/db `ergplan`, then `cd apps/api && python3.12 -m venv .venv && .venv/bin/pip install -e ".[dev]" && .venv/bin/playwright install chromium && .venv/bin/alembic upgrade head && .venv/bin/uvicorn ergplan.platform.app:app`, the run worker `.venv/bin/python -m ergplan.platform.worker`, and `cd apps/web && pnpm install && pnpm dev`.

Checks: `python3 tools/watch/watch.py` (watch) or `--once`. Review: `tools/review/run.sh N`. 15-minute site solves: `cd apps/api && .venv/bin/pytest -m slow`. Browser end-to-end on real bills: `tools/dev/e2e-journey.sh`.

## Status

Stages 0–6 done; each has a report in `docs/stage-reports/`. The journey runs from a dropped bill to a shared live plan (`/p/{token}`) with voice what-if; every client-visible number cites the order page it comes from.
