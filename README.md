# API Functional Testing MCP Server

An **MCP server** for API functional testing with Mule/CloudHub log validation. It reads a test
suite from a spreadsheet (`.numbers` or `.xlsx`), fires real HTTP requests, asserts the
responses, optionally downloads CloudHub logs and validates expected log strings per
transaction, and writes timestamped results back into the suite sheet.

See `api-log-test-mcp-dev-plan.md` for the full phased plan and [`memory/`](memory/) for design
notes and gotchas.

## Tools

| Tool | Status |
|------|--------|
| `read_test_suite` | ✅ parse a `.numbers`/`.xlsx` suite into structured cases |
| `assert_response` | ✅ `exact` / `json_subset` / `schema` / `status_only` (+ `ignore_paths`, `<<any>>`) |
| `call_api` | ✅ httpx runner, column-driven, stamps `X-Correlation-ID` |
| `snapshot_logs` | ✅ download logs once (file mock **or** Anypoint/CloudHub) |
| `validate_logs` | ✅ check `expected_log_strings`, correlation-scoped with whole-log fallback |
| `run_suite` | ✅ batched: call + assert → one log snapshot per run → validate → report |
| `get_auth_token` | 🚧 stub — only needed when a case sets `auth_required=yes` |

## Quick start

```bash
uv sync --all-extras --dev      # create env + install deps
uv run ruff check .             # lint
uv run pytest -q                # tests (all offline; network mocked)
```

### Run a suite (live)

```python
from api_log_test_mcp.tools.orchestrate import run_and_record
report, run_at = run_and_record("api_test_suite_sample.numbers")
```

Use `run_and_record` (not bare `run_suite`) — it runs **and** appends a timestamped results
block into the sheet. ⚠️ This makes **real HTTP calls** and writes back to the sheet.

## Use as an MCP server

Runs over **stdio**. Register it with your MCP client using an absolute `uv` path and
`--directory` pointing at your clone (so `.env` and sheets resolve).

**Claude Code (CLI):**
```bash
claude mcp add api-log-test -- ~/.local/bin/uv run \
  --directory /abs/path/to/api-functional-testing api-log-test-mcp
```

**Claude Desktop / any client (`mcpServers` JSON):**
```json
{
  "mcpServers": {
    "api-log-test": {
      "command": "/Users/<you>/.local/bin/uv",
      "args": ["run", "--directory", "/abs/path/to/api-functional-testing", "api-log-test-mcp"]
    }
  }
}
```

Inspect tools locally with: `uv run fastmcp dev src/api_log_test_mcp/server.py`.

## Configuration

Copy [`.env.example`](.env.example) to `.env` and fill in your own values. `.env` is gitignored —
never commit secrets.

- **Anypoint / CloudHub** (required only for `validate_logs=Yes` cases) — plain lowercase keys
  (no prefix): `application_logs_fetch_url`, `token_endpoint`, `client_id`, `client_secret`,
  `grant_type`. Token is acquired via OAuth2 **client-credentials**. The loader accepts both
  `=` and `:` separators.
- **Behaviour** (`ALT_` prefix): `ALT_FILE_LOG_PATH` (mock file log backend),
  `ALT_PROPAGATION_WAIT_SECONDS` (default 10), `ALT_LOG_CORRELATION_FALLBACK` (default true).

> Note: the CloudHub log URL is fixed in `.env` for now; building it dynamically from
> application id + version is isolated in `AnypointLogSource._log_url()` for later.

## Suite sheet schema

A metadata block on top (`Basepath | <base url>`), then a header row located by finding the
`test_id` column (not assumed to be row 1), then one row per case.

| Column | Meaning |
|--------|---------|
| `test_id` | unique id (also stamped into the per-test correlation id) |
| `description` | free text |
| `method`, `url` | request line; `url` is joined onto `Basepath` |
| `headers`, `body` | JSON in a single cell |
| `auth_required` | `yes`/`no` — when yes, a bearer token is attached (needs `get_auth_token`) |
| `expected_status` | int |
| `expected_response` | JSON in a cell; `<<any>>` as a value = field must exist, value not compared |
| `response_match_mode` | `exact` / `json_subset` (default) / `schema` / `status_only` |
| `ignore_paths` | comma/newline list of dotted paths (`*` wildcard) |
| `validate_logs` | `yes`/`no` — no = no log download at all |
| `expected_log_strings` | JSON array of strings (newline / `\|\|` delimiter accepted as fallback) |
| `log_match_mode` | `contains` (default) / `regex` / `all_of` / `any_of` |
| `log_source` | `anypoint` (CloudHub) or `file` (local mock) |

The schema lives in `src/api_log_test_mcp/tools/suite.py` (`COLUMNS`, with aliases) so changes
are localized. Results are appended back into the **same** sheet below the cases as timestamped
`RESULTS — run <ts>` blocks (each with a `correlation_id` column for evidence); parsing stops at
the first `RESULTS` marker.

## Adding a new LogSource

Implement `LogSource` (`src/api_log_test_mcp/logsource/base.py`): provide `discover_instances`
and `snapshot`. Wire it into `build_log_source` in `tools/logs.py` (keyed by the case's
`log_source` value). The snapshot store handles correlation indexing, so backends only do the
download.

## License

MIT — see [LICENSE](LICENSE).
