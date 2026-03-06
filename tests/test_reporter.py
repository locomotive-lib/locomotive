import pytest

from locomotive.reporter import (
    _delta_class,
    _format_delta,
    _format_duration,
    _format_value,
    _status_class,
)


# ── _format_value ─────────────────────────────────────────────────────


class TestFormatValue:
    def test_none(self):
        assert _format_value(None) == "-"

    def test_float(self):
        assert _format_value(3.14159, 2) == "3.14"

    def test_int_as_str(self):
        assert _format_value(42) == "42"


# ── _format_delta ─────────────────────────────────────────────────────


class TestFormatDelta:
    def test_none(self):
        assert _format_delta(None) == "-"

    def test_positive(self):
        result = _format_delta(12.5)
        assert "12.5%" in result
        assert "\u2191" in result  # up arrow

    def test_negative(self):
        result = _format_delta(-8.3)
        assert "8.3%" in result
        assert "\u2193" in result  # down arrow

    def test_zero(self):
        result = _format_delta(0)
        assert "\u2192" in result  # right arrow

    def test_non_numeric(self):
        assert _format_delta("bad") == "-"


# ── _format_duration ──────────────────────────────────────────────────


class TestFormatDuration:
    def test_seconds(self):
        assert _format_duration(45) == "45s"

    def test_minutes(self):
        assert _format_duration(90) == "1m 30s"

    def test_exact_minute(self):
        assert _format_duration(60) == "1m 0s"


# ── _status_class ─────────────────────────────────────────────────────


class TestStatusClass:
    @pytest.mark.parametrize("status,expected", [
        ("PASS", "status-pass"),
        ("WARNING", "status-warning"),
        ("DEGRADATION", "status-fail"),
        ("SKIP", "status-skip"),
        ("UNKNOWN", "status-unknown"),
    ])
    def test_mapping(self, status, expected):
        assert _status_class(status) == expected


# ── _delta_class ──────────────────────────────────────────────────────


class TestDeltaClass:
    def test_none(self):
        assert _delta_class(None, "p95_ms") == ""

    def test_rps_positive_is_good(self):
        assert _delta_class(10, "rps") == "delta-good"

    def test_rps_negative_is_bad(self):
        assert _delta_class(-10, "rps") == "delta-bad"

    def test_latency_positive_is_bad(self):
        assert _delta_class(10, "p95_ms") == "delta-bad"

    def test_latency_negative_is_good(self):
        assert _delta_class(-10, "p95_ms") == "delta-good"

    def test_zero(self):
        assert _delta_class(0, "p95_ms") == ""
