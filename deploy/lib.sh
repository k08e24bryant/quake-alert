# shellcheck shell=bash
# Shared by the deploy scripts: sourced, never run. Sets DEPLOY_DIR and ENV_FILE.

# A failing command inside $(...) fails the script too (bash only does that when asked).
shopt -s inherit_errexit

DEPLOY_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
ENV_FILE="$DEPLOY_DIR/.env"

log() {
	printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"
}

die() {
	printf 'error: %s\n' "$*" >&2
	exit 1
}

compose() {
	docker compose --project-directory "$DEPLOY_DIR" -f "$DEPLOY_DIR/docker-compose.prod.yml" "$@"
}

require_env_file() {
	[[ -f $ENV_FILE ]] || die "$ENV_FILE not found: cp .env.production.example .env, then fill it in"
	if grep -Eq '^[^#]*CHANGE_ME' "$ENV_FILE"; then # values only, not the comments
		die "$ENV_FILE still has CHANGE_ME placeholders"
	fi
}

# The value of NAME in .env (the last assignment wins), without surrounding quotes; empty if
# absent. Reads the file instead of sourcing it, so nothing in it is ever executed.
env_value() {
	local line value
	line=$(grep -E "^$1=" "$ENV_FILE" | tail -n 1) || true
	value=${line#*=}
	value=${value%$'\r'}
	if [[ $value =~ ^\"(.*)\"$ || $value =~ ^\'(.*)\'$ ]]; then
		value=${BASH_REMATCH[1]}
	fi
	printf '%s' "$value"
}
