from api_log_test_mcp.cache.snapshot_store import SnapshotStore
from api_log_test_mcp.logsource.file_source import FileLogSource


def test_file_source_discovers_and_snapshots(sample_log_path):
    source = FileLogSource(sample_log_path)
    assert source.discover_instances() == ["sample_app"]
    raw = source.snapshot()
    assert "sample_app" in raw.lines_by_instance
    assert raw.total_lines() == 9


def test_snapshot_store_builds_correlation_index(sample_log_path):
    store = SnapshotStore()
    snap = store.create(FileLogSource(sample_log_path))
    # three distinct correlation ids in the fixture
    assert set(snap.index) == {"order-001", "pay-042", "health-check-99"}
    assert len(snap.lines_for("order-001")) == 3
    # the no-correlation heartbeat line is not indexed
    assert all("Heartbeat" not in line for lines in snap.index.values() for line in lines)


def test_snapshot_discard(sample_log_path):
    store = SnapshotStore()
    snap = store.create(FileLogSource(sample_log_path))
    assert store.get(snap.snapshot_id) is snap
    assert store.discard(snap.snapshot_id) is True
    assert store.discard(snap.snapshot_id) is False
