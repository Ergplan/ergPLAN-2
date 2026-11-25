#!/usr/bin/env bash
# Schedules the nightly backup on the public VM (Stage 11) with a systemd timer: tools/dev/backup.sh
# at 02:30 India time into /data/backups (a compressed pg_dump and an archive of data/, 14 days kept).
#   sudo tools/dev/vm-backup-install.sh            install or update the units, enable the timer
#   sudo systemctl start ergplan-backup.service    take a backup now
#   systemctl list-timers ergplan-backup.timer · journalctl -u ergplan-backup.service
# A restore is rehearsed with tools/dev/restore-rehearsal.sh.
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run with sudo" >&2; exit 1; }
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
DIR="${BACKUP_DIR:-/data/backups}"
install -d -m 700 "$DIR"
cat > /etc/systemd/system/ergplan-backup.service <<UNIT
[Unit]
Description=ergPLAN nightly backup (pg_dump and data/ to $DIR)
Requires=docker.service
After=docker.service

[Service]
Type=oneshot
WorkingDirectory=$ROOT
Environment=BACKUP_DIR=$DIR
Environment=BACKUP_KEEP_DAYS=14
ExecStart=$ROOT/tools/dev/backup.sh
UNIT
cat > /etc/systemd/system/ergplan-backup.timer <<UNIT
[Unit]
Description=ergPLAN nightly backup at 02:30 India time

[Timer]
OnCalendar=*-*-* 02:30:00 Asia/Kolkata
Persistent=true

[Install]
WantedBy=timers.target
UNIT
systemctl daemon-reload
systemctl enable --now ergplan-backup.timer >/dev/null
systemctl list-timers ergplan-backup.timer --no-pager | sed -n '1,2p'
