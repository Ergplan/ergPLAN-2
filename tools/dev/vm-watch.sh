#!/usr/bin/env bash
# The watcher on a server (Stage 11), where the repository's .env is the deployment's: it runs
# against its own Postgres (a container published on 127.0.0.1:5432 only, database ergplan2_test,
# nothing to do with the deployment's database) and reads no .env.
#   tools/dev/vm-watch.sh            watch          tools/dev/vm-watch.sh --once     one pass
# First use on a new machine: python3 -m venv apps/api/.venv && apps/api/.venv/bin/pip install -e
# "apps/api[dev]" && (cd apps/web && pnpm install --frozen-lockfile)
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
NAME=ergplan-watch-pg
if ! docker inspect "$NAME" >/dev/null 2>&1; then
  docker run -d --name "$NAME" --restart unless-stopped -p 127.0.0.1:5432:5432 \
    -e POSTGRES_USER=ergplan -e POSTGRES_PASSWORD=ergplan -e POSTGRES_DB=ergplan2_test postgres:16 >/dev/null
fi
docker start "$NAME" >/dev/null
for _ in $(seq 1 30); do docker exec "$NAME" pg_isready -q -U ergplan && break; sleep 1; done
export DATABASE_URL=postgresql+psycopg://ergplan:ergplan@localhost:5432/ergplan2_test
export ERGPLAN_ENV_FILE=
exec python3 "$ROOT/tools/watch/watch.py" "$@"
