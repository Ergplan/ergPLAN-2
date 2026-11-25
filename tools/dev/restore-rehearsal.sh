#!/usr/bin/env bash
# Rehearses a restore (Stage 11): loads the newest database dump of BACKUP_DIR into a scratch
# database beside the live one, compares the row count of every table with the live database,
# checks the data/ archive against its recorded SHA-256 and lists it, then drops the scratch
# database. The live database is only read.
#   sudo tools/dev/restore-rehearsal.sh             (BACKUP_DIR defaults to /data/backups)
# Exit 0 when every table restored; tables written since the dump was taken are listed, not failed.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
DIR="${BACKUP_DIR:-/data/backups}"
SCRATCH=ergplan_restore_rehearsal
cd "$ROOT"
C=(docker compose --env-file .env -f docker/compose.yaml -f docker/compose.prod.yaml)
DUMP="$(ls -1 "$DIR"/db-*.dump 2>/dev/null | sort | tail -1)"
[ -n "$DUMP" ] || { echo "no dump in $DIR" >&2; exit 1; }
STAMP="$(basename "$DUMP" .dump)"; STAMP="${STAMP#db-}"
echo "rehearsing $DUMP ($(du -h "$DUMP" | cut -f1))"
(cd "$DIR" && sha256sum -c --quiet "$STAMP.sha256") && echo "checksums match ($STAMP.sha256)"
echo "data archive: $(tar -tzf "$DIR/data-$STAMP.tar.gz" | wc -l) entries"
psql() { "${C[@]}" exec -T postgres sh -c 'psql -U "$POSTGRES_USER" -v ON_ERROR_STOP=1 -At "$@"' sh "$@"; }
LIVE="$("${C[@]}" exec -T postgres sh -c 'echo "$POSTGRES_DB"')"
psql -d postgres -c "drop database if exists $SCRATCH" -c "create database $SCRATCH" >/dev/null
trap 'psql -d postgres -c "drop database if exists '"$SCRATCH"'" >/dev/null' EXIT
"${C[@]}" exec -T postgres sh -c 'pg_restore -U "$POSTGRES_USER" --no-owner --exit-on-error -d '"$SCRATCH" < "$DUMP"
COUNT="select coalesce(string_agg(format('select %L, count(*) from %I', tablename, tablename), ' union all ' order by tablename), 'select null, 0 where false') from pg_tables where schemaname = 'public'"
counts() { psql -d "$1" -F ' ' -c "$(psql -d "$1" -c "$COUNT")" | sort; }
counts "$SCRATCH" > /tmp/ergplan-rehearsal-restored.$$
counts "$LIVE" > /tmp/ergplan-rehearsal-live.$$
TABLES="$(wc -l < /tmp/ergplan-rehearsal-restored.$$)"
ROWS="$(awk '{n += $2} END {print n + 0}' /tmp/ergplan-rehearsal-restored.$$)"
[ "$TABLES" -gt 0 ] || { echo "the restored database has no tables" >&2; exit 1; }
MISSING="$(join -v 2 <(cut -d' ' -f1 /tmp/ergplan-rehearsal-restored.$$) <(cut -d' ' -f1 /tmp/ergplan-rehearsal-live.$$))"
[ -z "$MISSING" ] || { echo "tables not restored: $MISSING" >&2; exit 1; }
echo "restored $TABLES tables, $ROWS rows into $SCRATCH"
if DIFF="$(diff /tmp/ergplan-rehearsal-restored.$$ /tmp/ergplan-rehearsal-live.$$)"; then
  echo "every table has the live database's row count"
else
  echo "tables written since the dump (restored < / live >):"; echo "$DIFF" | grep '^[<>]'
fi
rm -f /tmp/ergplan-rehearsal-restored.$$ /tmp/ergplan-rehearsal-live.$$
echo "scratch database dropped; rehearsal done at $(date -u +%Y-%m-%dT%H:%M:%SZ)"
