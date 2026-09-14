from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, List, Optional, Union

from .utils import read_json, write_json, write_text, utc_now, ensure_dir


@dataclass
class Storage:
    root: Path

    @classmethod
    def from_root(cls, root: Union[str, Path]) -> "Storage":
        return cls(Path(root))

    def baseline_path(self) -> Path:
        return self.root / "baseline.json"

    def runs_dir(self) -> Path:
        return self.root / "runs"

    def run_dir(self, run_id: str) -> Path:
        return self.runs_dir() / run_id

    def raw_dir(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "raw"

    def metrics_path(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "metrics.json"

    def analysis_path(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "analysis.json"

    def report_path(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "report.html"

    def run_meta_path(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "run.json"

    def ensure_run(self, run_id: str) -> None:
        ensure_dir(self.raw_dir(run_id))

    def load_json(self, path: Path) -> Any:
        return read_json(path)

    def save_json(self, path: Path, data: Any) -> None:
        write_json(path, data)

    def save_text(self, path: Path, content: str) -> None:
        write_text(path, content)

    def set_baseline(self, run_id: str) -> None:
        payload = {"run_id": run_id, "set_at": utc_now()}
        self.save_json(self.baseline_path(), payload)

    def get_baseline(self) -> Optional[str]:
        path = self.baseline_path()
        if not path.exists():
            return None
        data = self.load_json(path)
        return data.get("run_id")

    def prune_runs(self, keep: Iterable[Optional[str]]) -> List[str]:
        """Delete every stored run not named in *keep*; return what was removed."""
        keep_ids = {run_id for run_id in keep if run_id}
        runs = self.runs_dir()
        if not runs.is_dir():
            return []
        removed: List[str] = []
        for path in sorted(runs.iterdir()):
            if path.is_dir() and path.name not in keep_ids:
                shutil.rmtree(path)
                removed.append(path.name)
        return removed

    def history_path(self) -> Path:
        return self.root / "history.json"

    def load_history(self) -> dict:
        path = self.history_path()
        if not path.exists():
            return {"runs": []}
        return self.load_json(path)

    def append_to_history(
        self,
        run_id: str,
        metrics: dict,
        run_meta: dict,
        max_runs: int,
    ) -> None:
        """Add the current run to history.json, trimming oldest entries beyond max_runs."""
        history = self.load_history()
        runs = history.get("runs") or []

        entry = {
            "run_id":     run_id,
            "started_at": run_meta.get("started_at", ""),
            "rps":        metrics.get("rps"),
            "avg_ms":     metrics.get("avg_ms"),
            "median_ms":  metrics.get("median_ms"),
            "p95_ms":     metrics.get("p95_ms"),
            "p99_ms":     metrics.get("p99_ms"),
            "max_ms":     metrics.get("max_ms"),
            "error_rate": metrics.get("error_rate"),
            "error_rate_4xx": metrics.get("error_rate_4xx"),
            "error_rate_5xx": metrics.get("error_rate_5xx"),
            "error_rate_503": metrics.get("error_rate_503"),
            "requests":   metrics.get("requests"),
            "failures":   metrics.get("failures"),
        }

        # Avoid duplicates if the same run is appended twice (e.g. retries)
        runs = [r for r in runs if r.get("run_id") != run_id]
        runs.append(entry)

        if max_runs > 0:
            runs = runs[-max_runs:]

        self.save_json(self.history_path(), {"runs": runs})
