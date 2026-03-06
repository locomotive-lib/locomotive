import pytest

from locomotive.analyzer import (
    Rule,
    _relative_change,
    evaluate_rule,
    analyze,
    load_rules,
    merge_results,
)


# ── _relative_change ──────────────────────────────────────────────────


class TestRelativeChange:
    def test_increase_current_higher(self):
        delta, magnitude = _relative_change(220, 200, "increase")
        assert delta == pytest.approx(10.0)
        assert magnitude == pytest.approx(10.0)

    def test_increase_current_lower(self):
        delta, magnitude = _relative_change(180, 200, "increase")
        assert delta == pytest.approx(-10.0)
        assert magnitude == 0.0  # clamped

    def test_decrease_current_lower(self):
        delta, magnitude = _relative_change(160, 200, "decrease")
        assert delta == pytest.approx(-20.0)
        assert magnitude == pytest.approx(20.0)

    def test_decrease_current_higher(self):
        delta, magnitude = _relative_change(220, 200, "decrease")
        assert delta == pytest.approx(10.0)
        assert magnitude == 0.0

    def test_baseline_zero(self):
        delta, magnitude = _relative_change(100, 0, "increase")
        assert delta is None
        assert magnitude is None


# ── load_rules ─────────────────────────────────────────────────────────


class TestLoadRules:
    def test_none_input(self):
        assert load_rules(None) == []

    def test_empty_dict(self):
        assert load_rules({}) == []

    def test_rules_not_list(self):
        assert load_rules({"rules": "bad"}) == []

    def test_valid_rules(self):
        data = {
            "rules": [
                {"metric": "p95_ms", "mode": "relative", "direction": "increase", "warn": 10, "fail": 25},
            ]
        }
        rules = load_rules(data)
        assert len(rules) == 1
        assert rules[0].metric == "p95_ms"
        assert rules[0].warn == 10.0

    def test_non_dict_items_skipped(self):
        data = {"rules": ["bad", None, 42]}
        assert load_rules(data) == []


# ── evaluate_rule ──────────────────────────────────────────────────────


class TestEvaluateRule:
    # Relative mode
    def test_relative_pass(self, rule_relative_increase, baseline_metrics):
        current = {**baseline_metrics, "p95_ms": 225.0}  # +2.3%
        result = evaluate_rule(rule_relative_increase, current, baseline_metrics)
        assert result["status"] == "PASS"

    def test_relative_warning(self, rule_relative_increase, baseline_metrics):
        current = {**baseline_metrics, "p95_ms": 250.0}  # +13.6%
        result = evaluate_rule(rule_relative_increase, current, baseline_metrics)
        assert result["status"] == "WARNING"

    def test_relative_degradation(self, rule_relative_increase, baseline_metrics):
        current = {**baseline_metrics, "p95_ms": 300.0}  # +36.4%
        result = evaluate_rule(rule_relative_increase, current, baseline_metrics)
        assert result["status"] == "DEGRADATION"

    def test_relative_skip_missing_current(self, rule_relative_increase, baseline_metrics):
        current = {"rps": 100}  # no p95_ms
        result = evaluate_rule(rule_relative_increase, current, baseline_metrics)
        assert result["status"] == "SKIP"

    def test_relative_skip_missing_baseline(self, rule_relative_increase):
        current = {"p95_ms": 250}
        result = evaluate_rule(rule_relative_increase, current, {})
        assert result["status"] == "SKIP"

    def test_relative_skip_baseline_zero(self, rule_relative_increase):
        current = {"p95_ms": 250}
        baseline = {"p95_ms": 0}
        result = evaluate_rule(rule_relative_increase, current, baseline)
        assert result["status"] == "SKIP"

    # Absolute mode
    def test_absolute_pass(self, rule_absolute_increase):
        result = evaluate_rule(rule_absolute_increase, {"error_rate": 0.5}, {})
        assert result["status"] == "PASS"

    def test_absolute_warning(self, rule_absolute_increase):
        result = evaluate_rule(rule_absolute_increase, {"error_rate": 2.0}, {})
        assert result["status"] == "WARNING"

    def test_absolute_degradation(self, rule_absolute_increase):
        result = evaluate_rule(rule_absolute_increase, {"error_rate": 6.0}, {})
        assert result["status"] == "DEGRADATION"

    def test_absolute_decrease_direction(self):
        rule = Rule(metric="rps", mode="absolute", direction="decrease", warn=100.0, fail=50.0)
        result = evaluate_rule(rule, {"rps": 40.0}, {})
        assert result["status"] == "DEGRADATION"

    def test_absolute_decrease_pass(self):
        rule = Rule(metric="rps", mode="absolute", direction="decrease", warn=100.0, fail=50.0)
        result = evaluate_rule(rule, {"rps": 150.0}, {})
        assert result["status"] == "PASS"

    # Unknown mode
    def test_unknown_mode(self):
        rule = Rule(metric="rps", mode="unknown", direction="increase", warn=10, fail=20)
        result = evaluate_rule(rule, {"rps": 100}, {"rps": 80})
        assert result["status"] == "SKIP"
        assert "unsupported" in result["reason"]


# ── analyze ────────────────────────────────────────────────────────────


class TestAnalyze:
    def test_empty_rules(self):
        result = analyze({}, {}, [])
        assert result["status"] == "PASS"
        assert result["results"] == []

    def test_worst_status_wins(self):
        rules = [
            Rule("rps", "absolute", "decrease", 100, 50),
            Rule("error_rate", "absolute", "increase", 1, 5),
        ]
        current = {"rps": 200.0, "error_rate": 6.0}
        result = analyze(current, {}, rules)
        assert result["status"] == "DEGRADATION"

    def test_summary_counts(self):
        rules = [
            Rule("rps", "absolute", "decrease", 100, 50),
            Rule("error_rate", "absolute", "increase", 1, 5),
            Rule("missing", "absolute", "increase", 1, 5),
        ]
        current = {"rps": 200.0, "error_rate": 2.0}
        result = analyze(current, {}, rules)
        assert result["summary"]["PASS"] == 1
        assert result["summary"]["WARNING"] == 1
        assert result["summary"]["SKIP"] == 1

    def test_skip_does_not_escalate(self):
        rules = [Rule("missing", "relative", "increase", 10, 20)]
        result = analyze({}, {}, rules)
        assert result["status"] == "PASS"


# ── merge_results ──────────────────────────────────────────────────────


class TestMergeResults:
    def test_empty_input(self):
        result = merge_results([])
        assert result["status"] == "PASS"
        assert result["results"] == []

    def test_merges_multiple_sets(self):
        set1 = [{"status": "PASS", "metric": "rps"}]
        set2 = [{"status": "WARNING", "metric": "p95"}]
        result = merge_results([set1, set2])
        assert result["status"] == "WARNING"
        assert len(result["results"]) == 2

    def test_worst_propagated(self):
        set1 = [{"status": "PASS"}]
        set2 = [{"status": "DEGRADATION"}]
        result = merge_results([set1, set2])
        assert result["status"] == "DEGRADATION"
