import pytest

from locomotive.gate import (
    _evaluate_threshold,
    _parse_thresholds,
    evaluate_gate,
    summarize_history,
)


# ── _parse_thresholds ─────────────────────────────────────────────────


class TestParseThresholds:
    def test_non_dict(self):
        assert _parse_thresholds("bad") == {}
        assert _parse_thresholds(None) == {}

    def test_nested_dict_passthrough(self):
        raw = {"error_rate": {"warn": 1, "fail": 5}}
        result = _parse_thresholds(raw)
        assert result["error_rate"] == {"warn": 1, "fail": 5}

    def test_shorthand_scalar(self):
        raw = {"error_rate": 5}
        result = _parse_thresholds(raw)
        assert result["error_rate"] == {"fail": 5}

    def test_none_value_skipped(self):
        raw = {"error_rate": None, "rps": {"fail": 100}}
        result = _parse_thresholds(raw)
        assert "error_rate" not in result
        assert "rps" in result


# ── _evaluate_threshold ───────────────────────────────────────────────


class TestEvaluateThreshold:
    def test_increase_above_fail(self):
        result = _evaluate_threshold("error_rate", 6.0, {"fail": 5.0}, "resilience", True, None)
        assert result["status"] == "DEGRADATION"

    def test_increase_equals_fail_is_not_degradation(self):
        """Strict inequality: current == fail should NOT be DEGRADATION (uses >)."""
        result = _evaluate_threshold("error_rate", 5.0, {"fail": 5.0}, "resilience", True, None)
        assert result["status"] != "DEGRADATION"
        # In resilience mode, error_rate auto-gets warn=0, so 5.0 > 0 → WARNING
        assert result["status"] == "WARNING"

    def test_strict_inequality_non_error_metric(self):
        """For non-error metrics, current == fail should be PASS (no auto-warn)."""
        result = _evaluate_threshold("p95_ms", 500.0, {"fail": 500.0}, "resilience", True, None)
        assert result["status"] == "PASS"

    def test_increase_above_warn(self):
        result = _evaluate_threshold("error_rate", 2.0, {"warn": 1.0, "fail": 5.0}, "resilience", True, None)
        assert result["status"] == "WARNING"

    def test_increase_below_warn(self):
        result = _evaluate_threshold("error_rate", 0.5, {"warn": 1.0, "fail": 5.0}, "resilience", True, None)
        assert result["status"] == "PASS"

    def test_decrease_direction(self):
        result = _evaluate_threshold("rps", 40.0, {"fail": 50.0, "direction": "decrease"}, "resilience", True, None)
        assert result["status"] == "DEGRADATION"

    def test_not_eligible_skips(self):
        result = _evaluate_threshold("error_rate", 10.0, {"fail": 5.0}, "resilience", False, "min_requests not met")
        assert result["status"] == "SKIP"
        assert "min_requests" in result["reason"]

    def test_missing_current_value(self):
        result = _evaluate_threshold("error_rate", None, {"fail": 5.0}, "resilience", True, None)
        assert result["status"] == "SKIP"

    def test_missing_thresholds(self):
        result = _evaluate_threshold("error_rate", 3.0, {}, "resilience", True, None)
        assert result["status"] == "SKIP"

    def test_resilience_auto_warn_for_error_metrics(self):
        """In resilience mode, error metrics get warn=0 automatically when only fail is set."""
        result = _evaluate_threshold("error_rate", 0.5, {"fail": 5.0}, "resilience", True, None)
        assert result["status"] == "WARNING"  # 0.5 > 0 (auto warn)

    def test_no_auto_warn_for_non_error_metrics(self):
        result = _evaluate_threshold("p95_ms", 300, {"fail": 500}, "resilience", True, None)
        assert result["status"] == "PASS"  # no auto warn for p95


# ── summarize_history ─────────────────────────────────────────────────


class TestSummarizeHistory:
    def test_empty_history(self):
        assert summarize_history([], 10) is None

    def test_warmup_filters_early_rows(self):
        history = [
            {"timestamp": 0.0, "rps": 100.0, "failures_s": 50.0},  # warmup
            {"timestamp": 5.0, "rps": 100.0, "failures_s": 50.0},  # warmup
            {"timestamp": 10.0, "rps": 200.0, "failures_s": 2.0},  # counted
            {"timestamp": 15.0, "rps": 200.0, "failures_s": 2.0},  # counted
        ]
        result = summarize_history(history, 10)
        assert result["requests"] == pytest.approx(400.0)
        assert result["failures"] == pytest.approx(4.0)

    def test_zero_requests(self):
        history = [
            {"timestamp": 0.0, "rps": 0.0, "failures_s": 0.0},
        ]
        result = summarize_history(history, 0)
        assert result["error_rate"] is None


# ── evaluate_gate ─────────────────────────────────────────────────────


class TestEvaluateGate:
    def test_no_thresholds_returns_none(self):
        """Without thresholds, gate returns None regardless of mode."""
        result = evaluate_gate({"error_rate": 1.0, "requests": 1000}, {}, "resilience")
        assert result is None

    def test_resilience_no_thresholds_returns_none(self):
        result = evaluate_gate({"rps": 100}, {}, "resilience")
        assert result is None

    def test_resilience_with_thresholds(self):
        cfg = {"thresholds": {"error_rate": {"fail": 5}}}
        metrics = {"error_rate": 2.0, "requests": 1000}
        result = evaluate_gate(metrics, cfg, "resilience")
        assert result is not None

    def test_min_requests_not_met(self):
        cfg = {"thresholds": {"error_rate": {"fail": 5}}, "min_requests": 1000}
        metrics = {"error_rate": 10.0, "requests": 50}
        result = evaluate_gate(metrics, cfg, "resilience")
        statuses = [r["status"] for r in result["results"]]
        assert all(s == "SKIP" for s in statuses)

    def test_warmup_recalculates_metrics(self):
        cfg = {"thresholds": {"error_rate": {"fail": 5}}, "warmup_seconds": 10}
        metrics = {"error_rate": 50.0, "requests": 1000, "failures": 500}
        history = {"requests": 800.0, "failures": 8.0, "error_rate": 1.0}
        result = evaluate_gate(metrics, cfg, "resilience", history_summary=history)
        # After warmup recalculation, error_rate should be 1%, not 50%
        statuses = [r["status"] for r in result["results"]]
        assert "DEGRADATION" not in statuses

    def test_gate_metadata(self):
        cfg = {"thresholds": {"error_rate": {"fail": 5}}}
        metrics = {"error_rate": 1.0, "requests": 1000}
        result = evaluate_gate(metrics, cfg, "resilience")
        assert result["gate"]["mode"] == "resilience"
