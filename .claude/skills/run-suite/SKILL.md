---
name: run-suite
description: Run an existing API functional test suite end-to-end against the live API via the deployed api-log-test MCP worker (the run_suite tool), then download the results workbook (with per-case evidence tabs) into resources/. Use when the user asks to run a prebuilt suite, run-and-record, or execute tests from an existing .xlsx suite or a server-stored suite_id. To run from an OpenAPI schema instead, use the run-schema skill.
---

# Run an existing suite and record the results

Runs a prebuilt suite end-to-end on the deployed `api-log-test` worker via the **`run_suite`** tool
(real HTTP requests + response assertions, and CloudHub log validation when configured), then
downloads the results `.xlsx` — a `tests` summary sheet plus one evidence tab per case (request,
response, diffs) — into `resources/`.

## Input — two ways to name the suite
- **A local `.xlsx` path** (e.g. `resources/employee-api-oas_suite.xlsx`) — uploaded to the run.
- **A server-stored suite** as `id:<suite_id>` — a `suite_id` from a prior `run_schema`, or from a
  manual upload (no file upload; the worker already has it, valid ~2h):
  ```bash
  curl -s --data-binary @<suite>.xlsx \
    -H "Authorization: Bearer $API_LOG_TEST_TOKEN" \
    "https://api-log-test-worker.nit4infy1.workers.dev/files?filename=<suite>.xlsx"
  ```
  This returns `{suite_id, case_count, ...}` — the upload path to use from Claude Desktop (or
  anywhere), since base64-ing a large workbook through the model is slow and unreliable.

## Steps
1. Determine the source (local path or `id:<suite_id>`) and an output path
   `resources/<name>_results.xlsx`.
2. Run from the `worker/` directory:
   ```bash
   cd worker
   MCP_URL="https://api-log-test-worker.nit4infy1.workers.dev/mcp" \
   MCP_TOKEN="$API_LOG_TEST_TOKEN" \
     npx tsx scripts/run-suite.mts <SOURCE> ../resources/<name>_results.xlsx
   ```
   (`<SOURCE>` is an absolute `.xlsx` path or `id:<suite_id>`. `MCP_TOKEN` is the bearer the
   worker requires on `/mcp`; export `API_LOG_TEST_TOKEN` in your shell first.)
3. Report the per-case PASS/FAIL table, the results download link, and the saved results path.

## Notes
- This runs an EXISTING suite. To generate a suite from an OpenAPI schema and run it in one step,
  use the `run-schema` skill (the `run_schema` tool).
- The run is async on the worker (returns a job_id and the script polls). Suites with
  `validate_logs` cases also wait ~60s+ for CloudHub log propagation, so the run can take a few
  minutes — that is expected (durable DO alarms drive the waits).
- CloudHub log validation only works once the Anypoint secrets are set (`wrangler secret put …`);
  otherwise those cases fail on the log step.
- The suite's `Basepath` must point at a reachable API. A placeholder base URL (e.g.
  `api.example.com`) will make every request fail — that is a real finding, not a tool bug.
