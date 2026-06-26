---
name: run-schema
description: From an OpenAPI/YAML schema, generate an API functional test suite AND run it end-to-end against the live API via the deployed api-log-test MCP worker — in one step (the run_schema tool). The suite's CloudHub log-fetch URL is built automatically from the schema's server description (deployment id). Use when the user asks to run a schema/spec, test an API from its OpenAPI spec end-to-end, or "pass a yaml and get the results".
---

# Run a schema: generate the suite and test it, in one step

Takes an OpenAPI 3.0 YAML schema, and via the deployed `api-log-test` worker's **`run_schema`**
tool generates a runnable `.xlsx` suite **and runs it** (real HTTP requests + response assertions +
CloudHub log validation) and records the results — saving both the generated suite and the results
workbook into `resources/`.

The suite's `application_logs_fetch_url` is filled in automatically: the worker takes its configured
`deployments_base_url` secret and appends the deployment id parsed from the schema's
`servers[0].description` (e.g. "…deployed in CloudHub with id 351c3653-…"). No manual edit step.

## Input
- A path to an OpenAPI 3.0 YAML schema (e.g. `resources/employee-api-oas.yaml`). The schema's
  `servers[0].url` is the live API to test; `servers[0].description` should contain the CloudHub
  deployment id so logs can be validated.

## Steps
1. Pick the schema path and derive outputs: `resources/<stem>_suite.xlsx` (generated suite) and
   `resources/<stem>_results.xlsx` (results).
2. Run from the `worker/` directory:
   ```bash
   cd worker
   MCP_URL="https://api-log-test-worker.nit4infy1.workers.dev/mcp" \
   MCP_TOKEN="$API_LOG_TEST_TOKEN" \
     npx tsx scripts/run-schema.mts ../resources/<schema>.yaml
   ```
   (Optionally pass a second arg for the results output path. `MCP_TOKEN` is the bearer the worker
   requires on `/mcp`; export `API_LOG_TEST_TOKEN` in your shell first.)
3. Report the generated suite (base path, case count, the filled `application_logs_fetch_url`),
   the per-case PASS/FAIL table, the results download link, and the saved paths.

## Notes
- `run_schema` generates AND runs. To run an already-built `.xlsx` suite (or a server-stored
  suite_id), use the `run-suite` skill / the `run_suite` tool instead.
- The run is async on the worker (returns a job_id; the script polls). Suites with `validate_logs`
  cases also wait ~60s+ for CloudHub log propagation, so the run can take a few minutes — expected.
- Log validation requires the worker secrets to be set (`token_endpoint`, `client_id`,
  `client_secret`, and `deployments_base_url`). If `deployments_base_url` is unset or the schema
  description has no deployment id, the log-fetch URL is left blank and log cases fail on the log
  step (HTTP/response assertions still run).
- A placeholder/unreachable `servers[0].url` makes every request fail — that's a real finding,
  not a tool bug.
