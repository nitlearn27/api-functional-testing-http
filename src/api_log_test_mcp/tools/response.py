"""Adapter from a TestCase + ApiResponse to the ResponseMatcher."""

from __future__ import annotations

from ..matching.response_matcher import assert_response
from ..models import ApiResponse, AssertResult, TestCase


def assert_case_response(case: TestCase, response: ApiResponse) -> AssertResult:
    """Assert ``response`` against the expectations declared on ``case``."""
    return assert_response(
        actual_body=response.body,
        expected=case.expected_response,
        mode=case.response_match_mode,
        ignore_paths=case.ignore_paths,
        actual_status=response.status,
        expected_status=case.expected_status,
    )
