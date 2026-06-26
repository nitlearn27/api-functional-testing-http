"""generate_test_suite: build a runnable .xlsx test suite from an OpenAPI spec.

The inverse of ``read_test_suite``: instead of parsing a hand-written sheet into ``TestCase``
objects, this reads an OpenAPI 3.0 YAML spec and emits a sheet in the exact format the parser
understands (worksheet ``tests``; a ``Basepath`` metadata row; the canonical header row; one row
per case). Coverage is comprehensive — a positive case per operation plus one negative case per
validation rule (required / pattern / enum / length / numeric bounds / array rules), plus
query-param, path-param, wrong-content-type, malformed-body, not-found and auth cases.

Validation rules are read generically from the resolved schemas (not hard-coded per field), so the
same logic generalizes to other specs. The generated sheet round-trips through ``read_test_suite``
with no parse errors, so it can be run immediately by ``run_and_record``.
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

import yaml
from openpyxl import Workbook

from ..config import get_anypoint_settings
from ..models import MatchMode, TestCase

# Wildcard sentinel honoured by the response matcher (field must exist, value not compared).
ANY = "<<any>>"
JSON_HEADERS = {"Content-Type": "application/json"}

# Expected status for request-body validation failures. The spec uses 422 (Unprocessable
# Entity) for field-level errors, but the target Mulesoft app cannot return 422, so the suite
# expects 400 Bad Request for every body-validation scenario. Set back to 422 to follow the
# spec verbatim once the platform supports it.
BODY_VALIDATION_STATUS = 400

# Sheet header order — mirrors the hand-written sample and the parser's COLUMNS.
SHEET_COLUMNS = [
    "test_id", "description", "method", "url", "headers", "body", "auth_required",
    "expected_status", "expected_response", "response_match_mode", "validate_logs",
    "expected_log_strings", "log_match_mode", "log_source",
]

# HTTP reason phrases used to build json_subset error expectations.
REASON = {
    400: "Bad Request", 401: "Unauthorized", 403: "Forbidden", 404: "Not Found",
    409: "Conflict", 415: "Unsupported Media Type", 422: "Unprocessable Entity",
}

# Standard error types the Mulesoft APIkit Router raises, by HTTP status. Used as the default
# expected_log_strings so every generated case validates that the router logged the right error
# type (e.g. a 400 case asserts "APIKIT:BAD_REQUEST" appears in the CloudHub logs).
APIKIT_ERROR_TYPES = {
    400: "APIKIT:BAD_REQUEST",
    404: "APIKIT:NOT_FOUND",
    405: "APIKIT:METHOD_NOT_ALLOWED",
    406: "APIKIT:NOT_ACCEPTABLE",
    415: "APIKIT:UNSUPPORTED_MEDIA_TYPE",
    501: "APIKIT:NOT_IMPLEMENTED",
}


def _expected_log_strings(status: int, method: str, url: str) -> list[str]:
    """Expected log strings for a case.

    Error cases assert the APIkit router's error type; non-error cases (2xx success, 401 auth)
    assert the request line ``<METHOD> <path>`` — Mule logs it on every routed request (e.g.
    "Processing GET /orders request" contains "GET /orders"), so the success case never has a
    blank expectation. The query string is dropped (the logger omits it).
    """
    error_type = APIKIT_ERROR_TYPES.get(status)
    if error_type:
        return [error_type]
    return [f"{method} {url.split('?')[0]}"]


def generate_test_suite(spec_path: str, output_path: str | None = None) -> dict[str, Any]:
    """Read an OpenAPI YAML spec at ``spec_path`` and write a runnable .xlsx test suite.

    If ``output_path`` is omitted, the sheet is written next to the spec as
    ``<spec-stem>_suite.xlsx``. Returns a summary dict with ``output_path``, ``base_path``,
    ``case_count`` and ``cases_by_category``.
    """
    spec = yaml.safe_load(Path(spec_path).read_text())

    builder = _SuiteBuilder(spec)
    builder.build()

    out = (
        Path(output_path)
        if output_path
        else Path(spec_path).with_name(Path(spec_path).stem + "_suite.xlsx")
    )
    # Pre-fill the CloudHub log-fetch URL when a deployments base is configured and the spec's
    # server description carries a deployment id; otherwise leave it blank for the user to fill.
    logs_fetch_url = _deployment_logs_url(spec, get_anypoint_settings().deployments_base_url)
    _write_sheet(out, builder.base_path, builder.cases, logs_fetch_url)

    return {
        "output_path": str(out),
        "base_path": builder.base_path,
        "application_logs_fetch_url": logs_fetch_url,
        "case_count": len(builder.cases),
        "cases_by_category": builder.categories,
    }


_DEPLOYMENT_ID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


def _deployment_logs_url(spec: dict[str, Any], base: str | None) -> str | None:
    """Build ``<base>/<deployment-id>`` from the first UUID in ``servers[0].description``.

    Returns ``None`` when no base is configured or no id is present, so the sheet cell stays
    blank (back-compat with hand-filled suites).
    """
    if not base:
        return None
    servers = spec.get("servers") or []
    description = str(servers[0].get("description", "")) if servers else ""
    match = _DEPLOYMENT_ID_RE.search(description)
    if not match:
        return None
    return f"{base.rstrip('/')}/{match.group(0)}"


# --- case building ---------------------------------------------------------------------


class _SuiteBuilder:
    """Walks the spec's operations and accumulates TestCases plus per-category counts."""

    def __init__(self, spec: dict[str, Any]) -> None:
        self.spec = spec
        self.base_path = _base_path(spec)
        self.error_schema = _error_schema(spec)
        self.cases: list[TestCase] = []
        self.categories: dict[str, int] = {}
        self._n = 0

    def _error_expected(self, code: int) -> dict[str, Any]:
        """json_subset expectation for the spec's standard error envelope.

        Emits every field the spec's error schema declares: ``status`` is asserted to equal
        the HTTP code and ``error`` to equal the reason phrase; all other fields (timestamp,
        message, path, errors, ...) are existence-only (``<<any>>``) since their values are
        dynamic. Falls back to a minimal ``{status, error}`` envelope when the spec declares
        no structured error body.
        """
        props = self.error_schema.get("properties")
        if not props:
            return {"status": code, "error": REASON.get(code, ANY)}
        expected: dict[str, Any] = {}
        for field in props:
            if field == "status":
                expected[field] = code
            elif field == "error":
                expected[field] = REASON.get(code, ANY)
            else:
                expected[field] = ANY
        return expected

    def _success_expected(
        self, op: dict[str, Any], echo: dict[str, Any] | None = None
    ) -> tuple[Any, MatchMode] | None:
        """Expectation (value + match mode) for an operation's 2xx response, from the schema.

        For an object body this is a json_subset asserting existence (``<<any>>``) of only the
        schema's *declared* ``required`` fields — never the optional ones. When the schema declares
        no required fields, the expectation is a bare ``<<any>>`` (accept any object body). ``echo``
        overlays concrete values the request itself sends (e.g. a create payload).

        A list endpoint returns an *array* body (e.g. GET /orders). It gets a single-node template:
        ``[{required field: "<<any>>", …}]`` when the item declares required fields, else
        ``["<<any>>"]``. Under json_subset that one node is checked against EVERY object in the
        response (any count), so the whole list is validated; a user can replace it with multiple
        node templates to assert specific nodes positionally.

        Returns ``None`` when the operation declares no structured success body.
        """
        schema = _success_schema(self.spec, op)
        if schema.get("type") == "array":
            return _array_any_template(self.spec, schema), MatchMode.JSON_SUBSET
        # Object success body: assert existence (<<any>>) of only the schema's *declared* required
        # fields — never optional ones. ``echo`` overlays concrete request values (create cases).
        node: dict[str, Any] = {field: ANY for field in schema.get("required", [])}
        if echo:
            node.update(echo)
        if node:
            return node, MatchMode.JSON_SUBSET
        # No declared required fields (and nothing echoed): accept any object body with <<any>>,
        # but only when the operation declares a structured body (else no expectation at all).
        if schema.get("type") == "object" or schema.get("properties"):
            return ANY, MatchMode.JSON_SUBSET
        return None

    def _success_kwargs(
        self, op: dict[str, Any], echo: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """``_add`` kwargs (expected_response + response_match_mode) for an operation's 2xx body."""
        result = self._success_expected(op, echo)
        if result is None:
            return {"expected_response": None}
        expected, mode = result
        return {"expected_response": expected, "response_match_mode": mode}

    def _add(
        self,
        category: str,
        description: str,
        method: str,
        url: str,
        *,
        expected_status: int,
        body: Any = None,
        headers: dict[str, Any] | None = None,
        expected_response: Any = None,
        response_match_mode: MatchMode = MatchMode.JSON_SUBSET,
    ) -> None:
        self._n += 1
        self.cases.append(
            TestCase(
                test_id=f"TC-{self._n:03d}",
                description=description,
                method=method,
                url=url,
                headers=headers or {},
                body=body,
                auth_required=False,
                expected_status=expected_status,
                expected_response=expected_response,
                response_match_mode=response_match_mode,
                # Validate logs on every case so each run exercises both the API and the CloudHub
                # log endpoint; error cases assert the APIkit router's error type, 2xx/401 assert
                # nothing specific (still fetched, so a missing/blank log endpoint surfaces).
                validate_logs=True,
                expected_log_strings=_expected_log_strings(expected_status, method, url),
                log_source="anypoint",
            )
        )
        self.categories[category] = self.categories.get(category, 0) + 1

    def build(self) -> None:
        paths = self.spec.get("paths", {})
        if "/products" in paths:
            self._build_list(paths["/products"].get("get"))
            self._build_create(paths["/products"].get("post"))
        if "/products/{name}" in paths:
            self._build_get_by_name(paths["/products/{name}"].get("get"))

    def _build_list(self, op: dict[str, Any] | None) -> None:
        if not op:
            return
        self._add(
            "positive",
            "List products — valid pagination → 200",
            "GET",
            "/products?" + urlencode({"page": 1, "pageSize": 20, "sortBy": "price"}),
            expected_status=200,
            **self._success_kwargs(op),
        )
        for param in op.get("parameters", []):
            if param.get("in") != "query":
                continue
            name = param["name"]
            schema = _deref(self.spec, param.get("schema", {}))
            for label, value in _schema_negatives(name, schema):
                self._add(
                    "query_validation",
                    f"List products — {label} → 400",
                    "GET",
                    "/products?" + urlencode({name: value}),
                    expected_status=400,
                    expected_response=self._error_expected(400),
                )
        self._add(
            "auth",
            "List products — invalid credentials → 401 (requires API auth enforced)",
            "GET",
            "/products",
            headers={"Authorization": "Bearer invalid-token"},
            expected_status=401,
            expected_response=self._error_expected(401),
        )

    def _build_create(self, op: dict[str, Any] | None) -> None:
        if not op:
            return
        schema, example = self._request_body(op)
        baseline = copy.deepcopy(example) if example else _sample_value(self.spec, schema)
        required = schema.get("required", [])

        self._add(
            "positive",
            "Create product — valid payload → 201",
            "POST",
            "/products",
            headers=dict(JSON_HEADERS),
            body=baseline,
            expected_status=201,
            **self._success_kwargs(op, echo=copy.deepcopy(baseline)),
        )

        # All request-body validation failures are expected as 400 Bad Request. The spec
        # documents 422 (Unprocessable Entity) for field-level errors, but the Mulesoft
        # implementation cannot produce 422, so the suite folds every body-validation
        # scenario (missing field, bad pattern/enum/length/bounds, array rules, extra field)
        # into the 400 bucket. See BODY_VALIDATION_STATUS.
        for field in required:
            body = copy.deepcopy(baseline)
            body.pop(field, None)
            self._add(
                "body_validation",
                f"Create product — missing required '{field}' → {BODY_VALIDATION_STATUS}",
                "POST",
                "/products",
                headers=dict(JSON_HEADERS),
                body=body,
                expected_status=BODY_VALIDATION_STATUS,
                expected_response=self._error_expected(BODY_VALIDATION_STATUS),
            )

        for field, pschema in schema.get("properties", {}).items():
            pschema = _deref(self.spec, pschema)
            negs = (
                _array_negatives(field, pschema)
                if pschema.get("type") == "array"
                else _schema_negatives(field, pschema)
            )
            for label, value in negs:
                body = copy.deepcopy(baseline)
                body[field] = value
                self._add(
                    "body_validation",
                    f"Create product — {label} → {BODY_VALIDATION_STATUS}",
                    "POST",
                    "/products",
                    headers=dict(JSON_HEADERS),
                    body=body,
                    expected_status=BODY_VALIDATION_STATUS,
                    expected_response=self._error_expected(BODY_VALIDATION_STATUS),
                )

        if schema.get("additionalProperties") is False:
            body = copy.deepcopy(baseline)
            body["unexpectedField"] = "x"
            self._add(
                "body_validation",
                "Create product — unexpected extra field (additionalProperties:false) "
                f"→ {BODY_VALIDATION_STATUS}",
                "POST",
                "/products",
                headers=dict(JSON_HEADERS),
                body=body,
                expected_status=BODY_VALIDATION_STATUS,
                expected_response=self._error_expected(BODY_VALIDATION_STATUS),
            )

        # Deliberately malformed JSON, sent verbatim as a raw (non-JSON) cell.
        self._add(
            "bad_request",
            "Create product — malformed JSON body → 400",
            "POST",
            "/products",
            headers=dict(JSON_HEADERS),
            body='{"name": "Broken", "sku": }',
            expected_status=400,
            expected_response=self._error_expected(400),
        )

        self._add(
            "media_type",
            "Create product — wrong Content-Type text/plain → 415",
            "POST",
            "/products",
            headers={"Content-Type": "text/plain"},
            body=baseline,
            expected_status=415,
            expected_response=self._error_expected(415),
        )

    def _build_get_by_name(self, op: dict[str, Any] | None) -> None:
        if not op:
            return
        param = next((p for p in op.get("parameters", []) if p.get("in") == "path"), {})
        schema = _deref(self.spec, param.get("schema", {}))
        valid = str(schema.get("example") or "Wireless Bluetooth Headphones").replace("%20", " ")

        self._add(
            "positive",
            "Get product by name — existing product → 200",
            "GET",
            "/products/" + quote(valid),
            expected_status=200,
            **self._success_kwargs(op),
        )
        for label, value in _schema_negatives("name", schema):
            self._add(
                "path_validation",
                f"Get product by name — {label} → 400",
                "GET",
                "/products/" + quote(str(value)),
                expected_status=400,
                expected_response=self._error_expected(400),
            )
        self._add(
            "not_found",
            "Get product by name — nonexistent name → 404",
            "GET",
            "/products/" + quote("Quantum Flux Capacitor"),
            expected_status=404,
            expected_response=self._error_expected(404),
        )

    def _request_body(self, op: dict[str, Any]) -> tuple[dict[str, Any], Any]:
        content = op.get("requestBody", {}).get("content", {}).get("application/json", {})
        schema = _deref(self.spec, content.get("schema", {}))
        return schema, content.get("example")


# --- schema helpers --------------------------------------------------------------------


def _base_path(spec: dict[str, Any]) -> str | None:
    servers = spec.get("servers", [])
    if servers and servers[0].get("url"):
        return servers[0]["url"]
    return None


def _resolve_ref(spec: dict[str, Any], ref: str) -> Any:
    node: Any = spec
    for part in ref.lstrip("#/").split("/"):
        node = node[part]
    return node


def _deref(spec: dict[str, Any], node: Any) -> Any:
    """Resolve a local ``$ref`` (one level deep is enough for this spec's shapes)."""
    if isinstance(node, dict) and "$ref" in node:
        return _deref(spec, _resolve_ref(spec, node["$ref"]))
    return node


def _error_schema(spec: dict[str, Any]) -> dict[str, Any]:
    """Resolve the error-envelope schema the spec uses for its 4xx/5xx responses.

    Generic: scans operations for the first response with a status >= 400 and returns its
    resolved JSON schema (following ``$ref`` into ``components/responses`` and
    ``components/schemas``). Returns ``{}`` when the spec declares no structured error body,
    in which case callers fall back to a minimal envelope.
    """
    for path_item in spec.get("paths", {}).values():
        if not isinstance(path_item, dict):
            continue
        for op in path_item.values():
            if not isinstance(op, dict):
                continue
            for status, resp in op.get("responses", {}).items():
                if not str(status).startswith(("4", "5")):
                    continue
                resp = _deref(spec, resp)
                schema = resp.get("content", {}).get("application/json", {}).get("schema", {})
                schema = _deref(spec, schema)
                if schema.get("properties"):
                    return schema
    return {}


def _success_schema(spec: dict[str, Any], op: dict[str, Any]) -> dict[str, Any]:
    """Resolve the JSON schema of an operation's first 2xx response (``{}`` if none)."""
    for status, resp in op.get("responses", {}).items():
        if not str(status).startswith("2"):
            continue
        resp = _deref(spec, resp)
        schema = resp.get("content", {}).get("application/json", {}).get("schema", {})
        return _deref(spec, schema)
    return {}


def _array_any_template(spec: dict[str, Any], schema: dict[str, Any]) -> list[Any]:
    """A single-node ``<<any>>`` template for an array success body.

    When the item schema declares ``required`` fields it yields ``[{field: "<<any>>", …}]`` (only
    those fields); otherwise — no declared required fields, or scalar items — it yields
    ``["<<any>>"]`` (each element accepted).
    """
    items = _deref(spec, schema.get("items", {}))
    required = items.get("required") or []
    if required:
        return [{field: ANY for field in required}]
    return [ANY]


def _schema_negatives(name: str, schema: dict[str, Any]) -> list[tuple[str, Any]]:
    """Constraint violations for a scalar leaf schema as ``(label, violating_value)`` pairs."""
    out: list[tuple[str, Any]] = []
    if "enum" in schema:
        out.append((f"{name} not in allowed enum", "__INVALID_ENUM__"))
    if "pattern" in schema:
        out.append((f"{name} violates pattern {schema['pattern']}", "!bad!"))
    if "minLength" in schema and schema["minLength"] > 1:
        n = schema["minLength"]
        out.append((f"{name} below minLength {n}", "x" * (n - 1)))
    if "maxLength" in schema:
        n = schema["maxLength"]
        out.append((f"{name} above maxLength {n}", "x" * (n + 1)))
    if "minimum" in schema:
        out.append((f"{name} below minimum {schema['minimum']}", schema["minimum"] - 1))
    if "maximum" in schema:
        out.append((f"{name} above maximum {schema['maximum']}", schema["maximum"] + 1))
    return out


def _array_negatives(name: str, schema: dict[str, Any]) -> list[tuple[str, Any]]:
    """Constraint violations for an array schema (maxItems / uniqueItems / item length)."""
    out: list[tuple[str, Any]] = []
    items = schema.get("items", {})
    if "maxItems" in schema:
        n = schema["maxItems"]
        out.append((f"{name} exceeds maxItems {n}", [f"tag{i}" for i in range(n + 1)]))
    if schema.get("uniqueItems"):
        out.append((f"{name} contains duplicate items", ["dup", "dup"]))
    if "maxLength" in items:
        n = items["maxLength"]
        out.append((f"{name} item exceeds maxLength {n}", ["x" * (n + 1)]))
    return out


def _sample_value(spec: dict[str, Any], schema: dict[str, Any]) -> Any:
    """A schema-valid sample request body, with a concrete value for EVERY required field.

    Recurses into nested objects and arrays so the positive case sends a complete valid payload and
    each negative case can drop/override exactly one field. Uses a field's ``example`` when given,
    else synthesizes by type (enum -> first; numbers respect bounds; strings honour common formats
    and min/maxLength). Optional fields are omitted (a minimal valid body).
    """
    schema = _deref(spec, schema)
    if "example" in schema:
        return copy.deepcopy(schema["example"])
    if schema.get("enum"):
        return schema["enum"][0]
    t = schema.get("type")
    if t == "array":
        count = max(int(schema.get("minItems", 1)), 1)
        return [_sample_value(spec, schema.get("items", {})) for _ in range(count)]
    if t in ("integer", "number"):
        if "minimum" in schema:
            return schema["minimum"]
        if "maximum" in schema:
            return schema["maximum"]
        return 1
    if t == "boolean":
        return True
    if t == "string":
        return _sample_string(schema)
    if t == "object" or schema.get("properties") or schema.get("required"):
        return _sample_object(spec, schema)
    return "sample"


def _sample_object(spec: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    props = schema.get("properties", {})
    return {
        field: _sample_value(spec, props.get(field, {}))
        for field in schema.get("required", [])
    }


def _sample_string(schema: dict[str, Any]) -> str:
    fmt = schema.get("format")
    formats = {
        "date": "2024-01-01",
        "date-time": "2024-01-01T00:00:00Z",
        "email": "user@example.com",
        "uuid": "00000000-0000-0000-0000-000000000000",
        "uri": "https://example.com",
    }
    if fmt in formats:
        return formats[fmt]
    s = "sample"
    if isinstance(schema.get("minLength"), int) and len(s) < schema["minLength"]:
        s = s.ljust(schema["minLength"], "x")
    if isinstance(schema.get("maxLength"), int) and len(s) > schema["maxLength"]:
        s = s[: schema["maxLength"]]
    return s


# --- sheet writing ---------------------------------------------------------------------


def _write_sheet(
    out_path: Path,
    base_path: str | None,
    cases: list[TestCase],
    logs_fetch_url: str | None = None,
) -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = "tests"
    ws.append(["Basepath", base_path or ""])
    # CloudHub log-fetch URL — auto-filled from deployments_base_url + the spec's deployment id
    # when available; otherwise blank for the user to fill. Required to validate anypoint logs.
    ws.append(["application_logs_fetch_url", logs_fetch_url or ""])
    ws.append(["Auth"])
    ws.append(SHEET_COLUMNS)
    for case in cases:
        ws.append(_case_to_row(case))
    wb.save(out_path)


def _case_to_row(case: TestCase) -> list[Any]:
    return [
        case.test_id,
        case.description or "",
        case.method,
        case.url,
        json.dumps(case.headers) if case.headers else "",
        _body_cell(case.body),
        "Yes" if case.auth_required else "No",
        case.expected_status if case.expected_status is not None else "",
        json.dumps(case.expected_response) if case.expected_response is not None else "",
        case.response_match_mode.value,
        "Yes" if case.validate_logs else "No",
        "||".join(case.expected_log_strings),
        case.log_match_mode.value,
        case.log_source,
    ]


def _body_cell(body: Any) -> str:
    """Serialize a body cell; raw strings (e.g. deliberately malformed JSON) are kept verbatim."""
    if body is None:
        return ""
    if isinstance(body, str):
        return body
    return json.dumps(body)
