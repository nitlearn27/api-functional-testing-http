from api_log_test_mcp.matching.response_matcher import assert_response
from api_log_test_mcp.models import MatchMode


def test_json_subset_passes_with_extra_keys():
    result = assert_response(
        actual_body={"a": 1, "b": 2, "extra": 9},
        expected={"a": 1, "b": 2},
        mode=MatchMode.JSON_SUBSET,
    )
    assert result.passed


def test_json_subset_reports_missing_and_mismatch():
    result = assert_response(
        actual_body={"a": 1},
        expected={"a": 2, "b": 3},
        mode=MatchMode.JSON_SUBSET,
    )
    assert not result.passed
    paths = {d.path for d in result.diffs}
    assert "a" in paths and "b" in paths


def test_exact_rejects_extra_keys():
    result = assert_response(
        actual_body={"a": 1, "b": 2},
        expected={"a": 1},
        mode=MatchMode.EXACT,
    )
    assert not result.passed
    assert any("unexpected key" in d.message for d in result.diffs)


def test_json_subset_single_node_template_passes_any_count():
    result = assert_response(
        actual_body=[{"a": 1}, {"a": 2}, {"a": 3}],
        expected=[{"a": "<<any>>"}],
        mode=MatchMode.JSON_SUBSET,
    )
    assert result.passed  # every object matches the template, count irrelevant


def test_json_subset_single_node_template_checks_every_object():
    result = assert_response(
        actual_body=[{"a": 1}, {"b": 2}],  # second object missing `a`
        expected=[{"a": "<<any>>"}],
        mode=MatchMode.JSON_SUBSET,
    )
    assert not result.passed
    assert any(d.path == "1.a" and d.message == "missing key" for d in result.diffs)


def test_json_subset_empty_list_passes_single_node_template():
    result = assert_response(
        actual_body=[], expected=[{"a": "<<any>>"}], mode=MatchMode.JSON_SUBSET
    )
    assert result.passed  # no objects to check


def test_json_subset_multi_node_template_is_positional():
    result = assert_response(
        actual_body=[{"a": 1}, {"b": 2}, {"c": 3}],
        expected=[{"a": "<<any>>"}, {"b": "<<any>>"}],
        mode=MatchMode.JSON_SUBSET,
    )
    assert result.passed  # index 0,1 match; index 2 ignored


def test_exact_still_requires_equal_list_length():
    result = assert_response(
        actual_body=[{"a": 1}, {"a": 2}],
        expected=[{"a": 1}],
        mode=MatchMode.EXACT,
    )
    assert not result.passed
    assert any(d.message == "list length mismatch" for d in result.diffs)


def test_ignore_paths_prune_volatile_fields():
    result = assert_response(
        actual_body={"id": "xyz", "data": {"ts": 123, "v": 1}},
        expected={"id": "abc", "data": {"ts": 999, "v": 1}},
        mode=MatchMode.JSON_SUBSET,
        ignore_paths=["id", "data.ts"],
    )
    assert result.passed


def test_ignore_paths_wildcard_in_list():
    result = assert_response(
        actual_body={"items": [{"id": 1, "v": "a"}, {"id": 2, "v": "b"}]},
        expected={"items": [{"id": 9, "v": "a"}, {"id": 8, "v": "b"}]},
        mode=MatchMode.JSON_SUBSET,
        ignore_paths=["items.*.id"],
    )
    assert result.passed


def test_schema_mode():
    schema = {
        "type": "object",
        "properties": {"n": {"type": "integer"}},
        "required": ["n"],
    }
    ok = assert_response(actual_body={"n": 5}, expected=schema, mode=MatchMode.SCHEMA)
    assert ok.passed
    bad = assert_response(actual_body={"n": "x"}, expected=schema, mode=MatchMode.SCHEMA)
    assert not bad.passed


def test_any_value_wildcard_accepts_any_present_value():
    # value differs but field exists -> passes because expected is <<any>>
    result = assert_response(
        actual_body={"id": "generated-123", "status": "ok"},
        expected={"id": "<<any>>", "status": "ok"},
        mode=MatchMode.JSON_SUBSET,
    )
    assert result.passed


def test_any_value_wildcard_still_requires_presence():
    result = assert_response(
        actual_body={"status": "ok"},  # no 'id' field
        expected={"id": "<<any>>", "status": "ok"},
        mode=MatchMode.JSON_SUBSET,
    )
    assert not result.passed
    assert any(d.path == "id" and d.message == "missing key" for d in result.diffs)


def test_any_value_wildcard_nested_and_exact_mode():
    result = assert_response(
        actual_body={"data": {"token": "xyz", "n": 5}},
        expected={"data": {"token": "<<any>>", "n": 5}},
        mode=MatchMode.EXACT,
    )
    assert result.passed


def test_any_value_wildcard_in_list_item():
    result = assert_response(
        actual_body={"items": [{"id": 1}, {"id": 99}]},
        expected={"items": ["<<any>>", {"id": 99}]},
        mode=MatchMode.JSON_SUBSET,
    )
    assert result.passed


def test_status_check():
    result = assert_response(
        actual_body={}, expected={}, actual_status=500, expected_status=200,
    )
    assert not result.passed
    assert not result.status_ok
