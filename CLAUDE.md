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

Use `run_and_record` (not bare `run_suite`) — it runs **and** records results into the sheet.

## Layout

```
src/api_log_test_mcp/
  server.py                 # FastMCP entry; registers all 7 tools
  config.py                 # Settings (ALT_ prefix) + AnypointSettings (unprefixed) + .env loader
  models.py                 # Pydantic contract (TestCase, TestSuite, CaseReport, SuiteReport, ...)
  matching/response_matcher # exact | json_subset | schema | status_only; ignore_paths; <<any>>
  logsource/                # LogSource ABC; FileLogSource (mock); AnypointLogSource + auth
  cache/snapshot_store.py   # ephemeral snapshot + correlation-id index
  tools/                    # suite (parser), http_runner (call_api), logs, orchestrate,
                            #   response, results_writer, auth (stub)
tests/                      # pytest; httpx MockTransport for network; conftest builds fixtures
```

## Tools (MCP)

`read_test_suite`, `call_api`, `assert_response`, `snapshot_logs`, `validate_logs`,
`run_suite` are implemented. `get_auth_token` (target-API OAuth) is still a stub — only needed
when a case sets `auth_required=yes`.

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
- **Worker tools** are consolidated to TWO, both of which *run* tests: **`run_schema`** (OpenAPI
  schema → generate the suite AND run it, in one call) and **`run_suite`** (run an existing
  suite_id / uploaded `.xlsx`). Skills: `run-schema` and `run-suite` (scripts of the same names in
  `worker/scripts/`). The Python server keeps its granular tools (`generate_test_suite`,
  `generate_and_run`, `run_and_record`, `run_suite`, …) — see the "Tools (MCP)" section.

## Conventions / guardrails

- Match the surrounding code style; keep comments at the existing density.
- Tests must stay offline — mock HTTP with `httpx.MockTransport`.
- Writing results into `.numbers` backs up + verifies the definition rows didn't change
  (numbers-parser `save()` can mangle cells) and restores on mismatch. Don't bypass that.
- Do not rewrite `.env` programmatically (it's an untracked secrets file).

## Current status (see memory/ for detail)

- Live: TC-001 PASS; TC-002 FAIL — the API returns 201 for a missing-`qty` order instead of
  400 (a real API finding, not a framework bug).
- CloudHub log validation works but uses **whole-log fallback** because the app doesn't log the
  inbound `X-Correlation-ID` yet.
- The generated-suite log URL is now built from `deployments_base_url` + the spec's deployment id
  at generation time (no longer hand-fixed in `.env` for the generate path). Still deferred:
  target-API OAuth.
