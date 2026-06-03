# API Functional Testing MCP Server

An MCP server that runs API functional tests and validates Mule/CloudHub log lines for each
transaction. This repository currently implements the **start-now slice** (dev-plan
Phases 1–2): a runnable server with the full no-network core working against mock data, plus
typed stubs for the credential-dependent tools.

See `api-log-test-mcp-dev-plan.md` for the full phased plan.

## Status

| Tool | Phase | Status |
|------|-------|--------|
| `read_test_suite` | 2 | ✅ implemented |
| `assert_response` | 2 | ✅ implemented |
| `snapshot_logs` | 2 | ✅ implemented (file/mock backend) |
| `validate_logs` | 2 | ✅ implemented |
| `call_api` | 3 | ✅ implemented (httpx, column-driven) |
| `run_suite` | 5 | ✅ implemented (honors `auth_required` / `validate_logs`) |
| `get_auth_token` | 3 | 🚧 stub (only needed when `auth_required=yes`) |

## Quick start

```bash
uv sync --all-extras --dev      # create env + install deps
uv run ruff check .             # lint
uv run pytest -q                # tests
```

### Run the server

```bash
uv run api-log-test-mcp                       # stdio transport
uv run fastmcp dev src/api_log_test_mcp/server.py   # MCP Inspector
```

## Configuration

Settings load from environment variables (prefix `ALT_`) or a `.env` file. For the mock log
pipeline:

```bash
ALT_LOG_BACKEND=file
ALT_FILE_LOG_PATH=tests/fixtures/sample_app.log
```

Target-API (`ALT_BASE_URL`, OAuth env refs) and Anypoint settings are declared but unused
until Phases 3–4. Credentials are referenced by env-var *name* and never read from the sheet.

## Excel suite schema (provisional — pending Gate A sign-off)

| Column | Meaning |
|--------|---------|
| `test_id` | unique key; also the correlation-ID join key |
| `method`, `url` | request line |
| `headers`, `body` | JSON in a single cell |
| `expected_status` | int |
| `expected_response` | JSON in a cell |
| `match_mode` | `exact` / `json_subset` (default) / `schema` |
| `ignore_paths` | comma/newline list of dotted paths (`*` wildcard) |
| `expected_logs` | JSON array of strings (newline / `\|\|` delimiter accepted as fallback) |
| `log_match_mode` | `contains` (default) |

The schema lives in `src/api_log_test_mcp/tools/suite.py` (`COLUMNS`) so a change is localized.

## Adding a new LogSource

Implement `LogSource` (`src/api_log_test_mcp/logsource/base.py`): provide `discover_instances`
and `snapshot`. Wire it into `build_log_source` in `tools/logs.py` and add a `LogBackend`
value. The snapshot store handles correlation indexing, so backends only do the download.
