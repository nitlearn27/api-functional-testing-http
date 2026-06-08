"""generate_test_suite: spec -> .xlsx suite, and the generated sheet round-trips the parser.

Offline: pure file I/O, no network. The key contract is that ``read_test_suite`` parses the
generated sheet with zero parse errors, so it is immediately runnable by ``run_and_record``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from api_log_test_mcp.tools.suite import read_test_suite
from api_log_test_mcp.tools.suite_generator import generate_test_suite

SPEC = Path(__file__).parent.parent / "resources" / "products-eapi1.yaml"
SPEC_BASE_PATH = yaml.safe_load(SPEC.read_text())["servers"][0]["url"]


@pytest.fixture
def generated(tmp_path: Path) -> tuple[dict, str]:
    out = tmp_path / "products_suite.xlsx"
    summary = generate_test_suite(str(SPEC), str(out))
    return summary, str(out)


def test_writes_sheet_with_spec_basepath(generated):
    summary, out = generated
    assert Path(out).exists()
    assert summary["base_path"] == SPEC_BASE_PATH  # base path comes straight from the spec
    assert summary["case_count"] >= 30  # comprehensive coverage


def test_worksheet_is_named_tests(generated):
    from openpyxl import load_workbook

    _, out = generated
    wb = load_workbook(out)
    assert wb.sheetnames == ["tests"]


def test_round_trips_through_parser_without_errors(generated):
    summary, out = generated
    suite = read_test_suite(out)
    assert suite.base_path == summary["base_path"]
    assert suite.parse_errors == []
    assert len(suite.cases) == summary["case_count"]


def test_covers_every_validation_category(generated):
    _, out = generated
    suite = read_test_suite(out)
    statuses = {c.expected_status for c in suite.cases}
    # positive + body/query/path/bad-request(400) + media-type(415) + 404 + auth(401).
    # No 422: body-validation failures are folded into 400 (Mulesoft cannot return 422).
    assert {200, 201, 400, 401, 404, 415} <= statuses
    assert 422 not in statuses

    descriptions = " || ".join(c.description or "" for c in suite.cases)
    assert "missing required" in descriptions  # required-field negative
    assert "violates pattern" in descriptions  # pattern negative
    assert "not in allowed enum" in descriptions  # enum negative
    assert "exceeds maxItems" in descriptions  # array negative


def test_error_cases_use_full_spec_error_envelope(generated):
    """Negative cases assert every field of the spec's ErrorResponse schema (not a subset)."""
    spec = yaml.safe_load(SPEC.read_text())
    envelope = set(spec["components"]["schemas"]["ErrorResponse"]["properties"])  # all 6 fields
    _, out = generated
    suite = read_test_suite(out)
    errors = [c for c in suite.cases if c.expected_status in {400, 401, 404, 415}]
    assert errors
    for case in errors:
        assert set(case.expected_response) == envelope, case.test_id
        # status/error are concrete; the rest are existence-only wildcards.
        assert case.expected_response["status"] == case.expected_status
        for dynamic in ("timestamp", "message", "path", "errors"):
            assert case.expected_response[dynamic] == "<<any>>"


def test_positive_get_by_name_asserts_all_required_product_fields(generated):
    """The 200 expectation reflects every required field of the spec's Product schema."""
    spec = yaml.safe_load(SPEC.read_text())
    required = set(spec["components"]["schemas"]["Product"]["required"])
    _, out = generated
    suite = read_test_suite(out)
    case = next(
        c for c in suite.cases
        if c.expected_status == 200 and "by name" in (c.description or "").lower()
    )
    assert required <= set(case.expected_response)


def test_all_body_validation_maps_to_400(generated):
    """Every POST body-validation negative expects 400 — none use 422 (Mulesoft cannot
    return 422), covering missing fields and value violations alike."""
    _, out = generated
    suite = read_test_suite(out)
    body_negs = [
        c for c in suite.cases
        if c.method == "POST" and any(
            k in (c.description or "")
            for k in ("missing required", "violates pattern", "not in allowed enum",
                      "maxLength", "minLength", "minimum", "maximum", "maxItems",
                      "duplicate items", "additionalProperties")
        )
    ]
    assert body_negs
    assert all(c.expected_status == 400 for c in body_negs), [
        (c.test_id, c.expected_status) for c in body_negs
    ]


def test_malformed_body_case_kept_as_raw_string(generated):
    _, out = generated
    suite = read_test_suite(out)
    malformed = [c for c in suite.cases if "malformed JSON" in (c.description or "")]
    assert malformed, "expected a malformed-JSON POST case"
    # The deliberately-broken body survives as a raw scalar string (not valid JSON).
    assert isinstance(malformed[0].body, str)
