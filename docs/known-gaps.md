# Known gaps

What is not yet right, and the stage that fixes it.

| Gap | Fixed in |
| --- | --- |
| Public VM (`replan2`): no mail account, so sign-in links are minted by hand (`tools/dev/vm.sh exec api python -m ergplan.platform.signin EMAIL`) and handed over; a person cannot ask for their own link | When `ERGPLAN_SMTP_URL` is set (`tools/dev/set-mail.sh`) |
| Public VM: five regulation PDFs that approved rules cite are not on the server (UPERC CRE Regulations 2024, KERC combined order 2025, GERC/MGVCL FY 2026-27, PSERC FY 2026-27, TGERC FY 2026-27). Numbers citing them show the citation and say the page is not stored; `/documents` lists them for analysts | When the person sends the files from the Mac mini (`python -m ergplan.documents.missing` then lists none) |
| Public VM: the solve budget is still 800 s in the VM's `.env`. With "search 3" a five-component captive mix sizes in about 50 s standalone; lower the budget to the default after a week of runs under production load | Next |
| Public VM: backups are on the VM's own disk (`/data/backups`); nothing is copied off the machine | Set `BACKUP_BUCKET` (gs://…) for the backup service |
| Public VM: the password option is still offered on the sign-in page although the person asked for no passwords; nobody has one on this server | The person decides whether to remove it |
| A cited PDF that is not stored is said so on the monthly rule book and at the link's target; the other screens (landed cost, plan, audit) still draw the page chip, which then opens the "not yet stored here" page rather than the PDF | When a server runs without its library for long |
| ergPLAN 2: values from the State Regulation Master carry `register` refs (the analyst's citation, no stored page). Each should be replaced by a row cited to the order's page; until then the plan shows them as "register" | rulebook work, state by state |
| ergPLAN 2: where no retail schedule is approved, the consumer's own energy and fixed charge price the grid as one flat rate with the register's ToD surcharge and rebate; fuel surcharge, duty and slabs are only in it as far as the bill's energy charge includes them. The tariff is read from the connection at run time and is not pinned, so a replay after the consumer changes it differs | approve the state's tariff schedule |
| ergPLAN 2: the register's Rajasthan rule makes the banking cycle depend on renewable capacity against contract demand (annual below, monthly above); the rule imported is the monthly one | settlement schema: a capacity-conditional cycle |
| ergPLAN 2: Chhattisgarh's SLDC charge (Rs/day) and reactive-energy charge (paise/kVARh) are in the register in units the oa_charge schema has no basis for; they are not imported (the import prints them) | oa_charge schema |
| ergPLAN 2: the battery at the open-access plant is modelled at the meter: the energy it stores bears the plant's losses and charges as if delivered when generated, and it may not charge from the grid or from exchange purchases | a plant-side model with injection-point quantities |
| ergPLAN 2: generation curves for the states added from the register are normative shapes by region (`packages/profiles/cuf`), scaled to the CUF the consumer states; a developer's 15-minute profile is not yet an input | generation profile upload |
| ergPLAN 2: with remote access on, the sign-in cookie is https-only, so the local-network http address stops signing people in until `tools/dev/remote.sh off` | a second public URL |
| ergPLAN 2: `/settle` (the analyst's settlement screen) settles a cycle rule without the grid rates, so banked energy is drawn in the order peak, normal, off-peak rather than by rate; runs pass the rates | pass the rate cards to `/settle` |
| The 25-year view escalates the settled year; it does not re-optimise later years (the tariff order's ToD structure, the open-access charges and the regulations are held at the evaluation date's rules). A re-optimisation per year, or per regulatory period, would be a later stage | Later |
| At hourly resolution the billing demand is the month's hourly peak; DISCOMs integrate demand over 15 minutes, so demand charges today and with the plan are understated by the month's peak ratio (about 12 % on the NPCL example), and the Evaluate page's "today" (on the 15-minute profile) is a little higher than the report's. The run says so; 15 minutes is one click in the settings | Later (peak-ratio scaling of hourly demand) |
| Payback and IRR are the plan's as a whole; the return on the owned plant alone (rooftop, battery) would need a second optimisation without the open-access purchase | Later |
| The supply voltage is not printed on some bills (NPCL); the Evaluate page asks for it rather than assuming the category's usual voltage, so a bills-only evaluation still needs one typed field | By design |
| The independent reviewer is a tool script that imports `openai` directly (outside `ergplan/providers`); it is not app code and logs no `model_calls` row | By design |
| Seeded rulebooks are approved by `build-agent@ergplan`; a jouleWise analyst should review each in `/rulebooks` and re-approve under their own name (duplicate → approve) | Before client use |
| HV-2 **rural** schedule (7.5% rebate on the 11 kV urban base rate, no ToD) is not representable: `tariff_schedule` has no "rebate on base" field | Stage 3 |
| NPCL and UPPCL tariffs are "in force seven days after newspaper publication"; versions carry the order date (2 Jul 2026) because the publication date is not in the corpus | When the date is known |
| UP open-access **exemptions** not yet on the rows: captive users exempt from CSS (CRE Reg. 11.II), UP Solar Energy Policy 2022 wheeling/transmission exemptions (50% captive and third party, 100% sale to DISCOM; CRE Reg. 10.III table p. 14), embedded OA consumers and EHV consumers exempt from wheeling (on the rows as notes, condition `other`) | Stage 4 (exemption engine) |
| UP biomass banking proviso (excess over the ceiling sold at Rs 1/unit or the PPA rate) not modelled | Stage 5 |
| Maharashtra OA wheeling charge, OA losses, additional surcharge and banking charge not yet read (MERC MYT order Case 217/2024 and DOA Regulations) | Stage 4 |
| Only HT industrial and commercial categories are seeded (UP HV-2; MH HT I(A), HT II); LT industrial (UP LMV-6, MH LT V) not yet | Stage 2–3 as clients need them |
| No ISTS waiver, regulatory limits or net-metering rulebooks yet (CERC documents not in the corpus) | Stage 4 |
| Real bills: NPCL and MSEDCL pass end to end; two per DISCOM are still needed for PVVNL, MVVNL, DVVNL (files in `data/bills/` not yet run) and a second MSEDCL month | Stage 3 |
| Only RTM and DAM IEX files are present (`data/iex/`); GDAM, G-TAM, TAM and HP-DAM files are needed for "six IEX CSVs loaded" | When the files arrive |
| billx returned no ToD split for the two real bills read, and the synthetic PVVNL bill names its slots `Tod-1..3` without hours; such a split is not used (the month's total is, with a warning); the API accepts typed ToD hours (`MonthFacts.tod`) but step 1 has no editor for them yet. Mapping slot names to the order's windows needs either billx printing the hours or a per-DISCOM slot table | Stage 3 (bill replay checks the ToD energy charges) |
| Back-solving monthly units from a bill amount when only the amount is known needs the approved tariff (`site_intake` takes energy as given) | Stage 3 |
| A flat-topped shape cannot reach a recorded MD well above its plateau by stretching alone; the rest of the MD is placed as one 15-minute peak block (`peak_method: linear+peak_block`), which is realistic for MD but not for the hour around it | Measured data replaces it |
| Voltage is assumed from the category (UP HV-2 11 kV, MSEDCL HT 22 kV) when not stated; MSEDCL HT supply may be 11, 22 or 33 kV | Client confirms in step 1 |
| Months without a bill are extrapolated at the billed months' mean daily energy (no seasonality) | Stage 3 (seasonality from history when billx returns it) |
| Six-consumer landed-cost spreadsheets are drafts by the build agent (`tests/fixtures/landed_cost_up.csv`, `landed_cost_mh.csv`); jouleWise to approve or replace | Before client use |
| Maharashtra captive CSS exemption (EA 2003 s. 42(2) proviso; MERC DOA Regulations) not in the rulebook: a captive MSEDCL consumer is shown paying CSS | When the MERC DOA Regulations are added |
| UP Solar Energy Policy 2022 wheeling and transmission exemptions (50% captive and third-party, 100% sale to DISCOM; CRE Regs p. 14) not applied: the policy's validity period is not in the corpus | When the policy is added |
| Normative PPA prices, LCOE and LCOS inputs are assumptions (editable) | Client quotes |
| A year of real OA settlement statements (anonymised) needed in `data/settlements/` to check the simulator within 0.5% (Stage 5 DoD); the simulator is checked against hand-worked cases and properties until then | When supplied |
| One bank per consumer (the first matching settlement rule); mixed rules across sources share it or net without banking | When a client mixes rules |
| Sizing passes under-value genuinely surplus banked energy by the deposit guard (at most the spread between the best withdrawal rate after the charge and the deposit slot's rate) and take the lower carry-over ceiling: sizes lean small where banking matters; the reported numbers are exact (judge) | A sizing refinement loop |
| Sources of different set-off categories in block-level netting (UP): the rule draws the bank before bilateral supply in a block; the settlement pass maximises direct set-off first, so a captive + third-party mix with banking may fail re-settlement (it is then not shown) | When such a mix is needed |
| Capacity of a captive thermal plant is not sized (existing units are dispatched with on/off, ramp and minimum up/down times); unit commitment at 15 minutes for a year is slow — use the hourly preview for sites with thermal | Stage 7 if needed |
| Battery degradation, auxiliary consumption and a depth-of-discharge limit are not in the dispatch | Stage 7 |
| IEX purchases are priced per month and 15-minute block of the day (the mean clearing price over the month's days in the file), not day by day; a year of daily prices is averaged into twelve representative days | When day-to-day price swings matter for a client |
| The client's mix is one scenario per name ("Your mix"); a second mix needs another name (the API takes one; the page offers one) | When clients ask to compare mixes side by side |
| A mix of several sources at 15 minutes takes three to four minutes to size and settle (the hourly sizing LP over 8,760 hours is most of it); the hourly preview takes about a minute | When clients need faster answers (warm starts, a coarser sizing pass) |
| Land area does not bound any candidate (no on-site ground-mount source yet) | When one is added |
| Generation for settlement comes from normative CUF curves unless a 15-minute injection is supplied | Developer profiles |
| The audit shows one time-of-day slot's landed cost per source (the first; "slots differ" is shown when they do); a working's per-slot figures are set against that slot only | When a client's working is per slot |
| Runs made before 2 Nov 2026 have no stored chain: the audit shows their waterfall lines without rates or rules until the plan is run again | Next run of each plan |
| A scanned working has no text to ground figures in; its figures are typed against the chain by hand (the quote check then fails) | OCR for workings, if needed |
| The audit compares annual money lines and site energy, not month by month | When a client's working is monthly |
| Accepting a proposal needs the regulation registered as a source document; an analyst gives its id by hand (no search in the queue yet) | Next analyst UI pass |

## Named rule gaps used by the bill replay (Stage 3)

A replayed bill line that differs from the approved tariff is "explained" only by one of these ids (`apps/api/ergplan/engine/gaps.py`; a test keeps the two lists equal).

| Gap id | What is missing | Fixed in |
| --- | --- | --- |
| `fppca_monthly` | Fuel and power purchase adjustment (MSEDCL FAC, UP FPPAS) is notified monthly by the DISCOM; no monthly FPPCA rulebook | Stage 4 (monthly FPPCA family with the DISCOM notifications) |
| `electricity_duty` | Electricity duty is set by the state Electricity Duty Acts (UP; Maharashtra 7.5% of demand + energy + wheeling + ToD + FAC on the April 2026 bill), not by the tariff order; the Acts are not in the corpus | When the Acts are added to the corpus |
| `tax_on_sale` | Maharashtra tax on sale of electricity (paise per unit) | With the duty Acts |
| `load_factor_incentive` | Load-factor incentive bands (MERC) are in the order but not yet read into `tariff_schedule.lf_incentive` | Stage 4 |
| `prompt_payment_rebate` | Prompt-payment rebate terms are in the orders' general conditions, not yet read | Stage 4 |
| `bulk_consumption_rebate` | MSEDCL bulk and incremental consumption rebates | Stage 4 |
| `state_subsidy` | State industrial subsidies (Maharashtra textile and region-based) are government schemes outside the tariff order | Not modelled (client-specific; entered as stated) |
| `regulatory_discount` | NPCL's 10% regulatory discount on demand + energy, printed on the FY 2026-27 bills; the order provision is not yet located | Stage 4 |
| `settlement_rule` | No approved settlement rule for the source's ownership in the state (in UP, third-party open access: the approved rows, from the CRE Regulations 2024, cover captive and group captive only): set-off in the same 15-minute block, no banking | When the state's OA/GEOA banking regulation is read and approved |
| `tod_split_unknown` | The bill gives no energy by ToD slot, so the ToD adjustment cannot be recomputed | When billx returns the split |
| `billing_demand_rule` | The schedule row states no billing-demand floor (MSEDCL) while recorded demand is below contract | Stage 4 (MERC billing-demand clause) |
| `demand_proration` | NPCL prorates the monthly demand charge by days in the billing period (31 / (365/12)); the provision is not yet located | Stage 4 |
| `tariff_period` | No approved tariff covers the bill's dates: the FY 2025-26 orders (UPERC for NPCL and the UPPCL DISCOMs, MERC for MSEDCL before 1 Apr 2026) are not in the corpus | When those orders are added |
| `ists_charges` | ISTS transmission charges (CERC Sharing of ISTS Charges and Losses Regulations 2020) are not in the corpus | When the CERC documents are added |
| `ists_waiver` | The CERC ISTS waiver schedule by commissioning date is not in the corpus | When the CERC documents are added |
| `oa_wheeling` | No open-access wheeling charge for the DISCOM and voltage (Maharashtra not yet read) | Stage 4 follow-up (MERC MYT order) |
| `oa_losses` | No open-access distribution loss for the DISCOM (NPCL and Maharashtra not yet read) | Stage 4 follow-up |
| `oa_css` | No CSS for the category and voltage | As categories are added |
| `oa_additional_surcharge` | No additional surcharge row (Maharashtra not yet read) | Stage 4 follow-up |
| `iex_fees` | IEX transaction fee and trader margin (CERC) not in the corpus | When the CERC documents are added |
| `scheduled_energy` | A capacity charge cannot be spread without the source's capacity and CUF | Scenario input |
| `green_tariff` | DISCOM green-tariff premiums are not yet read into the tariff schedules | Stage 4 follow-up |
| `iex_prices` | GDAM, G-TAM, TAM and HP-DAM files not supplied (RTM and DAM are loaded) | When the files arrive |
| `regulatory_limits` | No `regulatory_limit` rulebook yet: the RE cap (% of contract demand), storage mandate, minimum OA capacity and minimum contract demand for OA need their regulations (GEOA Rules 2022, state GEOA regulations) in the corpus | When those documents are added |
| `unmodelled_line` | A miscellaneous line the bill prints (delay-payment surcharge, rounding, adjustments) | By design |
| Voice has not been tried end to end with a real microphone in this workspace (no audio device); the Realtime session minting, the tool relay and the tools are tested, one real minting call is marked external | First use on the Mac mini |
| Editable assumption chips on the live plan cover PPA prices and contract demand; other assumptions (capex, CUF, grid factor) are visible in the jouleWise view but not yet editable by the client | Stage 8 |
| What-if is an hourly check with sizes held; re-sizing on a changed assumption needs a new run (analyst or "Build my plan" after the change) | By design for speed |
| Explanations are in English only | When a client asks |
| Voice sessions and text chat in intake mode can be opened by anyone who reaches the site (plan mode needs a valid share token); each session costs OpenAI audio usage. Login, roles per endpoint and rate limits are Stage 8 (rule 12) | Stage 8 |
| Karnataka open access: CSS, additional surcharge (not levied) and each ESCOM's HT wheeling charge and HT technical loss are approved; not yet: KPTCL transmission charges and losses (separate order), banking (separate KERC orders), the captive CSS exemption | When the KPTCL transmission order and KERC banking orders are added |
| Punjab open access: CSS (Large Supply), wheeling (LTOA/MTOA per MW; NRSE within the State 2 % in kind) and T&D losses by voltage are approved; not yet: STOA wheeling, additional surcharge, PSTCL transmission charges, banking, the captive CSS exemption | When the PSERC OA regulations and PSTCL order are added |
| Telangana open access: CSS and the aggregate T&D charge per DISCOM are approved (FY 2026-27 order); not yet: additional surcharge for H2 FY 2026-27 (the order states H1's Rs 0.13/kWh only), open-access losses (the order's 'L' includes commercial losses), banking (TGERC OA regulations), STU/transmission charge as a separate line, the captive CSS exemption (Electricity Act s. 42(2)) | When the TGERC OA regulations and the H2 additional-surcharge order are added |
| Gujarat HTP-I is approved (slabs, excess rate, energy and ToD bands by billing demand, HV/EHV rebate); not yet: the graded power-factor adjustment (11.6), open-access charges (GERC OA order), the other HT categories | When needed / the GERC OA order is added |
| Where the energy or ToD rate depends on billing demand (Gujarat), the optimizer and the judge price every block at the band of the contract demand; a plan that moves billing demand across 500 or 2,500 kVA is not re-banded (the monthly bill replay is exact) | A band-aware judge if a client sits near a boundary |
| Rajasthan (the stored file is an HTML page) and Haryana (a scanned 7-page notice) need the actual order PDFs | When supplied |
| Andhra Pradesh HT-III(A) is approved from Table 11 (page 197); not yet: the minimum energy charge (50 kVAh per kVA of billing demand), open-access charges, other HT categories | When needed |
| Tamil Nadu: FY 2026-27 rates are the FY 2025-26 rates indexed by CPI from 1 July 2026; the indexed schedule is not in the corpus | When TNERC publishes or jouleWise supplies it |
| Punjab ToD (mid-month seasons, Rs 5.50/kVAh off-peak cap) cannot be expressed by `tod_slots`; PSPCL energy is priced flat | Schema change |
| The VM runs at `https://<ip>.sslip.io` until a jouleWise hostname is pointed at it; sign-in links are issued by the operator until SMTP is set | A hostname and an SMTP account |
| The Mac mini serves the LAN on plain http (`http://<the mini's address>`): no https, no public hostname, sign-in links issued by the operator until SMTP is set; the session cookie travels unencrypted on the office network | A public hostname (Cloudflare Tunnel or port forwarding) with TLS, and an SMTP account |
| The mini's address comes from DHCP; if it changes, rerun `tools/dev/mini-install.sh` (it updates `ERGPLAN_PUBLIC_URL`) or reserve the address in the router | Router setting |
| No rate limiting on sign-in-link requests; wrong-password throttling is per API process and in memory (resets on restart) | Before public launch |
| Ownership of ids sent in a request body is checked by each route (`owned_bills`, `owned_connection`), not centrally; a new body-id route must call them | When body-id routes multiply |
| LlamaParse (billx's third reader) is off on the server: LlamaIndex refused the key (401 in both regions). Until a working key is placed, a value only the OpenAI model reads (account number, energy kWh, some charges) stays unverified, and a figure both local readers misread the same way (a power factor without its decimal point) stays disputed for the client to confirm | A working `LLAMA_CLOUD_API_KEY` on the server |
| On PVVNL open-access adjustment bills billx's own arithmetic checks G08, G09 and G21 fail on the "Other Adjustment (FPPA Surcharges+Arrears)" and "Total Arrear" lines (how the layout nests them), so the bill is held for review even when every planning value is verified; the client accepts it on the hand-off screen | A billx profile change for the layout, measured on the team's golden set |
| The reading view replays billx's stored evidence step by step (each reader's readings, then agreement, then the checks); billx does not yet stream live reading events (4.2 section 9D.2), so while a file is read the page shows billx's job state and the elapsed time only | billx job events |
| billx's source is in `billx/` (the `readers-docling-llamaparse` branch, ahead of the team's GitLab `main`). The team merges it there after regenerating billx's contract examples on the Mac (they are compared with PaddleOCR's regions; on Linux two of them fail); until then the two copies differ | The billx team's merge |
| Bill search is keyword ranking (BM25) over billx's stored readings; a question-answering assistant over search and analytics (numbers as references to rows, billx 4.2 section 9C) is not built | Next |
| Recognition reads the open-access kind (captive, group captive, third party, exchange, GEOA) only from words in billx's stored line labels and slot names, not from every word printed on the bill; most bills do not print it, so the client states it on the intake review | When billx returns the bill's open-access type as a field |
| Approved settlement rules exist for UP and Maharashtra only. In Karnataka, Telangana, Punjab, Andhra Pradesh, Gujarat, Rajasthan, Tamil Nadu, Haryana, Himachal Pradesh and Assam the hand-off says "no approved settlement rules yet" and banking and set-off are not applied | As each state's OA and banking regulation is read and approved |
| State rule books: the captive exemption from the cross-subsidy and additional surcharges is cited to the statute by name (`register` ref); the Act and the Rules are not in the corpus, so no page opens | Next |
| State rule books: the PPA tariff per source and the group-captive equity cost (₹0.25/kWh) are ergPLAN's assumptions unless the analyst quotes a tariff; no per-state benchmark | Next |
| State rule books: the IEX settlement text is ergPLAN's model of exchange purchases (assumption refs), not cited to the exchange's rules or the DISCOM's open-access procedure; IEX landed cost needs the month's price CSVs uploaded | Next |
| State rule books: no saving is stated where no retail schedule is approved (Rajasthan, Madhya Pradesh, Bihar, Chhattisgarh, Haryana, Odisha, Uttarakhand, Mumbai's licensees); a client's own book uses its bill's tariff | Next |
| State rule books: generated by an analyst's action; nothing runs on the first of the month by itself, and nobody is notified that drafts await review | Next |
| State rule books: the file is Markdown; no PDF export | Next |
| State rule books: Gujarat's saving is understated (the schedule's energy charge excludes FPPPA); Chhattisgarh's register rows should be re-checked against the order (its standby rate had been read as a per-unit levy) | Next |
| `standby_charge` rows are held but standby supply (drawal when the open-access source is down) is not modelled anywhere | Next |
| State rule books: in ten states (AP, BR, CG, GJ, HR, MP, OD, RJ, TN, UK) no figure opens a stored page — they rest on the State Regulation Master's citations (order, table and page by name). Each cited order has to be registered in the document library and each value grounded on its page | Next |
| The 5 MVA Uttar Pradesh site at 15 minutes solves in about 48 s on the VM with solver path "search 3" (135 s before); to be confirmed on CI at the next push | 11 |
| No approved rule says whether electricity duty or FPPCA falls on open-access, captive or rooftop units in Uttar Pradesh (and most states); an assumption is in use and shown as one. It closes when an `electricity_duty_oa` row is approved (`docs/questions-for-review.md`, 10) | 14 |
| The assumed duty is charged on every delivered unit, so also on energy that later lapses, is taken as the banking charge in kind, or is lost in the plant's battery (the cautious side) | Next |
| Uttar Pradesh's FY 2025-26 order is not in the rulebook: bills for supply before 2 Jul 2026 show "no tariff in force" and are not rebuilt | 14 |
| The audit chain shows FPPCA and duty inside the rates and in its notes, not as money lines of their own | Next |
| UPPCL's slot names (TOD-1, TOD-2, TOD-3) have no hours in the rulebook; the client types the hours | Analyst |
| A named ToD slot that covers two windows (a "peak" in the morning and the evening) is not mapped from its name | Next |
| A profile built before 25 Nov 2026 keeps its months one late until the intake is rebuilt from the bills (`POST /intake/from-bills`, then `POST /sites`) | Next |
| Two bills that resolve to the same supply month: the last one silently replaces the first | Next |
| A bill that prints no power factor is taken at 1.0 for the profile's peak in kW | Next |
| In the third-party five-component case the two batteries cost exactly the same; which is chosen is the LP's tie-break | Next |
| The results page prints the solver's raw status word (`time_limit`) beside the note in words | Next |
| Month models are rebuilt in Pyomo for each dispatch pass (about 4 s of the Uttar Pradesh site's 48 s) | Next |
| The independent reviewer has not run since Stage 10: no OpenAI key on the VM. Stages are tagged `-built`, not `-done`, until it has | Person |
| The captive plant's benchmark capital and O&M are constants in the code (as the rooftop's are); a run pins the treatment, not the benchmark values | Next |
| An inter-state captive plant uses the consumer's state's CUF curve, so its levelised cost (about ₹3.2 in Uttar Pradesh) is above the ₹2.70 inter-state PPA benchmark | Stage 15 (generation profiles) |
| No what-if for a captive plant's capital cost (capacity, contract demand and off only) | Next |
| Group captive: the equity share is taken on the plant's whole capital cost; the plant's own debt share is not modelled | Person (questions 14) |
| In payback and IRR, a bought source's saving leaves out its share of the demand saving, the banking charge in ₹/kWh and the lapse payment | Next |
| A drawal charge set as a percentage of the price is zero on a captive plant in a run (no price) but taken on the levelised cost on its card; the card uses life-average energy and the run the first year's, about 6% apart for solar | Next |
| The rule book's markdown file does not print the route notes; the web view does | Next |
| Rate-limit counts are held in the API process and lost on restart; move them to the database if the API ever runs more than one process | Next |
| Behind the Cloudflare tunnel (`compose.remote.yaml`) every visitor has one address, so a lockout or rate limit there applies to all | Next |
| The default trusted-proxy list covers all private ranges; where the API port is published to a LAN, set `ERGPLAN_TRUSTED_PROXIES` to the compose subnet. Production does not publish the port | Person |
| `signin_attempts` is never pruned; hashes of the retired per-person passwords stay in `users` (inert) | Person (questions 17) |
| An administrator username that is an email address can also sign in by emailed link | Person |
| No browser test of the sign-in form or the weak-password banner | Next |
| Password sign-in has not been exercised on the live address: the two variables are not in `.env` | Person (questions 16) |
| Imported drafts carry no prose conditions (billing-demand floor, excess-demand multiple, surcharge and wheeling exemptions, additional surcharge, banking, limits): approving one as it stands over an approved version would lose those rows; an analyst merges | Analyst (questions 20) |
| Uttar Pradesh HV-2 66–132 kV row is missing from the imported drafts (the ARR Network's column heading is broken there) | ARR Network |
| Uttar Pradesh FY 2026-27 green tariff (₹0.34, para 8.3.11) is not sourced; state transmission charge and losses are not in the ARR Network's tables | Stage 12 |
| Uttar Pradesh bills with supply before 22 Nov 2025 need the FY 2024-25 order (ARR Network orders 63 and 61), not imported | Next round |
| The ARR importer does not map consumption slabs, per-BHP rates, subsidy columns, or schedules printed as running text (Uttar Pradesh HV-3, LMV-1, 2, 3, 5, 8, 11) | Next round |
| No screen for the import; it runs by command or `POST /regreader/arr-network` | Next |
| MSEDCL: the ARR Network holds only the stayed MYT order of 28 Mar 2025 and a barely read FY 2023-24 order; neither is imported, so MSEDCL bills before 1 Apr 2026 have no sourced tariff | ARR Network |
| Andhra Pradesh: nothing imported (FY 2026-27 has no tables at the source; FY 2025-26 tables sit under headings that are not categories) | ARR Network / next round |
| Telangana: ToD, surcharge, losses and transmission not imported; imported rows need the consumer's sub-schedule as rate block and read voltages as printed (nothing at 66 kV) | Next round / analyst |
| Punjab: the FY 2024-25 order is not at the source; ToD, wheeling and green tariff tables are not headed approved | ARR Network |
| Haryana, Tamil Nadu, Uttarakhand, Odisha: partial schedules only (Uttarakhand lacks RTS-5 industry; Odisha has no schedule); Tamil Nadu's 1 Jul 2026 revision is not sourced | ARR Network / next round |
| Madhya Pradesh HV-2, 3, 4, 6 are not imported: two energy rates by load factor and the tariff_schedule family holds one; the drafts carry the order dates, though the tariff takes effect seven days after publication | Next (schema and engine) |
| Gujarat: no previous-year order at the source; time-of-use adders, wheeling, surcharge and green tariff are in the order's text only; the draft says HTP-1 as the heading prints | Stage 12 / analyst |
| Bihar: the 33 to 400 kV industrial rows are misfiled at the source; its ToD draft is scoped by `consumer_kind`, which plan evaluation does not set, so the row is not picked up yet | ARR Network / next |
| Chhattisgarh HV-3 and HV-4 at 33 and 11 kV need the bill's load-factor block; Odisha's LT surcharge and wheeling rows come from the green-energy open-access table and carry no `source` scope; Uttarakhand's wheeling charge is the general one (embedded consumers pay less) | Analyst on approval |
| Mumbai licensees: EHV and HT are scoped by rate block as printed, not by voltage; surcharges, transmission and open-access wheeling by level are not sourced | Analyst / next round |
| Seven states at the source that are new to ergPLAN (Goa, Himachal Pradesh, Jharkhand, Puducherry, Assam, West Bengal, Delhi) are not imported | Next round |
| The ARR importer does not read consumption slabs, per-HP or per-day rates, ToD printed as absolute rates, or any rule stated in an order's prose | Next round / Stage 12 |
| Differences between order pages and register rows found by the import (Chhattisgarh, Odisha, Tamil Nadu, Bihar, Madhya Pradesh) are listed in `docs/stage-reports/stage-14.md`; no rule was changed | Analyst (questions 22) |
| The uploaded generation CSV itself is not kept (series and sha256 only); a profile is chosen per site, not per scenario or candidate; timestamps are taken as IST; the CUF bounds are assumptions; no test on a real developer file (PVsyst or SCADA export) | Next round / person |
| Land available bounds nothing (no ground-mounted plant at the site in the optimizer); shifts typed with the old inputs (`SiteIntake.shifts`) do not shape the profile, only the Stage 15 answer does; with a ToD split, a large load raises its hours inside its slot but the month's peak may fall in another | Next round |
| ~~A roof area typed with the inputs is read live by a run~~: closed in the second round (runs pin `pins.site`) | Closed 26 Nov |
| No way back to the synthesised profile once a measured file is uploaded; no browser test or navigation entry for the questions screen; under a stated expansion the cost comparison still calls the sized year "today" (a note says otherwise) | Next round / person |
| Transition plan: constrained runs with both batteries can end on the time limit (shown as not proven); the share target is held in the hourly sizing pass only; "renewable share" is the share not drawn from the DISCOM; phases are ordered, not dated; no rule family holds procedures (open-access application, net-metering application, captive tests), so those steps are assumptions; the exchange phase is a stub | Next round / analyst |
| No state's open access regulations or SLDC procedure are stored, so every exchange verdict is "not yet sourced" and no plan offers IEX | Analyst / person (obtain the documents) |
| The monthly rule book and the landed-cost screen show exchange landed costs without the verdict; `/mix/options` without a connection does not apply it | Stage 19 |
| Contract-demand reduction and standby charges under an exchange rule are words only; no what-if for the deviation share; no analyst screen for the exchange sandbox; the exchange reader prompt has never been run; one RTM block of the copied year was filled by the builder from a neighbouring day, which one is not recorded | Next round / tenderos2 |
| The MCP connector has not been tried in claude.ai or Claude Code against the live server: password sign-in is off there until the administrator's two variables are in `.env` | Person |
| ~~`mcp_clients` and `mcp_tokens` are never pruned~~: closed in the second round | Closed 26 Nov |
| MCP results carry the screens' full models (`tools/list` is about 170 KB of schemas; plan and run summaries are large) | Next round, if Claude struggles |
| Maharashtra (MSEDCL): the billing-demand floor (MYT p. 608), open-access wheeling (Case 75 p. 83) and losses (p. 106) are missing; the register's banking matrix bars normal-slot banking from the solar hours, which the order allows (p. 79); the peak surcharge 20% or 25% is doubtful; tax on sale is not modelled; the 8% banking charge and the banking limit rest on the register; transmission and additional surcharge are not in the corpus | Analyst (questions 32) |
| Karnataka (BESCOM): the 90% billing-demand floor and rounding up (p. 561) are missing; banking is register-only and the order defers it to regulations not in the corpus; the power-factor surcharge and high-voltage rebate are not in the schedule; KPTCL transmission is not in the corpus | Analyst (questions 32) |
| Gujarat (MGVCL): the base FPPAS ₹2.52 (p. 137) is not in the approved schedule; the power-factor adjustment is not seeded; open-access loss, SLDC, transmission and additional surcharge rest on the register without a stored source; the settlement rule's 30% cap has no citation | Analyst |
| Punjab (PSPCL): condition 15 ToD, its 15/16 June and October seasons and the ₹5.50 floor are missing; the subsidised industrial tariff ₹5.835/kVAh, the voltage rebate and the ₹750/kVA demand surcharge are missing; the banking charge and the cap at contract demand rest on the register; the stored order PDF is not on the VM | Analyst (questions 32) |
| Telangana (TGSPDCL): the 80% billing-demand floor, the customer charge, minimum energy and excess-demand charges are missing (the plan's saving is overstated about 7% as batteries cut demand below the floor); no settlement, banking, open-access loss or additional surcharge is approved; the T&D charge 0.09 is doubtful | Analyst (questions 32) |
| Plan demand charges ignore billing-demand floors in every state that has one (₹10 to ₹27 lakh a year on the test sites) | Next round (engine) |
| Tax on sale (Maharashtra) and state duty acts are in no part of today's cost; the rule book's landed cost leaves out the banking upper bound the landed-cost screen includes | Next round |
| ~~A sizing pass with no feasible solution in time fails the run~~: closed in the second round (it falls back to today's supply, labelled); a time-limited or fallback run may replay differently on a faster machine | Closed 26 Nov / by design |
| The calculator: no approved rule in any state states a minimum contract demand for open access (the bar is tested on fixtures only); a signed-in user cannot save a calculation; duty and additional surcharge for a reference consumer are "not yet sourced" | Analyst / next round |
| With `ERGPLAN_OPEN_ACCESS` on, anyone with the address can upload bills, see every client's plans, edit drafts and run sizings (approving still needs a named sign-in); rate limits on sign-in and bill upload still apply | Person (switch it off in `.env`) |
| Himachal Pradesh: Small and Medium Industrial not imported (the load column is read as slabs); peak-load charges, wheeling, surcharge and losses not imported (tables broken at the source) | Next round / analyst |
| Jharkhand: voltage rebates and billing-demand floors not imported; FY 2025-26 open-access charges not imported | Next round / analyst |
| Assam: HT-IV and LT-V not imported (misfiled headings at the source); HT-II Option-1 ToD and FY 2023-24 ToD not imported; the surcharge table prints no unit; FY 2024-25 and FY 2025-26 orders not at the source; order 177's end date is a reading | Analyst / ARR Network |
| Goa: the imported additional surcharge is the petitioner's (0.74 / 0.68; the Commission approves 0.82 / 0.77, p. 209); Goa and Puducherry slabbed categories, ToD, surcharge and wheeling not imported; FY 2024-25 and FY 2027-28 onward not imported | Analyst (questions 36) |
| West Bengal (gists only at the source) and Delhi (FY 2021-22 orders only) are not imported | ARR Network / person |
| Stage 12: Gujarat's cited orders could not be obtained (gercin.org sends no intermediate certificate), Haryana's additional-surcharge order neither (herc.gov.in did not answer); Rajasthan's register rows cite nothing; 65 listed figures still rest on the register; on the live server Chhattisgarh's stored order copy differs from the one read in the build, so only 1 of its 11 figures is grounded there | Person (the orders) / next round |
| Stage 12's eight drafts need the analyst's approval, state by state; per-DISCOM surcharge and wheeling (Bihar, Odisha) and voltage-banded surcharge (Chhattisgarh) are not in the rule family as imported | Analyst |
| Bihar's ToD draft is now picked up (the platform sets `consumer_kind`); approved Odisha and Chhattisgarh open-access rows scoped by `consumer_kind` now apply to new plans in those states, as their scope says | Closed 26 Nov |
| The DISCOM tariffs tab shows `min_units_per_kva_month` in kWh; `core/units.py` has no kWh-per-kVA unit | Next round |
