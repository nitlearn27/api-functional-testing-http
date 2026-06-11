# API Log Test — Cloudflare Worker (TypeScript port)

A faithful TypeScript port of the Python `api-log-test-mcp` server, running as a **remote MCP
server on Cloudflare Workers over Streamable HTTP**. Same validation/matching/orchestration
logic; the I/O boundary changed to fit the platform (no filesystem).

## What changed vs. the Python version (and why)

| Area | Python | Worker | Reason |
|------|--------|--------|--------|
| Transport | FastMCP stdio | `McpAgent.serve("/mcp")` Streamable HTTP | Workers are HTTP, not stdio |
| Suite input | file path | base64 `.xlsx` in tool args | no filesystem |
| Results | appended into the file on disk | returned as base64 `.xlsx` | no filesystem |
| `.numbers` | supported | **dropped** | no JS reader; only `.yaml` + `.xlsx` |
| `file` log source | reads a local file | **unsupported** (only `anypoint`) | no filesystem |
| Long waits | `time.sleep` (60s + 3×60s) | **DO alarms** (`this.schedule`) | don't block a request |
| Run model | one sync `run_and_record` | async `run_test_suite` (start, then re-call with `job_id`) | waits run durably in the background |

Logic that is byte-for-byte faithful (covered by the vitest suite, incl. a parity test against
the Python generator): response matching, suite parsing, suite generation, results/evidence
workbook, correlation indexing + log validation, Anypoint auth + log-URL resolution + retries,
and the request/log-validation orchestration.

## Architecture

```
Claude Desktop ──Streamable HTTP──▶ TestMcpServer (McpAgent DO)   ← /mcp front door, thin tools
                                         │
                                         ▼  getAgentByName(JobRunner, job_id)
                                    JobRunner (Agent DO, one per job_id)
                                         │  start → runRequests → logPhase* → finalize
                                         ▼  (DO alarms drive the 60s + 3×60s waits)
                                    target API + Anypoint/CloudHub (outbound fetch)
```

Job state (suite bytes, per-case runs, per-source retry counter, final report + result
workbook) lives in the JobRunner DO's SQLite, so a run survives MCP-session loss and DO
eviction. `run_test_suite` (with `job_id`) and `GET /jobs/{id}` re-resolve the same DO.

## Tools (exactly two, by design)

| Tool | Input | Output |
|------|-------|--------|
| `generate_test_suite` | `spec_yaml` (OpenAPI 3.0 YAML) | summary + `cases` + `suite_id` + `download_url` |
| `run_test_suite` | `suite_id` OR `file_b64` to start; `job_id` to check | report + `result_download_url` when done, else `{ job_id, status, detail, status_url, next_check_seconds }` |

`run_test_suite` waits ~15s in-call, so quick suites return their full report in one call;
log-validation runs hand back a `job_id` — call `run_test_suite` again with it (or watch the
plain-HTTP `status_url`). The internal helpers (parser, matcher, runner) are no longer exposed
as separate tools.

## HTTP endpoints (besides `/mcp`)

| Route | Purpose |
|-------|---------|
| `GET /health` | liveness check |
| `POST /files` | manual suite upload — send raw `.xlsx` bytes (`curl --data-binary @suite.xlsx "<base>/files?filename=suite.xlsx"`); validates the workbook and returns `{ suite_id, case_count, download_url }`. Use the `suite_id` in `run_test_suite` instead of base64-ing large files through an MCP client. Max 2 MB. |
| `GET /files/{id}` | download a stored suite/results workbook (capability URL; expires after 2 h) |

## Develop

```bash
npm install
npm run typecheck      # tsc --noEmit
npm test               # vitest (offline; fetch + xlsx mocked/in-memory)
npm run dev            # wrangler dev → http://localhost:8799/mcp
```

## Configure secrets (Anypoint)

Local: copy `.dev.vars.example` to `.dev.vars` and fill in the values.
Production: set them as Worker secrets (never commit):

```bash
bash scripts/sync-secrets.sh      # pushes all five from the repo-root .env in one go
```

(or one at a time with `wrangler secret put <name>`). Re-run the script whenever a value in
`.env` changes. Secrets set this way are encrypted, never appear in the bundle or config, and
**survive every deploy**.

> ⚠️ Do NOT enter credentials in the dashboard as plain-text *variables* — every
> `wrangler deploy` replaces the variable set with the `vars` from `wrangler.jsonc`, silently
> deleting dashboard-added text vars. Only the **Secret** type (or `wrangler secret put`)
> persists. Verify anytime with `wrangler secret list`.

Behaviour tunables (`PROPAGATION_WAIT_SECONDS`, `LOG_FETCH_MAX_RETRIES`,
`LOG_FETCH_RETRY_WAIT_SECONDS`) are `vars` in `wrangler.jsonc` with the same defaults as the
Python `ALT_*` settings (60 / 3 / 60).

## Deploy

```bash
wrangler login          # one-time, interactive (run yourself: ! wrangler login)
wrangler deploy
```

This prints your Worker URL, e.g. `https://api-log-test-worker.<subdomain>.workers.dev`.
The MCP endpoint is that URL + `/mcp`.

## Connect from Claude Desktop

Add it as a remote MCP server (Streamable HTTP) pointing at `…/mcp`. Then paste a `.yaml`
spec in chat (or upload a `.xlsx` via `POST /files` and use the returned `suite_id`); the model
calls `run_test_suite`, re-calling with the `job_id` until `complete`, then offers the results
download link back.

> Note: the production server is currently unauthenticated. Before exposing it publicly, put
> OAuth in front via `@cloudflare/workers-oauth-provider` (see the agents "Securing MCP" docs).
