"""Pydantic models forming the shared contract across tools.

These are the JSON-serializable shapes that cross the MCP boundary. Keeping them in one
module means the orchestrator side and the implementation side agree on data shapes.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class MatchMode(StrEnum):
    """How a response body is compared against the expectation."""

    EXACT = "exact"
    JSON_SUBSET = "json_subset"
    SCHEMA = "schema"
    STATUS_ONLY = "status_only"


class LogMatchMode(StrEnum):
    """How expected log strings are matched against snapshot lines."""

    CONTAINS = "contains"
    REGEX = "regex"
    ALL_OF = "all_of"
    ANY_OF = "any_of"


# --- Test suite ------------------------------------------------------------------------


class TestCase(BaseModel):
    """A single normalized test case parsed from one suite row."""

    test_id: str
    description: str | None = None
    method: str = "GET"
    url: str = ""
    headers: dict[str, Any] = Field(default_factory=dict)
    body: Any = None
    auth_required: bool = True
    expected_status: int | None = None
    expected_response: Any = None
    response_match_mode: MatchMode = MatchMode.JSON_SUBSET
    ignore_paths: list[str] = Field(default_factory=list)
    validate_logs: bool = False
    expected_log_strings: list[str] = Field(default_factory=list)
    log_match_mode: LogMatchMode = LogMatchMode.CONTAINS
    log_source: str = "anypoint"


class ParseError(BaseModel):
    """A single malformed-row problem; collected, never fatal."""

    row: int = Field(description="1-based row number in the sheet (including header).")
    column: str | None = None
    message: str


class TestSuite(BaseModel):
    """Result of parsing a suite sheet: good cases plus collected parse errors."""

    base_path: str | None = None
    cases: list[TestCase] = Field(default_factory=list)
    parse_errors: list[ParseError] = Field(default_factory=list)


# --- API call / assertion --------------------------------------------------------------


class ApiResponse(BaseModel):
    """Normalized HTTP response shape returned by call_api (and accepted by assert_response)."""

    status: int
    headers: dict[str, str] = Field(default_factory=dict)
    body: Any = None
    latency_ms: float | None = None


class ResponseDiff(BaseModel):
    """One mismatch found while comparing an expected vs actual response body/status."""

    path: str
    expected: Any = None
    actual: Any = None
    message: str


class AssertResult(BaseModel):
    """Outcome of asserting a response against an expectation."""

    passed: bool
    mode: MatchMode
    status_ok: bool = True
    diffs: list[ResponseDiff] = Field(default_factory=list)


# --- Log validation --------------------------------------------------------------------


class LogValidationResult(BaseModel):
    """Outcome of validating expected log strings for a correlation ID."""

    passed: bool
    correlation_id: str
    matched: list[str] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)
    lines_considered: int = 0
    used_fallback: bool = False  # matched against the whole snapshot (no lines for the id)


# --- Suite report (run_suite, Phase 5) -------------------------------------------------


class CaseReport(BaseModel):
    """Per-case result inside a full suite run."""

    test_id: str
    passed: bool
    correlation_id: str | None = None
    actual_status: int | None = None
    expected_status: int | None = None
    response_assert: AssertResult | None = None
    log_validation: LogValidationResult | None = None
    error: str | None = None


class SuiteReport(BaseModel):
    """Aggregate result of a full run_suite execution."""

    total: int = 0
    passed: int = 0
    failed: int = 0
    cases: list[CaseReport] = Field(default_factory=list)
    parse_errors: list[ParseError] = Field(default_factory=list)
