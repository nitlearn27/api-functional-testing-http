"""generate_test_suite: build a runnable .xlsx test suite from an OpenAPI spec.

The inverse of ``read_test_suite``: instead of parsing a hand-written sheet into ``TestCase``
objects, this reads an OpenAPI 3.0 YAML spec and emits a sheet in the exact format the parser
understands (worksheet ``tests``; a ``Basepath`` metadata row; the canonical header row; one row
per case). It walks **every** path × method generically (no fixed/hard-coded paths). Coverage is
comprehensive — a positive case per operation plus one negative per validation rule (required /
pattern / enum / length / numeric bounds / array rules), including **query-param and header-param**
coverage (required ones sent on the happy path; omitting or violating them yields a 400), plus
path-param, wrong-content-type, malformed-body, not-found and auth cases.

``create_test_suite_from_application`` builds on this generator: it combines the app's flow logic
(base path, branches, DataWeave responses, loggers, error-handler) with the bundled OpenAPI schema
(valid request structures + validation negatives), going into each flow for accurate expected
responses. Both write the same sheet format, which round-trips through ``read_test_suite`` with no
parse errors, so it runs immediately via ``run_test_suite``.
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
from ..models import LogMatchMode, MatchMode, TestCase
from .mule_app import MuleApp, locate_oas, parse_mule_app

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
    "test_id",
    "description",
    "method",
    "url",
    "headers",
    "body",
    "auth_required",
    "expected_status",
    "expected_response",
    "response_match_mode",
    "validate_logs",
    "expected_log_strings",
    "log_match_mode",
    "log_source",
]

# HTTP reason phrases used to build json_subset error expectations.
REASON = {
    400: "Bad Request",
    401: "Unauthorized",
    403: "Forbidden",
    404: "Not Found",
    409: "Conflict",
    415: "Unsupported Media Type",
    422: "Unprocessable Entity",
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


def create_test_suite_from_application(
    app_root: str, output_path: str | None = None
) -> dict[str, Any]:
    """Read a Mule app folder + its bundled OpenAPI schema and write a comprehensive .xlsx suite.

    Combines, going into each flow: schema-driven coverage with **valid request structures** (the
    spec's example body + required query params/headers, plus query/header/body validation
    negatives) AND the flow's own logic — base path, the DataWeave success response (exact mock
    values) + entry/exit loggers on success cases, the error-handler's actual ``{message}`` body on
    error cases, a case per ``choice`` branch, and the framework error mappings (404/405/406) the
    schema doesn't model. The schema is extracted from ``target/repository/**/*-oas.zip`` (``~/.m2``
    fallback); without one it falls back to flow-only cases. ``output_path`` defaults to
    ``<app-name>_suite.xlsx``. Returns ``output_path``/``base_path``/``oas_used``/``case_count``/
    ``cases_by_category``.
    """
    app = parse_mule_app(app_root)
    oas_path = locate_oas(app_root)
    if oas_path is not None:
        spec = yaml.safe_load(Path(oas_path).read_text())
        builder = _SuiteBuilder(spec, mule_app=app)
        builder.build()
        cases, categories = builder.cases, builder.categories
        extra_cases, extra_cats = _framework_error_cases(app, spec)
        cases.extend(extra_cases)
        for category, count in extra_cats.items():
            categories[category] = categories.get(category, 0) + count
        for i, case in enumerate(cases, 1):  # renumber after appending the framework cases
            case.test_id = f"TC-{i:03d}"
    else:
        cases, categories = _flow_cases(app)

    out = (
        Path(output_path)
        if output_path
        else Path(app_root) / f"{Path(app_root).resolve().name}_suite.xlsx"
    )
    _write_sheet(out, app.base_path, cases, None)
    return {
        "output_path": str(out),
        "base_path": app.base_path,
        "oas_used": str(oas_path) if oas_path else None,
        "case_count": len(cases),
        "cases_by_category": categories,
    }


def _framework_error_cases(
    app: MuleApp, spec: dict[str, Any]
) -> tuple[list[TestCase], dict[str, int]]:
    """APIkit framework error cases the OpenAPI schema does not model (404 unknown / 405 / 406).

    Each asserts the app's actual ``{message: …}`` body + the ``APIKIT:*`` error. The 406 case sends
    a schema-valid request (required query params) plus an unsatisfiable ``Accept`` so it reaches
    the not-acceptable check instead of failing query validation first.
    """
    cases: list[TestCase] = []
    cats: dict[str, int] = {}

    def mk(
        category: str,
        desc: str,
        method: str,
        url: str,
        status: int,
        logs: list[str],
        headers: dict[str, Any] | None = None,
    ) -> None:
        env = app.error_envelope.get(status)
        cases.append(
            TestCase(
                test_id="TC-000",
                description=desc,
                method=method,
                url=url,
                headers=headers or {},
                auth_required=False,
                expected_status=status,
                expected_response=dict(env) if env else None,
                response_match_mode=MatchMode.JSON_SUBSET if env else MatchMode.STATUS_ONLY,
                validate_logs=False,
                expected_log_strings=logs,
            )
        )
        cats[category] = cats.get(category, 0) + 1

    get_paths = [p for m, p in app.endpoints if m == "GET"]
    if 404 in app.error_envelope:
        mk(
            "not_found",
            "GET /__nonexistent__ — unknown resource → 404",
            "GET",
            "/__nonexistent__",
            404,
            ["APIKIT:NOT_FOUND"],
        )
    if 405 in app.error_envelope and app.endpoints:
        path = app.endpoints[0][1]
        used = {m for m, p in app.endpoints if p == path}
        unused = next((m for m in ("DELETE", "PUT", "PATCH") if m not in used), "DELETE")
        mk(
            "method_not_allowed",
            f"{unused} {path} — method not allowed → 405",
            unused,
            path,
            405,
            ["APIKIT:METHOD_NOT_ALLOWED"],
        )
    if 406 in app.error_envelope and get_paths:
        url = _valid_get_url(spec, get_paths[0])
        mk(
            "not_acceptable",
            f"GET {get_paths[0]} — Accept application/xml → 406",
            "GET",
            url,
            406,
            ["APIKIT:NOT_ACCEPTABLE"],
            headers={"Accept": "application/xml"},
        )
    return cases, cats


def _valid_get_url(spec: dict[str, Any], path: str) -> str:
    """``path`` with required path/query params filled from the spec (so the request is valid)."""
    op = spec.get("paths", {}).get(path, {}).get("get", {})
    params = [{**p, "schema": _deref(spec, p.get("schema", {}))} for p in op.get("parameters", [])]
    path_params = [p for p in params if p.get("in") == "path"]
    query_params = [p for p in params if p.get("in") == "query"]
    return _apply_path_params(path, path_params) + _valid_query_string(query_params)


def _flow_cases(app: MuleApp) -> tuple[list[TestCase], dict[str, int]]:
    """Build cases from the Mule flow logic alone — endpoints, branches, responses, error-handler.

    No schema is consulted: request bodies are minimal/branch-triggering, the success status is the
    HTTP convention (GET→200, create→201; the flow sets no explicit status), the success body is the
    flow's own DataWeave output, and the error cases come from the global error-handler's mappings.
    """
    cases: list[TestCase] = []
    cats: dict[str, int] = {}
    counter = 0

    def add(
        category: str,
        description: str,
        method: str,
        url: str,
        status: int,
        logs: list[str],
        *,
        body: Any = None,
        headers: dict[str, Any] | None = None,
        expected: Any = None,
    ) -> None:
        nonlocal counter
        counter += 1
        cases.append(
            TestCase(
                test_id=f"TC-{counter:03d}",
                description=description,
                method=method,
                url=url,
                headers=headers or {},
                body=body,
                auth_required=False,
                expected_status=status,
                expected_response=expected,
                response_match_mode=MatchMode.JSON_SUBSET
                if expected is not None
                else MatchMode.STATUS_ONLY,
                validate_logs=False,
                expected_log_strings=logs,
                log_match_mode=LogMatchMode.ALL_OF if len(logs) > 1 else LogMatchMode.CONTAINS,
            )
        )
        cats[category] = cats.get(category, 0) + 1

    # Positive case(s) per endpoint: one per choice branch, else one plain — asserting the flow's
    # DataWeave response + its entry/exit loggers (and the branch logger for branch cases).
    for method, path in app.endpoints:
        status = 201 if method == "POST" else 200
        loggers = app.flow_loggers.get((method, path), [])
        response = app.flow_responses.get((method, path)) or None
        json_body = method in ("POST", "PUT", "PATCH")
        headers = dict(JSON_HEADERS) if json_body else None
        branches = [b for b in app.branches if (b.method, b.path) == (method, path)]
        if branches:
            for b in branches:
                if b.field is not None:
                    body, label = {b.field: b.value}, f"{b.field}='{b.value}' branch"
                else:
                    body, label = {}, "otherwise branch"  # empty payload → equality whens are false
                add(
                    "branch_logic",
                    f"{method} {path} — {label} → {status}",
                    method,
                    path,
                    status,
                    [*loggers, b.logger],
                    body=body,
                    headers=headers,
                    expected=response,
                )
        else:
            add(
                "positive",
                f"{method} {path} — valid request → {status}",
                method,
                path,
                status,
                loggers,
                body={} if json_body else None,
                headers=headers,
                expected=response,
            )

    # Error-handler cases: one per APIKIT mapping the global error-handler defines (and we can
    # trigger), asserting the app's actual {message: …} body + the APIKIT error type.
    env = app.error_envelope
    get_paths = [p for m, p in app.endpoints if m == "GET"]
    post_paths = [p for m, p in app.endpoints if m == "POST"]
    if 404 in env:
        add(
            "not_found",
            "GET /__nonexistent__ — unknown resource → 404",
            "GET",
            "/__nonexistent__",
            404,
            ["APIKIT:NOT_FOUND"],
            expected=dict(env[404]),
        )
    if 405 in env and app.endpoints:
        path = app.endpoints[0][1]
        used = {m for m, p in app.endpoints if p == path}
        unused = next((m for m in ("DELETE", "PUT", "PATCH") if m not in used), "DELETE")
        add(
            "method_not_allowed",
            f"{unused} {path} — method not allowed → 405",
            unused,
            path,
            405,
            ["APIKIT:METHOD_NOT_ALLOWED"],
            expected=dict(env[405]),
        )
    if 406 in env and get_paths:
        add(
            "not_acceptable",
            f"GET {get_paths[0]} — Accept application/xml → 406",
            "GET",
            get_paths[0],
            406,
            ["APIKIT:NOT_ACCEPTABLE"],
            headers={"Accept": "application/xml"},
            expected=dict(env[406]),
        )
    if 415 in env and post_paths:
        add(
            "media_type",
            f"POST {post_paths[0]} — wrong Content-Type text/plain → 415",
            "POST",
            post_paths[0],
            415,
            ["APIKIT:UNSUPPORTED_MEDIA_TYPE"],
            body="plain text",
            headers={"Content-Type": "text/plain"},
            expected=dict(env[415]),
        )
    if 400 in env and post_paths:
        add(
            "bad_request",
            f"POST {post_paths[0]} — malformed JSON body → 400",
            "POST",
            post_paths[0],
            400,
            ["APIKIT:BAD_REQUEST"],
            body='{"broken": }',
            headers=dict(JSON_HEADERS),
            expected=dict(env[400]),
        )
    return cases, cats


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

    def __init__(self, spec: dict[str, Any], mule_app: MuleApp | None = None) -> None:
        self.spec = spec
        self.base_path = _base_path(spec)
        self.error_schema = _error_schema(spec)
        self.cases: list[TestCase] = []
        self.categories: dict[str, int] = {}
        self._n = 0
        # From-application path: overlay each operation's cases with the app's real flow facts
        # (DataWeave response + loggers on success, the {message} envelope on errors) and add a
        # case per choice branch. None for the pure schema tool.
        self.mule_app = mule_app

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
        # fields — never optional ones. ``echo`` overlays concrete request values, but only for
        # fields the *response* schema actually declares — so a create endpoint whose response is a
        # different shape (e.g. {patientId, message} rather than the echoed payload) isn't asserted
        # to contain request fields it never returns.
        node: dict[str, Any] = {field: ANY for field in schema.get("required", [])}
        if echo:
            props = schema.get("properties", {})
            node.update({k: v for k, v in echo.items() if k in props})
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
                # Logs are deferred for now: validate_logs defaults to No so a run is responses-only
                # (no log phase). The expected log strings are still populated so log validation can
                # be switched on later by flipping the column (or by the from-application enricher).
                validate_logs=False,
                expected_log_strings=_expected_log_strings(expected_status, method, url),
                log_source="anypoint",
            )
        )
        self.categories[category] = self.categories.get(category, 0) + 1

    def build(self) -> None:
        """Walk every path × method generically (works for any spec, not a fixed set of paths)."""
        methods = ("get", "post", "put", "patch", "delete")
        for path, item in (self.spec.get("paths") or {}).items():
            if not isinstance(item, dict):
                continue
            for method in methods:
                op = item.get(method)
                if isinstance(op, dict):
                    self._build_operation(path, method.upper(), op)

    def _build_operation(self, path: str, method: str, op: dict[str, Any]) -> None:
        """Per-operation coverage: a positive case plus one negative per validation rule.

        Covers query params, **header params**, path params, request-body rules, wrong content
        type, not-found and auth — all read generically from the operation's declared parameters
        and schemas. Required query params and headers are sent on the positive case; omitting a
        required one (or violating its constraints) yields a 400 negative.
        """
        params = [
            {**p, "schema": _deref(self.spec, p.get("schema", {}))}
            for p in op.get("parameters", [])
        ]
        path_params = [p for p in params if p.get("in") == "path"]
        query_params = [p for p in params if p.get("in") == "query"]
        header_params = [p for p in params if p.get("in") == "header"]
        responses = op.get("responses", {})
        success_status = _first_status(responses, "2") or 200
        has_json_body = bool(op.get("requestBody", {}).get("content", {}).get("application/json"))
        secured = _nonempty(op.get("security", self.spec.get("security")))
        name = str(op.get("summary") or op.get("operationId") or f"{method} {path}")
        start = len(self.cases)  # for the from-application overlay at the end

        base_url = _apply_path_params(path, path_params)
        query = _valid_query_string(query_params)
        pos_url = base_url + query
        # Required headers are sent on every case (plus Content-Type when there's a JSON body).
        required_headers = {
            h["name"]: _valid_value(h["schema"]) for h in header_params if h.get("required")
        }

        baseline: Any = None
        headers: dict[str, Any] = dict(required_headers)
        if has_json_body:
            schema, example = self._request_body(op)
            baseline = copy.deepcopy(example) if example else _sample_value(self.spec, schema)
            headers.update(JSON_HEADERS)
        echo = (
            copy.deepcopy(baseline)
            if baseline is not None and method in ("POST", "PUT", "PATCH")
            else None
        )

        # Positive case (first documented 2xx), sending required query params + headers.
        self._add(
            "positive",
            f"{name} — valid request → {success_status}",
            method,
            pos_url,
            headers=headers or None,
            body=baseline,
            expected_status=success_status,
            **self._success_kwargs(op, echo=echo),
        )

        # Query-param negatives: omit each required one, and violate each value constraint → 400.
        for qp in query_params:
            if qp.get("required"):
                others = [p for p in query_params if p["name"] != qp["name"] and p.get("required")]
                self._add(
                    "query_validation",
                    f"{name} — missing required query '{qp['name']}' → 400",
                    method,
                    base_url + _valid_query_string(others),
                    headers=headers or None,
                    body=baseline,
                    expected_status=400,
                    expected_response=self._error_expected(400),
                )
            for label, value in _schema_negatives(qp["name"], qp["schema"]):
                q_dict = {
                    p["name"]: _valid_value(p["schema"]) for p in query_params if p.get("required")
                }
                q_dict[qp["name"]] = value
                self._add(
                    "query_validation",
                    f"{name} — {label} → 400",
                    method,
                    base_url + "?" + urlencode(q_dict),
                    headers=headers or None,
                    body=baseline,
                    expected_status=400,
                    expected_response=self._error_expected(400),
                )

        # Header-param negatives: omit each required header, and violate each constraint → 400.
        for hp in header_params:
            if hp.get("required"):
                without = {k: v for k, v in headers.items() if k != hp["name"]}
                self._add(
                    "header_validation",
                    f"{name} — missing required header '{hp['name']}' → 400",
                    method,
                    pos_url,
                    headers=without or None,
                    body=baseline,
                    expected_status=400,
                    expected_response=self._error_expected(400),
                )
            for label, value in _schema_negatives(hp["name"], hp["schema"]):
                bad = dict(headers)
                bad[hp["name"]] = value
                self._add(
                    "header_validation",
                    f"{name} — header {label} → 400",
                    method,
                    pos_url,
                    headers=bad,
                    body=baseline,
                    expected_status=400,
                    expected_response=self._error_expected(400),
                )

        # Path-param constraint negatives.
        for pp in path_params:
            for label, value in _schema_negatives(pp["name"], pp["schema"]):
                self._add(
                    "path_validation",
                    f"{name} — {label} → 400",
                    method,
                    _apply_path_params(path, path_params, {pp["name"]: str(value)}) + query,
                    headers=headers or None,
                    body=baseline,
                    expected_status=400,
                    expected_response=self._error_expected(400),
                )

        # Request-body validation negatives (all folded into 400; Mulesoft cannot return 422).
        if has_json_body and baseline is not None:
            schema, _ = self._request_body(op)
            for field in schema.get("required", []):
                body = copy.deepcopy(baseline)
                body.pop(field, None)
                self._add(
                    "body_validation",
                    f"{name} — missing required '{field}' → {BODY_VALIDATION_STATUS}",
                    method,
                    pos_url,
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
                        f"{name} — {label} → {BODY_VALIDATION_STATUS}",
                        method,
                        pos_url,
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
                    f"{name} — extra field (additionalProperties:false) → {BODY_VALIDATION_STATUS}",
                    method,
                    pos_url,
                    headers=dict(JSON_HEADERS),
                    body=body,
                    expected_status=BODY_VALIDATION_STATUS,
                    expected_response=self._error_expected(BODY_VALIDATION_STATUS),
                )
            # Deliberately malformed JSON, sent verbatim as a raw (non-JSON) cell.
            self._add(
                "bad_request",
                f"{name} — malformed JSON body → 400",
                method,
                pos_url,
                headers=dict(JSON_HEADERS),
                body='{"broken": }',
                expected_status=400,
                expected_response=self._error_expected(400),
            )
            self._add(
                "media_type",
                f"{name} — wrong Content-Type text/plain → 415",
                method,
                pos_url,
                headers={"Content-Type": "text/plain"},
                body=baseline,
                expected_status=415,
                expected_response=self._error_expected(415),
            )

        # Not-found for a GET addressed by a path parameter.
        if method == "GET" and path_params:
            bogus = {p["name"]: "Nonexistent-ZZZ-000" for p in path_params}
            self._add(
                "not_found",
                f"{name} — nonexistent resource → 404",
                method,
                _apply_path_params(path, path_params, bogus) + query,
                headers=headers or None,
                body=baseline,
                expected_status=404,
                expected_response=self._error_expected(404),
            )

        # Auth negative when the operation (or the API) declares security.
        if secured:
            self._add(
                "auth",
                f"{name} — invalid credentials → 401",
                method,
                pos_url,
                headers={**headers, "Authorization": "Bearer invalid-token"},
                body=baseline,
                expected_status=401,
                expected_response=self._error_expected(401),
            )

        # From-application overlay: real flow facts + a case per choice branch.
        if self.mule_app is not None:
            self._apply_mule(start, method, path, name, pos_url, headers, baseline, success_status)

    def _apply_mule(
        self,
        start: int,
        method: str,
        path: str,
        name: str,
        pos_url: str,
        headers: dict[str, Any],
        baseline: Any,
        success_status: int,
    ) -> None:
        """Overlay this operation's schema-driven cases with the app's real flow facts.

        Going into the flow itself: success (2xx) cases assert the flow's DataWeave response (exact
        mock values) and entry/exit loggers; error (4xx/5xx) cases assert the error-handler's actual
        ``{message: …}`` body. A case per ``choice`` branch is added — the schema's valid request
        body (``baseline``) with the branch field overlaid, asserting that branch's logger. Requests
        keep the schema-shaped query/header/body so the live API accepts them.
        """
        app = self.mule_app
        assert app is not None
        loggers = app.flow_loggers.get((method, path), [])
        flow_resp = app.flow_responses.get((method, path))
        for case in self.cases[start:]:
            status = case.expected_status or 0
            if status < 400:
                if loggers:
                    case.expected_log_strings = list(loggers)
                    case.log_match_mode = LogMatchMode.ALL_OF
                if flow_resp:
                    case.expected_response = dict(flow_resp)
                    case.response_match_mode = MatchMode.JSON_SUBSET
            elif status in app.error_envelope:
                case.expected_response = dict(app.error_envelope[status])
                case.response_match_mode = MatchMode.JSON_SUBSET
        for b in [br for br in app.branches if (br.method, br.path) == (method, path)]:
            body = copy.deepcopy(baseline) if isinstance(baseline, dict) else {}
            if b.field is not None:
                body[b.field] = b.value  # overlay the branch trigger onto the valid baseline
                label = f"{b.field}='{b.value}' branch"
            else:
                label = "otherwise branch"  # the baseline's own field value falls through to else
            self._add(
                "branch_logic",
                f"{name} — {label} → {success_status}",
                method,
                pos_url,
                headers=headers or None,
                body=body,
                expected_status=success_status,
                expected_response=dict(flow_resp) if flow_resp else None,
                response_match_mode=MatchMode.JSON_SUBSET,
            )
            self.cases[-1].expected_log_strings = [*loggers, b.logger]
            self.cases[-1].log_match_mode = LogMatchMode.ALL_OF

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


def _first_status(responses: dict[str, Any], prefix: str) -> int | None:
    """First response status (as int) whose code starts with ``prefix`` ("2" for success)."""
    for code in responses:
        if str(code).startswith(prefix):
            try:
                return int(code)
            except ValueError:
                return None
    return None


def _nonempty(security: Any) -> bool:
    return isinstance(security, list) and len(security) > 0


def _valid_value(schema: dict[str, Any]) -> Any:
    """A schema-conformant valid scalar: explicit example, else first enum, else a type default."""
    if "example" in schema:
        return str(schema["example"]).replace("%20", " ")
    if schema.get("enum"):
        return schema["enum"][0]
    t = schema.get("type")
    if t in ("integer", "number"):
        return schema.get("minimum", 1)
    if t == "boolean":
        return True
    return "sample"


def _apply_path_params(
    path: str, path_params: list[dict[str, Any]], overrides: dict[str, str] | None = None
) -> str:
    """Substitute ``{name}`` segments with a valid (or overridden) value, url-quoted."""
    overrides = overrides or {}
    by_name = {p["name"]: p for p in path_params}

    def repl(match: re.Match[str]) -> str:
        key = match.group(1)
        value = (
            overrides[key]
            if key in overrides
            else str(_valid_value(by_name.get(key, {}).get("schema", {})))
        )
        return quote(value, safe="")

    return re.sub(r"\{([^}]+)\}", repl, path)


def _valid_query_string(query_params: list[dict[str, Any]]) -> str:
    """Query string of just the required params, each set to a valid value ("" if none)."""
    required = [p for p in query_params if p.get("required")]
    if not required:
        return ""
    return "?" + urlencode({p["name"]: _valid_value(p["schema"]) for p in required})


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
        field: _sample_value(spec, props.get(field, {})) for field in schema.get("required", [])
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
