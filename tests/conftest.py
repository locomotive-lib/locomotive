import pytest

from locomotive.analyzer import Rule


@pytest.fixture
def rule_relative_increase():
    return Rule(metric="p95_ms", mode="relative", direction="increase", warn=10.0, fail=25.0)


@pytest.fixture
def rule_absolute_increase():
    return Rule(metric="error_rate", mode="absolute", direction="increase", warn=1.0, fail=5.0)


@pytest.fixture
def sample_metrics():
    return {
        "rps": 150.0,
        "avg_ms": 120.5,
        "p95_ms": 250.0,
        "p99_ms": 400.0,
        "error_rate": 2.5,
        "requests": 10000,
        "failures": 250,
        "error_rate_4xx": 1.0,
        "error_rate_5xx": 1.5,
        "error_rate_503": 0.5,
    }


@pytest.fixture
def baseline_metrics():
    return {
        "rps": 160.0,
        "avg_ms": 110.0,
        "p95_ms": 220.0,
        "p99_ms": 350.0,
        "error_rate": 1.0,
        "requests": 9500,
        "failures": 95,
    }
