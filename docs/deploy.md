# Deploying ergPLAN

## The Mac mini (the server, from 1 Nov 2026)

Prerequisites on the mini: Docker Desktop (or OrbStack) running, git, the billx checkout at `~/billx`,
and in `~/Downloads`: `ergplan-server/` (this kit), `Keys.rtf` (the OpenAI key is read from it, never
printed), `India_DISCOM_Tariff_Orders_Final_2026/`, `Tariff Orders/`, `Banking Rule - Uttar Pradesh.pdf`,
and the IEX `DAM_15min_*.xlsx` / `RTM_15min_*.xlsx` files.

```bash
bash ~/Downloads/ergplan-server/install.sh
```

It installs the code at `~/apps/ergplan` and writes `.env` (mode 600), builds and starts the stack
(first time 10–20 minutes), registers the regulations, restores the approved rulebooks, loads the IEX
prices, schedules the nightly backup and prints the address (`http://<the mini's address>`) with a
one-time sign-in link for the first analyst. Running it again updates the code from a newer kit and keeps
`.env` and the database.

Day to day, from `~/apps/ergplan`:

| What | Command |
| --- | --- |
| Status, logs | `tools/dev/mini.sh ps` · `tools/dev/mini.sh logs -f api` |
| A sign-in link for someone (no mail server yet) | `tools/dev/mini.sh exec api python -m ergplan.platform.signin someone@company.in` |
| Stop, start | `tools/dev/mini.sh stop` · `tools/dev/mini.sh up -d` |
| Update from GitHub instead of a kit | `git pull && tools/dev/mini.sh up -d --build` |
| Backup now | `ERGPLAN_SERVER=mini BACKUP_DIR=~/ergplan-backups tools/dev/backup.sh` |

Keep it up: Docker Desktop → Settings → General → "Start Docker Desktop when you sign in"; System
Settings → Energy → prevent automatic sleeping. Sign-in mail: set `ERGPLAN_SMTP_URL`
(`smtps://user:app-password@smtp.example.com:465`) in `.env`, then `tools/dev/mini.sh up -d`.
To reach it from outside the office, put a hostname in front (Cloudflare Tunnel, or a router port
forward with `SITE_ADDRESS=<hostname>`, `ERGPLAN_PUBLIC_URL=https://<hostname>` and port 443 published),
so Caddy serves https.

# The public VM `replan2` (asia-south2-b, from 25 Nov 2026, Stage 11)

Project `replanner-513407`, Debian 13, 4 vCPU, 16 GB, static address `replan2-ip`. The checkout at `/work/ergPLAN-2` is the deployment; Claude Code built it there. SSH is through IAP only. Everything below is run in that folder.

| To | Run |
| --- | --- |
| See the services | `tools/dev/vm.sh ps` · `tools/dev/vm.sh logs -f api` |
| Update to the latest code | `git pull && tools/dev/vm.sh up -d --build` (migrations run when the API starts); if `docker/caddy/Caddyfile` changed, also `tools/dev/vm.sh restart caddy` (the file is mounted alone, and a pull replaces it, so the running Caddy keeps the old one) |
| Let a person in (no mail account yet) | `tools/dev/vm.sh exec api python -m ergplan.platform.signin name@company.in` — hand over the link it prints; it works once, for 15 minutes. Emails at `ERGPLAN_ANALYST_DOMAIN` or listed in `ERGPLAN_ANALYST_EMAILS` are analysts |
| Check the deployment end to end | `python3 tools/dev/vm-check.py` (services, published ports, certificate, sign-in by link, a rule-book figure opening its stored page) |
| Change a setting or a key | edit `.env` (mode 600; never print it), then `tools/dev/vm.sh up -d` |
| Change the firewall | edit `tools/dev/vm-firewall.allow`, then `tools/dev/vm-firewall.sh` (`--check` shows what would change) |
| Reload the rulebooks after a pull | `tools/dev/vm.sh exec api python -m ergplan.rulebook.cli restore` |
| See which cited PDFs are missing | `tools/dev/vm.sh exec api python -m ergplan.documents.missing` |
| Add regulation PDFs | copy the Mac mini's library folders into `data/corpus/` (for example from Cloud Shell: `gcloud compute scp --recurse --tunnel-through-iap <folder> replan2:/work/ergPLAN-2/data/corpus/ --zone asia-south2-b`), then `tools/dev/vm.sh exec api python -m ergplan.documents.corpus --root /data/corpus/India_DISCOM_Tariff_Orders_Final_2026`; or upload one in the app under Documents. `python3 tools/dev/fetch-corpus.py` fetches from the regulators the files that match the rulebooks' citations byte for byte |
| Back up now | `sudo systemctl start ergplan-backup.service` (nightly at 02:30 India time: `systemctl list-timers ergplan-backup.timer`); files in `/data/backups`, 14 days |
| Rehearse a restore | `sudo tools/dev/restore-rehearsal.sh` |
| Restore for real | stop `api` and `worker`; `pg_restore --clean --if-exists -U ergplan -d ergplan2` from the chosen `db-*.dump` inside the `postgres` container; unpack `data-*.tar.gz` over the checkout; start them again |
| Run the watcher | `tools/dev/vm-watch.sh` (tmux window `watch`; its own Postgres on 127.0.0.1, never the deployment's `.env`) |
| Run the browser journeys | `E2E_TESTS="tests/e2e/test_ergplan2_journey.py tests/e2e/test_statebook_journey.py" tools/dev/e2e-journey.sh` (a scratch stack beside the deployment) |

After a reboot Docker starts the services again by itself (`restart: unless-stopped`); `python3 tools/dev/vm-check.py` confirms it. DNS: an A record for the hostname in `SITE_ADDRESS` to the static address (the zone is on Vercel DNS); Caddy obtains and renews the certificate.

# The Google Cloud VM `replanner` (asia-south2-b, from 1 Nov 2026)

How code reaches it: the builder commits and pushes to GitHub, then updates the VM over SSH from the
Mac mini (the build workspace cannot open SSH; the mini can). Each update copies a git bundle of `main`
and runs `tools/dev/vm-install.sh`, which fast-forwards `/opt/ergplan`, rebuilds and restarts the stack
and keeps `.env`, the database and `data/`. The first install also carries billx (from `~/billx`), the
regulation PDFs, the IEX files and the settings (the OpenAI key read on the mini from `Keys.rtf`, never
printed; the payload is deleted from the VM after the install).

Once, in the Console: the project-wide SSH key `ergplan` (Compute Engine → Metadata → SSH keys), a
firewall rule for TCP 80 and 443, a 50 GB boot disk, and (recommended) the external address reserved as
static. The site is `https://<ip-with-dashes>.sslip.io` (no DNS needed; Caddy obtains the certificate)
until a jouleWise hostname is pointed at the address.

Sign-in mail: `sudo bash tools/dev/set-mail.sh venture@aayuda.energy` asks for a Google app password (not shown), checks it with Gmail, writes `ERGPLAN_SMTP_URL` to `.env` and restarts the API.

On the VM, from `/opt/ergplan`: `sudo tools/dev/vm.sh ps`, `sudo tools/dev/vm.sh logs -f api`,
`sudo tools/dev/vm.sh exec api python -m ergplan.platform.signin someone@company.in`. Backups: root's
cron at 02:30 into `/var/backups/ergplan` (14 days). From a Mac with the Google Cloud CLI signed in,
`tools/dev/vm-push.sh` does the same install without the builder.

# The Google Cloud VM, by hand (Stage 8 runbook)

The stack is the same Compose file as on the laptop, with `docker/compose.prod.yaml` on top: only Caddy is
reachable from outside (80/443, automatic TLS for the jouleWise hostname); the API runs with
`ERGPLAN_ENV=prod` (sign-in by email link, development headers ignored, ownership enforced).

## 1. The VM (once)

```bash
gcloud compute instances create ergplan \
  --project=<project> --zone=asia-south2-a \
  --machine-type=e2-standard-4 --boot-disk-size=100GB \
  --image-family=ubuntu-2404-lts-amd64 --image-project=ubuntu-os-cloud \
  --tags=http-server,https-server
gcloud compute firewall-rules create ergplan-web --allow=tcp:80,tcp:443 --target-tags=http-server,https-server
```

Point the hostname (for example `plan.joulewise.in`) at the VM's external IP (an A record) before the
first start, so Caddy can obtain the certificate.

## 2. Software and code

```bash
sudo apt-get update && sudo apt-get install -y docker.io docker-compose-v2 git
sudo usermod -aG docker $USER && newgrp docker
sudo mkdir -p /opt/ergplan && sudo chown $USER /opt/ergplan
git clone https://github.com/Ergplan/Enterprise-RE-Planner /opt/ergplan
cd /opt/ergplan && cp .env.example .env      # fill in: see the Stage 8 block
```

Required in `.env`: `SITE_ADDRESS`, `ERGPLAN_ENV=prod`, `ERGPLAN_PUBLIC_URL=https://<hostname>`,
`POSTGRES_PASSWORD`, `OPENAI_API_KEY`, `BILLX_TOKEN`, `BILLX_OPENAI_API_KEY`,
`ERGPLAN_SMTP_URL` (mandatory: without it production refuses to issue sign-in links), `ERGPLAN_MAIL_FROM`. `tools/dev/secrets-check.py` refuses a
committed `.env`.

## 3. Data

Copy the regulation PDFs, IEX files and (optionally) bills into `data/` (git-ignored), then register the
corpus and restore the approved rulebooks:

```bash
docker compose -f docker/compose.yaml -f docker/compose.prod.yaml up -d --build
docker compose -f docker/compose.yaml -f docker/compose.prod.yaml exec api python -m ergplan.rulebook.cli restore
```

## 4. Backups

```bash
sudo crontab -e   # 30 2 * * * cd /opt/ergplan && BACKUP_BUCKET=gs://<bucket> tools/dev/backup.sh >> /var/log/ergplan-backup.log 2>&1
```

Nightly at 02:30: `pg_dump` (custom format) and `data/` as a tarball, with SHA-256 sums, kept 14 days
locally and copied to the bucket. Restore: `pg_restore -c -d ergplan db-<stamp>.dump` inside the
postgres container; untar `data-<stamp>.tar.gz` at the repository root.

## 5. Check

`https://<hostname>/api/health` shows the migration head; sign in with a `@joulewise.in` address (analyst);
open a shared plan from a private window (it opens without sign-in).

## Switching the administrator's password sign-in on

1. On the server, type `ERGPLAN_ADMIN_USER=` and `ERGPLAN_ADMIN_PASSWORD=` into `/work/ergPLAN-2/.env`: twelve characters or more, not the username with a suffix, and a username without "@".
2. Restart the API: `tools/dev/vm.sh up -d api` (the migration runs at start).
3. Check: `https://<host>/api/auth/me` shows `"password_sign_in": true`; the sign-in page shows the username and password form; the API log has no weak-password line; `python3 tools/dev/secrets-check.py` says OK.

Left empty, or on a server not served over https, password sign-in stays off and the emailed link is the only way in. Five wrong attempts lock that address for fifteen minutes; attempts are in the table `signin_attempts` and in the API log under `ergplan.signin`.

## The MCP connector (Stage 16)

Once password sign-in is on (above), add **`https://<host>/mcp`** as a custom connector in claude.ai (Settings, Connectors), or in Claude Code: `claude mcp add --transport http ergplan https://<host>/mcp`. Claude opens the sign-in page; type the administrator's username and password and choose "Sign in and allow". With password sign-in off, the connector refuses everyone and says why. A connector may send the sign-in back only to the hosts in `ERGPLAN_MCP_REDIRECT_HOSTS` (claude.ai, claude.com) or to localhost. A bill reaches a tool by the one-time upload link `read_bill` returns.

