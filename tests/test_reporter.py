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
        ("NO_DATA", "status-nodata"),
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


# ── NO_DATA rendering ─────────────────────────────────────────────────


class TestNoDataRendering:
    def _analysis(self):
        return {
            "status": "NO_DATA",
            "summary": {"PASS": 0, "WARNING": 0, "NO_DATA": 1,
                        "DEGRADATION": 0, "SKIP": 0},
            "results": [{
                "metric": "gate.p95_ms", "mode": "gate", "direction": "increase",
                "warn": None, "fail": None, "current": None, "baseline": None,
                "delta_percent": None, "status": "NO_DATA",
                "reason": "run recorded 0 requests",
            }],
        }

    def test_header_badge_and_summary(self):
        from locomotive.reporter import render_report

        html = render_report({"run_id": "r1"}, {"requests": 0}, None,
                             self._analysis(), "T")
        assert "status-badge-large nodata" in html
        assert ">NO_DATA<" in html
        assert "1 NO DATA" in html


# ── injection through config and metrics ──────────────────────────────
#
# The report is an HTML file assembled from strings, most of which the user
# wrote and some of which the *target* wrote (endpoint names come out of the
# URLs locust hit). Nothing here is a privilege boundary in the usual sense —
# it is the user's own config — but a report that mangles itself over a `<`
# in a chart title is a broken report, and one that ships to a team channel
# with someone else's markup in it is worse.


def _render(**kwargs):
    from locomotive.reporter import render_report
    from locomotive.report_config import resolve_report_config

    report_cfg = kwargs.pop("report_config", None)
    if report_cfg is not None:
        report_cfg = resolve_report_config(report_cfg)
    return render_report(
        kwargs.pop("run_meta", {"run_id": "r1"}),
        kwargs.pop("metrics", {"requests": 100, "p95_ms": 12.0}),
        kwargs.pop("baseline", None),
        kwargs.pop("analysis", None),
        kwargs.pop("title", "T"),
        report_config=report_cfg,
        **kwargs,
    )


BREAKOUT = "</style><script>alert(1)</script>"


class TestStyleBlockIsNotEscapable:
    def _style(self, doc):
        start = doc.index("<style>")
        return doc[start:doc.index("</style>", start)]

    def test_a_colour_cannot_close_the_style_element(self):
        doc = _render(report_config={"theme": {"colors": {"primary": f"red; }} {BREAKOUT}"}}})
        assert "<script>alert(1)</script>" not in doc
        assert doc.count("</style>") == 1

    def test_a_colour_name_cannot_close_the_style_element(self):
        doc = _render(report_config={"theme": {"colors": {f"primary{BREAKOUT}": "red"}}})
        assert "<script>alert(1)</script>" not in doc
        assert doc.count("</style>") == 1

    def test_a_brand_colour_cannot_close_the_style_element(self):
        doc = _render(report_config={"branding": {"name": "Acme", "color": f"red; }} {BREAKOUT}"}})
        assert "<script>alert(1)</script>" not in doc
        assert doc.count("</style>") == 1

    def test_real_colours_still_reach_the_stylesheet(self):
        doc = _render(report_config={
            "theme": {"colors": {"primary": "#ff8800", "pass": "rgba(0,128,0,.5)"}},
            "branding": {"name": "Acme", "color": "var(--primary)"},
        })
        style = self._style(doc)
        assert "--primary: #ff8800;" in style
        assert "--pass: rgba(0,128,0,.5);" in style
        assert "color: var(--primary);" in style

    def test_a_url_value_is_dropped_rather_than_written(self):
        doc = _render(report_config={"theme": {"colors": {"primary": "url(http://x/y.png)"}}})
        # ':' and '/' are not colour syntax; the declaration is left out.
        assert "url(" not in doc


class TestScriptBlockIsNotEscapable:
    def _history(self):
        return [
            {"Timestamp": str(1000 + i), "User Count": "5", "Requests/s": "10",
             "Failures/s": "0", "50%": "5", "95%": "9", "99%": "12"}
            for i in range(5)
        ]

    def test_a_dataset_label_cannot_close_the_script_element(self):
        doc = _render(
            stats_history=self._history(),
            report_config={"charts": {"throughput": {"datasets": [
                {"key": "rps", "label": "</script><script>alert(1)</script>"}
            ]}}},
        )
        assert "<script>alert(1)</script>" not in doc
        # The label is still there, as an escaped JS string.
        assert "\\u003c/script\\u003e" in doc

    def test_a_chart_name_cannot_break_the_canvas_id(self):
        doc = _render(
            stats_history=self._history(),
            report_config={"charts": {'x" onload="alert(1)': {
                "title": "X", "datasets": [{"key": "rps", "label": "RPS"}]}}},
        )
        assert 'onload="alert(1)"' not in doc
        assert 'onload=' not in doc

    def test_the_canvas_id_matches_the_one_the_script_looks_up(self):
        doc = _render(
            stats_history=self._history(),
            report_config={"charts": {"my chart": {
                "title": "X", "datasets": [{"key": "rps", "label": "RPS"}]}}},
        )
        assert 'id="my_chartChart"' in doc
        assert '"my_chartChart"' in doc


class TestBodyEscaping:
    def test_a_trend_metric_is_not_markup(self):
        history_runs = [
            {"run_id": f"r{i}", "started_at": "2026-01-0%dT10:00:00" % (i + 1), "p95_ms": 10.0}
            for i in range(3)
        ]
        doc = _render(
            report_config={
                "sections": ["kpi", "trends"],
                "trends": {"metrics": ["<img src=x onerror=alert(1)>"]},
            },
            history_runs=history_runs,
        )
        assert "<img src=x" not in doc
        assert "&lt;img src=x" in doc

    def test_a_kpi_format_string_is_not_markup(self):
        doc = _render(report_config={"kpi": {"cards": [
            {"metric": "requests", "label": "R", "format": "<b>{value:.0f}</b>"}
        ]}})
        assert "<b>100</b>" not in doc
        assert "&lt;b&gt;100&lt;/b&gt;" in doc

    def test_a_timezone_is_not_markup(self):
        doc = _render(report_config={"timezone": "<script>alert(1)</script>"})
        assert "<script>alert(1)</script>" not in doc

    def test_a_string_metric_is_not_markup(self):
        doc = _render(metrics={"requests": 100, "note": "<img src=x onerror=alert(1)>"})
        assert "<img src=x" not in doc

    def test_an_endpoint_name_from_the_target_is_not_markup(self):
        doc = _render(endpoint_stats=[{
            "Type": "GET", "Name": "/search?q=<img src=x onerror=alert(1)>",
            "Request Count": "10", "Failure Count": "0", "Average Response Time": "5",
            "50%": "5", "95%": "9", "99%": "12", "Max Response Time": "20",
            "Requests/s": "1",
        }])
        assert "<img src=x" not in doc
        assert "&lt;img src=x" in doc


# ── topology note ─────────────────────────────────────────────────────


def _renderer(run_meta):
    from locomotive.reporter import ReportConfig, ReportRenderer

    return ReportRenderer(
        config=ReportConfig(),
        run_meta=run_meta,
        current_metrics={"requests": 10},
        baseline_metrics=None,
        analysis=None,
    )


class TestTopologyNote:
    def test_absent_without_topology(self):
        assert _renderer({}) ._topology_note() == ""

    def test_absent_for_a_single_generator(self):
        # One process is the ordinary case and saying so would be noise.
        note = _renderer({"topology": {"load_generators": 1}})._topology_note()
        assert note == ""

    def test_names_the_count_when_distributed(self):
        note = _renderer({"topology": {"load_generators": 8}})._topology_note()
        assert "Load generators: 8" in note

    def test_survives_a_malformed_topology(self):
        # run.json is written by an older version, or by hand.
        assert _renderer({"topology": "four"})._topology_note() == ""
        assert _renderer({"topology": {"load_generators": "four"}})._topology_note() == ""

    def test_reaches_the_rendered_header(self):
        html = _renderer({"run_id": "r1", "topology": {"load_generators": 4}}).render()
        assert "Load generators: 4" in html
