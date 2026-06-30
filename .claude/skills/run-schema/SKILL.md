---
name: run-schema
description: Generate a test suite from an OpenAPI spec and run it end-to-end against a live API using local MCP tools. Use when the user asks to test an API spec or yaml file.
---

# Run a schema end-to-end

To test an OpenAPI 3.0 YAML spec against a live API, use the local MCP tools sequentially.

## Steps

1. **Generate the Test Suite**:
   Call the local MCP tool `create_test_suite_from_schema` with the path to the OpenAPI YAML file.
   - Example arguments: `schema_path="resources/openapi.yaml"`
   - This writes a `<stem>_suite.xlsx` file and returns a summary of the test cases.

2. **Execute the Test Run**:
   Call the local MCP tool `run_test_suite` with the generated suite path to trigger background execution.
   - Example arguments: `suite_path="resources/openapi_suite.xlsx"`
   - This returns a `job_id` and starts the run in a background daemon thread.

3. **Monitor Progress**:
   Call the local MCP tool `run_test_suite` with the `job_id` periodically (polling) until `status` is `"complete"`.
   - Example arguments: `job_id="<uuid-returned-in-step-2>"`
   - Report progress updates and percentage completion to the user.

4. **Summarize Results**:
   Once complete, retrieve the final report and output file path, and summarize the pass/fail statistics for the user.
