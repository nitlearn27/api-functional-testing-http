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


class _FixedUUID:
    hex = "abcdef012345deadbeefcafe00000000"  # [:12] -> "abcdef012345"


def test_log_phase_one_snapshot_and_merge(tmp_path, monkeypatch):
    # Two cases, both validate_logs=Yes against the file source: one expects a string that
    # appears on a line carrying its correlation id, the other expects a missing one.
    rows = [
        ["TC-1", "POST", "/orders", "no", 201, "yes",
         '["Order lookup succeeded"]', "contains", "file"],
        ["TC-2", "POST", "/orders", "no", 201, "yes",
         '["string that is not in the log"]', "contains", "file"],
    ]
    suite_path = tmp_path / "suite.xlsx"
    _make_suite(suite_path, rows)

    # Deterministic correlation ids so the log file can carry them: TC-1-abcdef012345, etc.
    monkeypatch.setattr(orchestrate.uuid, "uuid4", lambda: _FixedUUID())

    # A log where the expected message appears on the TC-1 correlation line AND, separately, on
    # an UNRELATED correlation line. The strict rule must record only the former (corr id +
    # message both present), never the message-only line.
    log = tmp_path / "app.log"
    log.write_text(
        "2026-06-04T10:00:01Z INFO event:TC-1-abcdef012345 - Order lookup succeeded\n"
        "2026-06-04T10:00:02Z INFO event:OTHER-999 - Order lookup succeeded\n"
    )

    monkeypatch.setattr(orchestrate, "call_api",
                        lambda *a, **k: ApiResponse(status=201, body=None))
    monkeypatch.setattr(
        orchestrate, "get_settings",
        # No waits/retries in the test (TC-2's logs are intentionally absent).
        lambda: Settings(file_log_path=str(log), propagation_wait_seconds=0,
                         log_fetch_max_retries=0),
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
    assert not tc1.log_validation.used_fallback  # matched on the correlation line, not whole-log
    # only the line carrying TC-1's correlation id is considered (the OTHER-999 line is excluded)
    assert tc1.log_validation.lines_considered == 1

    tc2 = by_id["TC-2"]
    assert tc2.log_validation is not None
    assert not tc2.log_validation.passed
    assert not tc2.passed
    assert tc2.log_validation.missing == ["string that is not in the log"]


def test_snapshot_retries_until_correlation_present(monkeypatch):
    """Re-fetch on an interval until the correlation id surfaces, discarding stale snapshots."""
    sids = iter(["s0", "s1", "s2"])
    fetched: list[str] = []
    monkeypatch.setattr(orchestrate, "snapshot_logs",
                        lambda *a, **k: fetched.append(n := next(sids)) or n)
    present = {"s0": False, "s1": True, "s2": True}
    monkeypatch.setattr(orchestrate, "correlation_present", lambda sid, cid: present[sid])
    discarded: list[str] = []
    monkeypatch.setattr(orchestrate, "discard_snapshot", lambda sid: discarded.append(sid))
    slept: list[float] = []
    monkeypatch.setattr(orchestrate.time, "sleep", lambda s: slept.append(s))

    settings = Settings(log_fetch_max_retries=3, log_fetch_retry_wait_seconds=60)
    sid = orchestrate._snapshot_with_retry(settings, "anypoint", ["TC-1-x"])

    assert sid == "s1"            # stopped as soon as the id appeared
    assert fetched == ["s0", "s1"]
    assert discarded == ["s0"]   # the stale first snapshot was dropped
    assert slept == [60]         # one 60s wait before the retry


def test_snapshot_retry_gives_up_after_max(monkeypatch):
    """After the retry budget is exhausted, return the freshest snapshot anyway."""
    sids = iter(["s0", "s1", "s2"])
    fetched: list[str] = []
    monkeypatch.setattr(orchestrate, "snapshot_logs",
                        lambda *a, **k: fetched.append(n := next(sids)) or n)
    monkeypatch.setattr(orchestrate, "correlation_present", lambda sid, cid: False)
    monkeypatch.setattr(orchestrate, "discard_snapshot", lambda sid: None)
    slept: list[float] = []
    monkeypatch.setattr(orchestrate.time, "sleep", lambda s: slept.append(s))

    settings = Settings(log_fetch_max_retries=2, log_fetch_retry_wait_seconds=60)
    sid = orchestrate._snapshot_with_retry(settings, "anypoint", ["TC-1-x"])

    assert sid == "s2"               # 1 initial + 2 retries
    assert fetched == ["s0", "s1", "s2"]
    assert slept == [60, 60]


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
