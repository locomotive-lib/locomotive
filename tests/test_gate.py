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

    def test_not_eligible_is_no_data(self):
        result = _evaluate_threshold("error_rate", 10.0, {"fail": 5.0}, "resilience", False, "min_requests not met")
        assert result["status"] == "NO_DATA"
        assert "min_requests" in result["reason"]

    def test_missing_current_value(self):
        result = _evaluate_threshold("error_rate", None, {"fail": 5.0}, "resilience", True, None)
        assert result["status"] == "NO_DATA"

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
        # Two rows, five seconds each: 200 rps * 5s, twice.
        assert result["requests"] == pytest.approx(2000.0)
        assert result["failures"] == pytest.approx(20.0)

    def test_zero_requests(self):
        history = [
            {"timestamp": 0.0, "rps": 0.0, "failures_s": 0.0},
        ]
        result = summarize_history(history, 0)
        assert result["error_rate"] is None


class TestHistoryIntervals:
    """``rps`` is a rate; a row is only worth as many requests as it is long."""

    def test_one_second_rows_are_unchanged(self):
        history = [{"timestamp": float(i), "rps": 10.0, "failures_s": 1.0} for i in range(5)]
        result = summarize_history(history, 0)
        assert result["requests"] == pytest.approx(50.0)
        assert result["failures"] == pytest.approx(5.0)

    def test_ten_second_rows_count_ten_seconds_each(self):
        history = [{"timestamp": float(i * 10), "rps": 10.0, "failures_s": 0.0} for i in range(5)]
        result = summarize_history(history, 0)
        # The same run, recorded at a coarser interval, made the same requests.
        assert result["requests"] == pytest.approx(500.0)

    def test_error_rate_is_unaffected_by_the_interval(self):
        fine = [{"timestamp": float(i), "rps": 10.0, "failures_s": 1.0} for i in range(10)]
        coarse = [{"timestamp": float(i * 5), "rps": 10.0, "failures_s": 1.0} for i in range(10)]
        assert summarize_history(fine, 0)["error_rate"] == pytest.approx(
            summarize_history(coarse, 0)["error_rate"]
        )

    def test_missing_timestamps_fall_back_to_one_row_per_second(self):
        history = [{"rps": 10.0, "failures_s": 0.0} for _ in range(4)]
        result = summarize_history(history, 0)
        assert result["requests"] == pytest.approx(40.0)

    def test_a_single_row_covers_one_second(self):
        result = summarize_history([{"timestamp": 1000.0, "rps": 7.0, "failures_s": 0.0}], 0)
        assert result["requests"] == pytest.approx(7.0)

    def test_a_hole_in_the_history_does_not_invent_requests(self):
        history = [
            {"timestamp": 0.0, "rps": 10.0, "failures_s": 0.0},
            {"timestamp": 1.0, "rps": 10.0, "failures_s": 0.0},
            {"timestamp": 2.0, "rps": 10.0, "failures_s": 0.0},
            # locust went quiet for five minutes; the next sample describes
            # its own second, not the silence before it.
            {"timestamp": 302.0, "rps": 10.0, "failures_s": 0.0},
        ]
        result = summarize_history(history, 0)
        assert result["requests"] == pytest.approx(40.0)

    def test_duplicate_timestamps_do_not_zero_a_row(self):
        history = [
            {"timestamp": 0.0, "rps": 10.0, "failures_s": 0.0},
            {"timestamp": 0.0, "rps": 10.0, "failures_s": 0.0},
            {"timestamp": 1.0, "rps": 10.0, "failures_s": 0.0},
        ]
        result = summarize_history(history, 0)
        assert result["requests"] == pytest.approx(30.0)

    def test_string_timestamps_are_read_as_numbers(self):
        history = [{"timestamp": str(i * 5), "rps": 10.0, "failures_s": 0.0} for i in range(3)]
        result = summarize_history(history, 5)
        # The first row is warmup; the other two are five seconds each.
        assert result["requests"] == pytest.approx(100.0)

    def test_min_requests_gate_sees_the_real_count(self):
        history = [{"timestamp": float(i * 5), "rps": 20.0, "failures_s": 0.0} for i in range(12)]
        summary = summarize_history(history, 10)
        result = evaluate_gate(
            {"error_rate": 0.0, "requests": 1200},
            {"thresholds": {"error_rate": {"fail": 5}}, "min_requests": 500, "warmup_seconds": 10},
            "resilience",
            summary,
        )
        # Ten counted rows at 20 rps over five seconds each is 1000 requests —
        # the old per-row sum said 200 and the gate reported NO_DATA.
        assert result["gate"]["requests_used"] == 1000
        assert result["results"][0]["status"] == "PASS"


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
        # A gate that could not be evaluated is not a gate that passed.
        assert all(s == "NO_DATA" for s in statuses)
        assert result["status"] == "NO_DATA"

    def test_zero_requests_is_no_data(self):
        cfg = {"thresholds": {"p95": {"fail": 500}}}
        metrics = {"p95": 0.0, "requests": 0}
        result = evaluate_gate(metrics, cfg, "resilience")
        assert result["status"] == "NO_DATA"
        assert "0 requests" in result["results"][0]["reason"]

    def test_min_requests_defaults_to_one(self):
        cfg = {"thresholds": {"p95": {"fail": 500}}}
        metrics = {"p95": 120.0, "requests": 1}
        result = evaluate_gate(metrics, cfg, "resilience")
        assert result["gate"]["min_requests"] is None
        assert result["gate"]["min_requests_effective"] == 1
        assert result["status"] == "PASS"

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
