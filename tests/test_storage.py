import json

import pytest

from locomotive.storage import Storage


@pytest.fixture
def storage(tmp_path):
    return Storage.from_root(tmp_path / "artifacts")


class TestStoragePaths:
    def test_run_dir(self, storage):
        path = storage.run_dir("run-1")
        assert path.name == "run-1"
        assert path.parent.name == "runs"

    def test_metrics_path(self, storage):
        path = storage.metrics_path("run-1")
        assert path.name == "metrics.json"

    def test_analysis_path(self, storage):
        path = storage.analysis_path("run-1")
        assert path.name == "analysis.json"

    def test_report_path(self, storage):
        path = storage.report_path("run-1")
        assert path.name == "report.html"


class TestStorageBaseline:
    def test_set_and_get_baseline(self, storage):
        storage.set_baseline("run-42")
        assert storage.get_baseline() == "run-42"

    def test_get_baseline_no_file(self, storage):
        assert storage.get_baseline() is None


class TestStorageJson:
    def test_save_load_roundtrip(self, storage):
        data = {"rps": 150.0, "p95_ms": 250}
        path = storage.metrics_path("run-1")
        storage.save_json(path, data)
        loaded = storage.load_json(path)
        assert loaded["rps"] == 150.0
        assert loaded["p95_ms"] == 250


class TestStorageHistory:
    def test_load_history_empty(self, storage):
        result = storage.load_history()
        assert result == {"runs": []}

    def test_append_to_history(self, storage):
        metrics = {"rps": 100, "p95_ms": 200}
        meta = {"started_at": "2025-01-01T00:00:00Z"}
        storage.append_to_history("run-1", metrics, meta, max_runs=10)
        history = storage.load_history()
        assert len(history["runs"]) == 1
        assert history["runs"][0]["run_id"] == "run-1"

    def test_history_deduplication(self, storage):
        metrics = {"rps": 100}
        meta = {}
        storage.append_to_history("run-1", metrics, meta, max_runs=10)
        storage.append_to_history("run-1", metrics, meta, max_runs=10)
        history = storage.load_history()
        assert len(history["runs"]) == 1

    def test_history_max_runs_trimming(self, storage):
        metrics = {"rps": 100}
        for i in range(5):
            storage.append_to_history(f"run-{i}", metrics, {}, max_runs=3)
        history = storage.load_history()
        assert len(history["runs"]) == 3
        assert history["runs"][0]["run_id"] == "run-2"
