# Stage 17 — The savings logic and the sizing tested on several states

Built on 26 Nov 2026 on the VM, from `stage-17-start` (4ee83e8), by two workers in parallel (Maharashtra and Karnataka; Gujarat, Punjab and Telangana); I reviewed and merged them (`c8f8548`, `4b05c02`, `e7e34c6`). Brief of 25 Nov 2026, part F.

**Status: built, tagged `stage-17-built`.** The findings, state by state, are in **`docs/stage-reports/multi-state-test.md`**. The reviewer could not run (no OpenAI key).

## What was built

- Five slow tests, one per state, each asserting what `tests/e2e/test_up_assumed.py` explores for Uttar Pradesh: `tests/e2e/test_mh_assumed.py` (MSEDCL HT I (A), 22 kV, 2,000 kVA, single shift), `test_ka_assumed.py` (BESCOM HT-2(a), 11 kV, 3,000 kVA, continuous process), `test_gj_assumed.py` (MGVCL HTP-I, 33 kV, 4,000 kVA, continuous process), `test_pb_assumed.py` (PSPCL Large Supply, 11 kV, 2,000 kVA, single shift), `test_ts_assumed.py` (TGSPDCL HT-I(A), 33 kV, 3,000 kVA, two shifts, against the approved rules). Three load shapes across the five.
- Each: twelve bills worked by hand in the test from the state's own order, with the document, page and table beside each constant; bill replay against them; today's cost; landed cost for solar and wind on every route the state allows; settlement after banking with the energy balance closed by hand; an optimizer run with five components hourly and at 15 minutes; the grid bill recomputed from the dispatch file; replay identical; the month's rule book.
- Each state's hand-worked landed cost in `tests/e2e/landed_hand/<state>.yaml`, which the Stage 19 calculator test reads.
- Nine defects fixed in the app, each with a test (the table in the multi-state report): a wheeling line alone taken for open access; the power factor worked from kWh/kVAh when none is printed (today's demand was 3 to 6% high on such bills, Uttar Pradesh included); ToD set in rupees read in settlement categories and in bill replay; slabbed demand rates; SLDC and RLDC per-unit charges missing from the landed cost in eight states; the banking charge grossing up drawal charges; the dispatch file's rounding.
- Nothing was approved; no rule was changed. Each rule missing or doubtful is listed with its page for the analyst (questions 32).

## Checked (done when)

| Check | Result |
| --- | --- |
| Each of the six states (and any Stage 14 made ready) has its test in the slow suite, passing, or with each failing assertion's defect fixed or listed as a rule gap | Uttar Pradesh (`test_up_assumed.py`, from Stage 11) and the five new tests pass. Where a listed gap makes the app differ from the order, the test asserts the app's figure with the gap named and works the order's figure beside it. The Stage 14 drafts were not tested: a draft cannot reach a client, and the brief's "through the sandbox" for states Stage 14 made ready is left for the second round |
| `docs/stage-reports/multi-state-test.md` is written | Yes |
| Watcher green | Green on the final merge (`e7e34c6`; the pass began at 13:51 UTC, after it) |
| `pytest -m slow` and the last CI run | The whole slow suite passed on the final tree (26 Nov: the six state tests, the Uttar Pradesh full test and transition plan, the MCP end-to-end, the mix journey; the Stage 6 to 8 end-to-end tests skip on the VM, which holds no real bills); the two browser journeys passed (135 s); CI green on `2d4db05` |
| Reviewer clean | Not run: no OpenAI key |

## Solver time and gap

| State | Hourly | 15-minute |
| --- | --- | --- |
| Maharashtra | 74.7 s, optimal, gap 0 | 84.4 s, optimal, gap 0 |
| Karnataka | 33.9 s, optimal, gap 0 | 44.8 s, optimal, gap 0 |
| Gujarat | 134–176 s, optimal, gap 0 (once, under load, the 300 s production limit ended with no feasible solution) | 217 s, optimal, gap 0 |
| Punjab | 28 s, optimal, gap 0 | 59 s, optimal, gap 0 |
| Telangana | 57 s, optimal, gap 0 | 80 s, optimal, gap 0 |

## Deviations from the master prompt

- The state tests raise the optimizer's time limit to 600 s so that a result does not depend on the machine's load (decisions, 26 Nov).

## Existing tests changed

- `tests/intake/test_bills_and_site.py::test_from_bills_then_build` and `tests/platform/test_api_intake.py` assert the profile's peak at MD × kWh/kVAh (was MD × 1); three `tests/platform/test_api_questions.py` expectations say the power factor is known from the bills.
- `tests/e2e/test_up_transition.py` (Stage 15): today, the rooftop's share and the phases' figures move with the power-factor fix (today ₹191,476,310.52, was .78; rooftop share 32.30%, was 31.88; wind 4,585 kW, was 4,610; phase 2 ₹13.89 cr, was 13.96; share 80.41%, was 80.16). The test asserted app outputs, not hand-worked figures, and was corrected to the new ones (commit `2abf7f7`).

## Reviewer findings

Not run: no OpenAI key (`docs/questions-for-review.md`, 1).

## Open questions

`docs/questions-for-review.md`, 32 and 33.

## Cost

- Application model calls: none. Reviewer: none. Builder: Claude Code on the subscription; two worker subagents and my review and merges.
