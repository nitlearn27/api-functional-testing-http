"""FastMCP server entry point.

Registers all eight tools so the contract is fixed early. The no-network core plus the
end-to-end runners are implemented; ``get_auth_token`` stays a stub until its phase lands.
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
def snapshot_logs(instances: list[str] | None = None) -> str:
    """Download logs once via the configured backend and return a snapshot_id handle."""
    return _logs.snapshot_logs(get_settings(), instances)


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
def run_suite(suite_path: str, retain_snapshots: bool = False) -> SuiteReport:
    """Run a full suite end-to-end (call + assert + optional log validation) and emit a report."""
    return _orchestrate.run_suite(suite_path, retain_snapshots)


@mcp.tool
def run_and_record(suite_path: str, retain_snapshots: bool = False) -> dict[str, Any]:
    """Run a suite end-to-end AND append a timestamped results block to the sheet.

    Prefer this over ``run_suite`` to actually test end-to-end: it makes real HTTP calls,
    optionally validates logs, and records the outcome back into the suite sheet. Returns the
    aggregate ``report`` plus the ``run_at`` timestamp of the recorded block.
    """
    report, run_at = _orchestrate.run_and_record(suite_path, retain_snapshots)
    return {"run_at": run_at, "report": report}


def main() -> None:
    """Console-script entry point: run the server over stdio."""
    mcp.run()


if __name__ == "__main__":
    main()
