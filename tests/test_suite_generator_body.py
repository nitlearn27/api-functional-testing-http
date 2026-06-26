"""Request-body generation: the create body must include EVERY required field (recursively).

Offline. A /products POST whose body requires a string, an array of objects, and a nested object —
none with an `example` — must still produce a complete valid body, so the positive create is valid
and each negative drops exactly one field.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from api_log_test_mcp.tools.suite import read_test_suite
from api_log_test_mcp.tools.suite_generator import generate_test_suite

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
                                "required": ["name", "tags", "dimensions"],
                                "properties": {
                                    "name": {"type": "string"},
                                    "tags": {
                                        "type": "array",
                                        "items": {"$ref": "#/components/schemas/Tag"},
                                    },
                                    "dimensions": {"$ref": "#/components/schemas/Dimensions"},
                                    "description": {"type": "string"},  # optional
                                },
                            }
                        }
                    },
                },
                "responses": {"201": {"description": "Created"}},
            }
        }
    },
    "components": {
        "schemas": {
            "Tag": {
                "type": "object",
                "required": ["key", "weight"],
                "properties": {
                    "key": {"type": "string"},
                    "weight": {"type": "integer", "minimum": 1},
                },
            },
            "Dimensions": {
                "type": "object",
                "required": ["width", "height"],
                "properties": {
                    "width": {"type": "integer", "minimum": 1},
                    "height": {"type": "integer", "minimum": 1},
                },
            },
        }
    },
}


def _cases(tmp_path: Path):
    spec_path = tmp_path / "products.yaml"
    spec_path.write_text(yaml.safe_dump(SPEC))
    out = tmp_path / "suite.xlsx"
    generate_test_suite(str(spec_path), str(out))
    return read_test_suite(str(out)).cases


def test_positive_body_has_all_required_fields_recursively(tmp_path: Path):
    cases = _cases(tmp_path)
    positive = next(c for c in cases if c.method == "POST" and c.expected_status == 201)
    assert positive.body == {
        "name": "sample",
        "tags": [{"key": "sample", "weight": 1}],
        "dimensions": {"width": 1, "height": 1},
    }


def test_missing_required_negative_drops_only_that_field(tmp_path: Path):
    cases = _cases(tmp_path)
    missing_name = next(c for c in cases if "missing required 'name'" in (c.description or ""))
    assert "name" not in missing_name.body
    assert missing_name.body["tags"] == [{"key": "sample", "weight": 1}]
    assert missing_name.body["dimensions"] == {"width": 1, "height": 1}
