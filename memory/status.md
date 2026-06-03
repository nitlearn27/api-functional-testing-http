# Status

## Live test results (api_test_suite_sample.numbers)
- **TC-001 — PASS**: POST /orders (valid) → 201; response matches `json_subset`; expected log
  strings found in CloudHub logs (via whole-log fallback).
- **TC-002 — FAIL**: POST /orders missing `qty` expects 400 VALIDATION_ERROR, but the API
  returns **201 ACCEPTED** (creates the order). This is a **real API finding** (missing-field
  validation gap), not a framework bug. Watch this flip to PASS once the API is fixed.
  - TC-002 also has a data issue in the sheet at times: smart-quote JSON in `expected_response`
    (invalid) — fix to straight quotes for field-level comparison.

## Implemented
read_test_suite (.numbers + .xlsx), call_api (httpx), assert_response (+`<<any>>`),
snapshot_logs/validate_logs, AnypointLogSource + client-credentials auth, batched run_suite,
results write-back with `correlation_id` column. Tests + ruff green.

## Deferred
- Build the CloudHub log URL dynamically from application id + version (fixed in `.env` now;
  isolated in `AnypointLogSource._log_url()`).
- Target-API OAuth (`tools/auth.get_auth_token`) — only needed for `auth_required=yes` cases.
- Strict per-correlation log matching — needs the Mule app to log the inbound X-Correlation-ID.

_Update this file as things change._
