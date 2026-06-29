"""FastMCP server entry point.

Three primary tools: ``create_test_suite_from_schema`` (OpenAPI in → suite),
``create_test_suite_from_application`` (Mule app folder → suite, reading flows + the bundled
schema), and ``run_test_suite`` (run a suite .xlsx, write a separate results file). The rest
(``read_test_suite``, ``call_api``, ``assert_response``, ``snapshot_logs``, ``validate_logs``,
``run_suite``, ``get_auth_token``) are low-level building blocks the primary tools use.
"""

from __future__ import annotations

from typing import Any

from fastmcp import FastMCP

from .config import get_settings
from .matching.response_matcher import assert_response as _assert_response
from .models import (
    ApiResponse,
    AssertResult,
    LogMatchMode,
    LogValidationResult,
    MatchMode,
    SuiteReport,
    TestSuite,
)
from .tools import auth as _auth
from .tools import http_runner as _http_runner
from .tools import logs as _logs
from .tools import orchestrate as _orchestrate
from .tools import suite_generator as _suite_generator
from .tools.suite import read_test_suite as _read_test_suite

mcp = FastMCP("api-log-test-mcp")


# --- Phase 2: implemented (no-network core) --------------------------------------------


@mcp.tool
def read_test_suite(path: str) -> TestSuite:
    """Parse an Excel test suite into structured test cases.

    Returns the parsed cases plus a ``parse_errors`` list for any malformed rows (bad rows
    are skipped, never fatal).
    """
    return _read_test_suite(path)


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
def assert_response(
    actual_status: int,
    actual_body: Any,
    expected: Any,
    mode: MatchMode = MatchMode.JSON_SUBSET,
    ignore_paths: list[str] | None = None,
    expected_status: int | None = None,
) -> AssertResult:
    """Assert an API response body/status against an expectation.

    ``mode`` is one of exact | json_subset | schema. ``ignore_paths`` are dotted paths
    (``*`` wildcard supported) pruned from both sides before comparison.
    """
    return _assert_response(
        actual_body=actual_body,
        expected=expected,
        mode=mode,
        ignore_paths=ignore_paths,
        actual_status=actual_status,
        expected_status=expected_status,
    )


@mcp.tool
def snapshot_logs(
    instances: list[str] | None = None,
    application_logs_fetch_url: str | None = None,
) -> str:
    """Download logs once via the configured backend and return a snapshot_id handle.

    For the ``anypoint`` backend, ``application_logs_fetch_url`` (the CloudHub log-file URL,
    normally read from the suite sheet) is required.
    """
    return _logs.snapshot_logs(
        get_settings(), instances, application_logs_fetch_url=application_logs_fetch_url
    )


@mcp.tool
def validate_logs(
    snapshot_id: str,
    correlation_id: str,
    expected: list[str],
    mode: LogMatchMode = LogMatchMode.CONTAINS,
) -> LogValidationResult:
    """Validate expected log strings for a correlation ID within a snapshot (in memory)."""
    return _logs.validate_logs(snapshot_id, correlation_id, expected, mode)


# --- Stubs (later phases; contract fixed now) ------------------------------------------


@mcp.tool
def get_auth_token() -> str:
    """[Phase 3] Acquire an OAuth2 client-credentials token for the target API."""
    return _auth.get_auth_token()


@mcp.tool
def call_api(
    method: str,
    url: str,
    headers: dict[str, str] | None = None,
    body: Any = None,
    correlation_id: str | None = None,
) -> ApiResponse:
    """Fire an HTTP request, stamp the correlation ID, return a normalized response."""
    return _http_runner.call_api(method, url, headers, body, correlation_id)


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


@mcp.tool
def run_suite(suite_path: str, retain_snapshots: bool = False) -> SuiteReport:
    """[low-level] Run a suite end-to-end and return the report only (writes no file).

    ``run_test_suite`` is the primary run tool; this is the building block it uses.
    """
    return _orchestrate.run_suite(suite_path, retain_snapshots)


def main() -> None:
    """Console-script entry point: run the server over stdio."""
    mcp.run()


if __name__ == "__main__":
    main()
