# Development Plan: API Functional Testing MCP Server

**Companion to:** the architecture document. This is the step-by-step execution plan.

**Sequencing principle:** front-load the things that can invalidate the whole approach (decisions + risky integrations), build the no-network pieces first so there's a runnable artifact early, and defer credential-dependent work until it's actually unblocked. Each phase ends with a concrete "done when" so progress is unambiguous.

---

## Phase 0 — Decisions and access (do before writing code)

Nothing below is coding; it is the unblocking work. Skipping it means rework later.

1. **Lock the Excel schema.** Agree the columns and — critically — how multiple expected log strings are encoded per row (delimiter, JSON array in one cell, or a linked second sheet keyed by `test_id`). Get one real sample sheet.
2. **Confirm Anypoint access.** Secure an Anypoint Platform user with the right role for the Access Management API token; obtain the org ID and env ID for the target environment; confirm which log endpoint variant applies (app-level, per-deployment, or per-instance).
3. **Confirm correlation ID behaviour.** Verify the Mule apps log a correlation ID and accept an inbound `X-Correlation-ID`. This is the join key between an API call and its log lines — if it doesn't exist, log validation can't reliably attribute lines to a test.
4. **Confirm target-API auth details.** OAuth2 client-credentials token URL, scopes, and where credentials will live (env/secret store).
5. **Agree defaults.** Response match mode (proposed `json_subset`) and log match mode (proposed `contains`); propagation wait time; environment to run against and how mutating tests are handled.

**Done when:** schema is signed off, a sample sheet exists, and Anypoint + OAuth credentials are obtainable. Phases 1–2 can start in parallel with items 2–3 if needed, since they don't touch the network.

---

## Phase 1 — Project scaffold

1. Set up the Python project (packaging, dependency management, lint/format, test runner).
2. Add FastMCP; create the server entry point.
3. Declare all tools as **stubs** with final names, typed signatures, and docstrings — no logic yet. This fixes the tool contract early so the orchestrator side and the implementation side agree.
4. Wire up config loading (base URL, token URL, log backend selection, credential references) from env/file.
5. Stand up CI: run tests + lint on every commit.

**Tools stubbed:** `read_test_suite`, `get_auth_token`, `call_api`, `assert_response`, `snapshot_logs`, `validate_logs` (and a placeholder `run_suite`).

**Done when:** the server starts, lists all tools in the MCP Inspector, and CI is green on an empty test suite.

---

## Phase 2 — The no-network core (build first; highest certainty, fastest feedback)

These have no external dependencies, so they're fully unit-testable and give you a working, demonstrable slice before any credentials exist.

1. **`read_test_suite`** — Excel parser → structured JSON test cases. Includes normalization (coerce types, parse header/body cells), default application, and a `parse_errors` list for malformed rows. Build against the real sample sheet from Phase 0.
2. **`assert_response`** — the `ResponseMatcher`. Implement the match modes (`exact`, `json_subset`, `schema`) with ignore-paths for volatile fields (timestamps, generated IDs).
3. **`LogSource` interface + `FileLogSource` (mock)** — the abstraction, plus a file-backed implementation that reads a sample log file. This lets the log tools be built and tested with zero backend access.
4. **`snapshot_logs` + `validate_logs` against the mock** — snapshot reads the sample file into the ephemeral cache and builds the correlation-ID index; validate filters by correlation ID and checks expected strings. Prove the download-once / validate-locally / discard lifecycle end to end on the mock.

**Done when:** all four areas have passing unit tests, and you can run a full mock suite (read → assert → snapshot → validate) without touching the network.

---

## Phase 3 — Target-API integration (OAuth2 + live calls)

1. **`get_auth_token`** — OAuth2 client-credentials flow with in-process caching and refresh-on-expiry. Credentials sourced from env/secret store, never the sheet.
2. **`call_api`** — the `HttpRunner`: fire the request, stamp the correlation ID, return status/headers/body/latency. Handle timeouts and transport errors cleanly.
3. Integration-test against a real (non-production) target API endpoint: token acquisition, a successful call, and a deliberately failing assertion.

**Depends on:** Phase 0 item 4. Can run in parallel with Phase 2 if a developer is free, but Phase 2 is the priority since it's lower-risk.

**Done when:** a real authenticated call to the target API runs and its response is asserted correctly.

---

## Phase 4 — Anypoint log integration (the riskiest external piece)

This is deliberately late: it depends on access that may take time to provision, and it's where the rate limits and auth complexity live.

1. **`AnypointAuthProvider`** — separate from the target-API auth. Acquire the platform bearer token via Access Management API; resolve and cache org ID and env ID. Keep this credential set strictly isolated from the OAuth2 one.
2. **`AnypointLogSource`** — implement `snapshot()` against the real CloudHub instance log-file download. Critical behaviours:
   - One full download per instance per run (respect 1/min limit).
   - Discover active instances from application status (multi-worker apps need one download each).
   - Client-side throttling + backoff/retry on HTTP 500 (file-limit) and 429.
   - Parse the downloaded log into the correlation-ID index on arrival.
3. Validate against a real deployed app: fire a known transaction, download the log once, confirm the correlation ID and expected lines are found.

**Depends on:** Phase 0 items 2–3, and Phase 3 (you need a real call to generate a real log line to find).

**Done when:** an end-to-end real run works — call the live API, download the instance log once, and validate the expected log strings for that transaction.

---

## Phase 5 — Orchestration and reporting

1. **`run_suite`** — the batched flow: all API calls + response assertions first → propagation wait → one snapshot per instance → validate all cases locally → discard snapshot (in a `finally`) → emit report. Add the `retain_snapshots` debug flag (default off).
2. **Results output** — structured report (pass/fail per case, response diffs, matched/missing log strings) plus optional write-back to Excel/JSON for sharing.

**Done when:** a single `run_suite` call executes a full real suite and produces a complete, shareable results report.

---

## Phase 6 — Packaging, hardening, docs

1. Package as a pip-installable module and/or Docker image.
2. Support both transports: stdio (local/Claude) and Streamable HTTP (remote/CI).
3. Write the README/usage docs: config reference, the Excel schema, how to add a new `LogSource`, and a sample suite.
4. Hardening pass: rate-limit pacing under realistic suite sizes, secret-handling review (no credentials in logs/output), cleanup-on-failure verification.

**Done when:** a fresh project can install the server, point it at a sheet + config, and run a suite following only the docs.

---

## Phase 7 — Later enhancements (explicitly deferred)

- **`CloudWatchLogSource`** — drop-in `LogSource` implementation once/if needed; no changes to tools or suites.
- Additional backends (Splunk, ELK, Datadog) on the same interface.
- A thin Skill layer on top, if conversational Claude-driven runs are wanted.
- Setup/teardown hooks for mutating tests.

---

## Critical path and parallelism

- **Critical path:** Phase 0 (items 2–3) → Phase 4 is the long pole, because Anypoint access provisioning and log integration carry the most uncertainty. Start the access requests on day one.
- **Parallelizable:** Phases 2 and 3 can overlap. Phase 2 needs only the sample sheet; Phase 3 needs only the OAuth details.
- **Lowest-risk-first:** Phase 2 delivers a runnable, demoable system on the mock backend before any live credential exists — good for early validation of the whole concept.

## Decision gates (don't pass without sign-off)

- **Gate A (before Phase 2):** Excel schema + log-string encoding signed off.
- **Gate B (before Phase 4):** Anypoint access confirmed + correlation-ID behaviour verified.
- **Gate C (before Phase 6):** end-to-end real run demonstrated and reviewed.
