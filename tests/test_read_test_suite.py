from api_log_test_mcp.models import LogMatchMode, MatchMode
from api_log_test_mcp.tools.suite import read_test_suite


def test_parses_valid_cases_and_base_path(sample_suite_path):
    suite = read_test_suite(sample_suite_path)
    assert suite.base_path == "https://api.example.test/"
    ids = {c.test_id for c in suite.cases}
    assert ids == {"order-001", "pay-042"}


def test_parses_application_logs_fetch_url(sample_suite_path):
    suite = read_test_suite(sample_suite_path)
    assert suite.application_logs_fetch_url == "https://logs.example.test/deployments/abc"


def test_application_logs_fetch_url_absent_is_none(tmp_path):
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.append(["Basepath", "https://api.example.test/"])
    ws.append(["test_id", "method", "url"])
    ws.append(["TC-1", "GET", "/x"])
    path = tmp_path / "no_log_url.xlsx"
    wb.save(path)

    suite = read_test_suite(str(path))
    assert suite.application_logs_fetch_url is None


def test_collects_parse_errors(sample_suite_path):
    suite = read_test_suite(sample_suite_path)
    messages = {(e.column, e.message) for e in suite.parse_errors}
    assert any("duplicate" in m for _, m in messages)
    assert any(col == "headers" for col, _ in messages)
    assert any("missing test_id" in m for _, m in messages)


def test_normalizes_fields(sample_suite_path):
    suite = read_test_suite(sample_suite_path)
    order = next(c for c in suite.cases if c.test_id == "order-001")
    assert order.method == "GET"  # coerced upper
    assert order.headers == {"Accept": "application/json"}
    assert order.auth_required is False
    assert order.expected_status == 200
    assert order.response_match_mode is MatchMode.JSON_SUBSET
    assert order.ignore_paths == ["data.id", "data.timestamp"]
    assert order.validate_logs is True
    assert order.expected_log_strings == ["Order lookup succeeded", "returning 200"]
    assert order.log_source == "file"


def test_expected_logs_delimiter_fallback(sample_suite_path):
    suite = read_test_suite(sample_suite_path)
    pay = next(c for c in suite.cases if c.test_id == "pay-042")
    assert pay.expected_log_strings == ["Payment declined", "gateway slow"]
    assert pay.response_match_mode is MatchMode.EXACT
    assert pay.validate_logs is False
    assert pay.log_match_mode is LogMatchMode.CONTAINS
    assert pay.body == {"amount": 10}


def test_missing_file_returns_error():
    suite = read_test_suite("/no/such/file.xlsx")
    assert suite.cases == []
    assert suite.parse_errors and "not found" in suite.parse_errors[0].message


def test_reads_real_numbers_file(numbers_suite_path):
    """The .numbers sample parses into the expected structure (stable facts only).

    Asserts only what is stable about the sample (it is a user-edited file), i.e. that
    .numbers reading, header detection, base_path and the key flags work — not the exact
    body/expected_response content.
    """
    suite = read_test_suite(numbers_suite_path)
    assert "cloudhub.io" in (suite.base_path or "")
    by_id = {c.test_id: c for c in suite.cases}
    assert set(by_id) == {"TC-001", "TC-002"}

    tc1 = by_id["TC-001"]
    assert tc1.method == "POST"
    assert tc1.url == "/orders"
    assert tc1.auth_required is False
    assert tc1.expected_status == 201
    assert tc1.response_match_mode is MatchMode.JSON_SUBSET

    assert by_id["TC-002"].expected_status == 400
