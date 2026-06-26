"""End-to-end mock pipeline: read -> (assert) -> snapshot -> validate -> discard, offline."""

from api_log_test_mcp.cache.snapshot_store import SnapshotStore
from api_log_test_mcp.config import LogBackend, Settings
from api_log_test_mcp.logsource.base import LogSource, RawSnapshot
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


class _StaticSource(LogSource):
    def __init__(self, lines: list[str]):
        self._lines = lines

    def discover_instances(self) -> list[str]:
        return ["cloudhub"]

    def snapshot(self, instances=None) -> RawSnapshot:
        return RawSnapshot(lines_by_instance={"cloudhub": self._lines})


def test_multiline_event_continuation_lines_group_under_header_id():
    """A Mule APIKIT:BAD_REQUEST event: only the header carries the correlation id; the boxed
    message + error type are continuation lines. They must group under the header's id so an
    expected string on a continuation line is still found (no whole-log fallback)."""
    event = [
        "2026-06-17 10:46:54 DefaultExceptionListener [correlationId: TC-004-ff7d9d4a6ab7]",
        "*" * 80,
        "Message    : required key [customerId] not found",
        "Error type : APIKIT:BAD_REQUEST",
        "*" * 80,
    ]
    store = SnapshotStore()
    snap = store.create(_StaticSource(event))
    res = logtools.validate_logs(
        snap.snapshot_id,
        "TC-004-ff7d9d4a6ab7",
        ["APIKIT:BAD_REQUEST"],
        correlation_fallback=False,
        store=store,
    )
    assert res.passed
    assert res.used_fallback is False
    assert res.lines_considered == len(event)


def test_log_match_mode_enum_default():
    assert LogMatchMode.CONTAINS.value == "contains"
