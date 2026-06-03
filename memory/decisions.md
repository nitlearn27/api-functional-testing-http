# Decisions

## Build scope
Built in vertical slices off `api-log-test-mcp-dev-plan.md`. Currently implemented:
read suite → call API → assert response → fetch CloudHub logs → validate log strings → record
results. Deferred: dynamic log URL (app id + version), target-API OAuth (`get_auth_token`).

## Tooling
**uv** (env/deps) + **pytest** + **ruff**, Python ≥ 3.11. Chosen for a fast single-tool setup.
`uv` is installed at `~/.local/bin`.

## Results recorded into the sheet, every run
The user runs the suite repeatedly and wants an in-sheet history to see when a case flips
FAIL→PASS. So every run appends a timestamped **`RESULTS — run <ts>`** block in the **same
`tests` sheet, below the cases** — not a separate sheet, not extra per-run columns (this layout
was explicitly chosen over a column-per-run matrix). Use
`tools.orchestrate.run_and_record(path)`.

## Correlation id as evidence
Each results block includes a `correlation_id` column = the id stamped on that test's request
(`X-Correlation-ID`), recorded even if the request fails, so logs can be traced later.

## Response matching
`response_match_mode`: `exact | json_subset (default) | schema | status_only`, with
`ignore_paths` for volatile fields and the `<<any>>` wildcard (field must exist, value not
compared).
