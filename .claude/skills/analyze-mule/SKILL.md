---
name: analyze-mule
description: Analyze a MuleSoft application's XML flows and OpenAPI schema to generate a functional test suite (.xlsx) using the local MCP tool. Run this when the user asks to generate test cases or create a suite from a MuleSoft app.
---

# Analyze a MuleSoft app and create a test-case suite

To create a functional test suite from a MuleSoft application, use the local MCP tool **`create_test_suite_from_application`**.

## Steps

1. **Locate the Application Root**: Ensure you have the path to the MuleSoft application's root directory (which contains `pom.xml` and `src/main/mule`).
2. **Invoke the MCP Tool**: Call `create_test_suite_from_application` with the app root path. 
   - Example arguments: `app_root="resources/test-enroll-impl4"`.
3. **Verify Generation**: The tool will parse the flow logic (HTTP endpoints, DataWeave responses, choices/branches, error mappings, and loggers) along with the bundled OpenAPI schema, write a `<app_name>_suite.xlsx` file, and return a summary of generated test cases.
4. **Report Results**: Present the generated suite summary (base path, case count, and categories) to the user.
