import pytest

from locomotive.cli import (
    _exit_code_for_status,
    _gate_status,
    _normalize_mode,
    _parse_int,
    _parse_list,
    _resolve_gate_config,
)


# ── _normalize_mode ───────────────────────────────────────────────────


class TestNormalizeMode:
    @pytest.mark.parametrize("value,expected", [
        ("resilience", "resilience"),
        ("acceptance", "resilience"),
        ("RESILIENCE", "resilience"),
        ("  acceptance  ", "resilience"),
        (None, ""),
        ("invalid", ""),
        ("", ""),
    ])
    def test_modes(self, value, expected):
        assert _normalize_mode(value) == expected


# ── _parse_list ───────────────────────────────────────────────────────


class TestParseList:
    def test_none(self):
        assert _parse_list(None) == []

    def test_list_input(self):
        assert _parse_list(["a", "b", ""]) == ["a", "b"]

    def test_comma_string(self):
        assert _parse_list("api, smoke, ") == ["api", "smoke"]

    def test_single_value(self):
        assert _parse_list(42) == ["42"]


# ── _parse_int ────────────────────────────────────────────────────────


class TestParseInt:
    def test_valid(self):
        assert _parse_int("10", "test") == 10

    def test_none(self):
        assert _parse_int(None, "test") is None

    def test_empty_string(self):
        assert _parse_int("", "test") is None

    def test_invalid(self):
        with pytest.raises(ValueError):
            _parse_int("abc", "test")


# ── _resolve_gate_config ─────────────────────────────────────────────


class TestResolveGateConfig:
    def test_mode_from_analysis(self):
        cfg = {"mode": "resilience", "gate": {"thresholds": {"error_rate": {"fail": 5}}}}
        mode, gate = _resolve_gate_config(cfg)
        assert mode == "resilience"

    def test_mode_from_gate(self):
        cfg = {"gate": {"mode": "acceptance", "thresholds": {"error_rate": {"fail": 0}}}}
        mode, gate = _resolve_gate_config(cfg)
        assert mode == "resilience"  # acceptance maps to resilience

    def test_auto_resilience_from_thresholds(self):
        cfg = {"gate": {"thresholds": {"error_rate": {"fail": 5}}}}
        mode, gate = _resolve_gate_config(cfg)
        assert mode == "resilience"

    def test_resilience_without_thresholds_cleared(self):
        cfg = {"mode": "resilience", "gate": {}}
        mode, gate = _resolve_gate_config(cfg)
        assert mode == ""

    def test_no_mode_no_thresholds(self):
        mode, gate = _resolve_gate_config({})
        assert mode == ""


# ── _gate_status ──────────────────────────────────────────────────────


class TestGateStatus:
    def test_all_pass(self):
        gate_eval = {"results": [{"status": "PASS"}, {"status": "PASS"}]}
        assert _gate_status(gate_eval) == "PASS"

    def test_contains_degradation(self):
        gate_eval = {"results": [{"status": "PASS"}, {"status": "DEGRADATION"}]}
        assert _gate_status(gate_eval) == "DEGRADATION"

    def test_contains_warning(self):
        gate_eval = {"results": [{"status": "PASS"}, {"status": "WARNING"}]}
        assert _gate_status(gate_eval) == "WARNING"

    def test_skip_ignored(self):
        gate_eval = {"results": [{"status": "SKIP"}, {"status": "SKIP"}]}
        assert _gate_status(gate_eval) == "PASS"

    def test_empty_results(self):
        assert _gate_status({"results": []}) == "PASS"
        assert _gate_status({}) == "PASS"


# ── _exit_code_for_status ─────────────────────────────────────────────


class TestExitCodeForStatus:
    @pytest.mark.parametrize("status,fail_on,expected", [
        ("DEGRADATION", "DEGRADATION", 1),
        ("WARNING", "DEGRADATION", 0),
        ("PASS", "DEGRADATION", 0),
        ("DEGRADATION", "WARNING", 1),
        ("WARNING", "WARNING", 1),
        ("PASS", "WARNING", 0),
    ])
    def test_exit_codes(self, status, fail_on, expected):
        assert _exit_code_for_status(status, fail_on) == expected
