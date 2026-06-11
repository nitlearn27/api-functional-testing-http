---
name: generate-test-suite
description: Generate an API functional test suite (.xlsx) from an OpenAPI/YAML spec via the deployed api-log-test MCP worker and save it into resources/. Use when the user asks to generate a test suite or test cases from a spec file (e.g. "generate a suite for resources/openapi.yaml").
---

# Generate a test suite from an OpenAPI spec

Generates a runnable `.xlsx` test suite from an OpenAPI 3.0 YAML spec using the deployed
`api-log-test` MCP worker, then **downloads it into `resources/`**. The worker has no
filesystem, so it returns a download link (not base64) and this skill fetches the file locally.

## Input
- A path to an OpenAPI YAML spec, provided by the user (e.g. `resources/openapi.yaml`). If none
  is given, ask which spec to use.

## Steps
1. Resolve the spec to an absolute path. Derive the output as `resources/<spec-stem>_suite.xlsx`
   (e.g. `openapi.yaml` → `resources/openapi_suite.xlsx`).
2. Run the generator script from the `worker/` directory. It calls `generate_test_suite` on the
   deployed worker and downloads the returned link:
   ```bash
   cd worker
   MCP_URL="https://api-log-test-worker.nit4infy1.workers.dev/mcp" \
     npx tsx scripts/generate-suite.mts <ABS_SPEC_PATH> ../resources/<spec-stem>_suite.xlsx
   ```
3. Report the script's output to the user: `base_path`, `case_count`, `categories`, the
   `download_url`, and the saved file path.
4. If `case_count` is `0`, tell the user the spec has no operations the generator can cover
   (it walks paths/operations generically; an empty result means nothing matched).

## Notes
- The download link is public-by-URL (an unguessable id) and the stored file auto-expires after
  2 hours. To re-download later, just re-run the skill.
- If the worker URL ever changes, update `MCP_URL` — the current origin is in
  `worker/wrangler.jsonc` (`PUBLIC_BASE_URL`), or check `wrangler deployments list`.
- This runs in Claude Code (which has shell + file access). Claude Desktop cannot save binary
  files to a specific local path, which is why suite-saving lives here.
