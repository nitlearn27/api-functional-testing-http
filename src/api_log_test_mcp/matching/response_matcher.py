"""ResponseMatcher: compare an actual API response against an expectation.

Three modes:
- ``exact``       — deep equality of the whole body.
- ``json_subset`` — every key/value in ``expected`` must appear in ``actual`` (actual may
                    have extra keys). A single-node expected list ``[tmpl]`` is a template
                    checked against EVERY actual element (any-length list); a multi-node expected
                    list is matched positionally (extra actual elements ignored).
- ``schema``      — ``expected`` is treated as a JSON Schema and validated against ``actual``.

``ignore_paths`` are dotted paths (e.g. ``data.id``, ``items.*.timestamp``) that are pruned
from both sides before comparison so volatile fields (generated IDs, timestamps) don't cause
spurious failures. ``*`` matches any single list index or dict key segment.

The wildcard value ``<<any>>`` may appear anywhere in the expected body (``exact`` and
``json_subset`` modes): the field's presence is still required, but its value is accepted as-is
without comparison. Use it for fields whose value is unpredictable but must exist.
"""

from __future__ import annotations

import copy
from typing import Any

from ..models import AssertResult, MatchMode, ResponseDiff

#: Expected-value wildcard: matches any actual value as long as the field is present.
ANY_VALUE = "<<any>>"


def assert_response(
    *,
    actual_body: Any,
    expected: Any,
    mode: MatchMode = MatchMode.JSON_SUBSET,
    ignore_paths: list[str] | None = None,
    actual_status: int | None = None,
    expected_status: int | None = None,
) -> AssertResult:
    """Compare ``actual_body`` against ``expected`` under ``mode``.

    Status is checked separately when ``expected_status`` is provided.
    """
    ignore_paths = ignore_paths or []

    status_ok = True
    diffs: list[ResponseDiff] = []
    if expected_status is not None:
        status_ok = actual_status == expected_status
        if not status_ok:
            diffs.append(
                ResponseDiff(
                    path="<status>",
                    expected=expected_status,
                    actual=actual_status,
                    message="status code mismatch",
                )
            )

    if mode is MatchMode.STATUS_ONLY:
        return AssertResult(
            passed=status_ok, mode=mode, status_ok=status_ok, diffs=diffs
        )

    pruned_actual = _prune(copy.deepcopy(actual_body), ignore_paths)

    if mode is MatchMode.SCHEMA:
        diffs.extend(_check_schema(pruned_actual, expected))
    else:
        pruned_expected = _prune(copy.deepcopy(expected), ignore_paths)
        subset = mode is MatchMode.JSON_SUBSET
        diffs.extend(_compare(pruned_expected, pruned_actual, path="", subset=subset))

    return AssertResult(
        passed=status_ok and not diffs,
        mode=mode,
        status_ok=status_ok,
        diffs=diffs,
    )


def _check_schema(actual: Any, schema: Any) -> list[ResponseDiff]:
    """Validate ``actual`` against a JSON Schema, returning diffs for each error."""
    import jsonschema

    validator = jsonschema.Draft202012Validator(schema)
    diffs: list[ResponseDiff] = []
    for err in sorted(validator.iter_errors(actual), key=lambda e: list(e.absolute_path)):
        path = ".".join(str(p) for p in err.absolute_path) or "<root>"
        diffs.append(ResponseDiff(path=path, message=err.message))
    return diffs


def _compare(expected: Any, actual: Any, *, path: str, subset: bool) -> list[ResponseDiff]:
    """Recursively compare expected vs actual, collecting diffs."""
    diffs: list[ResponseDiff] = []

    # `<<any>>` accepts whatever value is present here (presence was already checked by the
    # caller for dict keys / list indices), so stop comparing this subtree.
    if expected == ANY_VALUE:
        return diffs

    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            return [_type_diff(path, expected, actual)]
        if not subset:
            extra = set(actual) - set(expected)
            for key in sorted(extra):
                diffs.append(
                    ResponseDiff(
                        path=_join(path, key),
                        actual=actual[key],
                        message="unexpected key (exact mode)",
                    )
                )
        for key, exp_val in expected.items():
            child = _join(path, key)
            if key not in actual:
                diffs.append(
                    ResponseDiff(path=child, expected=exp_val, message="missing key")
                )
                continue
            diffs.extend(_compare(exp_val, actual[key], path=child, subset=subset))
        return diffs

    if isinstance(expected, list):
        if not isinstance(actual, list):
            return [_type_diff(path, expected, actual)]
        # json_subset with a single-node template: that node is checked against EVERY actual
        # element, so a list of any length passes iff every element matches the template (the
        # count is irrelevant). An empty actual list passes vacuously.
        if subset and len(expected) == 1:
            for idx, item in enumerate(actual):
                diffs.extend(_compare(expected[0], item, path=_join(path, str(idx)), subset=subset))
            return diffs
        # exact mode, or a multi-node expected: positional. Only exact requires the lengths to
        # match; for json_subset the extra actual nodes beyond the template are ignored.
        if not subset and len(expected) != len(actual):
            diffs.append(
                ResponseDiff(
                    path=path,
                    expected=len(expected),
                    actual=len(actual),
                    message="list length mismatch",
                )
            )
        for idx, exp_item in enumerate(expected):
            child = _join(path, str(idx))
            if idx >= len(actual):
                diffs.append(
                    ResponseDiff(path=child, expected=exp_item, message="missing list item")
                )
                continue
            diffs.extend(_compare(exp_item, actual[idx], path=child, subset=subset))
        return diffs

    if expected != actual:
        diffs.append(
            ResponseDiff(path=path or "<root>", expected=expected, actual=actual,
                         message="value mismatch")
        )
    return diffs


def _type_diff(path: str, expected: Any, actual: Any) -> ResponseDiff:
    return ResponseDiff(
        path=path or "<root>",
        expected=type(expected).__name__,
        actual=type(actual).__name__,
        message="type mismatch",
    )


def _join(path: str, segment: str) -> str:
    return f"{path}.{segment}" if path else segment


def _prune(value: Any, ignore_paths: list[str]) -> Any:
    """Remove every ignore path from ``value`` in place and return it."""
    for raw in ignore_paths:
        segments = [s for s in raw.strip().split(".") if s]
        if segments:
            _prune_one(value, segments)
    return value


def _prune_one(value: Any, segments: list[str]) -> None:
    head, rest = segments[0], segments[1:]

    if isinstance(value, dict):
        targets = list(value.keys()) if head == "*" else [head]
        for key in targets:
            if key not in value:
                continue
            if rest:
                _prune_one(value[key], rest)
            else:
                value.pop(key, None)
    elif isinstance(value, list):
        if head == "*":
            indices = range(len(value))
        elif head.isdigit() and int(head) < len(value):
            indices = [int(head)]
        else:
            return
        # Prune trailing elements first so index removal stays valid in the leaf case.
        for idx in sorted(indices, reverse=True):
            if rest:
                _prune_one(value[idx], rest)
            else:
                value.pop(idx)
