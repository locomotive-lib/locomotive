"""Markdown summaries and JUnit XML rendered from analysis.json."""

import xml.etree.ElementTree as ET

from locomotive.export import (
    describe_result,
    load_endpoint_rows,
    render_junit,
    render_markdown_summary,
)

METRICS = {"rps": 48.2, "avg_ms": 40.0, "p95_ms": 400.0, "p99_ms": 510.0,
           "error_rate": 0.5, "requests": 4820}
BASELINE = {"rps": 50.0, "avg_ms": 20.0, "p95_ms": 100.0, "p99_ms": 150.0,
            "error_rate": 0.0, "requests": 5000}


def result(metric, status, mode="relative", **overrides):
    base = {"metric": metric, "mode": mode, "direction": "increase",
            "warn": 10.0, "fail": 25.0, "current": None, "baseline": None,
            "delta_percent": None, "status": status, "reason": None}
    base.update(overrides)
    return base


def analysis(*results, status="DEGRADATION"):
    return {"status": status, "results": list(results)}


MIXED = analysis(
    result("gate.error_rate", "PASS", mode="gate", warn=0.0, fail=5.0, current=0.5),
    result("rps", "WARNING", direction="decrease", current=45.0, baseline=50.0, delta_percent=-10.0),
    result("p95_ms", "DEGRADATION", current=400.0, baseline=100.0, delta_percent=300.0),
)


# ── describe_result ───────────────────────────────────────────────────


class TestDescribeResult:
    def test_relative_rule(self):
        res = result("p95_ms", "DEGRADATION", current=400.0, baseline=100.0, delta_percent=300.0)
        assert describe_result(res) == "400 vs 100 (+300.0%); warn at +10%, fail at +25%"

    def test_relative_decrease(self):
        res = result("rps", "WARNING", direction="decrease", current=45.5, baseline=50.0,
                     delta_percent=-9.0)
        assert describe_result(res) == "45.50 vs 50 (-9.0%); warn at -10%, fail at -25%"

    def test_absolute_rule(self):
        res = result("error_rate", "PASS", mode="absolute", warn=1.0, fail=5.0, current=0.25)
        assert describe_result(res) == "0.25; warn at >= 1, fail at >= 5"

    def test_gate_threshold(self):
        res = result("gate.p95_ms", "DEGRADATION", mode="gate", warn=None, fail=500.0, current=612.0)
        assert describe_result(res) == "612; fail if > 500"

    def test_reason_is_kept(self):
        res = result("requests", "NO_DATA", mode="sanity", warn=None, fail=None,
                     reason="run recorded 0 requests")
        assert describe_result(res) == "run recorded 0 requests"


# ── markdown ──────────────────────────────────────────────────────────


def summary(**overrides):
    kwargs = dict(run_id="abc-102", metrics=METRICS, analysis=MIXED,
                  baseline_id="abc-101", baseline_metrics=BASELINE)
    kwargs.update(overrides)
    return render_markdown_summary(**kwargs)


class TestMarkdownSummary:
    def test_heading_carries_the_status(self):
        assert summary().splitlines()[0] == "### ❌ Load test: DEGRADATION"

    def test_metrics_are_compared_with_the_baseline(self):
        text = summary()
        assert "Run `abc-102` vs baseline `abc-101`" in text
        assert "| p95, ms | 100 | 400 | +300.0% |" in text
        assert "| Throughput, req/s | 50 | 48.20 | -3.6% |" in text

    def test_worst_checks_come_first(self):
        text = summary()
        assert text.index("p95_ms (relative)") < text.index("rps (relative)") < text.index("error_rate (gate)")

    def test_what_tripped_comes_before_the_numbers(self):
        text = summary()
        line = next(l for l in text.splitlines() if l.startswith("**What tripped:**"))
        assert line.startswith("**What tripped:** p95_ms (relative): 400 vs 100 (+300.0%)")
        assert "rps (relative)" in line
        assert "error_rate" not in line
        assert text.index("**What tripped:**") < text.index("| Metric |")

    def test_what_tripped_is_capped(self):
        many = analysis(*[result(f"m{i}", "DEGRADATION", mode="absolute", current=1.0) for i in range(5)])
        assert "; and 2 more" in summary(analysis=many)

    def test_a_run_that_compared_nothing_says_so(self):
        text = summary(baseline_id=None, baseline_metrics=None, analysis=analysis(status="PASS"))
        assert text.splitlines()[0] == "### ✅ Load test: PASS, nothing to compare against"
        assert "| Metric | Current |" in text
        assert "**No baseline was used**" in text

    def test_no_baseline_is_fine_when_only_thresholds_exist(self):
        gate_only = analysis(result("gate.p95_ms", "PASS", mode="gate", fail=500.0, current=12.0),
                             status="PASS")
        text = summary(baseline_id=None, baseline_metrics=None, analysis=gate_only,
                       rules_configured=False)
        assert text.splitlines()[0] == "### ✅ Load test: PASS"
        assert "No baseline was used" not in text

    def test_no_metrics(self):
        text = summary(metrics={}, analysis=None)
        assert "NO_DATA" in text.splitlines()[0]
        assert "produced no metrics" in text

    def test_nothing_configured(self):
        text = summary(analysis=None, baseline_id=None, baseline_metrics=None, rules_configured=False)
        assert text.splitlines()[0] == "### ✅ Load test: PASS"
        assert "nothing was checked" in text

    def test_ci_context(self):
        run_meta = {"meta": {"ci": {
            "provider": "gitlab", "branch": "feature/x", "target_branch": "main",
            "commit": "0123456789abcdef", "build_url": "https://gitlab.example/g/app/-/jobs/5",
        }}}
        text = summary(run_meta=run_meta)
        assert "`feature/x` → `main`" in text
        assert "`0123456789ab`" in text
        assert "[Build](https://gitlab.example/g/app/-/jobs/5)" in text

    def test_non_http_links_are_dropped(self):
        run_meta = {"meta": {"ci": {"provider": "jenkins", "build_url": "javascript:alert(1)"}}}
        assert "javascript" not in summary(run_meta=run_meta)

    def test_a_pipe_cannot_break_the_table(self):
        odd = analysis(result("a|b", "PASS", mode="absolute", current=1.0))
        assert "a\\|b (absolute)" in summary(analysis=odd)


STATS_HEADER = "Type,Name,Request Count,Failure Count,Average Response Time,Requests/s,95%,99%\n"


class TestEndpoints:
    def write_stats(self, tmp_path, rows):
        path = tmp_path / "locust_stats.csv"
        path.write_text(STATS_HEADER + "".join(rows), encoding="utf-8")
        return path

    def test_rows_with_the_total_last(self, tmp_path):
        path = self.write_stats(tmp_path, [
            ",Aggregated,30,1,10.25,7.5,40,60\n",
            "GET,/ping,20,0,3.5,5.0,6,9\n",
            'POST,"/login|x",10,1,24,2.5,40,60\n',
        ])
        rows = load_endpoint_rows(path)
        assert [r["Name"] for r in rows] == ["/ping", "/login|x", "Aggregated"]

        text = summary(endpoints=rows)
        assert "<details><summary>Endpoints</summary>" in text
        assert "| GET /ping | 20 | 0 | 3.50 | 6 | 9 | 5 |" in text
        assert "| POST /login\\|x | 10 | 1 | 24 | 40 | 60 | 2.50 |" in text
        table = text[text.index("<details>"):]
        assert table.index("/login") < table.index("**Total**")

    def test_a_long_list_is_cut_but_keeps_the_total(self, tmp_path):
        rows = [f"GET,/e{i},1,0,1,1,1,1\n" for i in range(60)] + [",Aggregated,60,0,1,60,1,1\n"]
        text = summary(endpoints=load_endpoint_rows(self.write_stats(tmp_path, rows)))
        assert "| …and 11 more |" in text
        assert "**Total**" in text

    def test_missing_file(self, tmp_path):
        assert load_endpoint_rows(tmp_path / "nope.csv") == []


# ── JUnit ─────────────────────────────────────────────────────────────


def junit(**overrides):
    kwargs = dict(run_id="abc-102", metrics=METRICS, analysis=MIXED)
    kwargs.update(overrides)
    return ET.fromstring(render_junit(**kwargs))


def cases(root):
    return {(tc.get("classname"), tc.get("name")): tc for tc in root.iter("testcase")}


class TestJunit:
    def test_counts(self):
        root = junit()
        suite = root.find("testsuite")
        assert (suite.get("tests"), suite.get("failures"), suite.get("skipped")) == ("3", "1", "0")
        assert root.get("failures") == "1"

    def test_degradation_is_a_failure(self):
        tc = cases(junit())[("locomotive.regression", "p95_ms (relative)")]
        failure = tc.find("failure")
        assert failure.get("type") == "DEGRADATION"
        assert "+300.0%" in failure.get("message")

    def test_warning_passes_by_default(self):
        tc = cases(junit())[("locomotive.regression", "rps (relative)")]
        assert tc.find("failure") is None
        assert tc.find("system-out").text.startswith("WARNING:")

    def test_warning_fails_under_fail_on_warning(self):
        root = junit(fail_on="WARNING")
        assert root.find("testsuite").get("failures") == "2"

    def test_skip_is_skipped(self):
        root = junit(analysis=analysis(result("p95_ms", "SKIP", reason="missing baseline value")))
        tc = cases(root)[("locomotive.regression", "p95_ms (relative)")]
        assert "missing baseline value" in tc.find("skipped").get("message")

    def test_no_metrics_is_one_failure(self):
        root = junit(metrics=None, analysis=None)
        tc = cases(root)[("locomotive.run", "metrics")]
        assert tc.find("failure").get("type") == "NO_DATA"

    def test_nothing_configured_is_not_an_empty_report(self):
        # The Jenkins junit step fails on a file with no test cases in it.
        root = junit(analysis=None)
        assert root.find("testsuite").get("tests") == "1"
        assert cases(root)[("locomotive.run", "checks")].find("skipped") is not None

    def test_markup_in_a_reason_is_escaped(self):
        odd = analysis(result("requests", "NO_DATA", mode="sanity", reason="<b>&</b>"))
        tc = cases(junit(analysis=odd))[("locomotive.sanity", "requests")]
        assert tc.find("failure").text == "<b>&</b>"
