# Stage 15 — Sizing from the user's own profiles and answers

Built on 26 Nov 2026 on the VM, from `stage-15-start` (c65e72f). Brief of 25 Nov 2026, part D. Three workers built the three bullets in parallel in separate worktrees; I reviewed each branch and merged them (`2dbe509`, `4f78e38`, then `4ee83e8`), at the same time as Stage 18 (`docs/stage-reports/stage-18.md`), so the stage diff `stage-15-start..stage-15-built` includes Stage 18's commits.

**Status: built, tagged `stage-15-built`.** The reviewer could not run (no OpenAI key).

## What was built

### Generation profiles supplied by the user

- `intake/generation_profiles.py`: reads a solar or wind CSV, hourly (8,760 or 8,784 values) or 15-minute (35,040 or 35,136), for a year starting at midnight on any day. One generation column whose header names its unit (kW, MW, kWh, MWh, CUF or pu from 0 to 1, CUF % from 0 to 100); kW, MW, kWh and MWh need a basis (`per_mw`, or `capacity` with `capacity_mw`). A timestamp column (IST), or `first_day` when there is none.
- Refused with the reason in plain words: a missing period, a blank, a word, NaN or a negative value, a duplicate or out-of-order timestamp, a wrong length, a unit not stated or two units, a block above capacity, an annual CUF outside 10 to 35% (solar) or 10 to 55% (wind), solar above 1% of capacity between 20:00 and 04:00. Nothing is interpolated. Example: "the file implies an annual CUF of 90.00%; a wind plant is between 10% and 55% (check the unit, the capacity, and whether the values are per MW)".
- Stored (table `generation_profiles`, migration 0016) per kW of capacity with the file's sha256 and provenance `measured` or `stated`; the CUF it implies, for the year and by month, is shown beside the library curve's.
- Runs, settlement and landed cost generate with the supplied profile in place of the library curve; a run pins the profile's id and sha256 (`pins.generation_profiles`) and a replay uses the pinned one even after a newer upload or a withdrawal.
- Endpoints: `POST` and `GET /sites/{id}/generation-profiles`, `GET /generation-profiles/{id}`, `POST /generation-profiles/{id}/withdraw`. Screen: `/plan/generation`.

### The questions the bills do not answer

- `intake/questions.py`: nine questions, each with its wording, unit, why it is asked and what it changes: shifts and their hours, the weekly off, seasonal shutdowns, large loads and when they run, the power factor, planned expansion, the roof, the land, the contract demand intended. A question is not asked when the inputs or every bill already say it.
- `intake/answers.py` is the only writer of `site_answers` (migration 0017): an answer is a `client_stated` value with who stated it and when; a change closes the previous row, a withdrawal records who withdrew it.
- How each answer is used:

  | Answer | Effect |
  | --- | --- |
  | Shifts | The working level in the stated hours of every working day; a night shift runs into the next day |
  | Weekly off | Off days at the base level |
  | Seasonal shutdowns | Those days at the base level, every year |
  | Large loads | Their kW in their hours on working days, then each ToD slot scaled back to the bill's energy |
  | Power factor | The peak (recorded kVA × PF) and the kVAh conversion in months whose bill prints none |
  | Planned expansion | Scales the year the sizing runs for; today's cost keeps the billed year |
  | Roof | Bounds the rooftop plant (10 m² per kWp) |
  | Contract demand intended | Replaces the bill's in the sizing |
  | Land | Stored and shown; it limits nothing (the optimizer has no ground-mounted plant at the site) |

  The synthesised profile still matches each bill's monthly kWh exactly, its ToD split and its recorded demand.
- Each answer makes a new load-profile version that records the answers, so a run pins them; old runs replay to the paisa. A measured 15-minute file replaces the synthesis: the four questions that only shape the synthesis are no longer asked, and the plan says the profile is measured.
- Endpoints: `GET /sites/{id}/questions`, `PUT` and `DELETE /sites/{id}/questions/{key}`. Screen: `/plan/questions`; the plan and the shared plan show "What you told us about the plant" as client-stated chips.
- Service functions for the Stage 16 MCP tools: `intake.questions.outstanding()`, `answer()`, `withdraw()`.

### The transition plan

- `report/transition.py` (pure) and `platform/transition.py` (the service; `transition_plan()` is the MCP entry), table `transition_plans` (migration 0019), five endpoints (`POST` and `GET /connections/{id}/transition-plan`, `GET /transition-plans/{id}`, analyst `POST /transition-plans/{id}/replay`, public `GET /p/{token}/transition-plan`), the section on the plan page and on the shared plan.
- From today's bill to a target (a renewable share, or the lowest cost), in phases the client can act on: rooftop, then open access, then storage. Each phase is an ordinary run restricted to its sources plus those of earlier phases (what an earlier phase built is kept as a minimum), re-settled by the judge and pinned, so the plan replays. Nothing is computed outside the engines; the page's only arithmetic is the differences between phases.
- Captive, group captive and third party are each sized; the plan takes the cheapest route whose settlement an approved rule covers and lists the others with the reason they were not taken.
- If the cheapest plan stops short of the target, a "most the rules allow" run is made and the binding limits are listed with their sources; a run that ended on its time limit is shown as not proven, with its gap.
- What must be signed or applied for is cited to a rule only where an approved rule row states the step (today only the day-ahead schedule of banking); every other step is labelled "general practice: no approved rule in the rule book states this step".

## The assumed Uttar Pradesh site, target 50% (`tests/e2e/test_up_transition.py`)

PVVNL HV-2, 5,000 kVA at 33 kV, twelve assumed bills, as of 1 Nov 2026, hourly. Today: ₹19.15 cr a year (₹191,476,310.78), 22,606,226 kWh, no renewable share.

| Phase | Built | Cost a year | Saving against today | Capital (cumulative) | Renewable share | Payback, IRR |
| --- | --- | --- | --- | --- | --- | --- |
| 1 Rooftop | 5,000 kW | ₹16.47 cr | ₹2.67 cr | ₹20.00 cr | 31.88% | 3.9 years, 26.81% |
| 2 Open access, captive | wind 4,610 kW | ₹13.96 cr | ₹5.19 cr | ₹51.34 cr | 80.16% | 4.5 years, 24.17% |
| 3 Storage | nothing (adds nothing) | ₹13.96 cr | — | — | 80.16% | — |

- Phase 1 is bound by the 75% contract-demand floor (the order's page 410) and by the rooftop's upper bound, which is an assumption because no roof area was stated (questions 29).
- Phase 2's rules: 15-minute set-off, monthly banking, the 8% banking charge in kind and lapse, each cited to the UP CRE Regulations 2024 (pages 16 and 19). Seven steps to apply for, one cited (the day-ahead schedule), six marked as general practice.
- Routes compared: captive ₹13.96 cr (taken); group captive ₹15.07 cr; third party ₹15.60 cr (not taken: no approved settlement rule covers it).
- The target of 50% is reached in phase 2. The plan took 143 s for five runs; every run replays identical. Measured separately on the same site, the most the rules allow is 100% at ₹16.58 cr a year with ₹85.87 cr capital (proven, 150 s).
- Every figure on the page is a SourcedValue whose order or regulation refs open `/documents/{id}#page=N`; the response's fields all have an owner in `docs/field-trace.md`.

## Checked (done when)

| Check | Result |
| --- | --- |
| A solar and a wind CSV, hourly and 15-minute, are accepted; a gap, a wrong length or an impossible capacity factor is refused with the reason | `tests/intake/test_generation_profiles.py` (16), `tests/platform/test_api_generation.py` (7) |
| A run with a supplied profile differs from the library-curve run, pins the sha256 and replays to the paisa | `test_a_run_uses_the_supplied_profiles_pins_them_and_replays`. On a synthetic 5 MVA MSEDCL site the saving is ₹6.95 cr with library curves and ₹7.50 cr with the supplied profiles (test CUFs: solar 25.54% against the library's 21%, wind 30.00% against 28%) |
| Each answer changes the synthesised profile in the stated way and appears on the report as client-stated | `tests/intake/test_profile_answers.py` (9), `tests/platform/test_api_questions.py`, `tests/e2e/test_stage15_answers.py` (load 40,150,000 kWh, ×1.2 under a +20% expansion; roof 4,000 m² → 400 kW, 2,000 m² → 200 kW; contract demand 5,000 → 6,000 kVA; four runs on profile versions 1, 2, 5, 7 all replay identical) |
| A measured file replaces the synthesis | `test_api_questions.py::test_a_measured_file_replaces_the_synthesis` |
| The transition plan for the assumed Uttar Pradesh site lists its phases with every figure sourced; field-trace complete | `tests/e2e/test_up_transition.py` (slow), `test_api_transition.py::test_every_figure_of_the_response_has_an_owner_in_the_field_trace`; `field-trace --check` OK |
| Watcher green | Green on the merged tree (`4ee83e8`; the pass began at 10:10 UTC, after the last merge) |
| `pytest -m slow` and the last CI run | The full fast suite and `pytest -m slow` passed on the merged tree (26 Nov; including the Uttar Pradesh full test, the Uttar Pradesh transition plan, the Stage 15 answers end-to-end and the mix journey; the Stage 6 to 8 end-to-end tests skip on the VM, which holds no real bills); CI green on `6534f23` |
| Reviewer clean | Not run: no OpenAI key |

## What was skipped, and why

- The uploaded generation CSV is not kept (the series and its sha256 are); there is no test on a real developer file (PVsyst or SCADA export), because none is in the repository.
- Land limits nothing: there is no ground-mounted plant at the site in the optimizer, and I did not invent one.
- The transition plan has no exchange phase: `report.transition.exchange_phase()` is the named place. Stage 18 built the exchange verdict, but no state has an approved exchange rule, so the phase would never appear today.
- Steps to apply for are assumptions where no rule family holds the procedure (open-access application, net-metering application, the captive tests).

## Deviations from the master prompt

- Stages 15 and 18 were built in parallel by workers (the brief: "subagents in parallel where the work divides"), so this stage's diff carries Stage 18's commits (questions 26).
- The run summary gains `sizes[].cuf` and `provenance.generation` only on runs that used a supplied profile, so runs stored before keep their digest and replay identical.

## Existing tests changed

None in this stage's three branches. The merges added `generation_profiles`, `site_answers` and `transition_plans` to the tables the tests empty (`tests/conftest.py`).

## Reviewer findings

Not run: no OpenAI key (`docs/questions-for-review.md`, 1).

## Open questions

`docs/questions-for-review.md`, 27 to 29.

## Cost

- Application model calls: none. Reviewer: none. Builder: Claude Code on the subscription; three worker subagents and my review and merges.
