# Progress on the brief of 25 Nov 2026

One line per stage. Times are UTC. Questions and the readings taken are in `docs/questions-for-review.md`; detail is in `docs/stage-reports/`.

| Stage | Brief part | State | Notes |
| --- | --- | --- | --- |
| 11 | A.1 | Built 25 Nov, tagged `stage-11-built` | Live at `https://ergplan.joulewise.tech`. Not `-done`: reviewer and accepted billx read need the OpenAI keys; reboot test left undone (questions 1, 2). Report: `docs/stage-reports/stage-11.md` |
| 11 | A.2 full-test findings | Done 25 Nov 20:15 | Findings 1, 2, 3, 4, 5, 8 to 12 fixed, each with a test (`0b6bbfd`, `f6b8c03`); 6 and 7 are with the analyst (questions 5, 6) |
| 11 | A.3 rerun | Done 25 Nov | Uttar Pradesh test, slow tests and CI read; the figure-by-figure table is in the stage report |
| 12 | — | Waits for the second round | Register figures to stored pages (questions 26) |
| 13 | B | Built 25 Nov, tagged `stage-13-built` | Administrator sign-in built and tested; **switched off on the live server** until the two variables are typed into `.env` (questions 16). Report: `docs/stage-reports/stage-13.md` |
| 14 | C | Built 26 Nov, tagged `stage-14-built` | 60 draft versions from 24 orders in twelve states, in the live database, none approved (the analyst's act); Andhra Pradesh and MSEDCL not imported (questions 19 to 25). Report: `docs/stage-reports/stage-14.md` |
| 15 | D | Built 26 Nov, tagged `stage-15-built` | Generation profiles, the questions the bills do not answer, the transition plan (UP to 50%: rooftop 5,000 kW, then captive wind 4,610 kW, ₹5.19 cr a year saved). Questions 27 to 29. Report: `docs/stage-reports/stage-15.md` |
| 16 | E | In progress since 26 Nov 10:50 | One worker: `/mcp` with OAuth backed by the administrator account |
| 17 | F | In progress since 26 Nov 10:50 | Two workers: Maharashtra and Karnataka; Gujarat, Punjab and Telangana |
| 18 | G | Built 26 Nov, tagged `stage-18-built` | IEX prices from `Ergplan/tenderos2@23d0afc` loaded on the live server; exchange drafts for 15 states; no stored regulation says whether the exchange may be used, so every verdict is "not yet sourced" (questions 30). Report: `docs/stage-reports/stage-18.md` |
| 19 | H | Not started | Landed-cost calculator |

To resume after an interruption: type "continue the brief".
