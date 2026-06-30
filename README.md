# API Functional Testing MCP Server

An **MCP server** for API functional testing with Mule/CloudHub log validation. It reads a test
suite from a spreadsheet (`.numbers` or `.xlsx`), fires real HTTP requests, asserts the
responses, optionally downloads CloudHub logs and validates expected log strings per
transaction, and writes timestamped results back into the suite sheet.

See `api-log-test-mcp-dev-plan.md` for the full phased plan and [`memory/`](memory/) for design
notes and gotchas.

## Exposed MCP Tools

The server exposes exactly **three primary tools** to keep the client interface clean and prevent token bloat:

| Tool | Parameters | Description |
| :--- | :--- | :--- |
| `create_test_suite_from_schema` | `schema_path`, `output_path` | Generates a runnable `.xlsx` test suite from an OpenAPI 3.0 spec. Walks all paths and methods. *(Does not run tests)* |
| `create_test_suite_from_application` | `app_root`, `output_path` | Generates a runnable `.xlsx` test suite from a MuleSoft app directory by parsing its XML flow logic and routing branches. *(Does not run tests)* |
| `run_test_suite` | `suite_path`, `job_id` | Runs the test suite in the background. Return `job_id` to start, then query with `job_id` to get `progress_percent` status and final reports (prevents stdio timeouts). |


## Prerequisites

- **Python ≥ 3.11**
- **[uv](https://docs.astral.sh/uv/)** — the package manager this project uses. It installs to
  `~/.local/bin`:
  ```bash
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
  ```
- **[Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code)** (only needed to register
  this as an MCP server — see below).

## Quick start

```bash
git clone https://github.com/nitlearn27/api-functional-testing-http.git
cd api-functional-testing-http

uv sync --all-extras --dev      # create env + install deps from uv.lock
uv run ruff check .             # lint
uv run pytest -q                # tests (all offline; network mocked)
```

If the tests pass, the server is healthy and ready to register.

### Generate a suite from an OpenAPI spec

```python
from api_log_test_mcp.tools.suite_generator import generate_test_suite
generate_test_suite("resources/products-eapi1.yaml")   # -> resources/products-eapi1_suite.xlsx
```

Builds a comprehensive `.xlsx` suite (a positive case per operation plus one negative per
validation rule) in the exact format the parser/runner consume. Edit the generated sheet to
taste, then run it.

### Run a suite (live)

```python
from api_log_test_mcp.tools.orchestrate import run_and_record
report, run_at = run_and_record("resources/products-eapi1_suite.xlsx")
```

Use `run_and_record` (not bare `run_suite`) — it runs **and** appends a timestamped results
block into the sheet (plus a per-case evidence tab). ⚠️ This makes **real HTTP calls** and writes
back to the sheet; cases with `validate_logs=yes` also fetch CloudHub logs (needs `.env`).

## Use as an MCP server

Runs over **stdio**. Register it with your MCP client using an absolute `uv` path and
`--directory` pointing at your clone (so `.env` and sheets resolve).

**Claude Code (CLI):**
```bash
claude mcp add api-log-test -- ~/.local/bin/uv run \
  --directory /abs/path/to/api-functional-testing api-log-test-mcp

claude mcp list   # verify it shows api-log-test as connected
```

> Use the **absolute** path to `uv` and `--directory` — the MCP client launches the process
> without your shell's `PATH` or working directory, so relative paths often fail.

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

Inspect tools locally with the MCP Inspector (FastMCP 3.x):
```bash
uv run fastmcp dev inspector -m api_log_test_mcp.server:mcp
```
The `-m` (module) form is required so the package's relative imports resolve. Or list/call tools
without a browser:
```bash
uv run fastmcp list --command "python -m api_log_test_mcp.server"
```

## Configuration

Copy [`.env.example`](.env.example) to `.env` and fill in your own values. `.env` is gitignored —
never commit secrets.

```bash
cp .env.example .env   # then edit .env with your values
```

> Only required for `validate_logs=Yes` cases. Pure HTTP-assertion suites run without an `.env`.

- **Anypoint / CloudHub** (required only for `validate_logs=Yes` cases) — plain lowercase keys
  (no prefix): `application_logs_fetch_url`, `token_endpoint`, `client_id`, `client_secret`,
  `grant_type`. Token is acquired via OAuth2 **client-credentials**. The loader accepts both
  `=` and `:` separators.
- **Behaviour** (`ALT_` prefix): `ALT_FILE_LOG_PATH` (mock file log backend),
  `ALT_PROPAGATION_WAIT_SECONDS` (wait before the first log fetch, default 60),
  `ALT_LOG_FETCH_MAX_RETRIES` (re-fetch until each correlation id's logs appear, default 3),
  `ALT_LOG_FETCH_RETRY_WAIT_SECONDS` (wait between retries, default 60),
  `ALT_LOG_CORRELATION_FALLBACK` (default true; the orchestrated run forces strict
  correlation scoping regardless).

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
