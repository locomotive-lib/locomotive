import pytest

from locomotive.launcher import (
    _apply_failure_rates,
    _extract_status_code,
    parse_locust_failures,
    parse_locust_stats,
    parse_locust_stats_history,
)


# ── _extract_status_code ──────────────────────────────────────────────


class TestExtractStatusCode:
    @pytest.mark.parametrize("text,expected", [
        ("HTTPError('503 Service Unavailable')", 503),
        ("HTTPError('404 Not Found')", 404),
        ("ConnectionError('Connection refused')", None),
        ("", None),
    ])
    def test_extract(self, text, expected):
        assert _extract_status_code(text) == expected


# ── parse_locust_stats ────────────────────────────────────────────────


class TestParseLocustStats:
    def test_standard_csv(self, tmp_path):
        csv_content = (
            "Type,Name,Request Count,Failure Count,Median Response Time,"
            "Average Response Time,Min Response Time,Max Response Time,"
            "Average Content Size,Requests/s,Failures/s,50%,66%,75%,80%,"
            "90%,95%,98%,99%,99.9%,99.99%,100%\n"
            'GET,/api/users,500,5,120,125.5,80,450,1024,50.0,0.5,'
            '120,130,140,150,170,200,250,300,400,440,450\n'
            ',"Aggregated",1000,10,115,120.0,75,500,2048,100.0,1.0,'
            '115,125,135,145,165,195,245,295,395,480,500\n'
        )
        csv_path = tmp_path / "locust_stats.csv"
        csv_path.write_text(csv_content)
        metrics = parse_locust_stats(csv_path)
        assert metrics["requests"] == 1000
        assert metrics["failures"] == 10
        assert metrics["avg_ms"] == pytest.approx(120.0)
        assert metrics["rps"] == pytest.approx(100.0)

    def test_fallback_to_first_row(self, tmp_path):
        csv_content = (
            "Type,Name,Request Count,Failure Count,Average Response Time,Requests/s,95%,99%\n"
            "GET,/health,200,0,50.0,20.0,80,100\n"
        )
        csv_path = tmp_path / "locust_stats.csv"
        csv_path.write_text(csv_content)
        metrics = parse_locust_stats(csv_path)
        assert metrics["requests"] == 200


# ── parse_locust_failures ─────────────────────────────────────────────


class TestParseLocustFailures:
    def test_status_code_counting(self, tmp_path):
        csv_content = (
            "Method,Name,Error,Occurrences\n"
            "GET,/api,\"HTTPError('503 Service Unavailable')\",10\n"
            "GET,/api,\"HTTPError('500 Internal Server Error')\",5\n"
            "GET,/api,\"HTTPError('404 Not Found')\",3\n"
            "POST,/api,\"ConnectionError('refused')\",2\n"
        )
        csv_path = tmp_path / "locust_failures.csv"
        csv_path.write_text(csv_content)
        result = parse_locust_failures(csv_path)
        assert result["failures_503"] == 10
        assert result["failures_5xx"] == 15  # 503 + 500
        assert result["failures_4xx"] == 3
        assert result["failures_other"] == 2

    def test_empty_csv(self, tmp_path):
        csv_path = tmp_path / "locust_failures.csv"
        csv_path.write_text("Method,Name,Error,Occurrences\n")
        result = parse_locust_failures(csv_path)
        assert result == {}


# ── parse_locust_stats_history ────────────────────────────────────────


class TestParseLocustStatsHistory:
    def test_basic_parsing(self, tmp_path):
        csv_content = (
            "Type,Name,User Count,Timestamp,Requests/s,Failures/s\n"
            ",Aggregated,10,1000,50.0,1.0\n"
            ",Aggregated,20,1001,100.0,2.0\n"
        )
        csv_path = tmp_path / "locust_stats_history.csv"
        csv_path.write_text(csv_content)
        history = parse_locust_stats_history(csv_path)
        assert len(history) == 2
        assert history[0]["rps"] == pytest.approx(50.0)
        assert history[1]["failures_s"] == pytest.approx(2.0)


# ── _apply_failure_rates ──────────────────────────────────────────────


class TestApplyFailureRates:
    def test_rates_calculated(self):
        metrics = {"requests": 1000, "failures": 20}
        breakdown = {"failures_4xx": 5, "failures_5xx": 15, "failures_503": 10}
        _apply_failure_rates(metrics, breakdown)
        assert metrics["error_rate_4xx"] == pytest.approx(0.5)
        assert metrics["error_rate_5xx"] == pytest.approx(1.5)
        assert metrics["error_rate_503"] == pytest.approx(1.0)
        assert metrics["failures_non_503"] == 10

    def test_zero_requests(self):
        metrics = {"requests": 0}
        breakdown = {"failures_4xx": 5}
        _apply_failure_rates(metrics, breakdown)
        assert "error_rate_4xx" not in metrics
