# Multi-state test — the savings logic and the sizing on several states

Stage 17, 26 Nov 2026, on the VM. Brief of 25 Nov 2026, part F. For each state, what `tests/e2e/test_up_assumed.py` does for Uttar Pradesh, as a slow test that asserts: twelve bills worked by hand from the state's own order; bill replay against them; today's cost; landed cost for solar and wind on each route against the order's own figures; settlement after banking, with the energy balance closed by hand; an optimizer run with five components at hourly blocks and one at 15 minutes; the grid bill recomputed from the dispatch file; replay identical; the month's rule book.

A difference is a defect fixed (with a test) or a rule gap listed, never a widened tolerance. Where a listed gap makes the app's figure differ from the order's, the test asserts the app's figure with the gap named beside it, and works the order's figure too. Nothing was approved.

Levies that no order states (fuel adjustment, duty or tax as a bill prints them) are typed as each bill would print them and marked as assumptions in the tests.

## Defects found and fixed

| # | Defect | Where | Effect before the fix | Test |
| --- | --- | --- | --- | --- |
| 1 | A retail wheeling charge alone was read as open access | `intake/recognise.py` | MSEDCL bills every unit with a wheeling charge (Case 75/2025 p. 100), so the Maharashtra site was taken for an open-access consumer | `tests/intake/test_recognise.py` (two) |
| 2 | With no power factor printed, today's demand was overstated by the kVAh factor (found in Maharashtra and, separately, Gujarat: ₹9.93 lakh a year, 5.9%; probably also the Uttar Pradesh full test's finding 9, a profile peak 2% over the recorded demand) | `intake/site_intake._bill_power_factor` | The profile peaked at MD × 1 while the avoided cost divided by kWh/kVAh: +3.1% on Maharashtra's demand (₹1,27,45,362 against ₹1,23,63,000). The power factor is now the bill's kWh/kVAh, cited to the bill. **This also lowers Uttar Pradesh's today's demand, by about 4.2%** | `test_bills_and_site`, `test_api_intake` (now MD × kWh/kVAh) |
| 3 | ToD set in rupees was read as no ToD | `engine/tariff_eval.slot_classes`, `engine/replay.tod_units` | KERC's +100 / 0 / +100 / −100 paise (p. 568) counted as 0%: every Karnataka slot normal, the ToD settlement netted as one slot, and the bill replay charged the rebate zone as a surcharge (April ₹1,18,20,100 against ₹1,07,27,252) | `test_tariff_eval.py` (parametrised), `test_replay_and_avoided.py` |
| 4 | The dispatch file rounded DISCOM kWh to 0.001 a block | `platform/runs.dispatch_blob` | The grid bill worked from the 15-minute file missed the run's by ₹5.22 (0.43 kWh); now to 0.000001 kWh | `tests/platform/test_api_evaluate.py` |
| 5 | Bill replay matched rupee-per-unit ToD zones to slots by percentage, so every zone fell on one slot (GERC +0.85 / −0.60; TGERC) | `engine/replay.py` | Gujarat energy ₹7.6 lakh a month wrong | `test_replay_matches_rupee_tod_adjustments_and_slabbed_demand` |
| 6 | Bill replay reported each demand slab's printed rate as a wrong rate | `engine/replay.py` | False "wrong demand rate" on every slabbed bill | the same test |
| 7 | Approved SLDC and RLDC per-unit charges were left out of the landed cost | `engine/landed_cost.py` | Eight states (GJ, HR, BR, AP, OD, TN, MP, UK); ₹0.05/kWh in Gujarat | `test_sldc_operating_charge_per_unit_is_a_landed_line` |
| 8 | (with defect 3) settlement categories ignored rupee adjustments in Gujarat, Telangana and Andhra Pradesh too: banked off-peak energy was drawn in the evening peak | `engine/tariff_eval.slot_classes` | | `test_slot_classes_read_rupee_adjustments` |
| 9 | The banking charge in kind also grossed up the drawal charges (the surcharge) | `engine/landed_cost.py` | Punjab third party ₹0.0956/kWh high | `test_banking_charge_in_kind_does_not_gross_up_the_drawal_charges` |

## Maharashtra — MSEDCL HT I (A), 22 kV, contract demand 2,000 kVA, single shift

5,189,000 kWh a year; the twelve hand bills (April 2026 to March 2027) total ₹6,35,26,665.04. From MERC Case 75/2025 (document 5ee7dce7): p. 100 demand ₹650/kVA/month, energy ₹8.44/kVAh, wheeling ₹0.81/kVAh; p. 77 Table 8 solar-hour rebate (09–17) 15% April–September and 25% October–March, peak (17–24) "+20%#", read through the MYT order (c1d27543) p. 565 as +25% for HT industry. Billing demand (MYT p. 608): the higher of MD, 75% of the highest of the preceding eleven months, and 70% of contract demand. Assumed as an MSEDCL bill prints them: FAC ₹0.20/kVAh, duty 7.5% of demand, energy, wheeling, ToD and FAC, tax on sale ₹0.1804/kWh.

**Agreed.** Bill replay, all twelve months: energy and ToD within ₹1, wheeling exact, demand exact in eleven months (November: gap MH-1); FAC, duty and tax on sale named as gaps. Today's cost: energy within ₹12 over the year, demand exact on recorded demand, FPPCA exact, duty within ₹1 of the bills' four-place rate. Landed cost: every route and both sources, each line and the total, within ₹0.0001 of the hand figure (cross-subsidy surcharge ₹2.11, p. 106). Settlement, every month within 1 kWh, the balance closed by hand: 2 MW solar (captive and third party alike) set-off 36,21,893, banked 57,307, charge 4,585 (8%), withdrawn 0, lapsed 52,722, grid 15,67,107 kWh; 0.8 MW wind banked 2,05,782, charge 16,463, withdrawn 1,70,870, lapsed 18,450, grid 32,61,673 kWh. Optimizer (rooftop up to 0.5 MW, captive open-access solar up to 3 MW, wind up to 2 MW, two batteries up to 1 MW): rooftop 500 kW, solar 1,467 kW, wind 643 kW, behind-the-meter battery 257 kW / 257 kWh; ₹6.23 cr a year to ₹3.40 cr, saving ₹2.83 cr, owned capital ₹12.84 cr. The grid bill from the dispatch file equals the run's to the paisa, hourly and 15-minute; replay identical. The November 2026 rule book's grid tariff by slot (10.58 / 8.41 / 12.76) and landed costs equal the hand figures.

**Differences.** Duty in today's cost ₹1.08 above the bills (the four-place bill-average rate). Tax on sale (₹9,36,096) is in no part of today's cost. The rule book's landed cost leaves out the banking upper bound the landed-cost screen includes (third-party solar 6.05 against 6.50). Plan demand ignores the 70% floor: ₹98,20,550 (hourly) or ₹1,00,31,356 (15-minute) against ₹1,09,20,000, up to ₹11.0 lakh a year understated (MH-1).

**Rules missing or doubtful.**
- MH-1: the billing-demand floor (MYT p. 608) is missing; November (1,300 kVA) is billed at 1,300, not 1,400 (₹65,000).
- MH-2: the open-access wheeling charge ₹0.81/kWh (Case 75 p. 83, Table 9 row E) is missing.
- MH-3: open-access losses are missing (transmission 3.26%, wheeling 7.50% at HT, p. 106); the delivered share is 1.
- MH-4: the settlement rule's register banking matrix lets normal-slot banking be drawn only in the normal slot; the order (p. 79, 18.21) and the rule's own drawal windows also allow the solar hours. On 0.8 MW wind the order would draw 18,450 kWh more a year — everything that now lapses.
- MH-5: tax on sale is not modelled.
- MH-6: the 8% banking charge in kind and the 30%/25% banking limit come from the register only; the DOA and GEOA Regulations are not in the corpus.
- MH-7 (doubtful): the peak surcharge is taken as +25%; Case 75 p. 77 prints "+20%#" without restating the note, and the row's own season note says 20%.
- MH-8: the intra-state transmission charge (MSETCL order) and MSEDCL's additional surcharge are not in the corpus. Also not in the rule book (no effect here): the load-factor incentive, prompt payment, the single-shift demand discount (MYT 7.23.2).

**Solver.** Hourly 74.7 s, optimal, gap 0; 15-minute 84.4 s, optimal, gap 0 (sizing on hourly blocks, dispatch re-run at 15 minutes). Test about 5.5 minutes.

## Karnataka — BESCOM HT-2(a), 11 kV, contract demand 3,000 kVA, continuous process

19,636,000 kWh a year; the twelve hand bills total ₹15,60,59,461.44. From the KERC Tariff Order 2025 (document 8e00613e): p. 566 demand ₹350/kVA/month, energy 660 paise/kWh; p. 568 ToD December–June +100 / 0 / +100 / −100 paise, July–November none; p. 561 billing demand the higher of MD and 90% of contract demand, rounded up to the next kVA, power-factor surcharge below 0.90 (none: the plant runs at 0.96); p. 562 high-voltage rebate only above 13.2 kV (none). Assumed: electricity tax 9% of charges, FAC ₹0.15/kWh (the order leaves both to the State Government, p. 539).

**Agreed.** Bill replay, all twelve months: energy and ToD within ₹1 (after defect 3), demand exact in May and March (KA-1 otherwise), FAC and tax named as gaps. Today's cost: energy within ₹12, demand exact on recorded demand, FAC exact. Landed cost: every route and source equals the hand figure (BESCOM HT wheeling ₹0.29 and loss 2.76%, p. 249; cross-subsidy surcharge ₹1.86, p. 260; additional surcharge 0, p. 266). Settlement (higher-to-lower drawal, 8% in kind, monthly, lapse), every month within 1 kWh, delivered share 0.9724, the balance closed by hand: 4 MW solar banked 31,407, charge 2,513, withdrawn 28,894, lapsed 0, grid 1,21,42,475 kWh; 3 MW wind never exceeds the load. Optimizer: rooftop 800 kW (its cap), solar 5,000 kW (its cap), wind 2,413 kW, behind-the-meter battery 86 kW; ₹15.57 cr a year to ₹10.20 cr, saving ₹5.37 cr, capital ₹40.71 cr. The grid bill from the dispatch file equals the run's, hourly and 15-minute; replay identical. Rule book: grid tariff 7.39 in every November slot; third-party landed 5.99 (solar) and 6.50 (wind) equal the hand figures.

**Differences.** Tax in today's cost ₹788.44 below the bills (the four-place rate on 1.96 crore kWh). Today's demand ₹3,92,000 below the bills (KA-1). Plan demand ignores the 90% floor: ₹1,02,79,756 (hourly) against ₹1,13,40,000, ₹10.6 lakh a year understated.

**Rules missing or doubtful.**
- KA-1: the 90% billing-demand floor and rounding up to the next kVA (p. 561, 1.A and 1.D) are missing; ten of twelve bills are under-billed in replay, ₹3.92 lakh a year.
- KA-2 (doubtful): banking (8% in kind, monthly, higher-to-lower, lapse) is register-only; the order defers banking charges to separate regulations (p. 252, 6.11), not in the corpus.
- KA-3: the power-factor surcharge (p. 561, 2(i)) and high-voltage rebate (p. 562, 3) are not in the schedule.
- KA-4: tax and FAC are outside the order.
- KA-5: KPTCL's transmission charge and loss (p. 250, 6.10.7) are not in the corpus.

**Solver.** Hourly 33.9 s, optimal, gap 0; 15-minute 44.8 s, optimal, gap 0. Test about 3 minutes.

## Hand-worked landed cost, November 2026 (`apps/api/tests/e2e/landed_hand/<state>.yaml`)

Build-up as the engine documents it: (generation + injection charges) ÷ delivered share, plus drawal charges, plus the banking charge, (generation + injection charges + losses) × 8/92 as if every unit were banked (drawal charges are not grossed up: energy lost in kind is never drawn), plus duty at the bills' rate, plus the equity cost ÷ delivered share. Assumed: PPA solar ₹3.10, wind ₹3.60; captive plant ₹42,000/kW solar and ₹68,000/kW wind, O&M 1.5% and 2%, 25 years, 10%; CUF Maharashtra 21% / 28%, Karnataka 20% / 30%; 26% equity on the plant's capital.

Maharashtra (duty 0.8415; the order's total adds the missing wheeling ₹0.81 and losses ÷ 0.894849):

| Route, source | Lines (approved rule book) | Total | Without banking | Order's total |
| --- | --- | ---: | ---: | ---: |
| Captive, solar | LCOE 3.0329 + bank 0.2637 + duty | 4.1382 | 3.8745 | 5.5095 |
| Group captive, solar | 3.10 + bank 0.2696 + duty + equity 0.6941 | 4.9052 | 4.6356 | 6.3666 |
| Third party, solar | 3.10 + CSS 2.11 + bank 0.2696 + duty | 6.3211 | 6.0515 | 7.7010 |
| Captive, wind | LCOE 3.6087 + bank 0.3138 + duty | 4.7640 | 4.4503 | 6.2089 |
| Group captive, wind | 3.60 + bank 0.3130 + duty + equity 0.7941 | 5.5487 | 5.2356 | 7.0857 |
| Third party, wind | 3.60 + 2.11 + bank 0.3130 + duty | 6.8646 | 6.5515 | 8.3083 |

Karnataka (duty 0.6438; the approved rule book and the order agree):

| Route, source | Lines | Total | Without banking |
| --- | --- | ---: | ---: |
| Captive, solar | LCOE 3.1846 + wheeling 0.29 + loss 0.0986 + bank 0.3107 + duty | 4.5277 | 4.2170 |
| Group captive, solar | 3.10 + 0.29 + 0.0962 + bank 0.3031 + duty + equity 0.7494 | 5.1826 | 4.8795 |
| Third party, solar | 3.10 + 0.29 + 0.0962 + CSS 1.86 + bank 0.3031 + duty | 6.2932 | 5.9900 |
| Captive, wind | LCOE 3.3681 + 0.29 + 0.1038 + bank 0.3271 + duty | 4.7329 | 4.4058 |
| Group captive, wind | 3.60 + 0.29 + 0.1104 + bank 0.3479 + duty + equity 0.7622 | 5.7543 | 5.4064 |
| Third party, wind | 3.60 + 0.29 + 0.1104 + 1.86 + bank 0.3479 + duty | 6.8521 | 6.5042 |

The Maharashtra and Karnataka third-party banking figures were first worked with the surcharge grossed up as well; the Gujarat branch's engine fix (defect 9) is right by the order's logic, the hand figures were corrected, and the levelised and equity costs are stated to four places as the app states rates.

## The calculator against the hand figures (Stage 19)

`tests/e2e/test_calculator_hand.py` reads every `landed_hand/<state>.yaml` and checks the calculator (`POST /calculator`) line by line against it: **Uttar Pradesh, Maharashtra, Karnataka, Gujarat, Punjab and Telangana all equal.** The calculator's figures (landed per kWh without banking or duty, a visitor having no bills; banking beside it):

| State | Source | Captive, own plant | Captive, 26% equity | Third party |
| --- | --- | ---: | ---: | ---: |
| Maharashtra | solar | 3.0329 (bank 0.2637) | 3.7941 (0.2696) | 5.2100 (0.2696) |
| Maharashtra | wind | 3.6087 (0.3138) | 4.3941 (0.3130) | 5.7100 (0.3130) |
| Karnataka | solar | 3.5732 (0.3107) | 4.2356 (0.3031) | 5.3462 (0.3031) |
| Karnataka | wind | 3.7619 (0.3271) | 4.7626 (0.3479) | 5.8604 (0.3479) |

Uttar Pradesh, Gujarat, Punjab and Telangana are in `docs/stage-reports/stage-19.md` and the files. Maharashtra's figures still lack the order's wheeling (₹0.81) and losses (3.26% + 7.50%), gaps MH-2 and MH-3; the files' `order` totals show what they add.

## Gujarat — MGVCL HTP-I, 33 kV, contract demand 4,000 kVA, continuous process

The twelve hand bills (April 2026 to March 2027) total ₹19,02,38,527.50 (energy ₹11,00,17,637.50, demand ₹1,67,62,250, FPPAS ₹6,34,58,640), from the GERC Tariff Order FY 2026-27 for MGVCL (document 17db86c4): demand slabs ₹150 / ₹260 / ₹475 per kVA and ₹555 above contract demand (p. 173); billing demand the highest of MD, 85% of contract demand and 100 kVA (p. 174); energy ₹4.30/kWh above 2,500 kVA (p. 173); ToD peak +₹0.85 (p. 173), 11:00–17:00 −₹0.60 (p. 175); EHV rebate 1.5% (p. 175); base FPPAS ₹2.52/kWh (p. 137).

**Agreed.** Bill replay, all twelve months, demand and energy (ToD and rebate) to the paisa. Today's cost: energy equal, demand within ₹1 (₹9.93 lakh a year too high before the power-factor fix), FPPCA at the bills' rate equal. Landed cost on three routes and two sources equal to four places. Settlement with a 9 MW solar stand-in (15-minute set-off, monthly bank capped at 30% of the month's drawal, no charge, lapse), every month to 0.002 kWh: set-off 1,14,17,832, banked 41,29,250, withdrawn 29,89,212, lapsed 11,40,038, over the cap 73,06,258, grid 1,07,74,956 kWh; the balance closes by hand. Grid bill from the dispatch file within print precision. Replay identical; the rule book's third-party figures (6.06 solar, 6.61 wind) equal the hand figures.

**Rules missing or doubtful.** The base FPPAS (p. 137, ₹2.52) is not in the approved schedule (replay marks it explained, not computed). Duty is set under the Gujarat Electricity Duty Act, not the order. The power-factor adjustment (11.6) is not seeded. Open-access rows rest on the register without a stored source: transmission loss 3.37%, SLDC ₹0.05, transmission ₹3,858.96/MW/day (GETCO order not stored), additional surcharge ₹0.99 (AS Order 05/2026 not stored); the order's own wheeling 0.2352 (p. 150), loss 6.50% (p. 151) and CSS 1.33 (p. 152) agree with the register. The settlement rule's 30% cap has no citation, and no banking charge is stated.

**Solver** (the tests raise the limit to 600 s): hourly 134–176 s, optimal, gap 0 (one hourly run under load failed at the production 300 s limit with no feasible solution: listed); 15-minute 217 s, optimal, gap 0. Plan: rooftop 1 MW, solar 2.58 MW, wind 5 MW (the most allowed), behind-the-meter battery 32 kW; ₹18.96 cr a year to ₹15.05 cr. Test about 10.5 minutes.

## Punjab — PSPCL Large Supply above 1,000 kVA, 11 kV, contract demand 2,000 kVA, single shift

The stored order PDF (8fc3d382) is not on the VM; PSERC's own chapter files of the same order were read (their printed page is two more than the stored PDF's). The twelve hand bills, every charge rounded to the rupee (condition 20), total ₹4,00,81,156: fixed charge ₹280/kVA on 80% of contract demand or the recorded demand if higher, capped at contract demand (p. 225–226 [227–228]); energy ₹6.00/kVAh (p. 225 [227]); ToD (condition 15 [246]) 1 Apr–15 Jun 22:00–06:00 −0.50, 16 Jun–15 Oct 18:00–22:00 +2.00, 16 Oct–31 Mar 22:00–06:00 −1.00 limited to −0.50 by the ₹5.50 floor.

**Agreed.** Replay: fixed charges all twelve months; energy at ₹6.00 exact, the difference exactly the ToD (net ₹35,942 a year). Today's cost: energy and fixed charges equal at the approved rates. Landed cost on three routes and two sources equal (after defect 9). Settlement with a 3 MW stand-in (set-off on each month's ToD-category totals, 10% in kind, monthly, no cap, lapse), every month equal: set-off 49,01,600, banked 28,57,037, charge 2,85,704, withdrawn 6,68,400, lapsed 19,02,933, grid 0 kWh. Grid bill from the dispatch file within ₹1 hourly; replay identical; the rule book (4.23 solar, 4.77 wind, banking apart) equals the hand figures.

**Rules missing or doubtful.** Condition 15 ToD is missing from the approved rules (−₹1.10 lakh a year on the plan's dispatch); the register's ToD seasons are whole months, not the order's 15/16 June and 15/16 October; the register calls 18:00–22:00 "normal" though the monsoon charges it as peak, so the banking categories are doubtful; the ₹5.50 floor is missing; the voltage rebate (condition 13.2) is missing (not exercised at 11 kV); the subsidised industrial tariff ₹5.835/kVAh (Chapter 6, p. 232) is not a rule, so today's and avoided cost are ₹0.165/kVAh high for an eligible plant; the ₹750/kVA demand surcharge (condition 23.1.2) is missing; FPPAS (condition 8) and duty (condition 2) have no figure in the order; the 10% banking charge and the renewable cap at 100% of contract demand come from the register without citation (the cap binds: solar and wind come to exactly 2.0 MW); no additional surcharge is stated.

**Solver.** Hourly 28 s, 15-minute 59 s, both optimal, gap 0. Plan: rooftop 1 MW, solar 1.49 MW, wind 0.51 MW; ₹4.00 cr a year to ₹2.71 cr.

## Telangana — TGSPDCL HT-I(A), 33 kV, contract demand 3,000 kVA, two shifts, against the approved rules

The twelve hand bills total ₹11,28,82,811.40, from the TGERC order FY 2026-27 (document 0c84182f): demand ₹500/kVA on the recorded demand or 80% of contract demand if higher (p. 281, 293–294); energy ₹7.15/kVAh (p. 281); ToD +1.50 at 06:00–10:00 and 18:00–22:00, −0.50 at 10:00–18:00 (p. 281); customer charge ₹3,500 a month (p. 300). The minimum of 50 kVAh per kVA does not bind.

**Agreed.** Replay: energy with ToD all twelve months to the paisa; demand in the six months where the recorded demand is at least 80% of contract demand. Today's energy within ₹1.40 (whole-kVAh rounding on the bills). Landed cost on three routes and two sources equal. Settlement: no approved rule, so the app abstains (block-by-block netting, no banking, said in its notes), and its figures equal the hand figures to 0.002 kWh. Grid bill from the dispatch file within ₹1; replay identical; the rule book (4.92 / 5.42) equals the hand figures. The approved HT-I(A) rates and ToD agree with p. 281.

**Differences, all rule gaps.** The 80% billing-demand floor (4.27.5 / 4.28.1) is missing from the approved row: ₹3.25 lakh a year on these bills; in the plan the optimizer uses batteries (1.45 MW behind the meter, 1.50 MW at the plant) to cut demand below the floor, which would add ₹26.8 lakh a year (hourly; ₹25.2 lakh at 15 minutes), so the saving is overstated by about 7%. Missing too: the customer charge (₹42,000 a year), minimum energy (4.28.2), excess-demand charges (4.27.7), FSA (no figure), duty (Telangana Electricity Duty Act); no settlement, banking, open-access loss or additional-surcharge rule is approved (the TGERC open-access regulations are not in the rule book). Doubtful: the approved T&D charge 0.09 is the 33 kV row of 3.21.22, an input to the CSS formula; the aggregate in Table 3-43 is 0.21 at 33 kV, and the actual wheeling and transmission charges are in MYT orders not stored.

**Solver.** Hourly 57 s, 15-minute 80 s, both optimal, gap 0. Plan: rooftop 1 MW, solar 2.57 MW, wind 2.48 MW, batteries as above; ₹11.19 cr a year to ₹7.55 cr.

## Hand-worked landed cost, Gujarat, Punjab, Telangana

Gujarat (losses = (generation + 0.2352) × (1/((1 − 3.37%)(1 − 6.50%)) − 1); transmission = 3,858.96 / (24 × 1,000 × CUF) at 10 MW, CUF 23% solar, 30% wind):

| Route | Solar | Wind |
| --- | ---: | ---: |
| Captive (levelised 2.7692 / 3.3681, assumed) | 4.0744 | 4.5742 |
| Group captive (equity 0.25) | 4.6905 | 5.0808 |
| Third party (CSS 1.33, additional surcharge 0.99, SLDC 0.05) | 6.7605 | 7.1508 |

Punjab, without banking (kept share 0.98 × 0.9781 × 0.96 = 0.92019552; banking = (generation ÷ kept) × (1/0.9 − 1), shown apart):

| Route | Solar | Solar banking | Wind | Wind banking |
| --- | ---: | ---: | ---: | ---: |
| Captive (levelised 3.3522 / 3.6087, assumed) | 3.6429 | 0.4048 | 3.9217 | 0.4357 |
| Group captive (equity 0.25) | 3.6188 | 0.3743 | 4.1622 | 0.4347 |
| Third party (CSS 0.86) | 4.2288 | 0.3743 | 4.7722 | 0.4347 |

Telangana:

| Route | Solar | Wind |
| --- | ---: | ---: |
| Captive (levelised 2.8950 / 3.6087) + T&D 0.09 | 2.9850 | 3.6987 |
| Group captive (+ 0.09 + equity 0.25) | 3.4400 | 3.9400 |
| Third party (+ 0.09 + CSS 1.73) | 4.9200 | 5.4200 |


## Source documents

The Maharashtra orders were in the cached library. The KERC order (8e00613e) was fetched from kerc.karnataka.gov.in with certificate checks on: the site omits its intermediate certificate, so GlobalSign's GCC R46 OV TLS CA 2025 was taken from its AIA address, checked to chain to the system root, and passed as the CA file; the file's SHA-256 matches the document id.
