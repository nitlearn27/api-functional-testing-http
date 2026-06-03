"""End-to-end mock pipeline: read -> (assert) -> snapshot -> validate -> discard, offline."""

from api_log_test_mcp.cache.snapshot_store import SnapshotStore
from api_log_test_mcp.config import LogBackend, Settings
from api_log_test_mcp.models import LogMatchMode
from api_log_test_mcp.tools import logs as logtools
from api_log_test_mcp.tools.suite import read_test_suite


def _settings(log_path: str) -> Settings:
    return Settings(log_backend=LogBackend.FILE, file_log_path=log_path)


def test_validate_logs_pass_and_fail(sample_log_path):
    store = SnapshotStore()
    sid = logtools.snapshot_logs(_settings(sample_log_path), store=store)

    ok = logtools.validate_logs(
        sid, "order-001", ["Order lookup succeeded", "returning 200"], store=store
    )
    assert ok.passed
    assert ok.lines_considered == 3
    assert ok.missing == []

    fail = logtools.validate_logs(
        sid, "pay-042", ["Payment declined", "this never appears"], store=store
    )
    assert not fail.passed
    assert fail.missing == ["this never appears"]
    assert "Payment declined" in fail.matched


def test_validate_unknown_correlation_id_strict(sample_log_path):
    store = SnapshotStore()
    sid = logtools.snapshot_logs(_settings(sample_log_path), store=store)
    res = logtools.validate_logs(
        sid, "nope", ["x"], correlation_fallback=False, store=store
    )
    assert not res.passed
    assert res.lines_considered == 0
    assert res.used_fallback is False


def test_validate_unknown_correlation_id_falls_back(sample_log_path):
    store = SnapshotStore()
    sid = logtools.snapshot_logs(_settings(sample_log_path), store=store)
    res = logtools.validate_logs(
        sid, "nope", ["Order lookup succeeded"], correlation_fallback=True, store=store
    )
    assert res.passed  # matched against the whole snapshot
    assert res.used_fallback is True


def test_full_mock_suite_run(sample_suite_path, sample_log_path):
    """read_test_suite -> snapshot -> validate each case's expected logs -> discard."""
    suite = read_test_suite(sample_suite_path)
    store = SnapshotStore()
    sid = logtools.snapshot_logs(_settings(sample_log_path), store=store)
    try:
        results = {}
        for case in suite.cases:
            if not case.expected_log_strings:
                continue
            results[case.test_id] = logtools.validate_logs(
                sid, case.test_id, case.expected_log_strings, case.log_match_mode, store=store
            )
        assert results["order-001"].passed
        assert results["pay-042"].passed  # "Payment declined" + "gateway slow" both present
    finally:
        assert logtools.discard_snapshot(sid, store=store) is True


def test_log_match_mode_enum_default():
    assert LogMatchMode.CONTAINS.value == "contains"
