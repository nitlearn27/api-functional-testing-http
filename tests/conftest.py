"""Shared test fixtures.

The Excel sample suite is generated programmatically (rather than committing a binary) so the
schema is visible and easy to edit alongside the parser.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from openpyxl import Workbook

FIXTURES = Path(__file__).parent / "fixtures"

HEADERS = [
    "test_id", "description", "method", "url", "headers", "body", "auth_required",
    "expected_status", "expected_response", "response_match_mode", "validate_logs",
    "expected_log_strings", "log_match_mode", "log_source", "ignore_paths",
]

# (row dict) — one valid row, one with delimiter logs, one duplicate id, one bad JSON,
# one missing test_id. Mirrors the real .numbers schema (header below a metadata block).
ROWS = [
    {
        "test_id": "order-001",
        "method": "get",
        "url": "/orders",
        "headers": json.dumps({"Accept": "application/json"}),
        "auth_required": "no",
        "expected_status": 200,
        "expected_response": json.dumps({"status": "ok"}),
        "response_match_mode": "json_subset",
        "validate_logs": "yes",
        "ignore_paths": "data.id, data.timestamp",
        "expected_log_strings": json.dumps(["Order lookup succeeded", "returning 200"]),
        "log_match_mode": "contains",
        "log_source": "file",
    },
    {
        "test_id": "pay-042",
        "method": "POST",
        "url": "/payments",
        "body": json.dumps({"amount": 10}),
        "auth_required": "no",
        "expected_status": 402,
        "response_match_mode": "exact",
        "validate_logs": "no",
        "expected_log_strings": "Payment declined||gateway slow",
        "log_match_mode": "contains",
    },
    {"test_id": "order-001", "method": "GET", "url": "/orders"},  # duplicate id -> error
    {"test_id": "bad-json", "method": "GET", "url": "/x",
     "headers": "{not valid json"},  # bad JSON in headers -> error
    {"test_id": "", "method": "GET", "url": "/y"},  # missing test_id -> error
]


@pytest.fixture
def sample_suite_path(tmp_path: Path) -> str:
    wb = Workbook()
    ws = wb.active
    # Metadata block above the header, like the real sheet.
    ws.append(["Basepath", "https://api.example.test/"])
    ws.append(["Auth"])
    ws.append([])
    ws.append(HEADERS)
    for row in ROWS:
        ws.append([row.get(h, "") for h in HEADERS])
    path = tmp_path / "sample_suite.xlsx"
    wb.save(path)
    return str(path)


@pytest.fixture
def numbers_suite_path() -> str:
    """The real committed Numbers suite used as the integration sample."""
    return str(Path(__file__).parent.parent / "api_test_suite_sample.numbers")


@pytest.fixture
def sample_log_path() -> str:
    return str(FIXTURES / "sample_app.log")
