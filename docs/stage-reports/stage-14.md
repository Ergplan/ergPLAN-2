# Stage 14 — Tariffs for every DISCOM, from the ARR Network

Built on 25 and 26 Nov 2026 on the VM, from `stage-14-start` (c6a1469). Brief of 25 Nov 2026, part C.

**Status: built, tagged `stage-14-built`.** Everything imported is a draft; the builder approved nothing. The reviewer could not run (no OpenAI key).

## What the source is, and what that meant

The ARR Network's JSON API (`/api/v1/`) carries revenue-requirement figures and each order's link and SHA-256, but no tariff figures. The tariff tables are on the site's order pages as the strings printed in the order, read by its section reader and not reviewed by a person. Banking has no section. The importer therefore reads those pages behind one client class (`docs/questions-for-review.md`, 19). Three consequences run through every state below:

- Only what an order prints in a table the commission approved can be imported. A rule stated in the order's prose (billing-demand floor, excess-demand rate, exemptions, a surcharge "approved as zero", a charge given in a sentence) is not there.
- A table the source filed under the wrong heading, cut at a page break or read with a broken column is left out, with the reason, not guessed.
- Banking, the regulatory limits and most open-access charges still rest on what ergPLAN had.

## What was built

- `regreader/arr_network.py`, `arr_rates.py`, `arr_map.py`, `arr_import.py`: the client, the parser of printed rates, hours and percentages, the mapper to the rule families, and the import (command and `POST /regreader/arr-network`). An import is idempotent: a second run says "unchanged".
- Every row is grounded: the order PDF is fetched from the regulator, its SHA-256 checked against the ARR Network's, stored, and the row's numbers must be printed on the cited page. No imported row failed this. Every order's PDF imported here is stored, so every imported number opens its page.
- Golden cases worked from the order's page for every imported version, and hand-worked months per DISCOM; cases can be scoped by date and origin so two tariff years each have their own (migration 0015; the approval gate is otherwise as strict as before).
- Found and fixed on the way: approving a previous year's version would have superseded the current year's; a golden file could delete another's cases; a document row without its file could not be completed; "100 kVA" was read as 100 kV.

## State by state

"Sourced" means imported as a draft with the order's page and printed figures, and grounded on the stored page. "Rests on" means what a client still sees today.

| State (DISCOMs) | Orders imported (tariff year, in force) | Now sourced to a page, as drafts | Still rests on | Missing at the source or not imported |
| --- | --- | --- | --- | --- |
| Uttar Pradesh (state DISCOMs; NPCL) | 67 and 66 (FY 2026-27, from 2 Jul 2026); 65 and 64 (FY 2025-26, 22 Nov 2025 to 1 Jul 2026) | Schedule: HV-1, HV-2 (three of four voltage bands), HV-4 and six LMV categories; ToD for HV-2, LMV-6, LMV-11; wheeling; distribution losses by voltage; cross-subsidy surcharge by category and voltage; green tariff FY 2025-26 | The approved FY 2026-27 rules (order-sourced, with the prose rules); banking; state transmission and losses | HV-2 at 66–132 kV (broken column at the source); FY 2026-27 green tariff (table computes ₹0.48, text approves ₹0.34); HV-3, LMV-1, 2, 3, 5, 8; the FY 2024-25 order |
| Maharashtra (TPC-D, AEML-D, BEST) | 100, 104, 108 (FY 2025-26 from 1 Apr 2025; FY 2026-27 from 1 Apr 2026) | HT schedule HT I to HT V with the retail wheeling charge; seasonal ToD for eight categories each; wheeling losses (TPC-D, AEML-D) | Nothing before: these three are new to ergPLAN | Cross-subsidy and additional surcharge, transmission, open-access wheeling by level; LT rows; EV rows |
| Maharashtra (MSEDCL) | None | None | The approved rules (post-remand order of 25 Mar 2026) | The source holds only the stayed MYT order of 28 Mar 2025 (labelled FY 2022-23 there) and a barely read FY 2023-24 order; bills before 1 Apr 2026 have no sourced tariff |
| Telangana | 187 (FY 2026-27, 1 Apr 2026 to 31 Mar 2027); 188 (FY 2025-26, from 1 May 2025) | HT schedule: every HT category of Appendix A that prints a demand charge | Approved ToD and open-access rules (FY 2026-27); banking | ToD (the table names no category and prints 16 of 24 hours; FY 2025-26 ToD is in no rulebook); surcharge, losses and transmission tables (one per DISCOM, cut at the page break); LT rows |
| Andhra Pradesh | None | None | The approved FY 2026-27 rules | Order 185 (FY 2026-27) has no tables at the source; order 186's tables sit under headings that are not categories |
| Punjab | 178 (FY 2025-26, 1 Apr 2025 to 31 Mar 2026); 179 (FY 2023-24, from 16 May 2023) | Schedule: Large Supply, Medium and Small Supply, Bulk Supply | The approved FY 2026-27 schedule (an order the source does not hold); open-access rules; ToD | The FY 2024-25 order; ToD (only the proposed table); wheeling and green tariff (tables not headed approved) |
| Haryana | 180 (FY 2026-27, from 1 Apr 2026) | HT Supply at five voltages, arc furnaces, LT Supply | The State Regulation Master for everything else | The rest of the schedule; no ToD or open-access tables |
| Gujarat | 189 (FY 2026-27, from 1 Apr 2026) | HTP-I (printed "HTP-1" in the heading) and HTP-II demand slabs and energy rates; values equal the approved rules | The approved rules (with excess rate, floors, voltage rebate, time of use); open-access on the Master | No previous year (the only other order is FY 2022-23); time-of-use adders, wheeling 23.52 paise, surcharge ₹1.33, green tariff ₹0.75 are in the order's text only |
| Madhya Pradesh | 41 (FY 2026-27, from 26 Mar 2026); 42 (FY 2025-26, from 29 Mar 2025) | HV-1 railway, HV-5 at 11 kV, HV-9 metro; additional surcharge (₹1.18; ₹1.24); green energy charge (₹0.30; ₹0.53) | The Master for surcharge, wheeling, transmission, losses, ToD, banking | HV-3, the main industrial schedule, and HV-2, 4, 6 (two energy rates by load factor; the rule family holds one); wheeling and surcharge tables not headed approved |
| Bihar | 1 (FY 2026-27, from 1 Apr 2026); 3 (FY 2025-26) | HT schedule for the 11 and 33 kV categories; HT time of day | The Master for all open-access charges | The 33 to 400 kV industrial rows (filed under the wrong heading at the source); surcharge (table headed "Workout"); FY 2026-27 wheeling |
| Chhattisgarh | 73 (FY 2026-27, from 1 Jul 2026); 76 (FY 2025-26, 1 Jul 2025 to 30 Jun 2026) | Schedule: HV-2 to HV-6, HV-9 and four LV categories; cross-subsidy surcharge by voltage (FY 2026-27) | The Master for wheeling, transmission, losses, additional surcharge, ToD | ToD (broken read at the source); wheeling (text only); April to June 2025 |
| Odisha (four DISCOMs) | 49 (FY 2026-27, from 1 Apr 2026); 50 (FY 2025-26) | Cross-subsidy surcharge and wheeling per DISCOM; transmission charge per MW a day | The Master for the retail schedule, losses, additional surcharge, ToD | A usable retail schedule (one uncategorised table by load-factor slab) |
| Uttarakhand | 55 (FY 2026-27, from 1 Apr 2026); 54 (FY 2025-26) | Schedule: RTS-1, 3, 4A, 6, 7; the wheeling charge | The Master for surcharges, transmission, losses, ToD | RTS-5 industry (by load factor and ToD rates), so no industrial bill can be replayed |
| Tamil Nadu | 184 (FY 2025-26, from 1 Jul 2025) | HT Tariff I to IV and five LT categories | The Master for open-access charges and ToD | The 1 Jul 2026 revision; slab and time-slot categories; no open-access or ToD tables |
| Karnataka, Rajasthan | None | None | As before | No orders at the source |

In all: 60 draft versions from 24 orders, none approved. Row counts and golden results per order are in the import's own summary (`python -m ergplan.regreader.arr_network import … --dry-run`).

## The twelve assumed Uttar Pradesh bills across both tariff years (sandbox)

On approved rules, 8 of the 12 bills have no tariff in force. With the two imported years as drafts in the sandbox, every month finds a tariff: November 2025 to June 2026 under the FY 2025-26 order, July to October 2026 under the FY 2026-27 order. Energy and ToD charges are rebuilt to within ₹1 in all twelve (`tests/e2e/test_up_arr_sandbox_replay.py`). Two things are not rebuilt: the demand charge (billed on the 75% floor, which is prose in the order, not a table) and supply on 1 to 21 November 2025, which belongs to the FY 2024-25 order (ARR Network orders 63 and 61), not imported.

## Checked (done when)

| Check | Result |
| --- | --- |
| For each state: what is sourced to a page, what rests on the Master, what is missing | The table above |
| Every imported version has golden tests and they pass | Every version has cases worked from the order's page and all of those pass. On three versions' families the approved rulebook's own cases also run and some fail, on purpose (Uttar Pradesh FY 2026-27: schedule 25 of 28, open-access 16 of 18 and 10 of 15; Telangana FY 2026-27: 34 of 40; Gujarat FY 2026-27: 21 of 34): the draft lacks the approved version's prose rules and the gate refuses it |
| The twelve assumed Uttar Pradesh bills replay across both tariff years in the sandbox | Yes for energy and ToD, to within ₹1; the demand charge and 1 to 21 Nov 2025 are named as missing (above) |
| No draft reaches a client; nothing imported was approved by the builder | The absurd-draft test passes. On the live database (26 Nov, 08:30 UTC) all thirteen import commands ran: 60 draft versions, and the 72 approved versions and their 200 rules are unchanged (a fingerprint of every approved version and rule taken before and after is identical). A second run says "unchanged" for all 60. Every order's PDF is stored. The two Telangana PDFs could not be downloaded from inside the server's container (the regulator's site refuses its TLS handshake; certificate checks were left on), so they were downloaded on the VM with certificate checks on, their SHA-256 matched the ARR Network's, and they were placed in the library. `tools/dev/vm-check.py`: every check passed |
| Watcher green | Green on the merged tree (the pass that ended 26 Nov 00:40 UTC began after the last code change; only documents changed since) |
| `pytest -m slow` and the last CI run | `pytest -m slow` passed on the merged tree (26 Nov; the Stage 6 to 8 end-to-end tests skip on the VM, which holds no real bills); the two browser journeys passed (161 s); CI green on `ca482a3` |
| Reviewer clean | Not run: no OpenAI key |

## What was skipped, and why

- The seven states that would be new to ergPLAN (Goa, Himachal Pradesh, Jharkhand, Puducherry, Assam, West Bengal, Delhi): second round. Karnataka and Rajasthan have no orders at the source.
- MSEDCL (Maharashtra): the source holds only the MYT order MERC stayed and an older order it barely read; importing the stayed order would put a tariff that was not in force into those dates.
- Andhra Pradesh: nothing sound at the source.
- No screen for the import.

## Deviations from the master prompt

- The importer reads HTML order pages, not the JSON API (there is no tariff JSON).
- "Every imported version has golden tests and they pass": every version has golden tests worked from the order page and those pass. Where an approved version of the same order already exists (Uttar Pradesh, Gujarat and Telangana FY 2026-27), the approved rulebook's own cases also run against the draft and some fail, because the draft lacks the prose rules. That is left visible on purpose: the approval gate refuses such a draft until an analyst merges the two (questions 20).
- Banking was not imported (no section at the source).
- Tagged `stage-14-built`, not `-done`, while the reviewer cannot run.

## For the analyst: differences found against the State Regulation Master

No rule was changed for any of these; each is where an order's page says something other than the register row a client sees today.

- **Chhattisgarh, cross-subsidy surcharge**: the register holds ₹1.43 flat; the FY 2026-27 order approves ₹1.23 at 220 kV and above and ₹1.22 at 132 kV (₹1.43 at 33 kV, 11 kV and LT) (Table 6-5, page 187).
- **Odisha, surcharge and wheeling**: the register applies TPCODL's HT figures (₹0.5642 and ₹1.1656) state-wide; the order sets them per DISCOM (HT surcharge ₹0.0163 TPNODL, ₹0.1726 TPWODL, ₹1.0602 TPSODL; wheeling ₹1.8086, ₹1.2090, ₹1.9732).
- **Tamil Nadu**: the register's "HT I-A, FY 2026-27, 608 and 7.5" equals the FY 2025-26 order, which says rates are revised from 1 Jul 2026.
- **Bihar, ToD**: the register has peak 00–06 and 18–22; the order's HT table has off-peak 09:00–17:00 at 80%, peak 17:00–23:00 at 120% (page 567).
- **Bihar, surcharge**: the register has ₹1.68 for the state; the order approves ₹1.77 for NBPDCL and ₹1.68 for SBPDCL (page 517).
- **Madhya Pradesh, surcharge**: the register's ₹1.49 is the ceiling column of a table the order calls an illustration.
- **Inside an order**: AEML-D's Table 6-50 (page 416) gives −15% for 00:00–06:00 in FY 2025-26 and its tariff schedule −10%; the draft follows the schedule.
- **Gujarat**: the order heads its clause "HTP-1" once and writes HTP-I elsewhere; the draft keeps the heading as printed.

## Reviewer findings

Not run: no OpenAI key (`docs/questions-for-review.md`, 1).

## Open questions

`docs/questions-for-review.md`, 19 to 24.

## Cost

- Application model calls: none (the importer calls no model). Reviewer: none. Builder: Claude Code on the subscription; seven subagents (survey, importer, four state workers, integration).
