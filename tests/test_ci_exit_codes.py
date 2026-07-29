"""End-to-end exit-code behaviour of `loco ci` and `loco analyze`.

These cover the family of bugs where a run that proved nothing came out green:
a gate that could not be evaluated, a run with zero requests, a rule whose
metric was never measured, and `ci` disagreeing with `analyze` on the same
artifacts.
"""

import argparse
import json

import pytest

from locomotive import cli


class FakeLauncher:
    """Stands in for `cli._run`: writes metrics.json and returns a code."""

    def __init__(self, metrics, returncode=0):
        self.metrics = metrics
        self.returncode = returncode

    def __call__(self, storage, run_id, locust_config):
        storage.ensure_run(run_id)
        if self.metrics is not None:
            storage.save_json(storage.metrics_path(run_id), self.metrics)
        return {"returncode": self.returncode, "run_id": run_id}


def make_args(**overrides):
    defaults = dict(
        storage=None, run_id="run-1", baseline=None, rules=None, fail_on=None,
        set_baseline=False, no_validate=True, title=None, output=None,
        locustfile="dummy.py", host=None, users=None, spawn_rate=None,
        run_time=None, tags=None, exclude_tags=None, stop_timeout=None,
        extra_arg=None, locust_cmd=None,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


@pytest.fixture
def base_config(tmp_path):
    return {
        "artifacts": {"storage": str(tmp_path / "artifacts")},
        "load": {"locustfile": "dummy.py", "users": 1, "spawn_rate": 1,
                 "run_time": "1s"},
        "report": {},
    }


def run_ci(monkeypatch, config, metrics, returncode=0, **args_kw):
    monkeypatch.setattr(cli, "_run", FakeLauncher(metrics, returncode))
    return cli.cmd_ci(make_args(**args_kw), config)


def load_analysis(config, run_id="run-1"):
    from locomotive.storage import Storage

    storage = Storage.from_root(config["artifacts"]["storage"])
    return json.loads(storage.analysis_path(run_id).read_text())


GOOD_METRICS = {"requests": 5000, "failures": 0, "error_rate": 0.0,
                "p95_ms": 120.0, "rps": 50.0}
EMPTY_METRICS = {"requests": 0, "failures": 0, "error_rate": 0.0,
                 "p95_ms": 0.0, "rps": 0.0}


# ── B4: a run with zero requests is not a passing run ─────────────────


class TestZeroRequestRun:
    def test_gate_thresholds_do_not_pass_on_zero(self, monkeypatch, base_config):
        base_config["analysis"] = {
            "mode": "resilience",
            "gate": {"thresholds": {"p95_ms": {"fail": 500}}},
        }
        code = run_ci(monkeypatch, base_config, EMPTY_METRICS)
        assert code == 1
        analysis = load_analysis(base_config)
        assert analysis["status"] == "NO_DATA"

    def test_no_gate_no_rules_still_caught(self, monkeypatch, base_config):
        # Nothing configured at all: the sanity check alone must fail the build.
        code = run_ci(monkeypatch, base_config, EMPTY_METRICS)
        assert code == 1

    def test_healthy_run_passes(self, monkeypatch, base_config):
        base_config["analysis"] = {
            "mode": "resilience",
            "gate": {"thresholds": {"p95_ms": {"fail": 500}}},
        }
        assert run_ci(monkeypatch, base_config, GOOD_METRICS) == 0

    def test_allow_no_data_opts_out(self, monkeypatch, base_config):
        base_config["analysis"] = {
            "allow_no_data": True,
            "mode": "resilience",
            "gate": {"thresholds": {"p95_ms": {"fail": 500}}},
        }
        assert run_ci(monkeypatch, base_config, EMPTY_METRICS) == 0
        analysis = load_analysis(base_config)
        assert analysis["status"] == "PASS"
        assert all(r["status"] != "NO_DATA" for r in analysis["results"])


# ── B3: min_requests unmet must not read as PASS ──────────────────────


class TestMinRequests:
    def test_unmet_min_requests_fails(self, monkeypatch, base_config):
        base_config["analysis"] = {
            "mode": "resilience",
            "gate": {"min_requests": 1000, "thresholds": {"error_rate": {"fail": 5}}},
        }
        metrics = dict(GOOD_METRICS, requests=50, failures=50, error_rate=100.0)
        code = run_ci(monkeypatch, base_config, metrics, returncode=1)
        assert code == 1
        analysis = load_analysis(base_config)
        assert analysis["status"] == "NO_DATA"
        assert "min_requests" in analysis["results"][0]["reason"]

    def test_met_min_requests_evaluates_normally(self, monkeypatch, base_config):
        base_config["analysis"] = {
            "mode": "resilience",
            "gate": {"min_requests": 1000, "thresholds": {"error_rate": {"fail": 5}}},
        }
        assert run_ci(monkeypatch, base_config, GOOD_METRICS) == 0


# ── B7: ci and analyze agree; locust's own exit code is not lost ──────


class TestCiMatchesAnalyze:
    REGRESSION_CONFIG = {
        "rules": [
            {"metric": "p95_ms", "mode": "relative", "direction": "increase",
             "warn": 10, "fail": 25},
        ],
    }

    def _with_baseline(self, config, monkeypatch, baseline_metrics):
        from locomotive.storage import Storage

        storage = Storage.from_root(config["artifacts"]["storage"])
        storage.ensure_run("base")
        storage.save_json(storage.metrics_path("base"), baseline_metrics)
        storage.set_baseline("base")

    def test_regression_fails_ci_too(self, monkeypatch, base_config):
        base_config["analysis"] = dict(self.REGRESSION_CONFIG)
        self._with_baseline(base_config, monkeypatch, GOOD_METRICS)
        slow = dict(GOOD_METRICS, p95_ms=400.0)
        assert run_ci(monkeypatch, base_config, slow) == 1
        # analyze on the same artifacts must give the same answer
        assert cli.cmd_analyze(make_args(), base_config) == 1

    def test_regression_and_gate_together(self, monkeypatch, base_config):
        base_config["analysis"] = dict(
            self.REGRESSION_CONFIG,
            mode="resilience",
            gate={"thresholds": {"error_rate": {"fail": 5}}},
        )
        self._with_baseline(base_config, monkeypatch, GOOD_METRICS)
        slow = dict(GOOD_METRICS, p95_ms=400.0)
        # gate is green, rules are red — default is that the build fails
        assert run_ci(monkeypatch, base_config, slow) == 1
        assert cli.cmd_analyze(make_args(), base_config) == 1

    def test_rules_advisory_restores_gate_only(self, monkeypatch, base_config):
        base_config["analysis"] = dict(
            self.REGRESSION_CONFIG,
            rules_advisory=True,
            mode="resilience",
            gate={"thresholds": {"error_rate": {"fail": 5}}},
        )
        self._with_baseline(base_config, monkeypatch, GOOD_METRICS)
        slow = dict(GOOD_METRICS, p95_ms=400.0)
        assert run_ci(monkeypatch, base_config, slow) == 0

    def test_locust_exit_code_not_swallowed(self, monkeypatch, base_config):
        base_config["analysis"] = {
            "mode": "resilience",
            "gate": {"thresholds": {"p95_ms": {"fail": 5000}}},
        }
        # Analysis is clean but locust itself exited non-zero.
        assert run_ci(monkeypatch, base_config, GOOD_METRICS, returncode=1) == 1

    def test_clean_run_returns_zero(self, monkeypatch, base_config):
        base_config["analysis"] = {
            "mode": "resilience",
            "gate": {"thresholds": {"p95_ms": {"fail": 5000}}},
        }
        assert run_ci(monkeypatch, base_config, GOOD_METRICS, returncode=0) == 0


# ── baseline promotion ────────────────────────────────────────────────


class TestSetBaseline:
    def test_no_data_run_is_not_promoted(self, monkeypatch, base_config):
        from locomotive.storage import Storage

        base_config["analysis"] = {
            "mode": "resilience",
            "gate": {"thresholds": {"p95_ms": {"fail": 500}}},
        }
        run_ci(monkeypatch, base_config, EMPTY_METRICS, set_baseline=True)
        storage = Storage.from_root(base_config["artifacts"]["storage"])
        assert storage.get_baseline() != "run-1"

    def test_healthy_run_is_promoted(self, monkeypatch, base_config):
        from locomotive.storage import Storage

        base_config["analysis"] = {
            "mode": "resilience",
            "gate": {"thresholds": {"p95_ms": {"fail": 500}}},
        }
        run_ci(monkeypatch, base_config, GOOD_METRICS, set_baseline=True)
        storage = Storage.from_root(base_config["artifacts"]["storage"])
        assert storage.get_baseline() == "run-1"


# ── fail_on parsing ───────────────────────────────────────────────────


class TestFailOn:
    def test_lowercase_config_value_still_applies(self, monkeypatch, base_config):
        base_config["analysis"] = {
            "fail_on": "warning",
            "mode": "resilience",
            "gate": {"thresholds": {"p95_ms": {"warn": 100, "fail": 5000}}},
        }
        assert run_ci(monkeypatch, base_config, GOOD_METRICS) == 1

    def test_unknown_value_falls_back_and_warns(self, capsys):
        assert cli._resolve_fail_on(None, {"fail_on": "nonsense"}) == "DEGRADATION"
        assert "nonsense" in capsys.readouterr().out

    def test_cli_flag_wins(self):
        assert cli._resolve_fail_on("warning", {"fail_on": "DEGRADATION"}) == "WARNING"

    def test_no_data_ignores_fail_on(self):
        # Nothing was measured, so nothing was proven, whatever fail_on says.
        assert cli._exit_code_for_status("NO_DATA", "DEGRADATION") == 1
        assert cli._exit_code_for_status("NO_DATA", "WARNING") == 1
