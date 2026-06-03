# Anypoint / CloudHub log integration

## Flow
`run_suite` (tools/orchestrate.py) runs **all calls + response asserts first**, then — if any
case has `validate_logs=Yes` — waits `propagation_wait_seconds`, takes **one log download per
distinct `log_source`** (batched, respects CloudHub's ~1/min limit), validates each case's
`expected_log_strings`, and discards snapshots.

## Pieces
- `logsource/anypoint_auth.py` — `AnypointAuthProvider`: OAuth2 **client-credentials** token,
  cached with refresh. POSTs JSON, falls back to form-encoding.
- `logsource/anypoint_source.py` — `AnypointLogSource.snapshot()` GETs the CH2 `/logs/file`
  URL with the bearer token, parses text→lines (JSON shape handled as fallback), retries on
  429/500. `_log_url()` is isolated so a future **dynamic URL (app id + version)** is a
  one-method change.
- `cache/snapshot_store.py` builds a correlationId→lines index from the downloaded lines.

## .env (Anypoint keys; unprefixed, gitignored)
`application_logs_fetch_url`, `token_endpoint`, `client_id`, `client_secret`, `grant_type`.
Read via `config.get_anypoint_settings()` (uses `AnypointSettings`, `env_prefix=""`). The loader
`config._read_env_file` accepts both `=` and `:` separators. Never print the token/secret.

## Correlation caveat (important)
The CloudHub app does **not** log our inbound `X-Correlation-ID`, so per-correlation filtering
finds no lines and validation uses the **whole-log fallback**
(`Settings.log_correlation_fallback`, default True) — results show "logs ok (whole-log
fallback)". For strict per-test matching, the Mule app must log the inbound id; then set
`ALT_LOG_CORRELATION_FALLBACK=false`.
