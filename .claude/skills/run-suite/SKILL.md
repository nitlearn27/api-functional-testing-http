---
name: run-suite
description: Execute an existing API functional test suite (.xlsx) against its Basepath locally using the run_test_suite MCP tool. Use when the user asks to run a prebuilt suite or run-and-record.
---

# Run an existing suite locally

To run a prebuilt `.xlsx` suite end-to-end against a target API, use the local MCP runner.

## Steps

1. **Trigger Background Execution**:
   Call the local MCP tool `run_test_suite` with the path to the `.xlsx` file.
   - Example arguments: `suite_path="resources/test-enroll-impl4_suite.xlsx"`
   - This starts the execution asynchronously in a background thread and immediately returns a `job_id`.

2. **Poll Status**:
   Call the local MCP tool `run_test_suite` with the returned `job_id` (leaving the `suite_path` blank) to check status.
   - Example arguments: `job_id="<uuid-returned-in-step-1>"`
   - Monitor the execution progress percentage and elapsed wait times.

3. **Present Final Report**:
   Once the status changes to `"complete"`, retrieve the final report and output file path, and summarize the test results.
