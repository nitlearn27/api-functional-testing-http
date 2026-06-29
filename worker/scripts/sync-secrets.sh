#!/usr/bin/env bash
# Push the Anypoint credentials from the repo-root .env to the deployed worker as
# wrangler SECRETS (deploy-proof, encrypted). Re-run whenever a value in .env changes.
#
# Values are piped straight to wrangler and never echoed. .env accepts both `=` and `:`
# separators. Run from the worker/ directory:  bash scripts/sync-secrets.sh
set -euo pipefail

ENV_FILE="${1:-../.env}"
[ -f "$ENV_FILE" ] || { echo "no .env found at $ENV_FILE" >&2; exit 1; }

for key in application_logs_fetch_url token_endpoint client_id client_secret grant_type deployments_base_url; do
  val=$(grep -E "^${key}[[:space:]]*[=:]" "$ENV_FILE" | head -1 \
        | sed -E "s/^${key}[[:space:]]*[=:][[:space:]]*//" | tr -d '\r')
  if [ -n "$val" ]; then
    printf '%s' "$val" | npx wrangler secret put "$key" >/dev/null
    echo "set $key (length ${#val})"
  else
    echo "MISSING $key in $ENV_FILE" >&2
  fi
done

echo "--- secrets now on the worker ---"
npx wrangler secret list
