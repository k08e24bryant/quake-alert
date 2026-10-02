#!/usr/bin/env bash
# Daily database backup: pg_dump (plain SQL) | gzip into BACKUP_DIR, keeping KEEP_DAYS days.
#
#   sudo ./backup.sh
#
# Cron (root's crontab, `sudo crontab -e`): every day at 02:15 UTC (09:15 WIB), before the
# app's own 03:00 UTC prune:
#   15 2 * * * /opt/quake-alert/deploy/backup.sh >> /var/log/quake-alert-backup.log 2>&1
#
# A file is complete or absent: the dump is written to *.partial, checked with gzip -t, and
# only then renamed. Restore, or test a restore, with restore.sh. These backups live on the
# same disk as the database: copy them off the VM too (docs/deploy.md, "Backups").
set -euo pipefail

# shellcheck source=deploy/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

BACKUP_DIR=${BACKUP_DIR:-/var/backups/quake-alert}
KEEP_DAYS=${KEEP_DAYS:-14}
# Global, not local to main: the EXIT trap runs after main has returned.
PARTIAL=""

main() {
	require_env_file
	umask 077
	mkdir -p "$BACKUP_DIR"

	local target
	target="$BACKUP_DIR/quake_alert-$(date -u +%Y%m%dT%H%M%SZ).sql.gz"
	PARTIAL="$target.partial"
	trap 'rm -f -- "$PARTIAL"' EXIT

	log "dumping the database to $target"
	# Single quotes on purpose: the variables are expanded by the shell inside the container.
	# shellcheck disable=SC2016
	compose exec -T db sh -c \
		'pg_dump --username="$POSTGRES_USER" --dbname="$POSTGRES_DB" --no-owner --no-privileges' |
		gzip -9 >"$PARTIAL"
	gzip -t "$PARTIAL"
	mv "$PARTIAL" "$target"

	# Older than KEEP_DAYS days (find's -mtime +N means at least N+1 whole days).
	find "$BACKUP_DIR" -maxdepth 1 -type f -name 'quake_alert-*.sql.gz' \
		-mtime +"$((KEEP_DAYS - 1))" -print -delete
	log "backup done: $target ($(du -h "$target" | cut -f1))"
}

main "$@"
