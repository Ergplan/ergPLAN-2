# Stage 19 — A landed-cost calculator for every state

Built on 26 Nov 2026 on the VM, from `stage-19-start` (2260b4f), by one worker in its own worktree; I reviewed and merged it (`b2b41b6`). Brief of 25 Nov 2026, part H.

**Status: built, tagged `stage-19-built`.** The calculator is **not open to visitors on the live server**: opening the app without sign-in is one of the brief's exceptions, so it sits behind `ERGPLAN_PUBLIC_CALCULATOR`, off (questions 34). The reviewer could not run (no OpenAI key).

## What was built

- `report/calculator.py`, the service: it calls `platform.routers.landed.landed()` once per route on a consumer described by its facts (no connection, no bill), takes the per-slot rows from the state rule book's route table (`report.statebook._route_table`: banking beside the landed cost, saving = grid tariff − landed cost), and the Stage 18 exchange verdict from `platform.exchange_plan.verdict_for` on approved rules. It decides only two things itself: whether a stated minimum contract demand bars open access, and the exchange verdict's effect; and it takes an hours-weighted grid average for the all-states table.
- `platform/routers/calculator.py`: `GET /calculator/options`, `POST /calculator` (one consumer), `POST /calculator/states` and `POST /calculator/states.csv` (every state at once), `GET /calculator/statebooks/{id}/file` (approved rule books only). Every request runs in a read-only transaction and is rate-limited per address (30 a minute).
- Inputs: state, DISCOM, consumer category, supply voltage, contract demand; the PPA tariff, or the plant's capital cost and CUF, and the equity share; the IEX market and month. A single consumer answers in about 1 s; the all-states table (40 DISCOMs in 17 states) in about 8 s.
- Routes side by side for solar and for wind: captive own plant, captive with 26% equity, third party, and the exchange; for each, the build-up line by line, the landed cost by ToD slot, the grid tariff beside it, the saving. Every figure opens its source through the existing `Fig` component; an unstated line shows "not yet sourced"; a route the rules bar says why.
- The exchange route shows the state's verdict. Today every state's is "not yet sourced", so a client or visitor sees the verdict and its reasons and not the exchange's landed cost; an analyst (and the MCP administrator) sees the figure marked as the analyst's figure (questions 34).
- MCP tool `landed_cost_calculator`, one file (`mcp/tools/landed_cost_calculator.py`), calling the same `run()` as the screen.
- Web: `/calculator` (`app/calculator/`, `components/calculator/`, `lib/calculator.ts`), a navigation link.

## Uttar Pradesh, worked by hand (`tests/e2e/landed_hand/up.yaml`)

From the stored order pages: transmission ₹0.3075 (p. 377), wheeling ₹0.9318 (p. 379), losses 3.18% and 3% (p. 384), cross-subsidy surcharge ₹0.49 (p. 386); the captive exemption from the surcharge (CRE Regulations p. 13) and the 8% banking charge (p. 19). All slots:

| Route | Solar | Wind |
| --- | ---: | ---: |
| Third party | 5.1104 | 5.6428 |
| Captive, 26% equity | 5.3594 (banking 0.4018) | 5.9983 (banking 0.4481) |
| Captive, own plant | 4.5490 (banking 0.3956) | 5.1621 (banking 0.4489) |

The calculator matched every line and its citation.

## The other states, against Stage 17's hand-worked files

`tests/e2e/test_calculator_hand.py` reads every `tests/e2e/landed_hand/<state>.yaml` (Uttar Pradesh, Maharashtra, Karnataka, Gujarat, Punjab, Telangana) and compares the calculator's answer line by line: **all six equal.** The figures are in `docs/stage-reports/multi-state-test.md`. Making the files agree found one error in the hand figures (Maharashtra's and Karnataka's third-party banking charge had grossed up the surcharge; the engine's Stage 17 fix is right) and nothing in the calculator.


## Checked (done when)

| Check | Result |
| --- | --- |
| For each state of Stage 17, the calculator's figures equal the hand-worked landed cost of that state's test | `tests/e2e/test_calculator_hand.py`: Uttar Pradesh, Maharashtra, Karnataka, Gujarat, Punjab, Telangana, line by line |
| Every figure on the screen carries a source_ref or is marked assumed or not sourced; field-trace complete | `test_every_figure_carries_a_source_ref_or_is_assumed_or_not_sourced`, `test_three_routes_…`; `field-trace --check` OK (20 new rows) |
| A visitor's use stores nothing | `test_switched_on_a_visitor_may_use_it_and_it_stores_nothing` (row counts of every table before and after), and the read-only transaction |
| The all-states table downloads as CSV | `test_every_state_at_once_and_as_csv` (40 DISCOMs, 17 states, every figure with the pages it opens) |
| The MCP tool returns the same figures as the screen | `tests/mcp/test_landed_cost_calculator.py` (identical JSON, the same refusal, listed read-only) |
| Watcher green | Green on the final merge (`e7e34c6`; the pass began at 13:51 UTC, after it) |
| `pytest -m slow` and the last CI run | The whole slow suite passed on the final tree (26 Nov: the six state tests, the Uttar Pradesh full test and transition plan, the MCP end-to-end, the mix journey; the Stage 6 to 8 end-to-end tests skip on the VM, which holds no real bills); the two browser journeys passed (135 s); CI green on `2d4db05` |
| Reviewer clean | Not run: no OpenAI key |

Also tested: off by default the calculator needs sign-in; the rate limit; a draft changes nothing; the exchange follows the state's verdict (not yet sourced, the analyst's figure, a fixture rule that offers, a Rajasthan fixture judged not practical); a minimum contract demand bars open access with the rule (fixture); an approved rule book downloads, a draft does not.

## What was skipped, and why

- Saving a calculation for a signed-in user: nothing is ever stored (`stored: false` in every answer).
- Duty and the additional surcharge for a reference consumer are "not yet sourced", as in the rule book.
- No approved rule in any state states a minimum contract demand for open access, so the bar works today only on test fixtures.
- The screen was checked by typecheck and unit tests, not in a browser.

## Reviewer findings

Not run: no OpenAI key (`docs/questions-for-review.md`, 1).

## Open questions

`docs/questions-for-review.md`, 34.

## Cost

- Application model calls: none. Reviewer: none. Builder: Claude Code on the subscription; one worker subagent and my review.
