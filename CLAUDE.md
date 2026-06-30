# CLAUDE.md

Project guidance for Claude Code. Keep this concise; deeper notes live in [`memory/`](memory/).

## What this is

An **MCP server** for API functional testing with Mule/CloudHub log validation. It reads a
test suite from a spreadsheet, fires real HTTP requests, asserts the responses, optionally
downloads CloudHub logs and validates expected log strings, and writes timestamped results
back into the suite sheet.

## Tooling (always use uv)

- Package/deps: **uv** (`uv` lives at `~/.local/bin` — prefix commands with
  `export PATH="$HOME/.local/bin:$PATH"`).
- Test: **pytest** · Lint/format: **ruff** · Python **>= 3.11** · ruff line length **100**.

```bash
export PATH="$HOME/.local/bin:$PATH"
uv sync --all-extras --dev      # install
uv run ruff check .             # lint
uv run pytest -q                # tests (all offline; network is mocked)
uv run fastmcp dev inspector -m api_log_test_mcp.server:mcp   # MCP Inspector (FastMCP 3.x; -m required)
```

## Run a suite (live)

```python
from api_log_test_mcp.tools.orchestrate import run_and_record
report, run_at = run_and_record("api_test_suite_sample.numbers")
```

Use `run_and_record` (not bare `run_suite`) — it runs **and** records results into a **separate
`<stem>_results.xlsx`** (e.g. `foo_suite.xlsx` → `foo_results.xlsx`); the suite file is **never
modified**. The results workbook is seeded from the suite (so it keeps the case definitions) and
gets a stacked timestamped `RESULTS` block + one evidence tab per case. It returns
`(report, run_at, results_path)`. It runs locally, so requests originate from this machine and can
reach `localhost` (a locally-run Mule app) or any public URL — Cloudflare is not involved. For a
**responses-only** run (HTTP + status/body assertions, no auth, no logs), give a suite whose cases
are `auth_required=No` + `validate_logs=No` (the log phase is then skipped entirely). When the
target app is down, every case fails with `App not running / unreachable at <url>`
(`tools/http_runner.py`).

## Layout

```
src/api_log_test_mcp/
  server.py                 # FastMCP entry; registers all 7 tools
  config.py                 # Settings (ALT_ prefix) + AnypointSettings (unprefixed) + .env loader
  models.py                 # Pydantic contract (TestCase, TestSuite, CaseReport, SuiteReport, ...)
  matching/response_matcher # exact | json_subset | schema | status_only; ignore_paths; <<any>>
  logsource/                # LogSource ABC; FileLogSource (mock); AnypointLogSource + auth
  cache/snapshot_store.py   # ephemeral snapshot + correlation-id index
  tools/                    # suite (parser), suite_generator (schema→suite + from-application),
                            #   mule_app (Mule folder reader), http_runner (call_api), logs,
                            #   orchestrate, response, results_writer, auth (stub)
tests/                      # pytest; httpx MockTransport for network; conftest builds fixtures
```

## Tools (MCP)

**Three primary tools** (create vs run cleanly separated):
- `create_test_suite_from_schema(schema_path)` — OpenAPI 3.0 in → suite `.xlsx`. Walks every
  path×method generically (incl. **query params + header params**: required ones sent on the happy
  path, negatives for missing/invalid). Does NOT run. (`tools/suite_generator.generate_test_suite`)
- `create_test_suite_from_application(app_root)` — point it at a Mule app root; builds cases from
  the **flow logic ONLY** (`src/main/mule/*.xml`): base path, endpoints, a positive case per
  endpoint/`choice` branch asserting the flow's DataWeave response + loggers, and a case per
  error-handler mapping (404/405/406/415/400) with the real `{message}` body + `APIKIT:*`. It does
  **NOT** read the OpenAPI schema — that's `create_test_suite_from_schema`'s job (the two are kept
  deliberately separate). Does NOT run.
  (`tools/suite_generator.create_test_suite_from_application` + `tools/mule_app.parse_mule_app`)
- `run_test_suite(suite_path, job_id)` — run a suite (from either create tool, optionally hand-edited)
  against its `Basepath` and write results to a **separate** `<stem>_results.xlsx`. Runs asynchronously in
  the background to prevent Claude Code stdio timeouts. Returns a `job_id` initially; call it again with only the
  `job_id` to poll progress percentage (e.g. `progress_percent: 34` and `"detail": "Waiting for log propagation..."`).
  (`run_and_record`)

Generated cases default `validate_logs=No` (logs deferred); the log strings are still populated so
log validation can be switched on later. These three are the **only** MCP tools exposed. The
building blocks they call (`tools/suite.read_test_suite`, `tools/http_runner.call_api`,
`matching/response_matcher.assert_response`, `tools/logs.snapshot_logs`/`validate_logs`,
`orchestrate.run_suite`, `tools/auth.get_auth_token`) remain importable from their modules but are
no longer registered as tools.

## Suite sheet (`.numbers` or `.xlsx`)

- A metadata block on top (`Basepath | <url>` and `application_logs_fetch_url | <url>`), then a
  header row located by finding the `test_id` column (not assumed to be row 1), then one row per
  case. The `application_logs_fetch_url` row is the CloudHub log-fetch URL — read from the sheet
  (not `.env`), and **required** when any case validates anypoint logs.
- Columns: `test_id, description, method, url, headers, body, auth_required, expected_status,
  expected_response, response_match_mode, validate_logs, expected_log_strings, log_match_mode,
  log_source`. Schema lives in `tools/suite.py` `COLUMNS` (with aliases).
- `headers/body/expected_response` are JSON-in-a-cell; `expected_log_strings` is a JSON array.
- `<<any>>` as an expected value = field must exist, value not compared.
- **Results are appended back into the same sheet** below the cases as timestamped
  `RESULTS — run <ts>` blocks (never a separate sheet/columns). Each block includes a
  `correlation_id` column for log evidence. Parsing stops at the first `RESULTS` marker.

## Config / .env

- Main settings use the `ALT_` env prefix (e.g. `ALT_FILE_LOG_PATH`,
  `ALT_PROPAGATION_WAIT_SECONDS` (default 60), `ALT_LOG_FETCH_MAX_RETRIES` (default 3),
  `ALT_LOG_FETCH_RETRY_WAIT_SECONDS` (default 60), `ALT_LOG_CORRELATION_FALLBACK`).
- **Anypoint** credentials are read from `.env` with plain lowercase keys (no prefix):
  `token_endpoint`, `client_id`, `client_secret`, `grant_type`. The loader accepts both `=` and
  `:` separators. `.env` is gitignored — never commit secrets, never print the token/secret.
  The per-suite log-fetch URL (`application_logs_fetch_url`) is **not** read from `.env` for a
  hand-written sheet — it travels with the suite and is injected by `tools/logs.build_log_source`.
- `deployments_base_url` (`.env`, plain lowercase; a Worker secret) is the `.../deployments` base.
  Suite generation appends `/<deployment-id>` — the first UUID in the spec's
  `servers[0].description` — to **auto-fill** the generated suite's `application_logs_fetch_url`
  (blank if the base or id is missing).
- **Worker tools** are THREE: **`run_schema`** (OpenAPI schema → generate the suite AND run it, in
  one call) and **`run_suite`** (run an existing suite_id / uploaded `.xlsx`) both *run* tests;
  **`create_test_case_all`** only *creates* a suite — model-analyzed `cases` → render the `.xlsx`
  and return it (`suite_id` + `suite_download_url`), it does NOT run them (run the created suite
  separately with `run_suite` / Python `run_and_record`). `create_test_case_all` is for sources
  that aren't a clean schema, e.g. a MuleSoft app: the model reads the flows/logic **and** the
  schema (incl. query params + headers) client-side and sends only the distilled cases. Skills:
  `run-schema`, `run-suite`, and `analyze-mule` (the Mule-analysis driver, calling
  `create_test_case_all` via `worker/scripts/create-cases.mts`). The Python server keeps its
  granular tools (`generate_test_suite`, `generate_and_run`, `run_and_record`, `run_suite`, …) —
  see the "Tools (MCP)" section.

## Conventions / guardrails

- Match the surrounding code style; keep comments at the existing density.
- Tests must stay offline — mock HTTP with `httpx.MockTransport`.
- Writing results into `.numbers` backs up + verifies the definition rows didn't change
  (numbers-parser `save()` can mangle cells) and restores on mismatch. Don't bypass that.
- Do not rewrite `.env` programmatically (it's an untracked secrets file).
- When generating test cases from a spec (incl. the `analyze-mule` flow), always account for the
  schema's **query params and header params** — send the required ones in the happy path (a missing
  required query param/header makes APIkit return 400) and add negatives for missing/invalid ones.
  Easy to miss because the Mule XML doesn't list them — only the schema does.

## Current status (see memory/ for detail)

- Live: TC-001 PASS; TC-002 FAIL — the API returns 201 for a missing-`qty` order instead of
  400 (a real API finding, not a framework bug).
- CloudHub log validation works but uses **whole-log fallback** because the app doesn't log the
  inbound `X-Correlation-ID` yet.
- The generated-suite log URL is now built from `deployments_base_url` + the spec's deployment id
  at generation time (no longer hand-fixed in `.env` for the generate path). Still deferred:
  target-API OAuth.
