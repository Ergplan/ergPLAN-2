# Stage 11 — Parallel deployment on the Google Cloud VM

Run on 25 Nov 2026 on the VM `replan2` (project `replanner-513407`, zone `asia-south2-b`, Debian 13, 4 vCPU, 16 GB), in `/work/ergPLAN-2`, from `stage-11-start` (972ac77, the last Stage 10 commit; this checkout has no `stage-10-done` tag). The Mac mini's install was not touched.

**Status: not finished.** The items marked PENDING below wait for the person (two keys, the first sign-in, the reboot). No `stage-11-done` tag until they are closed.

## What was built

- **The box** (step 1): Docker 29.10.1, a 99 GB disk (90 GB free), `ANTHROPIC_API_KEY` unset, git identity set in the repository. The project, zone and instance name came from the metadata server. The watcher's toolchain was installed (a Python 3.13 virtualenv, pnpm, a Postgres container on 127.0.0.1 for the tests). The watcher's first pass on the starting commit: every check green except `alembic-drift`, which had no database to ask (the box had none yet); with the test database's address in the environment, as in CI, it passed too.
- **Settings** (step 2): `/work/ergPLAN-2/.env`, mode 600, git-ignored, from `.env.example`: `ERGPLAN_ENV=prod`, `SITE_ADDRESS=ergplan.joulewise.tech`, `ERGPLAN_PUBLIC_URL=https://ergplan.joulewise.tech`, a generated `POSTGRES_PASSWORD` and `BILLX_TOKEN`, the first analyst in `ERGPLAN_ANALYST_EMAILS`, `ERGPLAN_OPTIMIZER_TIME_LIMIT_S=800`. The file, the keys and the sign-in links were never printed.
- **Network** (step 3): the static address `replan2-ip` (34.131.52.190, in use by `replan2`) and the IAP SSH rules (`replan2-allow-iap-ssh`, `replan2-deny-other-ssh`) were confirmed and left alone. `replan2-allow-web` (tcp 80 and 443, from anywhere, target tag `replan2`) was created by `tools/dev/vm-firewall.sh` from `tools/dev/vm-firewall.allow`; the script marks what it creates and refuses to touch a rule without its mark.
- **DNS** (step 4): the person added the A record in Vercel DNS. Vercel's two nameservers, Google and Cloudflare all answered 34.131.52.190 before Caddy was started; the certificate (Let's Encrypt, valid to 23 Feb 2027) was issued on the first try.
- **The stack** (step 5): `docker compose -f docker/compose.yaml -f docker/compose.prod.yaml up -d --build`; migrations at the API's start; `python -m ergplan.rulebook.cli restore` (72 approved versions); the register import (every state and family already present from the restore; Chhattisgarh's two rows without a basis are skipped, as before). Seven services running; `api`, `postgres` and `billx` report healthy; only Caddy is published (80, 443).
- **Sign-in** (step 6): the emailed link stays; there are no passwords on this server. No mail account, so links are minted with `tools/dev/vm.sh exec api python -m ergplan.platform.signin EMAIL` (known gap).
- **Regulation library** (step 7): `data/documents/` was empty. `tools/dev/fetch-corpus.py` downloaded the library index's regulator URLs and kept the files whose SHA-256 gives an id the rulebooks cite: five of the ten cited documents (UPERC NPCL and state DISCOMs FY 2026-27, MERC MSEDCL MYT order and Case 75/2025 order, APERC FY 2026-27), byte for byte the files the rules were written from. Five are missing (below). New code so that a number citing a missing PDF says so: `documents/missing.py`, `GET /documents/missing`, a page in words at `GET /documents/{id}`, the rule book's `unstored` count and unlinked citation, and the Documents page's list with an upload form.
- **Backups** (step 8): `tools/dev/vm-backup-install.sh` installed `ergplan-backup.timer` (02:30 India time) running `tools/dev/backup.sh` into `/data/backups`, 14 days kept. One backup taken (20261125T005931Z: database dump and `data/` archive, checksums recorded). `tools/dev/restore-rehearsal.sh` restored it into a scratch database: 32 tables, 1,945 rows, every table with the live database's row count; the scratch database was dropped. Rehearsed at 2026-11-25T00:59:40Z.
- **Restart safety** (step 9): every service has `restart: unless-stopped` and Docker is enabled at boot. **PENDING: the reboot itself** (it ends the builder's session; to be done with the person's word, then `python3 tools/dev/vm-check.py`).
- **Found on the way and fixed**: in production any signed-in person could add a PDF to the regulation library; it is now an analyst's action recorded under the analyst's name (test). The tests, the watcher and the browser journeys read no `.env` (`ERGPLAN_ENV_FILE`), because on a server that file is the deployment's.

## Checked (definition of done)

| Check | Result |
| --- | --- |
| The address serves the sign-in page with a valid certificate | Yes from the VM through its public address (`tools/dev/vm-check.py`, certificate verified). From outside the VM: one outside fetch still reached Vercel (404) through a resolver holding the old record; to be repeated — PENDING |
| An analyst signs in by link | By `vm-check.py`: a link minted on the server signs `vm-check@joulewise.in` in as an analyst and does not work twice. The person's own first sign-in — PENDING |
| The end-to-end journeys pass on the VM | `tests/e2e/test_statebook_journey.py` passed. `tests/e2e/test_ergplan2_journey.py` failed twice with the default 300 s solve budget (the sizing pass ended with no solution), and passed with 800 s (531 s), the value the VM now runs with. Both through `tools/dev/e2e-journey.sh`, which starts its own stack on a scratch database beside the deployment (the tests reset their database) |
| A bill is read by the real billx container | Read, not yet accepted: billx's three local readers read the synthetic PVVNL bill (18 fields, 8 meter rows, its arithmetic checks pass) and returned `unreadable` with the note `no_model_configured`, because no model key is set — PENDING the key |
| The month's rule books generate and one number opens its stored page | 21 drafts for November 2026. Andhra Pradesh's peak grid tariff opens APERC's order at page 197 and the quoted words are on that page's text |
| The stack survives a reboot | PENDING |
| A backup exists and was restored once | Yes (above) |
| Watcher green | Green at 02:15 UTC on the code committed |
| Reviewer clean | PENDING: the reviewer needs `OPENAI_API_KEY` |

## What was skipped, and why

- Certificate checks were not relaxed to fetch from commissions whose sites serve a broken certificate chain (KERC, TNERC); those PDFs come from the Mac mini.
- No mail server (the stage prompt).
- No off-machine copy of the backups (`BACKUP_BUCKET` is unset; known gap).

## Deviations from the master prompt

- The journeys run beside the deployment, not through it (decision of 25 Nov 2026).
- The solve budget is 800 s on this VM (decision of 25 Nov 2026): the Stage 6 measure "two 5 MW sites solve at 15 minutes in under 2 minutes on the VM" was not re-measured here, and a five-component mix takes about seven minutes on this machine.
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

## Reviewer findings

PENDING (`tools/review/run.sh 11` needs `OPENAI_API_KEY`).

## Open questions

1. `allow-web` opens 80 and 443 to every instance of the project, and `default-allow-rdp` opens 3389 from anywhere. Neither is this build's. Should they stay?
2. The sign-in page still offers a password to anyone who has set one. Remove the option on this server, or leave it unused?
3. Backups stay on the VM's disk. Name a bucket for `BACKUP_BUCKET`, or accept this for now?
4. A five-component mix takes about seven minutes on this machine type. Accept, or move to a faster type (more single-core speed, not more cores: the solver uses one thread)?

## Cost

- Application model calls (`model_calls`): none; no OpenAI key was set during the build.
- Reviewer: PENDING.
- Builder: Claude Code on the subscription, one session.
