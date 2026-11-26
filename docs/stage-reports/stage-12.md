# Stage 12 — Every register figure opens its page

Built on 26 Nov 2026 on the VM, in the brief's second round, from `stage-12-start` (2d4db05), by one worker in its own worktree; I reviewed and merged it (`14cfcef`) and ran it on the live server.

**Status: built, tagged `stage-12-built`.** Everything is a draft; nothing was approved (the analyst's act). The reviewer could not run before the OpenAI key is typed in (questions 1).

## What was built

- `regreader/register_pages.py`, the command `python -m ergplan.regreader.register_pages [--state XX] [--dry-run] [--fetch DIR] [--store DIR] [--json PATH]`. For every approved version with a `register` row it finds the order the row cites, reads the cited page and the two either side in the text layer (as the order prints the page number and as the PDF numbers it), and takes the line printing the figure as the quote. The quote must pass the grounding check and print the figure itself; a figure printed on more than one line is refused, not guessed. It re-issues the version as a draft (origin `register_pages`) through `rulebook.service`, with `order` refs where a figure is grounded and per-voltage refs where only some are; **the figure never changes**. It runs the approved version's own golden cases against the draft. No model is called. A second run says "unchanged".
- `packages/rulebooks/register/orders.yaml`: the catalogue of every order the register cites (title, regulator, date, regulator link, PDFs by SHA-256, or why not obtained) and, from reading the stored pages, the page or anchor text where a citation names no page. No figure is written there.
- `--fetch` downloads a missing order from the regulator with certificate checks on and keeps it only on a SHA-256 match; `--store` registers matching PDFs. Twelve orders were fetched from the regulators' own sites this way.
- Tests: `tests/regreader/test_register_pages.py` (9) on real lines of the Bihar FY 2026-27 order.

## Figures that open a stored page, per state

`statebook.trace()`, November 2026, reference consumers, summed over each state's DISCOM books: before, and with the drafts in place (a projection inside the command's own process; nothing reaches a client).

| State | Grounded / register figures | Page: before → with drafts | Register: before → with drafts |
| --- | --- | --- | --- |
| Andhra Pradesh | 5/13 | 0 → 432 | 903 → 471 |
| Bihar | 8/11 | 0 → 192 | 306 → 114 |
| Chhattisgarh | 7/11 on the worker's library; 1/11 on the live server (below) | 0 → 48 | 129 → 81 |
| Haryana | 6/11 | 0 → 96 | 402 → 306 |
| Madhya Pradesh | 5/12 | 0 → 360 | 483 → 123 |
| Odisha | 8/13 | 0 → 384 | 804 → 420 |
| Tamil Nadu | 4/13 | 0 → 72 | 270 → 198 |
| Uttarakhand | 6/8 | 0 → 72 | 121 → 49 |
| Gujarat | 0/14 | 20 → 20 | 640 → 640 |
| Rajasthan | 0/8 | 0 → 0 | 339 → 339 |

Karnataka, Maharashtra and Punjab have no register open-access rows (only uncited ToD, settlement or limit rows), so they are unchanged. All 32 ToD, settlement and regulatory-limit rows, and Tamil Nadu's tariff-schedule row, stay on the register: the register entered them without citing an order page.

## Rows kept on the register, by reason

- **No citation in the register**: Andhra Pradesh, Madhya Pradesh, Uttarakhand transmission and wheeling losses; Chhattisgarh transmission charge; Gujarat SLDC and losses; Haryana SLDC, losses and transmission; Odisha duty and transmission loss; Tamil Nadu SLDC and losses; all eight Rajasthan figures. Where the figure was found on the state's own stored order (Bihar, Chhattisgarh and Odisha losses, Chhattisgarh standby, Uttarakhand wheeling loss), the catalogue names the page and it is grounded.
- **The register's figure is a sum or quotient, not printed**: Andhra Pradesh SLDC 4017.07 (2465.57 + 18618/12), Bihar 11 kV wheeling 1.1 (0.53 + 0.57), Odisha SLDC 766.348, Uttarakhand SLDC 4516.42 (54197/12).
- **A nil not printed** (the order simply does not charge it): EHV wheeling in Andhra Pradesh, Bihar, Chhattisgarh, Madhya Pradesh, Odisha and Tamil Nadu. Uttarakhand's "not levied at 132 kV and above" is printed and grounded.
- **Absence, not a figure**: Chhattisgarh additional surcharge; Madhya Pradesh SLDC 0 for renewable energy.
- **Ambiguous**: Tamil Nadu transmission 6035.61, printed on three lines.
- **The rule's figure differs from the cited text**: Tamil Nadu's reactive charge is held as 0; the order says 17.50 paise/kVArh, which the rule family has no basis for.
- **The source is not an order**: Andhra Pradesh duty (a slide), Andhra Pradesh and Tamil Nadu standby (GEOA regulations not in the library), Tamil Nadu additional surcharge (a news report), Tamil Nadu tax (a state Act).
- **Order not obtained**: every Gujarat figure (the DGVCL tariff order, additional-surcharge order 05/2026 and GETCO MYT order: gercin.org fails certificate verification, sending no intermediate certificate) and Haryana's additional surcharge 1.37 (herc.gov.in did not answer from the VM).

## For the analyst: differences found (no figure was changed)

- Bihar: the surcharge is ₹1.77 NBPDCL and ₹1.68 SBPDCL, and 33 kV wheeling ₹0.52 / ₹0.53; the register applies SBPDCL's state-wide.
- Chhattisgarh: the surcharge is ₹1.23 at 132 kV and above; the register holds ₹1.43 at every voltage.
- Odisha: surcharge and wheeling are set per DISCOM (Tables 45 and 46); the register uses TPCODL's state-wide.
- Madhya Pradesh: the surcharge ₹1.49 is from Table 96, which the order heads "as per above Illustrations".
- Tamil Nadu: Table 6-8 prints its unit as "Rs. Crores"; the register reads 6035.61 as Rs/MW/day.
- Andhra Pradesh: the surcharge row is printed "HT II(A) Industry"; the register says HT-III(A) (the figure is the same).

## On the live server

Redeployed at `14cfcef`; the twelve fetched orders (SHA-256 matched to the catalogue) were stored with `--store`; `register_pages` proposed eight drafts (Andhra Pradesh, Bihar, Chhattisgarh, Haryana, Madhya Pradesh, Odisha, Tamil Nadu, Uttarakhand), every one's golden cases green; a second run said "unchanged" for all eight; the 72 approved versions and their 200 rules are unchanged (fingerprint before and after). Live drafts now: 101. On the live server Chhattisgarh's figures were read on the stored copy of its order, whose pages differ from the copy the worker read, so only 1 of 11 was grounded and the rest keep the register ref (listed in known gaps).

## Checked (done when)

| Check | Result |
| --- | --- |
| For every state, the rule book's count of figures that open a stored page, before and after | The table above |
| Every row that could not be grounded is listed with the reason | Above, and in the command's output and `--json` |
| Golden tests pass for each re-issued version | Andhra Pradesh 9/9, Bihar 7/7, Chhattisgarh 7/7, Haryana 7/7, Madhya Pradesh 7/7, Odisha 8/8, Tamil Nadu 10/10, Uttarakhand 7/7: the approved versions' own cases, unscoped |
| An analyst approves each state's new version in the app | Left for your analyst (never the builder's act) |
| Watcher green | WATCHER |
| Reviewer clean | REVIEWER |

## Readings

`docs/questions-for-review.md`, 37.

## Cost

- Application model calls: none. Builder: Claude Code on the subscription; one worker subagent and my review.
