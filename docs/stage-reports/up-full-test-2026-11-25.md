# Full test on assumed Uttar Pradesh bills — 25 Nov 2026

Run on the build machine at commit `main` + `search 2` solver settings, through the API
(`apps/api/tests/e2e/test_up_assumed.py`, run with `UP_OUT=<folder> pytest -m slow`). Three complete runs; every result
below was the same in each.

## The case

- PVVNL, HV-2 urban, 33 kV, contract demand 5,000 kVA, recorded demand 3,250 kVA, two shifts, 22.6 GWh a year.
- Twelve monthly bills, November 2025 to October 2026, **worked by hand from the FY 2026-27 order** (₹6.80/kVAh,
  ₹290/kVA on 75% of contract demand, ToD ±15% in the order's summer and winter windows, FPPCA ₹0.18/kVAh, duty
  ₹0.50/kVAh). Year's bill by hand: ₹19.15 crore.
- The bills reach the app as billx readings from a stand-in reader. **The real billx did not read anything in this test.**
- Rules in force on 1 November 2026; benchmark PPA tariffs (solar ₹3.10, wind ₹3.60); no IEX prices loaded.

## What agreed with the hand calculation

| Step | App | By hand | Difference |
| --- | --- | --- | --- |
| Bill replay, July to October 2026: demand charge | ₹10,87,500 a month | ₹10,87,500 | 0 |
| Bill replay, August 2026: energy and ToD | ₹1,41,69,352.44 | ₹1,41,69,352.03 | ₹0.41 |
| Bill replay, October 2026 (winter windows): energy and ToD | ₹1,34,60,884.92 | ₹1,34,60,884.43 | ₹0.49 |
| Grid rate per kWh by slot (kVAh billing, bill's kVAh/kWh 1.0438) | 7.0978 / 8.1625 / 6.0332 | (6.80, +1.02, −1.02) × 1.0438 | 0 |
| Year's energy charge at the tariff | ₹16,23,78,191 | ₹16,23,80,780 | ₹2,588 (0.002%) |
| Load profile, each month's kWh and ToD split | matches the bills | — | ≤ 1 kWh a month |
| Landed cost, open-access solar, third party | ₹5.1104 | 3.10 + 0.3075 + 0.9318 + 0.2811 + 0.49 | 0 |
| Losses used | 6.478% gross-up | 3.18% transmission, 3.00% distribution at 33 kV | 0 |
| Settlement, 6 MW captive solar: delivered share | 0.939154 | (1 − 0.0318)(1 − 0.03) | 0 |
| Settlement: banking charge in kind | 79,623 kWh | 8% of the 9,95,291 kWh surplus | 0 |
| Settlement: energy balance (set-off + withdrawn + grid) | 2,26,06,226 kWh | the year's consumption | < 1 kWh |
| Optimizer (hourly, five components, captive): energy balance | closes | — | 1 kWh |
| Optimizer: open-access energy cost | ₹2.08 cr solar, ₹5.88 cr wind | delivered kWh × landed cost | < ₹2 |
| Optimizer: grid bill recomputed from the hourly dispatch file | ₹4,01,58,761.88 | ₹4,01,58,761.24 | ₹0.64 |
| Optimizer: emissions before / after | 16,050 t / 3,945 t | kWh × 0.71 | 0 |
| Run replay | identical to the paisa | — | — |
| Client rule book (generated, approved, file) | figures equal the landed-cost screen | — | 0 |

Results of the recommended plan (captive): rooftop 1.00 MW (the cap given), open-access solar 2.73 MW, wind 4.62 MW,
no battery; grid purchase falls from 22.6 to 5.56 GWh; year's cost ₹17.54 cr to ₹13.78 cr. Third party: solar 1.69 MW,
wind 4.89 MW, a 0.35 MW battery at the plant; ₹14.96 cr.

## Defects and gaps found

| # | Weight | Finding |
| --- | --- | --- |
| 1 | High | **Consumption lands one month late.** A bill's energy is filed under the bill month (the month after the supply period): June 2026's units (the year's peak) sit in July, and March's winter ToD split is applied to April's summer windows. Solar and wind are matched against a shifted year. `intake.site_intake` should take the month from the supply period. |
| 2 | High | **Captive open-access plant has no capital.** With ownership "captive" the plant is priced at the benchmark PPA tariff with the surcharges exempt and no capital or equity cost, and payback (1.0 year) and IRR (113%) are worked on the rooftop's ₹4 crore alone against the whole plan's saving. The run's note says so; the headline still misleads. Group captive's ₹0.25/kWh equity line exists only in the rule book and landed-cost screen. |
| 3 | Medium | **FPPCA and electricity duty are outside both sides of the comparison** (₹1.60 crore a year on these bills, 8.4%). "Today's cost" shows ₹17.54 cr against bills of ₹19.15 cr. Whether duty and FPPCA fall on open-access units is not modelled, so the saving per unit can be off in either direction. The client's own bills print both rates. |
| 4 | Medium | **Bills before 2 July 2026 are not replayed but are marked "explained".** Only the FY 2026-27 order is in the rulebook; eight of twelve bills show no computed total and a difference equal to the whole bill, under a status that reads as success. The FY 2025-26 order is needed, and the status should say "no tariff in force for this date". |
| 5 | Medium | **The hourly five-component captive run stops on its time limit**: 173 seconds, optimality gap 1.46% (about ₹20 lakh a year of possible improvement), status `time_limit` in the run record. The same with the previous solver settings. The third-party case solves to optimum in 44 seconds. |
| 6 | Medium | **Third-party open access in Uttar Pradesh has no settlement rule**: no banking, surplus (9.95 lakh kWh on 6 MW) is lost. The approved rule is read as covering captive plants only. To be confirmed by an analyst against the CRE Regulations 2024. |
| 7 | Medium | **No additional surcharge row for the UP DISCOMs**: third-party landed cost may be understated. |
| 8 | Low | Landed-cost card for "battery at the open-access plant" is the behind-the-meter battery's (charged from the grid in the cheapest slot), which is not how the optimizer treats it. |
| 9 | Low | The synthesised profile peaks at 3,314 kVA in the highest month against a recorded 3,250 (2% over). |
| 10 | Low | The profile year is labelled April 2025 to March 2026 for bills of November 2025 to October 2026. |
| 11 | Low | UPPCL's own slot labels (TOD-1, TOD-2, TOD-3) are not recognised; the ToD split is dropped until the hours are typed. Seen with the fixture's labels; this test then used labels that carry the hours. |
| 12 | Low | The rule book prints "33.000 kV" and "5000.000 kVA". |

## Not covered by this test

The real billx reader on a real bill; the screens for this case (the browser journeys cover Rajasthan and Maharashtra);
a 15-minute run of this case (CI's Uttar Pradesh site covers one and is over its two-minute target); IEX sources; voice;
PDF export.
