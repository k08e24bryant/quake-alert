#!/usr/bin/env bash
# Point the production bot at https://API_DOMAIN/v1/telegram/webhook (setWebhook with
# secret_token), then show what Telegram has registered.
#
#   ./set_telegram_webhook.sh
#   DROP_PENDING_UPDATES=true ./set_telegram_webhook.sh   # discard updates queued meanwhile
#
# Reads TELEGRAM_BOT_TOKEN, TELEGRAM_WEBHOOK_SECRET and API_DOMAIN from .env. The token
# and the secret never appear on a command line (curl reads them from stdin) or in output.
# Refuses a *_dev_bot token: the dev bot is for local polling and synthetic quakes only.
set -euo pipefail

# shellcheck source=deploy/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

TOKEN=""

# telegram METHOD [CURL_CONFIG_LINE...]: call the Bot API, print the JSON answer.
telegram() {
	local method=$1
	shift
	{
		printf 'url = "https://api.telegram.org/bot%s/%s"\n' "$TOKEN" "$method"
		if (($# > 0)); then
			printf '%s\n' "$@"
		fi
	} | curl -sS --max-time 20 --config -
}

# json_field PYTHON_EXPRESSION: evaluate it on the JSON from stdin, bound to `d`.
json_field() {
	python3 -c "import json, sys; d = json.load(sys.stdin); print($1)"
}

main() {
	require_env_file
	TOKEN=$(env_value TELEGRAM_BOT_TOKEN)
	local secret domain
	secret=$(env_value TELEGRAM_WEBHOOK_SECRET)
	domain=$(env_value API_DOMAIN)
	[[ -n $TOKEN ]] || die "TELEGRAM_BOT_TOKEN is not set in $ENV_FILE"
	# Length checked apart: some regex engines cap {m,n} at 255.
	if [[ ! $secret =~ ^[A-Za-z0-9_-]+$ ]] || ((${#secret} > 256)); then
		die "TELEGRAM_WEBHOOK_SECRET must be 1-256 characters of A-Z a-z 0-9 _ -"
	fi
	[[ -n $domain ]] || die "API_DOMAIN is not set in $ENV_FILE"

	local me username
	me=$(telegram getMe)
	username=$(json_field 'd["result"]["username"] if d.get("ok") else ""' <<<"$me")
	[[ -n $username ]] || die "getMe failed: $(json_field 'd.get("description")' <<<"$me")"
	if [[ ${username,,} == *_dev_bot ]]; then
		die "refusing: @$username is a dev bot; put the production bot's token in $ENV_FILE"
	fi

	local url="https://$domain/v1/telegram/webhook"
	log "setting the webhook of @$username to $url"
	local result
	result=$(telegram setWebhook \
		"data-urlencode = \"url=$url\"" \
		"data-urlencode = \"secret_token=$secret\"" \
		'data-urlencode = "allowed_updates=[\"message\"]"' \
		"data-urlencode = \"drop_pending_updates=${DROP_PENDING_UPDATES:-false}\"")
	if [[ $(json_field 'd.get("ok")' <<<"$result") != True ]]; then
		die "setWebhook failed: $(json_field 'd.get("description")' <<<"$result")"
	fi

	log "registered:"
	# No f-string: Ubuntu 22.04's Python 3.10 can't take backslashes in one.
	telegram getWebhookInfo | json_field '"\n".join(
		"  %s: %s" % (k, d["result"].get(k))
		for k in ("url", "pending_update_count", "last_error_date", "last_error_message", "allowed_updates")
	)'
}

main "$@"
