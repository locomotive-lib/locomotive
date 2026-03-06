import copy

import pytest

from locomotive.report_config import (
    _deep_merge,
    _make_charts,
    resolve_report_config,
    PRESETS,
)


# ── _deep_merge ───────────────────────────────────────────────────────


class TestDeepMerge:
    def test_flat_override(self):
        result = _deep_merge({"a": 1, "b": 2}, {"b": 3})
        assert result == {"a": 1, "b": 3}

    def test_nested_merge(self):
        base = {"theme": {"mode": "light", "colors": {"primary": "blue"}}}
        override = {"theme": {"colors": {"fail": "red"}}}
        result = _deep_merge(base, override)
        assert result["theme"]["mode"] == "light"
        assert result["theme"]["colors"]["primary"] == "blue"
        assert result["theme"]["colors"]["fail"] == "red"

    def test_lists_replaced(self):
        base = {"items": [1, 2, 3]}
        override = {"items": [4, 5]}
        result = _deep_merge(base, override)
        assert result["items"] == [4, 5]

    def test_base_not_mutated(self):
        base = {"a": {"b": 1}}
        base_copy = copy.deepcopy(base)
        _deep_merge(base, {"a": {"b": 2}})
        assert base == base_copy


# ── _make_charts ──────────────────────────────────────────────────────


class TestMakeCharts:
    def test_known_charts_created(self):
        raw = {
            "throughput": {"enabled": True, "title": "T", "datasets": []},
            "response_time": {"enabled": True, "title": "R", "datasets": []},
        }
        charts = _make_charts(raw)
        assert "throughput" in charts
        assert "response_time" in charts

    def test_unknown_without_datasets_filtered(self):
        raw = {
            "throughput": {"enabled": True, "datasets": []},
            "kpi": {"cards": [{"metric": "rps"}]},  # misplaced, no datasets
        }
        charts = _make_charts(raw)
        assert "throughput" in charts
        assert "kpi" not in charts

    def test_custom_chart_with_datasets_kept(self):
        raw = {
            "my_chart": {
                "enabled": True,
                "title": "Custom",
                "datasets": [{"key": "rps", "label": "RPS"}],
            },
        }
        charts = _make_charts(raw)
        assert "my_chart" in charts

    def test_non_dict_values_skipped(self):
        raw = {"throughput": "bad"}
        charts = _make_charts(raw)
        assert len(charts) == 0


# ── resolve_report_config ─────────────────────────────────────────────


class TestResolveReportConfig:
    def test_empty_input_returns_defaults(self):
        cfg = resolve_report_config({})
        assert cfg.title == "CI Load Test Report"
        assert len(cfg.kpi_cards) > 0
        assert "throughput" in cfg.charts
        assert "response_time" in cfg.charts
        assert cfg.theme.mode == "light"

    def test_preset_latency(self):
        cfg = resolve_report_config({"preset": "latency"})
        metrics = [c.metric for c in cfg.kpi_cards]
        assert "avg_ms" in metrics
        assert "p95_ms" in metrics
        assert cfg.charts["throughput"].enabled is False
        assert cfg.charts["response_time"].enabled is True

    def test_preset_throughput(self):
        cfg = resolve_report_config({"preset": "throughput"})
        metrics = [c.metric for c in cfg.kpi_cards]
        assert "rps" in metrics
        assert cfg.charts["response_time"].enabled is False
        assert cfg.charts["throughput"].enabled is True

    def test_preset_errors(self):
        cfg = resolve_report_config({"preset": "errors"})
        metrics = [c.metric for c in cfg.kpi_cards]
        assert "error_rate" in metrics
        assert "error_rate_4xx" in metrics

    def test_theme_color_shortcut(self):
        cfg = resolve_report_config({"theme": {"color": "#e11d48"}})
        assert cfg.theme.colors.get("primary") == "#e11d48"

    def test_theme_color_does_not_override_explicit_primary(self):
        cfg = resolve_report_config({
            "theme": {"color": "#e11d48", "colors": {"primary": "#ff0000"}}
        })
        assert cfg.theme.colors["primary"] == "#ff0000"

    def test_rescue_misplaced_kpi(self):
        """kpi nested inside charts should be rescued to top level."""
        cfg = resolve_report_config({
            "charts": {
                "kpi": {"cards": [{"metric": "rps", "label": "RPS"}]},
            }
        })
        metrics = [c.metric for c in cfg.kpi_cards]
        assert "rps" in metrics

    def test_rescue_misplaced_endpoint_table(self):
        """endpoint_table nested inside charts should be rescued."""
        cfg = resolve_report_config({
            "charts": {
                "endpoint_table": {
                    "columns": [{"key": "name", "label": "Endpoint"}]
                },
            }
        })
        assert len(cfg.endpoint_columns) == 1
        assert cfg.endpoint_columns[0].key == "name"

    def test_custom_title(self):
        cfg = resolve_report_config({"title": "My Report"})
        assert cfg.title == "My Report"

    def test_custom_sections(self):
        cfg = resolve_report_config({"sections": ["kpi", "charts"]})
        assert cfg.sections == ["kpi", "charts"]

    def test_user_overrides_on_top_of_preset(self):
        cfg = resolve_report_config({
            "preset": "latency",
            "title": "Custom Title",
        })
        assert cfg.title == "Custom Title"
        # preset still applied
        assert cfg.charts["throughput"].enabled is False
