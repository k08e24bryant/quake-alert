#!/usr/bin/env bash
# Deploy: update the checkout, pull the images, start PostgreSQL and Redis, run the
# migrations, (re)start api, worker and caddy, and wait until the API is ready, both inside
# the stack and at https://API_DOMAIN/readyz.
#
#   ./deploy.sh
#
# Rolling back: set IMAGE_TAG in .env to an earlier sha-<commit> tag and run it again.
# If the migrations fail, the script stops before touching api/worker: the running version
# keeps serving.
#
# For testing the script itself (not for normal deploys):
#   SKIP_GIT_PULL=1      don't `git pull`
#   SKIP_PULL=1          use the images already on this machine
#   SKIP_PUBLIC_CHECK=1  don't check https://API_DOMAIN/readyz (e.g. before DNS is set)
set -euo pipefail

# shellcheck source=deploy/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

WAIT_TIMEOUT_SECONDS=${WAIT_TIMEOUT_SECONDS:-300}

main() {
	require_env_file
	local domain
	domain=$(env_value API_DOMAIN)
	[[ -n $domain ]] || die "API_DOMAIN is not set in $ENV_FILE"

	if [[ ${SKIP_GIT_PULL:-0} != 1 ]]; then
		log "updating the checkout"
		git -C "$DEPLOY_DIR" pull --ff-only
	fi

	if [[ ${SKIP_PULL:-0} != 1 ]]; then
		log "pulling images"
		compose --profile deploy pull --quiet
	fi

	log "starting db and redis"
	compose up -d --wait --wait-timeout "$WAIT_TIMEOUT_SECONDS" db redis

	log "running migrations"
	compose --profile deploy run --rm migrate

	log "starting api, worker and caddy"
	compose up -d --wait --wait-timeout "$WAIT_TIMEOUT_SECONDS" --remove-orphans

	log "checking /readyz inside the stack"
	compose exec -T api python -c \
		"import urllib.request; print(urllib.request.urlopen('http://localhost:8000/readyz', timeout=5).read().decode())"

	if [[ ${SKIP_PUBLIC_CHECK:-0} != 1 ]]; then
		log "checking https://$domain/readyz (the first deploy waits for its certificate)"
		curl -fsS --retry 20 --retry-delay 3 --retry-all-errors --max-time 10 "https://$domain/readyz"
		echo
	fi

	compose ps
	docker image prune -f >/dev/null
	log "deployed"
}

main "$@"
