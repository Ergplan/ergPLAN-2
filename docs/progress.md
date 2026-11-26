# Progress on the brief of 25 Nov 2026

One line per stage. Times are UTC. Questions and the readings taken are in `docs/questions-for-review.md`; detail is in `docs/stage-reports/`.

| Stage | Brief part | State | Notes |
| --- | --- | --- | --- |
| 11 | A.1 | Built 25 Nov, tagged `stage-11-built` | Live at `https://ergplan.joulewise.tech`. Not `-done`: reviewer and accepted billx read need the OpenAI keys; reboot test left undone (questions 1, 2). Report: `docs/stage-reports/stage-11.md` |
| 11 | A.2 full-test findings | Done 25 Nov 20:15 | Findings 1, 2, 3, 4, 5, 8 to 12 fixed, each with a test (`0b6bbfd`, `f6b8c03`); 6 and 7 are with the analyst (questions 5, 6) |
| 11 | A.3 rerun | Done 25 Nov | Uttar Pradesh test, slow tests and CI read; the figure-by-figure table is in the stage report |
| 12 | — | Second round, in progress since 26 Nov 14:40 | One worker: register figures to stored pages, as drafts |
| 13 | B | Built 25 Nov, tagged `stage-13-built` | Administrator sign-in built and tested; **switched off on the live server** until the two variables are typed into `.env` (questions 16). Report: `docs/stage-reports/stage-13.md` |
| 14 | C | Built 26 Nov, tagged `stage-14-built` | 60 draft versions from 24 orders in twelve states, in the live database, none approved (the analyst's act); Andhra Pradesh and MSEDCL not imported (questions 19 to 25). Report: `docs/stage-reports/stage-14.md` |
| 15 | D | Built 26 Nov, tagged `stage-15-built` | Generation profiles, the questions the bills do not answer, the transition plan (UP to 50%: rooftop 5,000 kW, then captive wind 4,610 kW, ₹5.19 cr a year saved). Questions 27 to 29. Report: `docs/stage-reports/stage-15.md` |
| 16 | E | Built 26 Nov, tagged `stage-16-built` | `/mcp` with OAuth backed by the administrator account, live; refuses everyone until the two admin variables are in `.env`. Connector address: `https://ergplan.joulewise.tech/mcp` (questions 31). Report: `docs/stage-reports/stage-16.md` |
| 17 | F | Built 26 Nov, tagged `stage-17-built` | Five state tests (MH, KA, GJ, PB, TS) pass in the slow suite; nine defects fixed; rule gaps for the analyst (questions 32, 33). Report: `docs/stage-reports/multi-state-test.md`, `stage-17.md` |
| 18 | G | Built 26 Nov, tagged `stage-18-built` | IEX prices from `Ergplan/tenderos2@23d0afc` loaded on the live server; exchange drafts for 15 states; no stored regulation says whether the exchange may be used, so every verdict is "not yet sourced" (questions 30). Report: `docs/stage-reports/stage-18.md` |
| 19 | H | Built 26 Nov, tagged `stage-19-built` | `/calculator` and the MCP tool, live behind sign-in; equals the hand-worked landed cost in all six states; closed to visitors until you switch it on (questions 34). Report: `docs/stage-reports/stage-19.md` |

To resume after an interruption: type "continue the brief".


**Second round** (from 26 Nov 14:40): Stage 12; the seven ARR Network states not yet imported (GA, HP, JH, PY, AS, WB, DL); code gaps (a run pins the roof typed with the inputs, a time-limited sizing falls back instead of failing, plain words for solver statuses, Bihar's ToD scope, MCP token pruning). Then the multi-state tests again.