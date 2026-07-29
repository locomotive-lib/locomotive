"""Report configuration: dataclasses, presets, and resolution logic."""
from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Colours
#
# Theme colours are the one part of the config that the report writes into a
# stylesheet rather than into the document body, and a ``<style>`` block has
# no entity syntax — ``html.escape`` does nothing there. So the answer is to
# recognise a colour rather than to escape one: a value that needs a ";", a
# ":" or a brace is not a colour, and both the renderer (which drops it) and
# ``loco validate`` (which says so) ask the same two questions here.
# ---------------------------------------------------------------------------

# Enough for "#3b82f6", "rgba(0,0,0,.5)", "1px solid red" and "var(--primary)",
# and short of ";", ":", "{", "}", quotes and slashes — the characters needed
# to close a declaration, open a url() or start a comment.
_CSS_VALUE_RE = re.compile(r"^[A-Za-z0-9 #%(),.\-_+]{1,64}$")
_CSS_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def css_name(name: Any) -> Optional[str]:
    """A custom-property name safe to write into a stylesheet, or ``None``."""
    text = str(name)
    return text if _CSS_NAME_RE.match(text) else None


def css_value(value: Any) -> Optional[str]:
    """A declaration value safe to write into a stylesheet, or ``None``."""
    text = str(value).strip()
    return text if _CSS_VALUE_RE.match(text) else None


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class KpiCardConfig:
    metric: str
    label: str
    format: str = "{value:.2f}"
    unit: Optional[str] = None
    multiplier: float = 1.0


@dataclass
class ChartDatasetConfig:
    key: str
    label: str
    color: str = "#3b82f6"
    fill: bool = False
    y_axis: str = "left"
    dash: Optional[List[int]] = None


@dataclass
class ChartConfig:
    enabled: bool = True
    title: str = ""
    datasets: List[ChartDatasetConfig] = field(default_factory=list)


@dataclass
class EndpointColumnConfig:
    key: str
    label: str
    highlight: Optional[Dict[str, float]] = None  # {"warn": N, "fail": N}


@dataclass
class ThemeConfig:
    mode: str = "light"
    colors: Dict[str, str] = field(default_factory=dict)


@dataclass
class BrandingConfig:
    name: str = "Locomotive"
    color: Optional[str] = None  # maps to --primary


@dataclass
class TrendsConfig:
    metrics: List[str] = field(default_factory=lambda: ["p95_ms", "rps", "error_rate"])


@dataclass
class ReportConfig:
    title: str = "CI Load Test Report"
    output: Optional[str] = None
    theme: ThemeConfig = field(default_factory=ThemeConfig)
    branding: BrandingConfig = field(default_factory=BrandingConfig)
    sections: List[str] = field(
        default_factory=lambda: ["kpi", "charts", "regression", "endpoints"]
    )
    kpi_cards: List[KpiCardConfig] = field(default_factory=list)
    charts: Dict[str, ChartConfig] = field(default_factory=dict)
    endpoint_columns: List[EndpointColumnConfig] = field(default_factory=list)
    trends: TrendsConfig = field(default_factory=TrendsConfig)
    timezone: str = "UTC"


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

_DEFAULT_KPI_CARDS: List[Dict[str, Any]] = [
    {"metric": "rps",        "label": "Requests/sec",   "format": "{value:.1f}"},
    {"metric": "avg_ms",     "label": "Avg Response",   "format": "{value:.0f}", "unit": "ms"},
    {"metric": "p95_ms",     "label": "P95 Response",   "format": "{value:.0f}", "unit": "ms"},
    {"metric": "error_rate", "label": "Error Rate",     "format": "{value:.2f}", "unit": "%"},
    {"metric": "requests",   "label": "Total Requests", "format": "{value:,}"},
    {"metric": "duration",   "label": "Duration",       "format": "duration"},
]

_DEFAULT_THROUGHPUT_DATASETS: List[Dict[str, Any]] = [
    {"key": "rps",    "label": "RPS",      "color": "#3b82f6", "fill": True,  "y_axis": "left"},
    {"key": "users",  "label": "Users",    "color": "#8b5cf6", "fill": False, "y_axis": "right", "dash": [5, 5]},
    {"key": "errors", "label": "Errors/s", "color": "#ef4444", "fill": True,  "y_axis": "left"},
]

_DEFAULT_RESPONSE_DATASETS: List[Dict[str, Any]] = [
    {"key": "p50", "label": "P50", "color": "#10b981"},
    {"key": "p95", "label": "P95", "color": "#f59e0b"},
    {"key": "p99", "label": "P99", "color": "#ef4444"},
]

_DEFAULT_ENDPOINT_COLUMNS: List[Dict[str, Any]] = [
    {"key": "name",       "label": "Endpoint"},
    {"key": "requests",   "label": "Requests"},
    {"key": "failures",   "label": "Failures",  "highlight": {"fail": 1}},
    {"key": "avg",        "label": "Avg (ms)"},
    {"key": "p50",        "label": "P50"},
    {"key": "p95",        "label": "P95 (ms)",  "highlight": {"warn": 400, "fail": 500}},
    {"key": "p99",        "label": "P99 (ms)"},
    {"key": "max",        "label": "Max (ms)"},
    {"key": "rps",        "label": "RPS"},
    {"key": "error_rate", "label": "Err %",     "highlight": {"warn": 1, "fail": 5}},
]

# Dark theme CSS variable overrides
DARK_COLORS: Dict[str, str] = {
    "bg":           "#0f172a",
    "card":         "#1e293b",
    "line":         "#334155",
    "text":         "#f1f5f9",
    "text-muted":   "#94a3b8",
    "primary-light":"#1e3a5f",
    "pass-bg":      "#064e3b",
    "warn-bg":      "#78350f",
    "fail-bg":      "#7f1d1d",
    "skip-bg":      "#1f2937",
}

# Human-readable labels for trend metrics
METRIC_LABELS: Dict[str, str] = {
    "rps":             "Requests/s",
    "avg_ms":          "Avg Response (ms)",
    "median_ms":       "Median (ms)",
    "p95_ms":          "P95 (ms)",
    "p99_ms":          "P99 (ms)",
    "max_ms":          "Max (ms)",
    "error_rate":      "Error Rate (%)",
    "error_rate_4xx":  "4xx Error Rate (%)",
    "error_rate_5xx":  "5xx Error Rate (%)",
    "error_rate_503":  "503 Error Rate (%)",
    "requests":        "Total Requests",
    "failures":        "Total Failures",
}

# Chart colors per metric for trend charts
METRIC_COLORS: Dict[str, str] = {
    "rps":            "#3b82f6",
    "avg_ms":         "#8b5cf6",
    "median_ms":      "#06b6d4",
    "p95_ms":         "#f59e0b",
    "p99_ms":         "#ef4444",
    "max_ms":         "#dc2626",
    "error_rate":     "#ef4444",
    "error_rate_4xx": "#f97316",
    "error_rate_5xx": "#dc2626",
    "error_rate_503": "#9f1239",
    "requests":       "#10b981",
    "failures":       "#f43f5e",
}


# ---------------------------------------------------------------------------
# Presets
# ---------------------------------------------------------------------------

PRESETS: Dict[str, Dict[str, Any]] = {
    "default": {},
    "latency": {
        "kpi": {
            "cards": [
                {"metric": "avg_ms",    "label": "Avg Response",    "format": "{value:.0f}", "unit": "ms"},
                {"metric": "median_ms", "label": "Median Response", "format": "{value:.0f}", "unit": "ms"},
                {"metric": "p95_ms",    "label": "P95 Response",    "format": "{value:.0f}", "unit": "ms"},
                {"metric": "p99_ms",    "label": "P99 Response",    "format": "{value:.0f}", "unit": "ms"},
                {"metric": "max_ms",    "label": "Max Response",    "format": "{value:.0f}", "unit": "ms"},
                {"metric": "duration",  "label": "Duration",        "format": "duration"},
            ]
        },
        "charts": {
            "throughput":    {"enabled": False},
            "response_time": {"enabled": True},
        },
        "trends": {"metrics": ["avg_ms", "p95_ms", "p99_ms"]},
    },
    "throughput": {
        "kpi": {
            "cards": [
                {"metric": "rps",        "label": "Requests/sec",   "format": "{value:.1f}"},
                {"metric": "requests",   "label": "Total Requests", "format": "{value:,}"},
                {"metric": "error_rate", "label": "Error Rate",     "format": "{value:.2f}", "unit": "%"},
                {"metric": "duration",   "label": "Duration",       "format": "duration"},
            ]
        },
        "charts": {
            "throughput":    {"enabled": True},
            "response_time": {"enabled": False},
        },
        "trends": {"metrics": ["rps", "requests"]},
    },
    "errors": {
        "kpi": {
            "cards": [
                {"metric": "error_rate",     "label": "Total Error Rate", "format": "{value:.2f}", "unit": "%"},
                {"metric": "error_rate_4xx", "label": "4xx Errors",       "format": "{value:.2f}", "unit": "%"},
                {"metric": "error_rate_5xx", "label": "5xx Errors",       "format": "{value:.2f}", "unit": "%"},
                {"metric": "error_rate_503", "label": "503 Errors",       "format": "{value:.2f}", "unit": "%"},
                {"metric": "failures",       "label": "Total Failures",   "format": "{value:,}"},
                {"metric": "duration",       "label": "Duration",         "format": "duration"},
            ]
        },
        "charts": {
            "throughput": {
                "enabled": True,
                "title":   "Errors Over Time",
                "datasets": [
                    {"key": "errors", "label": "Errors/s", "color": "#ef4444", "fill": True,  "y_axis": "left"},
                    {"key": "rps",    "label": "RPS",      "color": "#3b82f6", "fill": False, "y_axis": "left"},
                ],
            },
            "response_time": {"enabled": False},
        },
        "trends": {"metrics": ["error_rate", "error_rate_5xx", "error_rate_503"]},
    },
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """Recursively merge *override* into *base*. Lists are replaced entirely."""
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(result.get(key), dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _build_default_raw() -> Dict[str, Any]:
    return {
        "title": "CI Load Test Report",
        "theme": {"mode": "light", "colors": {}},
        "branding": {"name": "Locomotive", "color": None},
        "sections": ["kpi", "charts", "regression", "endpoints"],
        "kpi": {"cards": copy.deepcopy(_DEFAULT_KPI_CARDS)},
        "charts": {
            "throughput": {
                "enabled":  True,
                "title":    "Throughput & Users Over Time",
                "datasets": copy.deepcopy(_DEFAULT_THROUGHPUT_DATASETS),
            },
            "response_time": {
                "enabled":  True,
                "title":    "Response Time Percentiles",
                "datasets": copy.deepcopy(_DEFAULT_RESPONSE_DATASETS),
            },
        },
        "endpoint_table": {"columns": copy.deepcopy(_DEFAULT_ENDPOINT_COLUMNS)},
        "trends": {"metrics": ["p95_ms", "rps", "error_rate"]},
        "timezone": "UTC",
    }


# ---------------------------------------------------------------------------
# Materialisation helpers
# ---------------------------------------------------------------------------

def _make_kpi_cards(raw: List[Dict[str, Any]]) -> List[KpiCardConfig]:
    return [
        KpiCardConfig(
            metric=c["metric"],
            label=c.get("label", c["metric"]),
            format=c.get("format", "{value:.2f}"),
            unit=c.get("unit"),
            multiplier=float(c.get("multiplier", 1.0)),
        )
        for c in raw
    ]


def _make_datasets(raw: List[Dict[str, Any]]) -> List[ChartDatasetConfig]:
    return [
        ChartDatasetConfig(
            key=d["key"],
            label=d.get("label", d["key"]),
            color=d.get("color", "#3b82f6"),
            fill=bool(d.get("fill", False)),
            y_axis=d.get("y_axis", "left"),
            dash=d.get("dash"),
        )
        for d in raw
    ]


_KNOWN_CHARTS = {"throughput", "response_time"}


def _make_charts(raw: Dict[str, Any]) -> Dict[str, ChartConfig]:
    result: Dict[str, ChartConfig] = {}
    for name, cfg in raw.items():
        if not isinstance(cfg, dict):
            continue
        # Skip entries that are clearly not chart configs (e.g. misplaced
        # endpoint_table or kpi blocks nested under "charts" by mistake).
        if name not in _KNOWN_CHARTS and "datasets" not in cfg:
            continue
        result[name] = ChartConfig(
            enabled=bool(cfg.get("enabled", True)),
            title=cfg.get("title", ""),
            datasets=_make_datasets(cfg.get("datasets", [])),
        )
    return result


def _make_endpoint_columns(raw: List[Dict[str, Any]]) -> List[EndpointColumnConfig]:
    cols = []
    for c in raw:
        highlight = c.get("highlight")
        if isinstance(highlight, dict):
            highlight = {k: float(v) for k, v in highlight.items()}
        cols.append(EndpointColumnConfig(key=c["key"], label=c.get("label", c["key"]), highlight=highlight))
    return cols


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def resolve_report_config(raw: Dict[str, Any]) -> ReportConfig:
    """Resolve a raw ``report`` config section into a :class:`ReportConfig`.

    Resolution order: defaults → preset → user overrides.
    """
    merged = _build_default_raw()

    preset_name = raw.get("preset", "default")
    if preset_name and preset_name != "default" and preset_name in PRESETS:
        merged = _deep_merge(merged, PRESETS[preset_name])

    overlay = {k: v for k, v in raw.items() if k not in ("preset",) and v is not None}
    # Rescue misplaced keys: users sometimes nest kpi/endpoint_table inside charts
    charts_overlay = overlay.get("charts")
    if isinstance(charts_overlay, dict):
        for misplaced in ("kpi", "endpoint_table"):
            if misplaced in charts_overlay and misplaced not in overlay:
                overlay[misplaced] = charts_overlay.pop(misplaced)
    merged = _deep_merge(merged, overlay)

    theme_raw = merged.get("theme", {})
    theme_colors = {k: v for k, v in (theme_raw.get("colors") or {}).items() if not k.startswith("_")}
    # theme.color is a shortcut for theme.colors.primary
    if "color" in theme_raw and "primary" not in theme_colors:
        theme_colors["primary"] = theme_raw["color"]
    theme = ThemeConfig(
        mode=theme_raw.get("mode", "light"),
        colors=theme_colors,
    )

    br = merged.get("branding", {})
    branding = BrandingConfig(
        name=br.get("name", "Locomotive"),
        color=br.get("color"),
    )

    sections = merged.get("sections", ["kpi", "charts", "regression", "endpoints"])

    kpi_cards = _make_kpi_cards(merged.get("kpi", {}).get("cards", _DEFAULT_KPI_CARDS))

    charts = _make_charts(merged.get("charts", {}))

    ep_columns = _make_endpoint_columns(
        merged.get("endpoint_table", {}).get("columns", _DEFAULT_ENDPOINT_COLUMNS)
    )

    trends_raw = merged.get("trends", {})
    trends = TrendsConfig(metrics=trends_raw.get("metrics", ["p95_ms", "rps", "error_rate"]))

    return ReportConfig(
        title=merged.get("title", "CI Load Test Report"),
        output=merged.get("output"),
        theme=theme,
        branding=branding,
        sections=sections,
        kpi_cards=kpi_cards,
        charts=charts,
        endpoint_columns=ep_columns,
        trends=trends,
        timezone=merged.get("timezone", "UTC"),
    )
