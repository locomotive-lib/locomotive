from __future__ import annotations

import csv
import html
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .report_config import (
    DARK_COLORS,
    METRIC_COLORS,
    METRIC_LABELS,
    ChartConfig,
    ChartDatasetConfig,
    KpiCardConfig,
    ReportConfig,
    css_name,
    css_value,
    resolve_report_config,
)
from .utils import utc_now


# ---------------------------------------------------------------------------
# Embedding helpers
#
# The report is one HTML file built by string concatenation, and almost
# everything in it — the title, chart labels, theme colours, the timezone
# suffix, endpoint names straight out of the target's URLs — comes from
# somewhere other than this module. ``html.escape`` covers the body, but a
# ``<style>`` block and a ``<script>`` block have their own rules: neither
# parses entities, and both end at the first matching close tag *inside* the
# text. So a colour of ``red} </style><script>...`` or a chart label
# containing ``</script>`` walks straight out of its element. These three
# helpers are the answer for the three places that isn't plain HTML.
# ---------------------------------------------------------------------------

def _js_json(value: Any) -> str:
    """``json.dumps`` for a value that will sit inside a ``<script>`` block.

    JSON's own escaping leaves ``<`` alone, so a string containing
    ``</script>`` ends the script element and everything after it becomes
    markup. Escaping the three characters that can start a tag keeps the
    payload identical to JavaScript while making it inert to the HTML parser.
    """
    encoded = json.dumps(value)
    for char, escaped in (
        ("<", "\\u003c"), (">", "\\u003e"), ("&", "\\u0026"),
        # Not markup, but a literal line separator ends a JS string.
        (" ", "\\u2028"), (" ", "\\u2029"),
    ):
        encoded = encoded.replace(char, escaped)
    return encoded


_DOM_ID_UNSAFE_RE = re.compile(r"[^A-Za-z0-9_-]")


def _dom_id(prefix: str, raw: Any, suffix: str = "") -> str:
    """A DOM id built from config text.

    Chart ids are made of dictionary keys and metric names out of the config,
    and they are written into an ``id`` attribute *and* into
    ``getElementById`` — so a quote in one of them breaks the attribute and a
    stray character breaks the pairing between the two. Reducing them to the
    id alphabet keeps both halves in step.
    """
    return f"{prefix}{_DOM_ID_UNSAFE_RE.sub('_', str(raw))}{suffix}"


def _chart_canvas_id(name: Any) -> str:
    return _dom_id("", name, "Chart")


def _trend_canvas_id(metric: Any) -> str:
    return _dom_id("trendChart_", metric)


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _format_value(value: Any, decimals: int = 2) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.{decimals}f}"
    # Metrics are usually numbers, but ``baseline.json`` is a file on disk and
    # a string that lands here is written into a table cell.
    return html.escape(str(value))


def _format_delta(value: Any) -> str:
    if value is None:
        return "-"
    try:
        v = float(value)
        arrow = "\u2191" if v > 0 else "\u2193" if v < 0 else "\u2192"
        return f"{arrow} {abs(v):.1f}%"
    except (TypeError, ValueError):
        return "-"


def _format_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes = int(seconds // 60)
    secs = int(seconds % 60)
    return f"{minutes}m {secs}s"


_FULL_SHA = re.compile(r"^[0-9a-f]{40}$", re.IGNORECASE)


def _short_id(value: Any) -> str:
    """A run id short enough for the header, without hiding what tells runs apart.

    A bare commit SHA is cut to 12 characters, as it always was. The ids
    Locomotive builds itself — ``<short commit>-<build id>`` — are kept whole:
    cut at 12 they left exactly the shared commit, so a rebuild and the
    baseline it was compared with both read ``042d14c81983``.
    """
    text = str(value)
    if _FULL_SHA.match(text):
        return text[:12]
    return text if len(text) <= 40 else text[:40] + "…"


def _status_class(status: str) -> str:
    return {
        "PASS":        "status-pass",
        "WARNING":     "status-warning",
        "DEGRADATION": "status-fail",
        "NO_DATA":     "status-nodata",
        "SKIP":        "status-skip",
    }.get(status, "status-unknown")


def _delta_class(value: Any, metric: str) -> str:
    if value is None:
        return ""
    try:
        v = float(value)
        if "rps" in metric.lower():
            return "delta-good" if v > 0 else "delta-bad" if v < 0 else ""
        return "delta-good" if v < 0 else "delta-bad" if v > 0 else ""
    except (TypeError, ValueError):
        return ""


# ---------------------------------------------------------------------------
# CSV loaders
# ---------------------------------------------------------------------------

def load_stats_history(history_path: Path) -> List[Dict[str, Any]]:
    if not history_path.exists():
        return []
    history: List[Dict[str, Any]] = []
    with open(history_path, "r", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get("Name") in ("Aggregated", ""):
                history.append(row)
    return history


def load_endpoint_stats(stats_path: Path) -> List[Dict[str, Any]]:
    if not stats_path.exists():
        return []
    endpoints: List[Dict[str, Any]] = []
    with open(stats_path, "r", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get("Name") and row.get("Name") != "Aggregated":
                endpoints.append(row)
    return endpoints


def _build_chart_data(history: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not history:
        return {"labels": [], "rps": [], "users": [], "p50": [], "p95": [], "p99": [], "errors": []}

    labels, rps_data, users_data = [], [], []
    p50_data, p95_data, p99_data, errors_data = [], [], [], []
    start_ts: Optional[int] = None

    for row in history:
        try:
            ts = int(row.get("Timestamp", 0))
            if start_ts is None:
                start_ts = ts
            labels.append(ts - start_ts)
            users_data.append(int(row.get("User Count", 0)))

            def _f(key: str) -> Optional[float]:
                v = row.get(key, "0")
                return float(v) if v and v != "N/A" else None

            rps_data.append(_f("Requests/s") or 0)
            errors_data.append(_f("Failures/s") or 0)
            p50_data.append(_f("50%"))
            p95_data.append(_f("95%"))
            p99_data.append(_f("99%"))
        except (ValueError, TypeError):
            continue

    return {
        "labels": labels, "rps": rps_data, "users": users_data,
        "p50": p50_data, "p95": p95_data, "p99": p99_data, "errors": errors_data,
    }


# ---------------------------------------------------------------------------
# Endpoint field extractors
# ---------------------------------------------------------------------------

def _ep_safe_float(ep: Dict, key: str) -> float:
    try:
        return float(ep.get(key, 0) or 0)
    except (TypeError, ValueError):
        return 0.0


_ENDPOINT_DISPLAY: Dict[str, Any] = {
    "name":       lambda ep: html.escape(f"{ep.get('Type', '')} {ep.get('Name', '')}".strip()),
    "requests":   lambda ep: ep.get("Request Count", "0"),
    "failures":   lambda ep: ep.get("Failure Count", "0"),
    "avg":        lambda ep: _format_value(_ep_safe_float(ep, "Average Response Time"), 1),
    "p50":        lambda ep: _format_value(_ep_safe_float(ep, "50%"), 0),
    "p95":        lambda ep: _format_value(_ep_safe_float(ep, "95%"), 0),
    "p99":        lambda ep: _format_value(_ep_safe_float(ep, "99%"), 0),
    "max":        lambda ep: _format_value(_ep_safe_float(ep, "Max Response Time"), 0),
    "rps":        lambda ep: _format_value(_ep_safe_float(ep, "Requests/s"), 2),
    "error_rate": lambda ep: _format_value(
        _ep_safe_float(ep, "Failure Count") / max(_ep_safe_float(ep, "Request Count"), 1) * 100, 2
    ),
}

_ENDPOINT_NUMERIC: Dict[str, Any] = {
    "failures":   lambda ep: _ep_safe_float(ep, "Failure Count"),
    "avg":        lambda ep: _ep_safe_float(ep, "Average Response Time"),
    "p50":        lambda ep: _ep_safe_float(ep, "50%"),
    "p95":        lambda ep: _ep_safe_float(ep, "95%"),
    "p99":        lambda ep: _ep_safe_float(ep, "99%"),
    "max":        lambda ep: _ep_safe_float(ep, "Max Response Time"),
    "rps":        lambda ep: _ep_safe_float(ep, "Requests/s"),
    "error_rate": lambda ep: (
        _ep_safe_float(ep, "Failure Count") / max(_ep_safe_float(ep, "Request Count"), 1) * 100
    ),
}


# ---------------------------------------------------------------------------
# ReportRenderer
# ---------------------------------------------------------------------------

class ReportRenderer:
    def __init__(
        self,
        config: ReportConfig,
        run_meta: Dict[str, Any],
        current_metrics: Dict[str, Any],
        baseline_metrics: Optional[Dict[str, Any]],
        analysis: Optional[Dict[str, Any]],
        stats_history: Optional[List[Dict[str, Any]]] = None,
        endpoint_stats: Optional[List[Dict[str, Any]]] = None,
        history_runs: Optional[List[Dict[str, Any]]] = None,
        stylesheet: Optional[str] = None,
    ):
        self.cfg = config
        self.stylesheet = stylesheet
        self.run_meta = run_meta
        self.current = current_metrics
        self.baseline = baseline_metrics
        self.analysis = analysis
        self.stats_history = stats_history or []
        self.endpoint_stats = endpoint_stats or []
        self.history_runs = history_runs or []

        self.status = (analysis.get("status", "PASS") if analysis else "PASS")
        self.generated_at = self._format_datetime(utc_now())
        self.chart_data = _build_chart_data(self.stats_history)
        self.has_charts = len(self.stats_history) > 2

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def render(self) -> str:
        body = self._render_body()
        return self._wrap_document(body)

    # ------------------------------------------------------------------
    # Document wrapper
    # ------------------------------------------------------------------

    def _wrap_document(self, body: str) -> str:
        title_safe = html.escape(self.cfg.title)
        return (
            "<!doctype html>\n"
            '<html lang="en">\n'
            "<head>\n"
            '  <meta charset="utf-8" />\n'
            '  <meta name="viewport" content="width=device-width, initial-scale=1" />\n'
            f"  <title>{title_safe}</title>\n"
            f"{self._chart_js_tag()}"
            f"  <style>\n{self._build_css()}\n  </style>\n"
            f"{self._stylesheet_tag()}"
            "</head>\n"
            "<body>\n"
            '  <div class="container">\n'
            f"{body}\n"
            "  </div>\n"
            f"{self._build_js()}\n"
            "</body>\n"
            "</html>"
        )

    # ------------------------------------------------------------------
    # CSS
    # ------------------------------------------------------------------

    def _stylesheet_tag(self) -> str:
        # The same rules as the <style> block, from a file next to the report.
        # Where a Content-Security-Policy refuses inline styles — Jenkins serves
        # published reports with `style-src 'self'` — the file still applies;
        # where the file is missing, as with a report.html downloaded on its
        # own, the inline copy does.
        if not self.stylesheet:
            return ""
        return f'  <link rel="stylesheet" href="{html.escape(self.stylesheet, quote=True)}" />\n'

    def _build_css(self) -> str:
        parts = [self._css_base()]
        if self.cfg.theme.mode == "dark":
            parts.append(self._css_dark())
        user = self._css_user_overrides()
        if user:
            parts.append(user)
        return "\n".join(parts)

    def _css_base(self) -> str:
        return """\
    :root {
      --pass: #059669; --pass-bg: #d1fae5;
      --warn: #d97706; --warn-bg: #fef3c7;
      --fail: #dc2626; --fail-bg: #fee2e2;
      --skip: #6b7280; --skip-bg: #f3f4f6;
      --nodata: #7c3aed; --nodata-bg: #ede9fe;
      --bg: #f8fafc; --card: #ffffff; --line: #e2e8f0;
      --text: #1e293b; --text-muted: #64748b;
      --primary: #3b82f6; --primary-light: #dbeafe;
    }
    * { box-sizing: border-box; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      margin: 0; padding: 24px;
      background: var(--bg); color: var(--text); line-height: 1.5;
    }
    .container { max-width: 1400px; margin: 0 auto; }

    .header {
      display: flex; align-items: center; justify-content: space-between;
      margin-bottom: 24px; flex-wrap: wrap; gap: 16px;
      border-bottom: 3px solid var(--primary); padding-bottom: 20px;
    }
    .title { font-size: 28px; font-weight: 700; margin: 0; }
    .status-badge-large {
      padding: 8px 20px; border-radius: 8px;
      font-weight: 700; font-size: 14px; text-transform: uppercase; letter-spacing: 0.05em;
    }
    .status-badge-large.pass    { background: var(--pass-bg); color: var(--pass); }
    .status-badge-large.warning { background: var(--warn-bg); color: var(--warn); }
    .status-badge-large.fail    { background: var(--fail-bg); color: var(--fail); }
    .status-badge-large.nodata  { background: var(--nodata-bg); color: var(--nodata); }
    .meta { color: var(--text-muted); font-size: 13px; margin-top: 4px; }
    .meta-ids { font-family: monospace; font-size: 12px; }
    .meta a { color: inherit; }
    .chart-fallback { color: var(--text-muted); font-size: 13px; margin: 0 0 8px; }

    .card {
      background: var(--card); border: 1px solid var(--line);
      border-radius: 12px; padding: 20px; margin-bottom: 20px;
      box-shadow: 0 1px 3px rgba(0,0,0,0.05);
      border-top: 3px solid var(--primary);
    }
    .card-title {
      font-size: 14px; font-weight: 600; color: var(--text-muted);
      text-transform: uppercase; letter-spacing: 0.05em;
      margin-bottom: 16px; display: flex; align-items: center; gap: 8px;
    }
    .card-title::before {
      content: ""; width: 4px; height: 16px;
      background: var(--primary); border-radius: 2px;
    }

    .kpi-grid {
      display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      gap: 16px; margin-bottom: 24px;
    }
    .kpi-card {
      background: var(--card); border: 1px solid var(--line);
      border-radius: 12px; padding: 20px; text-align: center;
      border-top: 3px solid var(--primary);
    }
    .kpi-value { font-size: 32px; font-weight: 700; color: var(--text); line-height: 1.2; }
    .kpi-unit  { font-size: 16px; font-weight: 400; margin-left: 2px; }
    .kpi-label { font-size: 12px; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.05em; margin-top: 4px; }
    .kpi-delta { font-size: 13px; margin-top: 8px; min-height: 18px; }
    .delta-good { color: var(--pass); }
    .delta-bad  { color: var(--fail); }

    .charts-grid {
      display: grid; grid-template-columns: repeat(auto-fit, minmax(400px, 1fr));
      gap: 20px; margin-bottom: 24px;
    }
    .chart-container { position: relative; height: 280px; }

    .section-label {
      font-size: 12px; font-weight: 600; color: var(--text-muted);
      text-transform: uppercase; letter-spacing: 0.08em;
      margin: 8px 0 12px; display: flex; align-items: center; gap: 8px;
    }
    .section-label::after {
      content: ""; flex: 1; height: 1px; background: var(--line);
    }

    table { width: 100%; border-collapse: collapse; font-size: 14px; }
    th, td { padding: 12px 16px; border-bottom: 1px solid var(--line); text-align: left; }
    th {
      font-size: 11px; font-weight: 600; text-transform: uppercase;
      letter-spacing: 0.05em; color: var(--text-muted); background: var(--bg);
    }
    td.num, th.num { text-align: right; font-family: monospace; }
    tr:hover { background: var(--bg); }
    .endpoint-name { font-weight: 500; }

    .highlight-fail { color: var(--fail); font-weight: 600; }
    .highlight-warn { color: var(--warn); font-weight: 600; }

    .status-pass    { color: var(--pass); font-weight: 600; }
    .status-warning { color: var(--warn); font-weight: 600; }
    .status-fail    { color: var(--fail); font-weight: 600; }
    .status-skip    { color: var(--skip); }
    .status-nodata  { color: var(--nodata); font-weight: 600; }

    .status-summary { display: flex; gap: 12px; flex-wrap: wrap; margin-bottom: 16px; }
    .status-badge { padding: 4px 12px; border-radius: 6px; font-size: 12px; font-weight: 600; }
    .status-badge.pass    { background: var(--pass-bg); color: var(--pass); }
    .status-badge.warning { background: var(--warn-bg); color: var(--warn); }
    .status-badge.fail    { background: var(--fail-bg); color: var(--fail); }
    .status-badge.skip    { background: var(--skip-bg); color: var(--skip); }
    .status-badge.nodata  { background: var(--nodata-bg); color: var(--nodata); }

    .info-message {
      background: var(--warn-bg); border: 1px solid #fbbf24;
      border-radius: 8px; padding: 12px 16px; font-size: 13px;
      color: #92400e; margin-bottom: 16px;
    }

    .footer {
      text-align: center; padding: 24px;
      color: var(--text-muted); font-size: 12px;
    }
    .footer a { color: var(--primary); text-decoration: none; }
    .footer a:hover { text-decoration: underline; }

    @media (max-width: 900px) {
      .charts-grid { grid-template-columns: 1fr; }
    }"""

    def _css_dark(self) -> str:
        lines = ["    :root {"]
        for var, val in DARK_COLORS.items():
            lines.append(f"      --{var}: {val};")
        lines.append("    }")
        lines.append("    .info-message { color: #fbbf24; border-color: #78350f; }")
        return "\n".join(lines)

    def _css_user_overrides(self) -> str:
        colors: Dict[str, str] = dict(self.cfg.theme.colors)
        if not colors and not self.cfg.branding.color:
            return ""
        parts: List[str] = []
        # Names and values are checked rather than escaped: CSS has no entity
        # syntax to escape *into*, so anything that isn't recognisably a
        # colour is left out of the stylesheet entirely. `loco validate` says
        # so before the run, which is where a typo should be caught.
        declarations = []
        for var, val in colors.items():
            name, value = css_name(var), css_value(val)
            if name and value:
                declarations.append(f"      --{name}: {value};")
        if declarations:
            parts.append("\n".join(["    :root {", *declarations, "    }"]))
        brand = css_value(self.cfg.branding.color) if self.cfg.branding.color else None
        if brand:
            parts.append(f"    .footer .brand-name {{ color: {brand}; }}")
        return "\n".join(parts)

    # ------------------------------------------------------------------
    # Shared primitives
    # ------------------------------------------------------------------

    def _card(self, title: str, content: str) -> str:
        return (
            f'    <div class="card">\n'
            f'      <div class="card-title">{title}</div>\n'
            f"      {content}\n"
            f"    </div>"
        )

    def _table(self, headers: List[str], rows: List[str], num_columns: Optional[List[int]] = None) -> str:
        num_set = set(num_columns or [])
        th_parts = []
        for i, h in enumerate(headers):
            cls = ' class="num"' if i in num_set else ""
            th_parts.append(f"<th{cls}>{h}</th>")
        thead = "<tr>" + "".join(th_parts) + "</tr>"
        tbody = "\n".join(rows)
        return f"<table><thead>{thead}</thead><tbody>{tbody}</tbody></table>"

    def _charts_grid(self, cards: List[str]) -> str:
        return '    <div class="charts-grid">\n' + "\n".join(cards) + "\n    </div>"

    def _format_datetime(self, iso: str) -> str:
        """Convert ISO timestamp to human-readable format in configured timezone."""
        try:
            from datetime import datetime, timezone as tz, timedelta
            import re
            dt = datetime.fromisoformat(iso)
            tz_name = self.cfg.timezone
            # Parse offset like "UTC+3", "UTC-5:30", or plain "UTC"
            m = re.match(r"^UTC([+-])(\d{1,2})(?::(\d{2}))?$", tz_name)
            if m:
                sign = 1 if m.group(1) == "+" else -1
                hours = int(m.group(2))
                minutes = int(m.group(3) or 0)
                offset = tz(timedelta(hours=sign * hours, minutes=sign * minutes))
                dt = dt.astimezone(offset)
            elif tz_name == "UTC":
                dt = dt.astimezone(tz.utc)
            return dt.strftime("%b %d, %Y at %H:%M") + f" {tz_name}"
        except Exception:
            return iso

    # ------------------------------------------------------------------
    # Body: section orchestration
    # ------------------------------------------------------------------

    def _render_body(self) -> str:
        _section_map = {
            "kpi":        self._render_kpi,
            "charts":     self._render_charts,
            "regression": self._render_regression,
            "endpoints":  self._render_endpoints,
            "trends":     self._render_trends,
        }
        parts = [self._render_header()]
        for section in self.cfg.sections:
            fn = _section_map.get(section)
            if fn:
                rendered = fn()
                if rendered:
                    parts.append(rendered)
        parts.append(self._render_footer())
        return "\n".join(parts)

    # ------------------------------------------------------------------
    # Header
    # ------------------------------------------------------------------

    def _render_header(self) -> str:
        title_safe = html.escape(self.cfg.title)
        run_id = html.escape(_short_id(self.run_meta.get("run_id", "-")))
        baseline_id = html.escape(_short_id(self.run_meta.get("baseline_id") or "-"))
        badge_cls = _status_class(self.status).replace("status-", "")

        return (
            f'    <div class="header">\n'
            f"      <div>\n"
            f'        <h1 class="title">{title_safe}</h1>\n'
            f'        <div class="meta">\n'
            # The timezone name is config text and rides along in this string.
            f"          Generated at {html.escape(self.generated_at)}<br>\n"
            f"{self._ci_line()}"
            f'          <span class="meta-ids">Run: {run_id} | Baseline: {baseline_id}'
            f"{self._topology_note()}</span>\n"
            f"        </div>\n"
            f"      </div>\n"
            f'      <div class="status-badge-large {badge_cls}">{html.escape(self.status)}</div>\n'
            f"    </div>"
        )

    def _topology_note(self) -> str:
        """How many processes generated this load, when it was more than one.

        Throughput is not comparable between a one-process run and an
        eight-process one, and the difference is invisible in every number
        on the page. A run that used one process says nothing — that is the
        assumption a reader already has.
        """
        topology = self.run_meta.get("topology")
        if not isinstance(topology, dict):
            return ""
        try:
            generators = int(topology.get("load_generators") or 1)
        except (TypeError, ValueError):
            return ""
        if generators < 2:
            return ""
        return f" | Load generators: {generators}"

    def _ci_line(self) -> str:
        """Branch, commit, pull request and build, when a CI recorded them."""
        meta = self.run_meta.get("meta")
        ci = meta.get("ci") if isinstance(meta, dict) else None
        if not isinstance(ci, dict) or ci.get("provider") in (None, "local"):
            return ""
        parts: List[str] = []
        if ci.get("branch"):
            branch = str(ci["branch"])
            if ci.get("target_branch"):
                branch += f" → {ci['target_branch']}"
            parts.append(html.escape(branch))
        if ci.get("commit"):
            parts.append(f'<span class="meta-ids">{html.escape(str(ci["commit"])[:12])}</span>')
        if ci.get("change_id"):
            prefix = "!" if ci.get("provider") == "gitlab" else "#"
            parts.append(self._link(ci.get("change_url"), f"{prefix}{ci['change_id']}"))
        if ci.get("build_url"):
            parts.append(self._link(ci.get("build_url"), "Build"))
        if not parts:
            return ""
        return "          " + " · ".join(parts) + "<br>\n"

    @staticmethod
    def _link(url: Any, label: str) -> str:
        """An anchor for an http(s) URL, and the bare label for anything else.

        The URLs come out of CI environment variables, and a ``javascript:``
        one would otherwise be a link that runs code.
        """
        text = html.escape(label)
        if not isinstance(url, str) or not url.lower().startswith(("http://", "https://")):
            return text
        return f'<a href="{html.escape(url, quote=True)}">{text}</a>'

    def _chart_js_tag(self) -> str:
        url = self.cfg.chart_js_url
        if not url:
            return ""
        return f'  <script src="{html.escape(url, quote=True)}"></script>\n'

    @staticmethod
    def _chart_content(canvas_id: str) -> str:
        # Shown until the script draws the chart. Where scripts never run —
        # Jenkins serves published reports with a policy that blocks them —
        # it stays, and says why the space is empty.
        return (
            '<div class="chart-container">'
            '<p class="chart-fallback">This chart is drawn with JavaScript and Chart.js, '
            "and they did not run here. Jenkins blocks scripts in published reports "
            "unless a Resource Root URL is configured.</p>"
            f'<canvas id="{canvas_id}"></canvas></div>'
        )

    # ------------------------------------------------------------------
    # KPI cards
    # ------------------------------------------------------------------

    def _render_kpi(self) -> str:
        cards = "\n".join(self._render_kpi_card(c) for c in self.cfg.kpi_cards)
        return f'    <div class="kpi-grid">\n{cards}\n    </div>'

    def _render_kpi_card(self, card: KpiCardConfig) -> str:
        value_html, unit_html, delta_html = self._kpi_parts(card)
        label_safe = html.escape(card.label)
        return (
            f'      <div class="kpi-card">\n'
            f'        <div class="kpi-value">{value_html}{unit_html}</div>\n'
            f'        <div class="kpi-label">{label_safe}</div>\n'
            f'        <div class="kpi-delta">{delta_html}</div>\n'
            f"      </div>"
        )

    def _kpi_parts(self, card: KpiCardConfig) -> Tuple[str, str, str]:
        if card.metric == "duration":
            run_time = self.run_meta.get("run_time", 60)
            value_html = _format_duration(run_time) if isinstance(run_time, (int, float)) else str(run_time)
            return value_html, "", ""

        raw = self.current.get(card.metric, 0) or 0
        try:
            val = float(raw) * card.multiplier
            if card.format == "duration":
                value_html = _format_duration(val)
            else:
                # `format` is a template out of the config — "{value:.2f}" is
                # the point, but so is everything else the user typed around it.
                value_html = html.escape(card.format.format(value=val))
        except (ValueError, TypeError, KeyError):
            value_html = html.escape(str(raw))

        unit_html = (
            f'<span class="kpi-unit">{html.escape(card.unit)}</span>' if card.unit else ""
        )
        delta_html = self._kpi_delta(card.metric, raw)
        return value_html, unit_html, delta_html

    def _kpi_delta(self, metric: str, current_val: Any) -> str:
        if not self.baseline or current_val is None:
            return ""
        base_val = self.baseline.get(metric)
        if not base_val:
            return ""
        try:
            d = (float(current_val) - float(base_val)) / float(base_val) * 100
            cls = _delta_class(d, metric)
            return f'<span class="{cls}">{_format_delta(d)}</span>'
        except (TypeError, ValueError, ZeroDivisionError):
            return ""

    # ------------------------------------------------------------------
    # Charts
    # ------------------------------------------------------------------

    def _render_charts(self) -> str:
        if not self.has_charts:
            return ""
        enabled = [(n, c) for n, c in self.cfg.charts.items() if c.enabled]
        if not enabled:
            return ""
        cards = []
        for name, cfg in enabled:
            content = self._chart_content(_chart_canvas_id(name))
            cards.append(self._card(html.escape(cfg.title or str(name)), content))
        return self._charts_grid(cards)

    # ------------------------------------------------------------------
    # Regression analysis
    # ------------------------------------------------------------------

    def _render_regression(self) -> str:
        summary = self._summary_html()
        rows = self._analysis_rows()
        table = self._table(["Metric", "Baseline", "Current", "Delta", "Status"], rows, num_columns=[1, 2, 3])
        return self._card("Regression Analysis", summary + table)

    def _summary_html(self) -> str:
        if self.analysis and self.analysis.get("summary"):
            s = self.analysis["summary"]
            return (
                '<div class="status-summary">'
                f'<span class="status-badge pass">{s.get("PASS", 0)} PASS</span>'
                f'<span class="status-badge warning">{s.get("WARNING", 0)} WARN</span>'
                f'<span class="status-badge fail">{s.get("DEGRADATION", 0)} FAIL</span>'
                f'<span class="status-badge nodata">{s.get("NO_DATA", 0)} NO DATA</span>'
                f'<span class="status-badge skip">{s.get("SKIP", 0)} SKIP</span>'
                "</div>"
            )
        if not self.baseline:
            return (
                '<div class="info-message">'
                "<strong>First run detected:</strong> No baseline available for comparison. "
                "This run will be used as baseline for future comparisons."
                "</div>"
            )
        return ""

    def _analysis_rows(self) -> List[str]:
        rows: List[str] = []
        if self.analysis and self.analysis.get("results"):
            for res in self.analysis["results"]:
                status_text = str(res.get("status", ""))
                delta = res.get("delta_percent")
                metric = str(res.get("metric", ""))
                delta_cls = _delta_class(delta, metric)
                rows.append(
                    f"<tr>"
                    f"<td>{html.escape(metric)}</td>"
                    f"<td class='num'>{_format_value(res.get('baseline'))}</td>"
                    f"<td class='num'>{_format_value(res.get('current'))}</td>"
                    f"<td class='num {delta_cls}'>{_format_delta(delta)}</td>"
                    f"<td class='{_status_class(status_text)}'>{html.escape(status_text)}</td>"
                    f"</tr>"
                )
        else:
            for key, value in self.current.items():
                rows.append(
                    f"<tr>"
                    f"<td>{html.escape(str(key))}</td>"
                    f"<td class='num'>-</td>"
                    f"<td class='num'>{_format_value(value)}</td>"
                    f"<td class='num'>-</td>"
                    f"<td class='status-skip'>SKIP</td>"
                    f"</tr>"
                )
        return rows

    # ------------------------------------------------------------------
    # Endpoint table
    # ------------------------------------------------------------------

    def _render_endpoints(self) -> str:
        if not self.endpoint_stats:
            return ""
        headers = [html.escape(c.label) for c in self.cfg.endpoint_columns]
        num_cols = [i for i, c in enumerate(self.cfg.endpoint_columns) if c.key != "name"]
        rows = self._endpoint_rows()
        return self._card("Endpoint Statistics", self._table(headers, rows, num_columns=num_cols))

    def _endpoint_rows(self) -> List[str]:
        rows: List[str] = []
        for ep in self.endpoint_stats:
            cells: List[str] = []
            for col in self.cfg.endpoint_columns:
                display_fn = _ENDPOINT_DISPLAY.get(col.key)
                display = display_fn(ep) if display_fn else "-"

                css_classes: List[str] = []
                if col.key == "name":
                    css_classes.append("endpoint-name")
                else:
                    css_classes.append("num")

                # Highlight
                if col.highlight and col.key != "name":
                    numeric_fn = _ENDPOINT_NUMERIC.get(col.key)
                    if numeric_fn:
                        try:
                            num = numeric_fn(ep)
                            fail_t = col.highlight.get("fail")
                            warn_t = col.highlight.get("warn")
                            if fail_t is not None and num >= fail_t:
                                css_classes.append("highlight-fail")
                            elif warn_t is not None and num >= warn_t:
                                css_classes.append("highlight-warn")
                        except (TypeError, ValueError):
                            pass

                cls_attr = f' class="{" ".join(css_classes)}"' if css_classes else ""
                cells.append(f"<td{cls_attr}>{display}</td>")
            rows.append(f"<tr>{''.join(cells)}</tr>")
        return rows

    # ------------------------------------------------------------------
    # Trends
    # ------------------------------------------------------------------

    def _render_trends(self) -> str:
        if len(self.history_runs) < 2:
            return ""
        n = len(self.history_runs)
        cards: List[str] = []
        for metric in self.cfg.trends.metrics:
            label = METRIC_LABELS.get(metric, metric)
            content = self._chart_content(_trend_canvas_id(metric))
            # `trends.metrics` is a list the user writes, and an unknown metric
            # is used as its own label — so this title is user text.
            cards.append(self._card(f"{html.escape(str(label))} — last {n} runs", content))
        if not cards:
            return ""
        return (
            '    <div class="section-label">Performance Trends</div>\n'
            + self._charts_grid(cards)
        )

    # ------------------------------------------------------------------
    # Footer
    # ------------------------------------------------------------------

    def _render_footer(self) -> str:
        name = html.escape(self.cfg.branding.name)
        if self.cfg.branding.name == "Locomotive":
            footer_text = (
                'Generated by <a href="https://github.com/loclocko/locomotive">Locomotive</a>'
                " \u2014 CI/CD Load Testing"
            )
        else:
            footer_text = (
                f'<span class="brand-name">{name}</span> \u00b7 '
                'Powered by <a href="https://github.com/loclocko/locomotive">Locomotive</a>'
            )
        return f'    <div class="footer">{footer_text}</div>'

    # ------------------------------------------------------------------
    # JavaScript
    # ------------------------------------------------------------------

    def _build_js(self) -> str:
        parts: List[str] = []

        # Main charts
        if self.has_charts:
            enabled = [(n, c) for n, c in self.cfg.charts.items() if c.enabled]
            if enabled:
                parts.append(f"const chartData = {_js_json(self.chart_data)};")
                for name, cfg in enabled:
                    parts.append(self._chart_init(_chart_canvas_id(name), cfg))

        # Trend charts
        if "trends" in self.cfg.sections and len(self.history_runs) >= 2:
            trends_json = self._build_trends_js_data()
            parts.append(f"const trendsData = {_js_json(trends_json)};")
            for metric in self.cfg.trends.metrics:
                parts.append(self._trend_chart_init(metric))

        if not parts:
            return ""
        # Without Chart.js (blocked CDN, offline runner) the fallback notes stay
        # and nothing throws; with it, they are removed before drawing.
        return (
            "  <script>\n"
            "    if (typeof Chart !== 'undefined') {\n"
            "    document.querySelectorAll('.chart-fallback').forEach(function (el) { el.remove(); });\n\n    "
            + "\n\n    ".join(parts)
            + "\n    }\n  </script>"
        )

    def _chart_init(self, canvas_id: str, cfg: ChartConfig) -> str:
        has_right = any(ds.y_axis == "right" for ds in cfg.datasets)
        datasets_str = ",\n          ".join(self._dataset_js(ds) for ds in cfg.datasets)

        if has_right:
            scales = (
                "scales: {\n"
                "          x: { title: { display: true, text: 'Time' } },\n"
                "          y: { type: 'linear', position: 'left', title: { display: true, text: 'Requests/s' }, min: 0 },\n"
                "          y1: { type: 'linear', position: 'right', title: { display: true, text: 'Users' }, grid: { drawOnChartArea: false }, min: 0 }\n"
                "        }"
            )
        else:
            y_label = "Response Time (ms)" if "response" in canvas_id.lower() else "Value"
            scales = (
                f"scales: {{\n"
                f"          x: {{ title: {{ display: true, text: 'Time' }} }},\n"
                f"          y: {{ title: {{ display: true, text: '{y_label}' }}, min: 0 }}\n"
                f"        }}"
            )

        return (
            f"new Chart(document.getElementById({_js_json(canvas_id)}), {{\n"
            f"      type: 'line',\n"
            f"      data: {{\n"
            f"        labels: chartData.labels.map(t => t + 's'),\n"
            f"        datasets: [\n"
            f"          {datasets_str}\n"
            f"        ]\n"
            f"      }},\n"
            f"      options: {{\n"
            f"        responsive: true, maintainAspectRatio: false,\n"
            f"        interaction: {{ mode: 'index', intersect: false }},\n"
            f"        plugins: {{ legend: {{ position: 'top' }} }},\n"
            f"        {scales}\n"
            f"      }}\n"
            f"    }});"
        )

    def _dataset_js(self, ds: ChartDatasetConfig) -> str:
        bg = f"{ds.color}26" if ds.fill else "transparent"
        dash = f", borderDash: {_js_json(ds.dash)}" if ds.dash else ""
        y_id = "y1" if ds.y_axis == "right" else "y"
        return (
            f"{{ label: {_js_json(ds.label)}, data: chartData.{ds.key}, "
            f"borderColor: {_js_json(ds.color)}, backgroundColor: {_js_json(bg)}, "
            f"fill: {'true' if ds.fill else 'false'}, tension: 0.3, "
            f"pointRadius: 0, pointHitRadius: 8, borderWidth: 2, "
            f"yAxisID: {_js_json(y_id)}{dash} }}"
        )

    def _build_trends_js_data(self) -> Dict[str, Any]:
        labels: List[str] = []
        for run in self.history_runs:
            started = run.get("started_at", "")
            try:
                date_part = started[:10]
                time_part = started[11:16]
                from datetime import datetime
                dt = datetime.strptime(date_part, "%Y-%m-%d")
                label = dt.strftime("%b %d") + " " + time_part
            except (ValueError, TypeError):
                label = str(run.get("run_id", "?"))[:8]
            labels.append(label)

        data: Dict[str, Any] = {"labels": labels}
        n = len(self.history_runs)
        for metric in self.cfg.trends.metrics:
            values = []
            for run in self.history_runs:
                v = run.get(metric)
                values.append(float(v) if v is not None else None)
            # Last point = current run → True, rest False
            is_current = [False] * n
            if n > 0:
                is_current[-1] = True
            data[metric] = {"values": values, "is_current": is_current}
        return data

    def _trend_chart_init(self, metric: str) -> str:
        color = METRIC_COLORS.get(metric, "#3b82f6")
        label = METRIC_LABELS.get(metric, metric)
        canvas_id = f"trendChart_{metric}"
        n = len(self.history_runs)

        point_colors = [f"{color}80"] * n
        point_sizes  = [3] * n
        if n > 0:
            point_colors[-1] = color
            point_sizes[-1]  = 7

        return (
            f"new Chart(document.getElementById({_js_json(canvas_id)}), {{\n"
            f"      type: 'line',\n"
            f"      data: {{\n"
            f"        labels: trendsData.labels,\n"
            f"        datasets: [{{\n"
            f"          label: {_js_json(label)},\n"
            f"          data: trendsData[{_js_json(metric)}].values,\n"
            f"          borderColor: {_js_json(color)},\n"
            f"          backgroundColor: {_js_json(color + '19')},\n"
            f"          pointBackgroundColor: {_js_json(point_colors)},\n"
            f"          pointRadius: {_js_json(point_sizes)},\n"
            f"          fill: true, tension: 0.3\n"
            f"        }}]\n"
            f"      }},\n"
            f"      options: {{\n"
            f"        responsive: true, maintainAspectRatio: false,\n"
            f"        interaction: {{ mode: 'index', intersect: false }},\n"
            f"        plugins: {{ legend: {{ position: 'top' }} }},\n"
            f"        scales: {{\n"
            f"          x: {{ title: {{ display: true, text: 'Run' }} }},\n"
            f"          y: {{ title: {{ display: true, text: {_js_json(label)} }}, min: 0 }}\n"
            f"        }}\n"
            f"      }}\n"
            f"    }});"
        )


# ---------------------------------------------------------------------------
# Public API (backward-compatible)
# ---------------------------------------------------------------------------

def render_report(
    run_meta: Dict[str, Any],
    current_metrics: Dict[str, Any],
    baseline_metrics: Optional[Dict[str, Any]],
    analysis: Optional[Dict[str, Any]],
    title: str,
    stats_history: Optional[List[Dict[str, Any]]] = None,
    endpoint_stats: Optional[List[Dict[str, Any]]] = None,
    report_config: Optional[ReportConfig] = None,
    history_runs: Optional[List[Dict[str, Any]]] = None,
    stylesheet: Optional[str] = None,
) -> str:
    if report_config is None:
        report_config = resolve_report_config({"title": title})
    elif title and report_config.title == "CI Load Test Report":
        report_config.title = title

    return ReportRenderer(
        config=report_config,
        run_meta=run_meta,
        current_metrics=current_metrics,
        baseline_metrics=baseline_metrics,
        analysis=analysis,
        stats_history=stats_history,
        endpoint_stats=endpoint_stats,
        history_runs=history_runs,
        stylesheet=stylesheet,
    ).render()


def render_stylesheet(report_config: Optional[ReportConfig] = None) -> str:
    """The report's CSS on its own, for the file linked from the report.

    Jenkins serves published reports with ``style-src 'self'``: the inline
    ``<style>`` block is refused, and the report used to open unstyled, but a
    stylesheet from the same directory is allowed.
    """
    config = report_config if report_config is not None else resolve_report_config({})
    return ReportRenderer(config, {}, {}, None, None)._build_css() + "\n"
