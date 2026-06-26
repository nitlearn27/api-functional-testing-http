"""Regression: a list (array) GET success body yields a single-node <<any>> json_subset template.

Self-contained — writes a minimal /products spec whose GET returns an array, so it does not
depend on resources/ fixtures. The positive list case must get a readable `[{field: "<<any>>"}]`
template (not an empty expected_response, and not a JSON Schema).
"""

from __future__ import annotations

import copy
from pathlib import Path

import yaml

from api_log_test_mcp.models import MatchMode
from api_log_test_mcp.tools.suite import read_test_suite
from api_log_test_mcp.tools.suite_generator import generate_test_suite

SPEC = {
    "openapi": "3.0.0",
    "info": {"title": "Products", "version": "1.0"},
    "servers": [{"url": "https://api.example.com/api"}],
    "paths": {
        "/products": {
            "get": {
                "summary": "List products",
                "responses": {
                    "200": {
                        "description": "OK",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "array",
                                    "items": {"$ref": "#/components/schemas/Product"},
                                }
                            }
                        },
                    }
                },
            }
        }
    },
    "components": {
        "schemas": {
            "Product": {
                "type": "object",
                "required": ["name", "price"],
                "properties": {"name": {"type": "string"}, "price": {"type": "number"}},
            }
        }
    },
}


def _generate(spec: dict, tmp_path: Path):
    spec_path = tmp_path / "products.yaml"
    spec_path.write_text(yaml.safe_dump(spec))
    out = tmp_path / "suite.xlsx"
    generate_test_suite(str(spec_path), str(out))
    suite = read_test_suite(str(out))
    return next(c for c in suite.cases if c.method == "GET" and c.expected_status == 200)


def test_array_success_body_gets_any_template(tmp_path: Path):
    positive = _generate(SPEC, tmp_path)
    assert positive.response_match_mode == MatchMode.JSON_SUBSET
    assert positive.expected_response == [{"name": "<<any>>", "price": "<<any>>"}]


def test_array_item_with_no_required_yields_bare_any(tmp_path: Path):
    spec = copy.deepcopy(SPEC)
    spec["components"]["schemas"]["Product"].pop("required")  # no declared required fields
    positive = _generate(spec, tmp_path)
    assert positive.response_match_mode == MatchMode.JSON_SUBSET
    assert positive.expected_response == ["<<any>>"]
