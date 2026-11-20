# Stage 9 — ergPLAN 2

20 Nov 2026. Builder: Claude Code (subscription). Reviewer: OpenAI gpt-5.1 (`tools/review/run.sh 9`).

## What was built

1. **The erg_plan 2.0 settlement, ported and proven.** `apps/api/ergplan/settlement/cycle.py` is the settlement and banking engine of erg_plan 2.0 in Decimal: real-time set-off per 15-minute block or per month's ToD category, the banking limit of a cycle, the charge in kind, banked energy drawn within the cycle by the drawal matrix with the highest-rate category first (a max flow per step), lapse or sale at the cycle's end, and rooftop settled first. The original TypeScript was run on six years of blocks and 400 allocation cases (`tools/dev/erg2-fixtures/harness.ts`); the port matches it on every total, pool, limit, drawal, settled cell and block (`tests/settlement/test_cycle_vs_erg2.py`).
2. **One judge, two methods.** A settlement rule that says `method: cycle_pooled` is settled by the cycle engine; `settlement.simulate()` is still the only judge. The optimizer models the same thing (one step per banking cycle, a bucket per ToD category, the drawal matrix, the cycle's limit with a binary where the higher of two shares applies) and the two agree to the watt-hour on hand-worked cases (`tests/optimizer/test_cycle_model.py`) and on real sites.
3. **The State Regulation Master imported.** `tools/dev/import-register.py` wrote, through `rulebook.service`, approved versions for AP, BR, CG, GJ, HR, KA, MH, MP, OD, PB, RJ, TN and UK: open-access charges and losses (by voltage), ToD slots with their category, the cycle settlement rule, the capacity limit against contract demand, and Tamil Nadu's retail schedule; 42 versions, each with golden tests. Each value's source is of the new kind `register`. UP's and Maharashtra's cited settlement rules were re-issued with the cycle settlement's fields added and every cited field kept (Maharashtra's banking charge, which the cited rule did not state, now comes from the register and says so).
4. **Any state can be planned.** Where no retail schedule is approved, the consumer's own energy charge per unit and fixed charge per kVA price the grid: read from an accepted bill (`intake.site_intake.bill_tariff`, bill refs) or typed (`client_stated`).
5. **Five components with targets and inputs.** Rooftop solar, open-access solar, open-access wind, a battery behind the meter and a battery at the open-access plant (new: `oa_bess`, `optimizer.model.OABattery`). Each takes a target (a ceiling, or taken as given) and its cost, O&M, CUF, life, degradation, round-trip efficiency and duration; blanks are assumptions shown as such.
6. **The plan, drawn.** `apps/web/components/site/IsoSite.tsx`: an isometric view of the site (factory, rooftop panels, battery, meter), the DISCOM's line and the open-access plant (solar field, wind turbines, battery), with targets on the Evaluate page and the optimizer's sizes on the results.
7. **Remote access.** `tools/dev/remote.sh on` starts a Cloudflare tunnel (`docker/compose.remote.yaml`) and points the app at its https address; the app stays in production mode (sign-in, each consumer sees their own evaluations).
8. **Its own identity.** Repository `Ergplan/ergPLAN-2`, compose project `ergplan2`, databases `ergplan2*`, the Mac installer installs to `~/apps/ergplan2` on port 8090 beside the first ergPLAN.

## Checked

- Watcher: see the last section.
- Browser, end to end (`tests/e2e/test_ergplan2_journey.py`): a Rajasthan factory typed in with its charges, the five components on with targets, a CUF and a cost, the drawing showing the targets, the run, the results with the sized drawing, the 25-year view, "register" and "you said" chips. Passed (4 min 22 s).
- API (`tests/platform/test_api_ergplan2.py`): five components sized together on an MSEDCL site with the judge's agreement; a target taken as given; a plant battery refused without a plant; a register state on the consumer's own tariff.
- Hand-worked months on the approved UP and Maharashtra rules (`tests/settlement/test_cycle_rules.py`).

## What was skipped, and why

- No real bill from a register state was read end to end: the workspace has real bills only for NPCL and MSEDCL (both cited states). `bill_tariff()` is exercised by the existing bill tests' accepted bills only through `from_bills`; the register-state journey types the charges. Open question 2.
- The VM deployment: the Google VM has been unreachable since 11 Nov. ergPLAN 2 was not deployed anywhere by this stage; the Mac kit is ready (below).
- The tunnel was not started from here (it needs Docker on the server); `remote.sh` was syntax-checked only. Open question 3.
- Chhattisgarh's SLDC (Rs/day) and reactive-energy (paise/kVARh) charges are not imported: no basis in the schema (known gaps).

## Deviations from the master prompt

All recorded in `docs/decisions.md`, 20 Nov 2026: the `register` kind of source_ref; the consumer's own tariff as a schedule outside the rulebook; the cycle settlement replacing first-in-first-out for UP and Maharashtra; the repository and compose project; `oa_bess` renamed for the old storage-PPA source (`oa_storage_ppa`).

## Reviewer findings

Run 1 (gpt-5.1, 178k tokens in, 3k out): 22 findings, 2 marked code defects, 20 by design or unprovable from the diff.

| Finding | Kind | What was done |
| --- | --- | --- |
| UP's settlement cases point at `tests/settlement/rules/UP-v1.yaml` while the approved rule is now cycle-pooled: the golden meaning may be lost | code defect (as reported) | The file is the rule as first issued, kept on purpose for the chronological engine. The headers of both case files now say so, and the approved rules got hand-worked cases of their own: `tests/settlement/test_cycle_rules.py` |
| The same for Maharashtra | code defect (as reported) | As above. While writing its cases the cited Maharashtra rule was found to state no banking charge; the register's 8% is now on the re-issued rule (v4), cited to the register |
| Say in the architecture that the consumer's own tariff bypasses rulebook versions | by design | In `docs/decisions.md` and `docs/known-gaps.md` (not pinned by a run) |
| Floats inside the optimizer | by design | Unchanged: floats stay inside the solver; every stored or returned value is a Decimal from `exact_of` |
| Register and "you said" chips on every screen cannot be proved from the diff | unprovable | Checked in the browser journey on the results page |

Run 2: see the last section.

## Open questions

1. The repository is public. The code holds no keys, no real bills and no client data from erg_plan 2.0; say if it should be private.
2. Send one accepted bill each from two register states (say Rajasthan and Tamil Nadu) so the bill-to-tariff path is proven on real layouts; billx may need a layout profile for each DISCOM.
3. Remote access: a quick tunnel gives a random address that changes at each start. For a fixed address (ergplan.joulewise.in, say) a Cloudflare account, the domain on it and a tunnel token are needed. Which do you want?
4. UP and Maharashtra now bank as a monthly pool (erg_plan 2.0's model) instead of first in, first out with carry-forward (UPERC's words). Confirm that this is the reading to keep.
5. The register's Rajasthan rule makes the cycle annual when renewable capacity is below contract demand; the imported rule is monthly. Should the capacity-conditional cycle be modelled?
6. The account number 994927801935 in the first repository's billx tests is replaced by a dummy here. If it is a real consumer's number, the first (public) repository still has it.

## Cost

Builder: Claude Code session on the subscription (no API cost). Reviewer: see the review files (gpt-5.1; run 1: 178,142 tokens in, 3,133 out). No `model_calls` rows: this stage made no model call from the app.
