"""run_suite end-to-end with the HTTP layer mocked (no network, no logs)."""

import functools
import json
from pathlib import Path

import httpx
from openpyxl import Workbook

from api_log_test_mcp.tools import orchestrate
from api_log_test_mcp.tools.http_runner import call_api as real_call_api

HEADERS = [
    "test_id", "method", "url", "headers", "body", "auth_required",
    "expected_status", "expected_response", "response_match_mode", "validate_logs",
]

ROWS = [
    ["TC-001", "POST", "/orders", "", json.dumps({"sku": "ABC-100", "qty": 2}), "no",
     201, json.dumps({"status": "ACCEPTED", "sku": "ABC-100"}), "json_subset", "no"],
    ["TC-002", "POST", "/orders", "", json.dumps({"sku": "ABC-100"}), "no",
     400, json.dumps({"error": "VALIDATION_ERROR", "field": "qty"}), "json_subset", "no"],
]


def _make_suite(tmp_path: Path) -> str:
    wb = Workbook()
    ws = wb.active
    ws.append(["Basepath", "https://api.test/"])
    ws.append(HEADERS)
    for row in ROWS:
        ws.append(row)
    path = tmp_path / "suite.xlsx"
    wb.save(path)
    return str(path)


def _mock_handler(request: httpx.Request) -> httpx.Response:
    payload = json.loads(request.content)
    if "qty" in payload:
        return httpx.Response(201, json={"status": "ACCEPTED", "sku": payload["sku"]})
    return httpx.Response(400, json={"error": "VALIDATION_ERROR", "field": "qty"})


def test_run_suite_all_pass(tmp_path, monkeypatch):
    client = httpx.Client(transport=httpx.MockTransport(_mock_handler))
    monkeypatch.setattr(
        orchestrate, "call_api", functools.partial(real_call_api, client=client)
    )

    report = orchestrate.run_suite(_make_suite(tmp_path))

    assert report.total == 2
    assert report.passed == 2
    assert report.failed == 0
    assert all(c.log_validation is None for c in report.cases)  # validate_logs=no -> skipped


def test_run_suite_reports_failure(tmp_path, monkeypatch):
    # Always return 500 so both cases fail their status assertion.
    def fail_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    client = httpx.Client(transport=httpx.MockTransport(fail_handler))
    monkeypatch.setattr(
        orchestrate, "call_api", functools.partial(real_call_api, client=client)
    )

    report = orchestrate.run_suite(_make_suite(tmp_path))
    assert report.passed == 0
    assert report.failed == 2
    assert all(not c.passed for c in report.cases)
