#!/usr/bin/env bash
# Restore a backup made by backup.sh.
#
#   ./restore.sh BACKUP.sql.gz
#       Restore test: restore into a scratch database (restore_check), compare its row counts
#       and schema version with the live database, then drop it. The live data is not
#       touched. Run it regularly (docs/deploy.md, "Restore test").
#
#   ./restore.sh BACKUP.sql.gz --replace-live
#       Replace the live database with the backup: stops api and worker, takes a safety
#       backup first, recreates the database, restores, runs the migrations, starts api and
#       worker again. Asks for confirmation; ASSUME_YES=1 skips the question.
#
# KEEP_SCRATCH=1 keeps restore_check for inspection.
set -euo pipefail

# shellcheck source=deploy/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

SCRATCH_DB=restore_check
TABLES=(earthquakes subscriptions notification_deliveries ingestion_runs)

# psql_in DATABASE [PSQL_ARGS...]: psql in the db container, as POSTGRES_USER.
psql_in() {
	local database=$1
	shift
	# shellcheck disable=SC2016
	compose exec -T -e TARGET_DB="$database" -e PGOPTIONS="-c client_min_messages=warning" db sh -c \
		'psql -v ON_ERROR_STOP=1 -X -q --username="$POSTGRES_USER" --dbname="$TARGET_DB" "$@"' \
		psql "$@"
}

recreate_database() {
	local database=$1
	psql_in postgres -c "DROP DATABASE IF EXISTS \"$database\" WITH (FORCE)"
	psql_in postgres -c "CREATE DATABASE \"$database\""
}

restore_into() {
	local backup=$1 database=$2
	log "restoring $backup into $database"
	# --output: the dump's own SELECTs (set_config) would print result rows. Errors still show.
	gzip -dc "$backup" | psql_in "$database" --single-transaction --output=/dev/null
}

# counts DATABASE: "table rows" lines, then the Alembic revision. Fails if any query does.
counts() {
	local database=$1 table value
	for table in "${TABLES[@]}"; do
		value=$(psql_in "$database" -tA -c "SELECT count(*) FROM $table")
		printf '%s %s\n' "$table" "$value"
	done
	value=$(psql_in "$database" -tA -c 'SELECT version_num FROM alembic_version')
	printf 'alembic_version %s\n' "$value"
}

restore_test() {
	local backup=$1
	recreate_database "$SCRATCH_DB"
	restore_into "$backup" "$SCRATCH_DB"
	local live_db restored live
	live_db=$(env_value POSTGRES_DB)
	restored=$(counts "$SCRATCH_DB")
	live=$(counts "$live_db")
	log "row counts: backup vs live (live has whatever arrived after the backup)"
	paste <(printf '%s\n' "$restored") <(printf '%s\n' "$live" | cut -d' ' -f2) |
		awk 'BEGIN { printf "  %-26s %10s %10s\n", "", "backup", "live" }
		     { printf "  %-26s %10s %10s\n", $1, $2, $3 }'
	psql_in "$SCRATCH_DB" -tA -c 'SELECT postgis_version()' >/dev/null
	log "PostGIS works in the restored database"
	if [[ ${KEEP_SCRATCH:-0} == 1 ]]; then
		log "kept $SCRATCH_DB (drop it with: ./restore.sh --drop-scratch)"
	else
		psql_in postgres -c "DROP DATABASE \"$SCRATCH_DB\" WITH (FORCE)"
	fi
	log "restore test passed"
}

replace_live() {
	local backup=$1 live_db answer
	live_db=$(env_value POSTGRES_DB)
	[[ -n $live_db ]] || die "POSTGRES_DB is not set in $ENV_FILE"
	if [[ ${ASSUME_YES:-0} != 1 ]]; then
		printf 'This REPLACES the live database "%s" with %s. Type the database name to go on: ' \
			"$live_db" "$backup"
		read -r answer
		[[ $answer == "$live_db" ]] || die "cancelled"
	fi
	log "taking a safety backup of the current database first"
	"$DEPLOY_DIR/backup.sh"
	log "stopping api and worker"
	compose stop api worker
	recreate_database "$live_db"
	restore_into "$backup" "$live_db"
	log "running migrations (the backup may predate the current schema)"
	compose --profile deploy run --rm migrate
	log "starting api and worker"
	compose up -d --wait api worker
	counts "$live_db" | sed 's/^/  /'
	log "live database replaced"
}

main() {
	require_env_file
	if [[ ${1:-} == --drop-scratch ]]; then
		psql_in postgres -c "DROP DATABASE IF EXISTS \"$SCRATCH_DB\" WITH (FORCE)"
		return
	fi
	local backup=${1:-}
	[[ -n $backup ]] || die "usage: restore.sh BACKUP.sql.gz [--replace-live]"
	[[ -f $backup ]] || die "$backup not found"
	gzip -t "$backup" || die "$backup is not a valid gzip file"
	case ${2:-} in
		"") restore_test "$backup" ;;
		--replace-live) replace_live "$backup" ;;
		*) die "unknown option: $2" ;;
	esac
}

main "$@"
