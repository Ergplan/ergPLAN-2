# Stage 18 — IEX: data in, and exchange purchase suggested only where it can really be scheduled

Built on 26 Nov 2026 on the VM, from `stage-18-start` (c65e72f), in parallel with Stage 15 by one worker in its own worktree; I reviewed and merged the branch (`fa5edae`). Brief of 25 Nov 2026, part G.

**Status: built, tagged `stage-18-built`.** The headline: **no stored document in any state says whether an open-access consumer may buy on the exchange, or how the purchase must be scheduled, and there is no Rajasthan document at all.** So every state's verdict is "not yet sourced" and no plan offers an exchange source until an analyst stores the regulations, completes a state's draft and approves it. The verdicts, the uniform schedule and the deviation pricing are built and tested on fixture rules. The reviewer could not run (no OpenAI key).

## What was built

### Prices in

- Source: `Ergplan/tenderos2` at commit `23d0afc22ff1017f30e1d97233f3e3532b17fbfb`, cloned read-only. I read the builder (`tools/build_iex_prices.py`) and both files before writing. IEX's own 15-minute exports are not in that repository, only the built JSON.
- Copied into `packages/prices/iex/tenderos2/` with `ORIGIN.yaml` (repository, branch, commit, path and SHA-256 of each file and of the builder): `iex_15min_prices.json` (the series loaded) and `iex_hourly_prices.json` (the cross-check). Never imported or fetched at run time; nothing is fetched from IEX's website.
- What it holds: GDAM, DAM and RTM, 35,040 blocks each, in Rs/MWh; GDAM also carries cleared volume. The file is one 365-day calendar year built from data of 2 Nov 2025 to 18 Nov 2026: 1 Jan to 18 Nov are 2026 days, 19 Nov to 31 Dec are 2025 days. The loader rebuilds each real date and checks it against the file's own count (2025: 43 days, 2026: 322). Stored range: 19 Nov 2025 to 18 Nov 2026.
- `intake/iex_vintage.py` loads one vintage per market into `price_series`; each vintage names the repository, the commit, the file and its sha256, both files' hashes, the builder and the cross-check result. `python -m ergplan.intake.iex_vintage` (idempotent; `--check` writes nothing).
- **Cross-check.** The hourly mean of the 15-minute series is not exactly the hourly file, and cannot be: the builder rounds blocks to Rs 0.1 and hours to Rs 1, both from unrounded data. Over 8,760 hours per market the largest difference is Rs 0.525/MWh (GDAM 17 hours above Rs 0.5, DAM 18, RTM 14; none above Rs 0.55). The loader refuses a file with any hour above Rs 0.55. One RTM block was filled by the builder from the nearest day (`blocksFilled: 1`); the file does not say which, and each vintage's note says so.
- Rs/MWh is stored as printed and converted to INR/kWh only by `core.units.convert`; the old division by 1,000 in `runs._iex_prices` is gone (it now reads through `intake/price_series.py`).
- An analyst's upload is kept for newer months: it neither overlaps nor supersedes the copied year, and each month is read from the vintage whose data for that month is newest. A run pins every vintage it read (`pins.price_vintages_by_segment_all`); runs with the older single pin still replay.

### Feasibility before price

- Rule family `exchange_purchase` (`packages/rulebooks/schemas/exchange_purchase.schema.json`): thirteen fields — permitted; eligibility by contract demand, voltage and metering; the SLDC's no-objection or standing clearance and its validity; the schedule (uniform over the day, or by block); day-ahead notice and revision; whether the DISCOM contract demand must be reduced; standby charges; deviation settlement on under- and over-drawal; cross-subsidy and additional surcharges on exchange power; banking or time-shifting; minimum period or lock-in. Each is `stated`, with a page citation, or `not_stated`, with the regulation to obtain (rule 18). An optional list of cited barriers.
- Drafts for fifteen states (`packages/rulebooks/proposals/exchange_purchase/`, loaded by `python -m ergplan.regreader.exchange_drafts`): every quote and number is checked against the stored page (`regreader.grounding`) and proposed through `rulebook.service.propose` only. No model was called; nothing was approved.

  | State | Stated from a stored tariff order | Page |
  | --- | --- | --- |
  | Telangana | standby | FY 2026-27 order, p. 170 |
  | Bihar | cross-subsidy surcharge, additional surcharge, standby | p. 511, 519, 520 |
  | Chhattisgarh | standby (₹11.93/kWh) | p. 217 |
  | Andhra Pradesh | standby | p. 247 |
  | Haryana | cross-subsidy surcharge | p. 333 |
  | Madhya Pradesh | additional surcharge | p. 138 |
  | Odisha | additional surcharge (none) | p. 245 |
  | Gujarat | eligibility (above 1,000 kVA) | p. 152 |
  | Rajasthan, Karnataka, Maharashtra, Tamil Nadu, Uttarakhand, Uttar Pradesh, Punjab | nothing: all thirteen fields not stated | — |

- To obtain, per state (named in each draft): the commission's intra-state open access regulations with amendments; the SLDC's procedure for short-term open access and collective (exchange) transactions, with the no-objection or standing-clearance format and validity; the deviation settlement regulations as they apply to open-access consumers.

### The suggestion

- `engine/exchange.py` (pure), reached through `engine.scope.exchange_verdict()`. Verdicts:
  - **not yet sourced**: no approved row, or permission or schedule not stated;
  - **not practical**: not permitted, contract demand or voltage below the rule's minimum, or a cited barrier;
  - **feasible with conditions**: anything conditional, or any field not stated;
  - **feasible**: nothing conditional.
- `runs.assemble` asks for the verdict through the run's rule reader (approved rules only; the version is pinned). Where it does not allow the purchase, the exchange source is left out and a note says why, citing the rule. `GET /connections/{id}/mix/options` marks it unavailable in the verdict's words; `POST /connections/{id}/mix` answers 422.
- A uniform schedule is modelled as one flat block per day or window (`Candidate.uniform_hours`, equality constraints in `optimizer.model.build`) and checked again on the solved dispatch; a violation fails the run.
- Deviation risk is added per block to the exchange's landed rate: under-drawal at the landed cost less any compensation the rule states, over-drawal at (multiple − 1) × the grid rate, on a share of 5% each way (`ERGPLAN_EXCHANGE_DEVIATION_PCT`, an `assumption`, pinned). A side the rule does not state is not priced.
- Each exchange source the plan offers shows the verdict, what to apply for, the schedule in words, the deviation cost, energy, landed rate, grid rate and saving by month, the energy by hour of day, and a sentence naming the months and hours it pays. Endpoints: `GET /connections/{id}/exchange` (client, approved rules); analyst sandbox `GET /exchange/sandbox/{version_id}` and `POST /exchange/sandbox/{version_id}/month-cost`. Web: `ExchangeVerdicts` on the results page and the plan.

## Hand-worked June 2026, real prices, fixture rules

Sums from the copied file: DAM June, 2,880 blocks, Σ = 14,884,463.7 Rs/MWh (mean 5,168.22); DAM 09:00–17:00, Σ = 1,670,076.8; GDAM 22:00–06:00, Σ = 8,515,279.5.

| Fixture | Schedule | Loss, charges | Total for June | Landed |
| --- | --- | --- | --- | --- |
| Rajasthan-shaped (uniform required, a barrier; verdict not practical) | 1,000 kW flat, DAM | 5%, ₹2.50/kWh | ₹5,762,521.72 | ₹8.4247/kWh |
| Workable A (by block) | 2,000 kW 09:00–17:00, DAM | 3%, ₹1.80/kWh | ₹1,756,774.32 | ₹3.7731/kWh |
| Workable B (by block, over-drawal at 1.5×) | 1,500 kW 22:00–06:00, GDAM | 4.5%, ₹0.90/kWh | ₹3,558,517.31 | ₹10.3505/kWh |

A probe on the synthetic 5 MVA MSEDCL site, hourly, the real DAM year, 4 MW cap, today ₹43.56 cr: under a uniform fixture rule 32.16 GWh flat at an average ₹7.48/kWh landed, cost ₹35.75 cr; by block 26.74 GWh at ₹6.47, cost ₹34.97 cr. Fixture rules, not readings of any regulation.

## Checked (done when)

| Check | Result |
| --- | --- |
| A year of prices in `price_series`, its vintage naming repository, commit, file and sha256 | `tests/intake/test_iex_vintage.py` |
| Hourly mean of the 15-minute series equals the hourly file | Within the files' rounding (Rs 0.525/MWh at most), tested per market; a series that disagrees is refused |
| Analyst's upload kept for newer months; conversion only in `core.units` | `test_prices_are_read_in_inr_per_kwh_and_an_analysts_newer_month_is_kept` |
| A rule family per state, cited like every other rule, draft until approved; "not stated" where the regulation does not say | Fifteen drafts, all quotes grounded; `tests/regreader/test_exchange_drafts.py`; golden files per state |
| Rajasthan and two workable states, a hand-worked landed cost for one month of real prices | `tests/engine/test_exchange.py`; the same figures through the sandbox in `tests/platform/test_api_exchange.py`. On fixture rules: no stored regulation exists |
| A non-uniform schedule is refused where the rule requires a uniform one | `tests/engine/test_exchange.py`, `tests/optimizer/test_exchange_schedule.py`, 422 in the sandbox test |
| The optimizer offers exchange sources only where the verdict allows; a draft reaches only the sandbox | `tests/platform/test_api_exchange.py` |
| A run with the vintage pins it and replays to the paisa | `test_a_uniform_rule_is_a_flat_block_and_the_run_pins_prices_and_rule_and_replays` (identical after a newer upload and a newer rule) |
| Watcher green | Green on the merged tree (`4ee83e8`; the pass began at 10:10 UTC, after the last merge) |
| `pytest -m slow` and the last CI run | The full fast suite and `pytest -m slow` passed on the merged tree (26 Nov; including the Uttar Pradesh full test, the Uttar Pradesh transition plan, the Stage 15 answers end-to-end and the mix journey; the Stage 6 to 8 end-to-end tests skip on the VM, which holds no real bills); CI green on `6534f23` |
| Reviewer clean | Not run: no OpenAI key |

## On the live server

Redeployed at `6534f23` on 26 Nov (migrations to 0019). `python -m ergplan.intake.iex_vintage` loaded GDAM, DAM and RTM (35,040 blocks each, 19 Nov 2025 to 18 Nov 2026); a second run changed nothing. `python -m ergplan.regreader.exchange_drafts --pdf-dir /data/documents` proposed the fifteen drafts (Andhra Pradesh after its order, SHA-256 `56d2ce7c…`, was placed in the library from the copy read in Stage 14). The 72 approved versions and their 200 rules are unchanged (fingerprint before and after). `tools/dev/vm-check.py`: every check passed.

## What was skipped, and why

- The hourly fallback (hold the hour's price for four blocks) was not built: the 15-minute series exists.
- The monthly rule book and the landed-cost screen still show exchange landed costs without the verdict, and `/mix/options` without a connection does not apply it; both belong with Stage 19's calculator.
- Contract-demand reduction and standby charges are conditions in words, not modelled. No analyst screen for the exchange sandbox (API only). The exchange reader prompt (`regreader_exchange_purchase_v2.md`, added because a test requires a prompt per family) has never been run.
- A standard drop-the-bill scenario includes no exchange source, so a plan speaks of the exchange only when one is asked for.

## Deviations from the master prompt

- Built in parallel with Stage 15 (questions 26). No migration was needed.

## Existing tests changed

`tests/platform/test_api_mix.py` and `tests/e2e/test_mix_journey.py` now approve a fixture exchange rule in the test database: exchange sources are no longer offered without one (the behaviour the brief asks for).

## Reviewer findings

Not run: no OpenAI key (`docs/questions-for-review.md`, 1).

## Open questions

`docs/questions-for-review.md`, 30.

## Cost

- Application model calls: none. Reviewer: none. Builder: Claude Code on the subscription; one worker subagent and my review and merge.
