"""FastMCP server entry point.

Exactly three tools: ``create_test_suite_from_schema`` (OpenAPI in → suite),
``create_test_suite_from_application`` (Mule app folder → suite, reading flows + the bundled
schema), and ``run_test_suite`` (run a suite .xlsx, write a separate results file). The underlying
building blocks (HTTP runner, response matcher, log snapshot/validate, auth) stay importable from
``tools/`` for these three to use, but are deliberately not exposed as MCP tools.
"""

from __future__ import annotations

from typing import Any

from fastmcp import FastMCP

from .tools import orchestrate as _orchestrate
from .tools import suite_generator as _suite_generator

mcp = FastMCP("api-log-test-mcp")


@mcp.tool
def create_test_suite_from_schema(
    schema_path: str, output_path: str | None = None
) -> dict[str, Any]:
    """Create a runnable .xlsx test suite from an OpenAPI 3.0 schema. Does NOT run the tests.

    Walks every path × method and builds comprehensive coverage — a positive case per operation
    plus one negative per validation rule, including the schema's **query params and header params**
    (required ones are sent; omitting/violating them yields a 400) and request-body rules. Writes
    the suite next to the schema as ``<stem>_suite.xlsx`` (or ``output_path``) and returns a summary
    (``output_path``, ``base_path``, ``case_count``, ``cases_by_category``). Run it with
    ``run_test_suite``.
    """
    return _suite_generator.generate_test_suite(schema_path, output_path)


@mcp.tool
def create_test_suite_from_application(
    app_root: str, output_path: str | None = None
) -> dict[str, Any]:
    """Create a .xlsx test suite from a MuleSoft app's root folder.

    Combines flow logic and OpenAPI schema validation. Reads ``src/main/mule/*.xml`` and builds
    cases from the flow logic (base path, endpoints, entry/exit loggers, DataWeave responses,
    choices/branches, and error-handler mappings) and combines them with the bundled OpenAPI
    schema (query, header, path parameter, and body validations) extracted from
    ``target/repository/**/*-oas.zip`` or ``~/.m2``. If no schema is found, it falls back to
    flow-only test cases. Writes ``<app-name>_suite.xlsx`` (or ``output_path``); run it with
    ``run_test_suite``.
    """
    return _suite_generator.create_test_suite_from_application(app_root, output_path)


@mcp.tool
def run_test_suite(suite_path: str) -> dict[str, Any]:
    """Run a test-suite .xlsx against its Basepath and write the results to a separate file.

    The primary run tool. Takes a suite from ``create_test_suite_from_schema`` /
    ``create_test_suite_from_application`` (optionally hand-edited). Runs on this machine, so it
    reaches ``localhost`` or any public URL; makes the HTTP calls + response (status/body)
    assertions. The suite file is never modified — results go to a sibling ``<stem>_results.xlsx``
    (a timestamped RESULTS block + one evidence tab per case). When the app is unreachable every
    case reports "App not running". Returns ``report``, ``run_at`` and ``results_path``.
    """
    report, run_at, results_path = _orchestrate.run_and_record(suite_path)
    return {"run_at": run_at, "results_path": results_path, "report": report}


def main() -> None:
    """Console-script entry point: run the server over stdio."""
    mcp.run()


if __name__ == "__main__":
    main()
