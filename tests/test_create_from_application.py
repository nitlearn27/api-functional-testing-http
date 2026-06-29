"""create_test_suite_from_application: FLOW LOGIC ONLY (no schema). Offline (file I/O only)."""

from __future__ import annotations

from pathlib import Path

from api_log_test_mcp.tools.suite import read_test_suite
from api_log_test_mcp.tools.suite_generator import create_test_suite_from_application

APP = str(Path(__file__).parent.parent / "resources" / "test-enroll-impl4")


def _build(tmp_path: Path):
    out = tmp_path / "app_suite.xlsx"
    from unittest.mock import patch

    import api_log_test_mcp.tools.suite_generator as sg

    with patch.object(sg, "locate_oas", return_value=None):
        summary = create_test_suite_from_application(APP, str(out))
    return summary, read_test_suite(str(out))


def test_base_path_from_listener_and_round_trips(tmp_path: Path):
    summary, suite = _build(tmp_path)
    assert summary.get("oas_used") is None  # the schema is deliberately not consulted
    assert suite.parse_errors == []
    assert suite.base_path == "http://localhost:8081/api"  # from the Mule listener, not the schema


def test_does_not_use_schema_derived_cases(tmp_path: Path):
    _, suite = _build(tmp_path)
    # No query-param case (the patientId query param lives only in the schema) ...
    assert all("patientId=" not in c.url for c in suite.cases)
    # ... and no per-field body-validation negatives (those come from the schema tool).
    assert all("missing required '" not in (c.description or "") for c in suite.cases)


def test_get_positive_asserts_the_flow_response_and_loggers(tmp_path: Path):
    _, suite = _build(tmp_path)
    get = next(c for c in suite.cases if c.method == "GET" and c.expected_status == 200)
    assert get.url == "/patients"  # no query string — purely the flow's endpoint
    assert get.expected_response["patientId"] == "PAT-00123"  # the flow's DataWeave output
    assert "street" not in get.expected_response  # nested address object skipped, not flattened
    assert "encoding" not in get.expected_response  # DataWeave `as Object {…}` coercion excluded
    assert get.expected_log_strings == ["Start GET", "End GET"]


def test_choice_branch_cases(tmp_path: Path):
    _, suite = _build(tmp_path)
    male = next(c for c in suite.cases if "first flow for male" in c.expected_log_strings)
    assert male.method == "POST" and male.body == {"gender": "male"}
    assert male.expected_log_strings == ["Start POST", "End POST", "first flow for male"]
    assert any("flow for female" in c.expected_log_strings for c in suite.cases)


def test_error_handler_cases_from_flows(tmp_path: Path):
    _, suite = _build(tmp_path)
    by_status = {c.expected_status: c for c in suite.cases}
    # Every APIKIT mapping the error-handler defines, with the app's actual {message} body.
    assert by_status[404].expected_response == {"message": "Resource not found"}
    assert by_status[405].expected_response == {"message": "Method not allowed"}
    assert by_status[406].expected_response == {"message": "Not acceptable"}
    assert by_status[415].expected_response == {"message": "Unsupported media type"}
    assert by_status[400].expected_response == {"message": "Bad request"}
    assert by_status[404].expected_log_strings == ["APIKIT:NOT_FOUND"]
