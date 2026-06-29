---
name: analyze-mule
description: Analyze a MuleSoft application's flows AND its OpenAPI schema and CREATE an API functional test-case suite (.xlsx) from them via the api-log-test worker's create_test_case_all tool. YOU read the Mule XML flows, DataWeave transforms, error-handler, choice branches and loggers (plus the schema's query params, headers and body constraints), distill them into test cases, and the worker only renders them into the suite — it does NOT run them (run separately with the run-suite skill). Use when the user asks to create/generate a test suite from a MuleSoft app or "make test cases from the app".
---

# Analyze a MuleSoft app and create a test-case suite

Unlike `run-schema` (which sends a clean OpenAPI YAML and lets the worker parse it), a MuleSoft
app is a folder of XML flows, DataWeave transforms, an error-handler and `logger`/`choice`
components — its **internal logic** isn't in any schema. So **you** do the analysis client-side and
send only the distilled test cases to the worker's **`create_test_case_all`** tool, which renders a
runnable `.xlsx` suite and returns it (`suite_id` + `suite_download_url`). This tool only **creates**
the suite — it does NOT run the tests; run it separately with the `run-suite` skill / `run_suite`
tool (or the Python `run_and_record` for a local app).

The differentiator: cases assert **which flow path executed** via `expected_log_strings` (e.g. a
`gender == 'male'` POST must log `"first flow for male"`). That validates logic a schema can't.

## Input
- A MuleSoft app directory (e.g. `resources/test-enroll-impl4/`). Optionally a co-located OpenAPI
  spec (a sibling `*.yaml`, or the Anypoint Exchange OAS the `apikit:config` `api=` attribute
  references) for request schemas, **query params, header params**, and validation constraints.

## Steps

1. **Read the flows.** Open `src/main/mule/*.xml`. Extract:
   - **Base path** — from the `http:listener` `path` (e.g. `/api/*`) plus, if an OpenAPI spec is
     present, its `servers[0].url`. The full base is the deployed origin + listener base.
   - **Endpoints** — the APIkit implementation flows named `<method>:\<path>:<config>` (or
     `<method>:\<path>:<contentType>:<config>`), e.g. `get:\patients:…`,
     `post:\patients:application\json:…` → `GET /patients`, `POST /patients`.
   - **Query params & headers (from the OpenAPI spec — do NOT skip these).** For every operation,
     read its `parameters` and pull out `in: query` and `in: header` entries (plus any
     `requestBody` content types that imply a required `Content-Type`, and any `produces`/response
     content types that imply an `Accept`). Note which are `required: true` and each one's schema
     constraints (enum / pattern / min·maxLength / min·max / format). These drive both the
     happy-path request (send the required ones) and the negatives (omit/violate them). This is the
     step most easily missed — the Mule XML alone does not list query params or headers; the schema
     does, and APIkit validates them (a missing required query param or header is a 400).
   - **Success shape + status** — from each flow's DataWeave `ee:set-payload` (the mock/transform
     response). Default 200 for GET, 201 for create POSTs unless the flow sets `httpStatus`.
   - **Error mappings** — the global `error-handler`'s `on-error-*` blocks map APIkit error types
     to statuses + a JSON envelope (e.g. `APIKIT:BAD_REQUEST`→400, `:NOT_FOUND`→404,
     `:METHOD_NOT_ALLOWED`→405, `:UNSUPPORTED_MEDIA_TYPE`→415, `:NOT_IMPLEMENTED`→501).
   - **Loggers** — every `<logger message="…">`, in flow order (e.g. `Start GET`, `End GET`).
   - **Branches** — every `<choice>`/`<when expression="…">`/`<otherwise>` and the logger inside
     each branch (e.g. `gender == 'male'` → `first flow for male`; otherwise → `flow for female`).
   - **Deployment id** — a UUID in the OpenAPI `servers[0].description`, if present, for log fetch.

2. **Build the cases.** One per behaviour you want to validate. For each, set `validate_logs: true`
   and `expected_log_strings` to the loggers proving that path ran (use `log_match_mode: "all_of"`
   when several must all appear, `"contains"` for a single substring). Cover:
   - **Happy path** per endpoint (assert response shape with `"<<any>>"` for dynamic values + the
     entry/exit loggers). **Send every required query param and required header from the schema**
     (plus the correct `Content-Type`/`Accept`). A missing required query param or header makes
     APIkit return 400 — if you omit it the happy-path case fails for the wrong reason (this is the
     most commonly missed thing).
   - **Query-param & header coverage** — for each `in: query` and `in: header` parameter the schema
     declares on the operation: if it is `required`, add a negative that OMITS it (expect 400); for
     any value constraint (enum / pattern / min·maxLength / min·max / format), add a negative that
     sends a violating value in that param or header (expect 400).
   - **Branch logic** — one case per `choice` branch, sending a body that triggers it and asserting
     that branch's logger string (this is the core value).
   - **APIkit errors** — a case per error mapping (e.g. unmapped method → 405, unknown path → 404),
     asserting the status, the error envelope, and the `APIKIT:*` log string.
   - **Body negatives** — when the spec defines a request body, add missing-required / bad
     pattern·enum·length·bounds / wrong-Content-Type cases (all expecting 400; the Mule app can't
     return 422). Send a raw malformed-JSON string body verbatim for the malformed case.

3. **Write `cases.json`** to a scratch path:
   ```json
   {
     "base_path": "<deployed origin>/api",
     "deployment_id": "<uuid from servers[0].description, if any>",
     "cases": [
       { "test_id": "TC-001", "method": "GET", "url": "/patients?status=active",
         "headers": { "Accept": "application/json" }, "expected_status": 200,
         "expected_response": { "patientId": "<<any>>" },
         "validate_logs": true, "expected_log_strings": ["Start GET", "End GET"], "log_match_mode": "all_of" },
       { "test_id": "TC-002", "method": "GET", "url": "/patients", "expected_status": 400,
         "expected_response": { "message": "Bad request" },
         "validate_logs": false, "expected_log_strings": ["APIKIT:BAD_REQUEST"], "log_match_mode": "contains" },
       { "test_id": "TC-003", "method": "POST", "url": "/patients", "expected_status": 201,
         "headers": { "Content-Type": "application/json" }, "body": { "gender": "male" },
         "validate_logs": true, "expected_log_strings": ["Start POST", "first flow for male", "End POST"], "log_match_mode": "all_of" }
     ]
   }
   ```
   (`url` carries the query string; put required headers in `headers`. TC-001 sends the required
   `status` query param + `Accept`; TC-002 OMITS the required `status` to assert the 400. The query
   param/header names above are **illustrative** — use the ones the schema actually declares.
   Use `application_logs_fetch_url` instead of `deployment_id` if you already know the full URL;
   omit `test_id` to let the worker auto-number `TC-001…`.)

4. **Create the suite** from the `worker/` directory:
   ```bash
   cd worker
   MCP_URL="https://api-log-test-worker.nit4infy1.workers.dev/mcp" \
   MCP_TOKEN="$API_LOG_TEST_TOKEN" \
     npx tsx scripts/create-cases.mts <path-to-cases.json>
   ```
   (Optionally pass a second arg for the suite output path. `MCP_TOKEN` is the bearer the worker
   requires on `/mcp`. For a **local** app on `localhost`, the deployed cloud worker can't reach it
   for the *run* step, but creating the suite works against any worker; run the suite locally with
   the Python `run_and_record`.)

5. **Report** the created suite (base path, case count, the filled `application_logs_fetch_url`),
   and the saved path (`resources/<stem>_suite.xlsx`). To get PASS/FAIL, run the suite next with the
   `run-suite` skill (`run_suite`) or the Python `run_and_record`.

## Notes
- `create_test_case_all` only **creates** the suite (renders + stores it, returns `suite_id` +
  `suite_download_url`). It does NOT run the tests — running is a separate step (`run_suite`, or the
  Python `run_and_record` for a local app). This keeps create and run cleanly separated.
- To create a suite from an OpenAPI schema alone (no Mule analysis), use `run-schema` (which both
  generates and runs from the schema).
