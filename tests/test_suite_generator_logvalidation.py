"""Generated suites default validate_logs=Yes and assert APIkit router error types.

Offline. Uses a /products POST spec (the Python builder is /products-specific) so it emits the
body-validation (400) and wrong-content-type (415) cases that carry the APIkit error strings.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from api_log_test_mcp.tools.suite import read_test_suite
from api_log_test_mcp.tools.suite_generator import _expected_log_strings, generate_test_suite

SPEC = {
    "openapi": "3.0.0",
    "info": {"title": "Products", "version": "1.0"},
    "servers": [{"url": "https://api.example.com/api"}],
    "paths": {
        "/products": {
            "post": {
                "summary": "Create product",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "required": ["name"],
                                "properties": {"name": {"type": "string"}},
                            }
                        }
                    },
                },
                "responses": {"201": {"description": "Created"}},
            }
        }
    },
}


def test_expected_log_strings_helper():
    assert _expected_log_strings(400, "POST", "/products") == ["APIKIT:BAD_REQUEST"]
    assert _expected_log_strings(415, "POST", "/products") == ["APIKIT:UNSUPPORTED_MEDIA_TYPE"]
    assert _expected_log_strings(404, "GET", "/products/x") == ["APIKIT:NOT_FOUND"]
    # success: the request line, query stripped (no APIkit error type for a 2xx)
    assert _expected_log_strings(200, "GET", "/products?page=1") == ["GET /products"]


def test_generated_cases_default_log_validation(tmp_path: Path):
    spec_path = tmp_path / "products.yaml"
    spec_path.write_text(yaml.safe_dump(SPEC))
    out = tmp_path / "suite.xlsx"
    generate_test_suite(str(spec_path), str(out))

    suite = read_test_suite(str(out))
    assert all(c.validate_logs for c in suite.cases)

    by_status = {c.expected_status: c for c in suite.cases}
    assert by_status[400].expected_log_strings == ["APIKIT:BAD_REQUEST"]
    assert by_status[415].expected_log_strings == ["APIKIT:UNSUPPORTED_MEDIA_TYPE"]
    # success (201): the request line for the create operation, not blank
    assert by_status[201].expected_log_strings == ["POST /products"]
