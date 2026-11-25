# Stage 11 — Parallel deployment on the Google Cloud VM

Run on 25 Nov 2026 on the VM `replan2` (project `replanner-513407`, zone `asia-south2-b`, Debian 13, 4 vCPU, 16 GB), in `/work/ergPLAN-2`, from `stage-11-start` (972ac77, the last Stage 10 commit; this checkout has no `stage-10-done` tag). The Mac mini's install was not touched.

**Status: built, tagged `stage-11-built`.** Not `stage-11-done`: the independent reviewer could not run (no OpenAI key on the VM) and the reboot was left undone under the brief of 25 Nov 2026 ("do not stop"); both are in `docs/questions-for-review.md` (1, 2). The second half of this report covers part A of that brief: the findings of the Uttar Pradesh full test.

## What was built

- **The box** (step 1): Docker 29.10.1, a 99 GB disk (90 GB free), `ANTHROPIC_API_KEY` unset, git identity set in the repository. The project, zone and instance name came from the metadata server. The watcher's toolchain was installed (a Python 3.13 virtualenv, pnpm, a Postgres container on 127.0.0.1 for the tests). The watcher's first pass on the starting commit: every check green except `alembic-drift`, which had no database to ask (the box had none yet); with the test database's address in the environment, as in CI, it passed too.
- **Settings** (step 2): `/work/ergPLAN-2/.env`, mode 600, git-ignored, from `.env.example`: `ERGPLAN_ENV=prod`, `SITE_ADDRESS=ergplan.joulewise.tech`, `ERGPLAN_PUBLIC_URL=https://ergplan.joulewise.tech`, a generated `POSTGRES_PASSWORD` and `BILLX_TOKEN`, the first analyst in `ERGPLAN_ANALYST_EMAILS`, `ERGPLAN_OPTIMIZER_TIME_LIMIT_S=800`. The file, the keys and the sign-in links were never printed.
- **Network** (step 3): the static address `replan2-ip` (34.131.52.190, in use by `replan2`) and the IAP SSH rules (`replan2-allow-iap-ssh`, `replan2-deny-other-ssh`) were confirmed and left alone. `replan2-allow-web` (tcp 80 and 443, from anywhere, target tag `replan2`) was created by `tools/dev/vm-firewall.sh` from `tools/dev/vm-firewall.allow`; the script marks what it creates and refuses to touch a rule without its mark.
- **DNS** (step 4): the person added the A record in Vercel DNS. Vercel's two nameservers, Google and Cloudflare all answered 34.131.52.190 before Caddy was started; the certificate (Let's Encrypt, valid to 23 Feb 2027) was issued on the first try.
- **The stack** (step 5): `docker compose -f docker/compose.yaml -f docker/compose.prod.yaml up -d --build`; migrations at the API's start; `python -m ergplan.rulebook.cli restore` (72 approved versions); the register import (every state and family already present from the restore; Chhattisgarh's two rows without a basis are skipped, as before). Seven services running; `api`, `postgres` and `billx` report healthy; only Caddy is published (80, 443).
- **Sign-in** (step 6): the emailed link stays; there are no passwords on this server. No mail account, so links are minted with `tools/dev/vm.sh exec api python -m ergplan.platform.signin EMAIL` (known gap).
- **Regulation library** (step 7): `data/documents/` was empty. `tools/dev/fetch-corpus.py` downloaded the library index's regulator URLs and kept the files whose SHA-256 gives an id the rulebooks cite: five of the ten cited documents (UPERC NPCL and state DISCOMs FY 2026-27, MERC MSEDCL MYT order and Case 75/2025 order, APERC FY 2026-27), byte for byte the files the rules were written from. Five are missing (below). New code so that a number citing a missing PDF says so: `documents/missing.py`, `GET /documents/missing`, a page in words at `GET /documents/{id}`, the rule book's `unstored` count and unlinked citation, and the Documents page's list with an upload form.
- **Backups** (step 8): `tools/dev/vm-backup-install.sh` installed `ergplan-backup.timer` (02:30 India time) running `tools/dev/backup.sh` into `/data/backups`, 14 days kept. One backup taken (20261125T005931Z: database dump and `data/` archive, checksums recorded). `tools/dev/restore-rehearsal.sh` restored it into a scratch database: 32 tables, 1,945 rows, every table with the live database's row count; the scratch database was dropped. Rehearsed at 2026-11-25T00:59:40Z.
- **Restart safety** (step 9): every service has `restart: unless-stopped` and Docker is enabled at boot. **The reboot itself was left undone** (it ends the builder's session and the brief asks for no stop; `docs/questions-for-review.md`, 2).
- **Found on the way and fixed**: in production any signed-in person could add a PDF to the regulation library; it is now an analyst's action recorded under the analyst's name (test). The tests, the watcher and the browser journeys read no `.env` (`ERGPLAN_ENV_FILE`), because on a server that file is the deployment's.

## Checked (definition of done)

| Check | Result |
| --- | --- |
| The address serves the sign-in page with a valid certificate | Yes from the VM through its public address (`tools/dev/vm-check.py`, certificate verified). From outside the VM: a later outside fetch reached the ergPLAN sign-in page (the first had reached Vercel through a resolver holding the old record) |
| An analyst signs in by link | By `vm-check.py`: a link minted on the server signs `vm-check@joulewise.in` in as an analyst and does not work twice. The person's own first sign-in has not happened (questions 3) |
| The end-to-end journeys pass on the VM | `tests/e2e/test_statebook_journey.py` passed. `tests/e2e/test_ergplan2_journey.py` failed twice with the default 300 s solve budget (the sizing pass ended with no solution), and passed with 800 s (531 s), the value the VM now runs with. Both through `tools/dev/e2e-journey.sh`, which starts its own stack on a scratch database beside the deployment (the tests reset their database) |
| A bill is read by the real billx container | Read, not yet accepted: billx's three local readers read the synthetic PVVNL bill (18 fields, 8 meter rows, its arithmetic checks pass) and returned `unreadable` with the note `no_model_configured`, because no model key is set. Not met until the key is in `.env` (questions 1) |
| The month's rule books generate and one number opens its stored page | 21 drafts for November 2026. Andhra Pradesh's peak grid tariff opens APERC's order at page 197 and the quoted words are on that page's text |
| The stack survives a reboot | Not tested: left undone (questions 2) |
| A backup exists and was restored once | Yes (above) |
| Watcher green | Green at 20:14 UTC on 25 Nov on the last code commit (`f6b8c03`) |
| Reviewer clean | Not run: the reviewer needs `OPENAI_API_KEY` (questions 1) |

## What was skipped, and why

- Certificate checks were not relaxed to fetch from commissions whose sites serve a broken certificate chain (KERC, TNERC); those PDFs come from the Mac mini.
- No mail server (the stage prompt).
- No off-machine copy of the backups (`BACKUP_BUCKET` is unset; known gap).

## Deviations from the master prompt

- The journeys run beside the deployment, not through it (decision of 25 Nov 2026).
- The solve budget is 800 s on this VM (decision of 25 Nov 2026), set before the search was changed; the two 5 MW sites now solve at 15 minutes in 66 s and 23 s here and a five-component mix in under a minute, so the budget can come down (known gap).
- The branch and bound for small models is ergPLAN's own over HiGHS's LP solver (decision of 25 Nov 2026; questions 13).
- Stages are tagged `-built` while the reviewer cannot run.
- The watcher's virtualenv is Python 3.13 (Debian 13); the application's image is Python 3.12 as locked.
- The password option of 1 Nov 2026 is still in the application although the person asked for no passwords; nobody has one here (open question 2).

## Firewall rules of the project that this build did not create (untouched)

`allow-iap-ssh` (tcp 22 from 35.235.240.0/20, all instances), `allow-web` (tcp 80, 443 from anywhere, **all instances**), `default-allow-icmp`, `default-allow-internal`, `default-allow-rdp` (tcp 3389 from anywhere), `default-allow-ssh` (tcp 22 from anywhere; `replan2-deny-other-ssh` at priority 900 overrides it for this VM), `replan2-allow-iap-ssh`, `replan2-deny-other-ssh`, `replanner-ssh-allow`, `replanner-ssh-deny`. The other reserved address, `replanner-ip` (34.131.62.161), is not in use.

## Documents the approved rules cite that are not on the VM

| Document id | Cited by | Likely file on the Mac mini |
| --- | --- | --- |
| `2ce52fb6-…` | UP settlement and open-access rules (10 rules, pages 13–20) | `Banking Rule - Uttar Pradesh.pdf` (UPERC CRE Regulations 2024) |
| `8e00613e-…` | Karnataka, five ESCOMs (16 rules, pages 249–568) | `31_Karnataka/Karnataka_ESCOMs_FY2025-26_Combined_Tariff_Order_27-Mar-2025.pdf` |
| `17db86c4-…` | Gujarat (4 rules, pages 173–175) | `Tariff Orders/MGVCL Tariff Order for FY 2026-27 - Gujarat Tariff Order.pdf` |
| `8fc3d382-…` | Punjab (10 rules, pages 225–239) | `Tariff Orders/Punjab Tariff order.pdf` |
| `0c84182f-…` | Telangana (16 rules, pages 152–281) | `Tariff Orders/Telangana Tariff Order.pdf` |

To send them: upload each on the Documents page (it confirms a match), or copy them into `data/corpus/` and register (`docs/deploy.md`).

## The Uttar Pradesh full-test findings (brief of 25 Nov 2026, part A)

Each finding of `docs/stage-reports/up-full-test-2026-11-25.md` has a test that fails on the old code.

| Finding | What changed |
| --- | --- |
| 1. Profile year did not follow the bills | The profile year is the months the bills cover; a bill is filed under its supply month |
| 2. A captive plant carried no capital | Captive: capital and O&M from benchmarks (assumptions), no tariff. 26% equity: the equity outlay at year 0 plus the tariff. Third party: tariff only. Payback and IRR are on the owned capital; the plan's saving is separate |
| 3. Today's cost left out FPPCA and duty | Both are charged at the average rate on the client's accepted bills, cited to the bills; on units not bought from the DISCOM, FPPCA is not charged and duty is (assumptions, shown as such) |
| 4. A bill before any approved tariff read as explained | It reads "no tariff in force", with the earliest approved tariff named |
| 5. Solve time, and the run that stopped on its limit | The search for models with at most 40 whole-number choices is done in `optimizer/highs_solve.py` over HiGHS's LP solver (a deviation, recorded); a run that stops on its limit says so, with its gap |
| 6, 7 | Analyst questions: no rule changed; the regulation text is quoted in `docs/questions-for-review.md` (5, 6) |
| 8. No landed-cost card for the battery at the plant | It has one, priced from the plant |
| 9. The profile could not hold the recorded demand | The test's bills are inconsistent (their window energy averages above the recorded demand); the profile keeps energy and ToD split and warns with the excess |
| 10, 11, 12 | ToD hours from the rule, UPPCL's unnamed slots said in words, quantities printed without trailing zeros |

### What changed in each figure (assumed Uttar Pradesh site, same bills, on this VM)

| Figure | Before | After |
| --- | --- | --- |
| Bills replayed | 12 explained | 4 rebuilt, 8 "no tariff in force" |
| Profile year starts | April 2025 | November 2025 (the bills' first month) |
| Today's yearly cost | ₹17.54 crore | ₹19.15 crore (FPPCA ₹0.42 crore and duty ₹1.18 crore now in; the hand-worked total of the bills) |
| Average today | ₹7.76/kWh | ₹8.47/kWh |
| Captive five-component run | stopped on its limit, 22.6% gap, 169 s; the answer was "build nothing" | proven optimal in 35 s: rooftop 1,000 kW, solar 3,469 kW, wind 4,610 kW |
| Captive: cost after, saving | none | ₹14.50 crore a year, saving ₹4.65 crore |
| Captive: capital, payback, IRR | none shown | ₹49.9 crore of the consumer's own; 4.6 years; 23.6% (with the plant's capital left out, as before finding 2, the same plan read 0.8 years and 127%) |
| Third-party run | optimal in 83 s | optimal in 32 s |
| Third party: sizes | solar 1,690 kW, wind 4,887 kW, battery 352 kWh | solar 1,729 kW, wind 4,920 kW, battery 518 kWh (the levies changed the grid's cost) |
| Third party: saving | ₹2.58 crore | ₹2.86 crore |
| Third party: payback, IRR | 1.4 years, 79.7% (whole plan's saving on the rooftop and battery) | 4.3 years, 24.7% (on the ₹4.6 crore of owned capital, against the saving it brings) |
| Uttar Pradesh full test, wall time | 593 s | 212 s |
| Mix journey at 15 minutes | 204 s and 186 s | 53 s and 74 s |
| 5 MW sites at 15 minutes (limit 120 s) | UP 139 s (failed), MH 45 s | UP 66 s, MH 23 s |

### Slow tests and CI

- On the final code (`f6b8c03`): `tests/optimizer/test_sites.py` (2 passed), `tests/e2e/test_up_assumed.py` (passed, no failed step), `tests/e2e/test_mix_journey.py` (2 passed). The two browser journeys (`test_statebook_journey`, `test_ergplan2_journey`, through `tools/dev/e2e-journey.sh`) passed in 128 s together; the second took 531 s before.
- `test_stage6_optimise`, `test_stage7_journey`, `test_stage8_evaluate` skip on the VM: they need the real bills, which are not here (`data/bills/` is empty).
- CI: the run on `0b6bbfd` (the findings) passed, the first green run on `main` since the Uttar Pradesh 15-minute site went over its limit. The run on `f6b8c03` (finding 2) is read at the start of Stage 13 and noted in its report.

## Reviewer findings

Not run: `tools/review/run.sh 11` needs `OPENAI_API_KEY`, which is not in `.env`. The stage's diff is `stage-11-start..stage-11-built`; the review follows when the key is there.

## Open questions

The questions raised by the findings, with the reading taken for each, are in `docs/questions-for-review.md` (1 to 15). Those from the deployment:

1. `allow-web` opens 80 and 443 to every instance of the project, and `default-allow-rdp` opens 3389 from anywhere. Neither is this build's. Should they stay?
2. The sign-in page still offers a password to anyone who has set one. Remove the option on this server, or leave it unused?
3. Backups stay on the VM's disk. Name a bucket for `BACKUP_BUCKET`, or accept this for now?
4. Closed: a five-component mix now takes under a minute on this machine.

## Cost

- Application model calls (`model_calls`): none; no OpenAI key was set during the build.
- Reviewer: none (not run).
- Builder: Claude Code on the subscription, one session with five subagents for the findings.
