"""run_suite log-validation phase: one snapshot per run, merged results (offline)."""

from openpyxl import Workbook

from api_log_test_mcp.config import Settings
from api_log_test_mcp.models import ApiResponse
from api_log_test_mcp.tools import logs as logtools
from api_log_test_mcp.tools import orchestrate

HEADERS = [
    "test_id", "method", "url", "auth_required", "expected_status",
    "validate_logs", "expected_log_strings", "log_match_mode", "log_source",
]


def _make_suite(path, rows) -> None:
    wb = Workbook()
    ws = wb.active
    ws.append(["Basepath", "https://api.test/"])
    ws.append([])
    ws.append(HEADERS)
    for row in rows:
        ws.append(row)
    wb.save(path)


def test_log_phase_one_snapshot_and_merge(tmp_path, sample_log_path, monkeypatch):
    # Two cases, both validate_logs=Yes against the file source: one expects a string present
    # in the sample log, the other expects a missing one.
    rows = [
        ["TC-1", "POST", "/orders", "no", 201, "yes",
         '["Order lookup succeeded"]', "contains", "file"],
        ["TC-2", "POST", "/orders", "no", 201, "yes",
         '["string that is not in the log"]', "contains", "file"],
    ]
    suite_path = tmp_path / "suite.xlsx"
    _make_suite(suite_path, rows)

    # Mock the HTTP call so the response assertion passes without network.
    monkeypatch.setattr(orchestrate, "call_api",
                        lambda *a, **k: ApiResponse(status=201, body=None))

    # Settings: file backend pointed at the sample log; no propagation wait in tests.
    monkeypatch.setattr(
        orchestrate, "get_settings",
        lambda: Settings(file_log_path=sample_log_path, propagation_wait_seconds=0),
    )

    # Count how many times a snapshot is taken.
    real_snapshot = logtools.snapshot_logs
    calls = {"n": 0}

    def counting_snapshot(*a, **k):
        calls["n"] += 1
        return real_snapshot(*a, **k)

    monkeypatch.setattr(orchestrate, "snapshot_logs", counting_snapshot)

    report = orchestrate.run_suite(str(suite_path))

    assert calls["n"] == 1  # both cases share log_source=file -> single download
    by_id = {c.test_id: c for c in report.cases}

    tc1 = by_id["TC-1"]
    assert tc1.log_validation is not None
    assert tc1.log_validation.passed
    assert tc1.passed
    # correlation id (generated) won't be in the sample log -> whole-log fallback
    assert tc1.log_validation.used_fallback

    tc2 = by_id["TC-2"]
    assert tc2.log_validation is not None
    assert not tc2.log_validation.passed
    assert not tc2.passed
    assert tc2.log_validation.missing == ["string that is not in the log"]


def test_no_log_phase_when_all_disabled(tmp_path, monkeypatch):
    rows = [["TC-1", "POST", "/orders", "no", 201, "no", "", "contains", "file"]]
    suite_path = tmp_path / "suite.xlsx"
    _make_suite(suite_path, rows)

    monkeypatch.setattr(orchestrate, "call_api",
                        lambda *a, **k: ApiResponse(status=201, body=None))
    monkeypatch.setattr(orchestrate, "get_settings", lambda: Settings(propagation_wait_seconds=0))

    def fail_snapshot(*a, **k):
        raise AssertionError("snapshot_logs should not be called when validate_logs=no")

    monkeypatch.setattr(orchestrate, "snapshot_logs", fail_snapshot)

    report = orchestrate.run_suite(str(suite_path))
    assert report.passed == 1
    assert report.cases[0].log_validation is None
