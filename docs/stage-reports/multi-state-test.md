# Multi-state test — the savings logic and the sizing on several states

Stage 17, 26 Nov 2026, on the VM. Brief of 25 Nov 2026, part F. For each state, what `tests/e2e/test_up_assumed.py` does for Uttar Pradesh, as a slow test that asserts: twelve bills worked by hand from the state's own order; bill replay against them; today's cost; landed cost for solar and wind on each route against the order's own figures; settlement after banking, with the energy balance closed by hand; an optimizer run with five components at hourly blocks and one at 15 minutes; the grid bill recomputed from the dispatch file; replay identical; the month's rule book.

A difference is a defect fixed (with a test) or a rule gap listed, never a widened tolerance. Where a listed gap makes the app's figure differ from the order's, the test asserts the app's figure with the gap named beside it, and works the order's figure too. Nothing was approved.

Levies that no order states (fuel adjustment, duty or tax as a bill prints them) are typed as each bill would print them and marked as assumptions in the tests.

## Defects found and fixed

| # | Defect | Where | Effect before the fix | Test |
| --- | --- | --- | --- | --- |
| 1 | A retail wheeling charge alone was read as open access | `intake/recognise.py` | MSEDCL bills every unit with a wheeling charge (Case 75/2025 p. 100), so the Maharashtra site was taken for an open-access consumer | `tests/intake/test_recognise.py` (two) |
| 2 | With no power factor printed, today's demand was overstated by the kVAh factor | `intake/site_intake._bill_power_factor` | The profile peaked at MD × 1 while the avoided cost divided by kWh/kVAh: +3.1% on Maharashtra's demand (₹1,27,45,362 against ₹1,23,63,000). The power factor is now the bill's kWh/kVAh, cited to the bill. **This also lowers Uttar Pradesh's today's demand, by about 4.2%** | `test_bills_and_site`, `test_api_intake` (now MD × kWh/kVAh) |
| 3 | ToD set in rupees was read as no ToD | `engine/tariff_eval.slot_classes`, `engine/replay.tod_units` | KERC's +100 / 0 / +100 / −100 paise (p. 568) counted as 0%: every Karnataka slot normal, the ToD settlement netted as one slot, and the bill replay charged the rebate zone as a surcharge (April ₹1,18,20,100 against ₹1,07,27,252) | `test_tariff_eval.py` (parametrised), `test_replay_and_avoided.py` |
| 4 | The dispatch file rounded DISCOM kWh to 0.001 a block | `platform/runs.dispatch_blob` | The grid bill worked from the 15-minute file missed the run's by ₹5.22 (0.43 kWh); now to 0.000001 kWh | `tests/platform/test_api_evaluate.py` |

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

Build-up as the engine documents it: (generation + injection charges) ÷ delivered share, plus drawal charges, plus the banking charge on all of those × 8/92 (as if every unit were banked), plus duty at the bills' rate, plus the equity cost ÷ delivered share. Assumed: PPA solar ₹3.10, wind ₹3.60; captive plant ₹42,000/kW solar and ₹68,000/kW wind, O&M 1.5% and 2%, 25 years, 10%; CUF Maharashtra 21% / 28%, Karnataka 20% / 30%; 26% equity on the plant's capital.

Maharashtra (duty 0.8415; the order's total adds the missing wheeling ₹0.81 and losses ÷ 0.894849):

| Route, source | Lines (approved rule book) | Total | Without banking | Order's total |
| --- | --- | ---: | ---: | ---: |
| Captive, solar | LCOE 3.0329 + bank 0.2637 + duty | 4.1382 | 3.8745 | 5.5095 |
| Group captive, solar | 3.10 + bank 0.2696 + duty + equity 0.6941 | 4.9052 | 4.6356 | 6.3666 |
| Third party, solar | 3.10 + CSS 2.11 + bank 0.4530 + duty | 6.5046 | 6.0515 | 7.8845 |
| Captive, wind | LCOE 3.6087 + bank 0.3138 + duty | 4.7641 | 4.4503 | 6.2089 |
| Group captive, wind | 3.60 + bank 0.3130 + duty + equity 0.7941 | 5.5487 | 5.2356 | 7.0857 |
| Third party, wind | 3.60 + 2.11 + bank 0.4965 + duty | 7.0481 | 6.5515 | 8.4918 |

Karnataka (duty 0.6438; the approved rule book and the order agree):

| Route, source | Lines | Total | Without banking |
| --- | --- | ---: | ---: |
| Captive, solar | LCOE 3.1846 + wheeling 0.29 + loss 0.0986 + bank 0.3107 + duty | 4.5277 | 4.2170 |
| Group captive, solar | 3.10 + 0.29 + 0.0962 + bank 0.3031 + duty + equity 0.7494 | 5.1826 | 4.8795 |
| Third party, solar | 3.10 + 0.29 + 0.0962 + CSS 1.86 + bank 0.4649 + duty | 6.4549 | 5.9900 |
| Captive, wind | LCOE 3.3681 + 0.29 + 0.1038 + bank 0.3271 + duty | 4.7329 | 4.4058 |
| Group captive, wind | 3.60 + 0.29 + 0.1104 + bank 0.3479 + duty + equity 0.7622 | 5.7543 | 5.4064 |
| Third party, wind | 3.60 + 0.29 + 0.1104 + 1.86 + bank 0.5096 + duty | 7.0138 | 6.5042 |

<!-- GJ PB TS -->

## Source documents

The Maharashtra orders were in the cached library. The KERC order (8e00613e) was fetched from kerc.karnataka.gov.in with certificate checks on: the site omits its intermediate certificate, so GlobalSign's GCC R46 OV TLS CA 2025 was taken from its AIA address, checked to chain to the system root, and passed as the CA file; the file's SHA-256 matches the document id.
