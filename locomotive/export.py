"""Results in the shapes CI systems and code review read.

The HTML report is for a person with a browser. A pull or merge request
comment is markdown, and JUnit XML is the one results format that GitLab (the
merge request test widget), Jenkins (the JUnit plugin, and GitHub Checks
through it) and most GitHub Actions reporters all display. Both are rendered
from the same analysis.json the report and the exit code come from, so a
comment cannot tell a different story from the build.
"""
from __future__ import annotations

import csv
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, List, Optional

# The metrics a reviewer reads first, in the order they read them.
SUMMARY_METRICS = (
    ("rps", "Throughput, req/s"),
    ("avg_ms", "Average, ms"),
    ("p95_ms", "p95, ms"),
    ("p99_ms", "p99, ms"),
    ("error_rate", "Errors, %"),
    ("requests", "Requests"),
)

STATUS_ICONS = {
    "PASS": "✅",
    "WARNING": "⚠️",
    "DEGRADATION": "❌",
    "FAILED": "❌",
    "NO_DATA": "⛔",
    "SKIP": "⏭️",
}

# Worst first: nobody should have to scroll to find what failed.
_STATUS_ORDER = {"FAILED": 0, "DEGRADATION": 0, "NO_DATA": 1, "WARNING": 2, "SKIP": 3, "PASS": 4}

_TRIPPED = ("FAILED", "DEGRADATION", "NO_DATA", "WARNING")
_MAX_TRIPPED = 3
_MAX_ENDPOINT_ROWS = 50


def _number(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _fmt(value: Any) -> str:
    num = _number(value)
    if num is None:
        return "-"
    if num.is_integer():
        return str(int(num))
    return f"{num:.2f}"


def locust_exit_result(code: Any) -> Optional[Dict[str, Any]]:
    """Locust's own exit code as a failed check, when it is not 0.

    The build fails on that code even when every threshold passes: Locust exits
    1 as soon as any request failed (its --exit-code-on-error default), and
    non-zero when the run broke. Without this check the summary and the JUnit
    file said PASS about a red build.
    """
    if code is None or isinstance(code, bool):
        return None
    try:
        value = int(code)
    except (TypeError, ValueError):
        return None
    if value == 0:
        return None
    if value == 1:
        reason = "Locust exited with 1: any failed request does that, and so does a run that broke"
    else:
        reason = f"Locust exited with {value}"
    return {
        "metric": "locust_exit_code", "mode": "run", "direction": "increase",
        "warn": None, "fail": None, "current": None, "baseline": None,
        "delta_percent": None, "status": "FAILED", "reason": reason,
    }


def overall_status(
    metrics: Optional[Dict[str, Any]],
    analysis: Optional[Dict[str, Any]],
    locust_exit_code: Any = None,
) -> str:
    """The status the build ends with, as the exit code decides it.

    ``verdict`` is set when regression rules are advisory and the gate decides.
    """
    analysis = analysis or {}
    if analysis.get("verdict") or analysis.get("status"):
        status = str(analysis.get("verdict") or analysis["status"])
    else:
        # No analysis and no metrics is a run that measured nothing; no
        # analysis with metrics is a run nothing was configured to judge.
        status = "PASS" if metrics else "NO_DATA"
    if locust_exit_result(locust_exit_code) and status in ("PASS", "WARNING", "SKIP"):
        return "FAILED"
    return status


def check_label(result: Dict[str, Any]) -> str:
    """``p95_ms (relative)``, ``error_rate (gate)``, ``requests``."""
    metric = str(result.get("metric") or "?")
    if metric.startswith("gate."):
        metric = metric[len("gate."):]
    mode = result.get("mode")
    if mode in ("relative", "absolute", "gate"):
        return f"{metric} ({mode})"
    return metric


def check_kind(result: Dict[str, Any]) -> str:
    mode = str(result.get("mode") or "")
    if mode in ("relative", "absolute"):
        return "regression"
    return mode or "check"


def _limits(result: Dict[str, Any]) -> str:
    mode = result.get("mode")
    increase = str(result.get("direction") or "increase") != "decrease"
    bounds: List[str] = []
    for name in ("warn", "fail"):
        value = _number(result.get(name))
        if value is None:
            continue
        if mode == "relative":
            bounds.append(f"{name} at {'+' if increase else '-'}{value:g}%")
        elif mode == "absolute":
            # analyzer: current >= limit for increase, <= for decrease.
            bounds.append(f"{name} at {'>=' if increase else '<='} {value:g}")
        elif mode == "gate":
            # gate: strictly above for increase, at or below for decrease.
            bounds.append(f"{name} if {'>' if increase else '<='} {value:g}")
    return ", ".join(bounds)


def describe_result(result: Dict[str, Any]) -> str:
    """One line: what the check measured, and where its limits are."""
    parts: List[str] = []
    current = _number(result.get("current"))
    baseline = _number(result.get("baseline"))
    delta = _number(result.get("delta_percent"))
    if current is not None:
        if result.get("mode") == "relative" and baseline is not None:
            text = f"{_fmt(current)} vs {_fmt(baseline)}"
            if delta is not None:
                text += f" ({delta:+.1f}%)"
        else:
            text = _fmt(current)
        parts.append(text)
    limits = _limits(result)
    if limits:
        parts.append(limits)
    if result.get("reason"):
        parts.append(str(result["reason"]))
    return "; ".join(parts)


def _checks(analysis: Optional[Dict[str, Any]], locust_exit_code: Any) -> List[Dict[str, Any]]:
    results = list((analysis or {}).get("results") or [])
    run_check = locust_exit_result(locust_exit_code)
    if run_check:
        results.append(run_check)
    return results


def load_endpoint_rows(stats_path: Path) -> List[Dict[str, str]]:
    """Rows of locust's stats CSV, with the Aggregated row last."""
    if not stats_path.is_file():
        return []
    with stats_path.open(newline="", encoding="utf-8") as handle:
        rows = [row for row in csv.DictReader(handle) if row.get("Name")]
    rows.sort(key=lambda row: row.get("Name") == "Aggregated")
    return rows


# ── markdown ──────────────────────────────────────────────────────────


def _cell(text: Any) -> str:
    """Text that cannot end a markdown table cell or row."""
    return (
        str(text)
        .replace("\\", "\\\\")
        .replace("|", "\\|")
        .replace("\r", " ")
        .replace("\n", " ")
    )


def _code(text: Any) -> str:
    return "`" + str(text).replace("`", "'") + "`"


def _link(label: str, url: Any) -> Optional[str]:
    if not isinstance(url, str) or not url.lower().startswith(("http://", "https://")):
        return None
    return f"[{label}]({url.replace(' ', '%20').replace(')', '%29')})"


def _change(current: Any, baseline: Any) -> str:
    cur, base = _number(current), _number(baseline)
    if cur is None or base is None or base == 0:
        return "-"
    return f"{(cur - base) / base * 100:+.1f}%"


def _endpoint_table(rows: List[Dict[str, str]]) -> List[str]:
    shown, hidden = rows, 0
    if len(rows) > _MAX_ENDPOINT_ROWS:
        # The Aggregated row sorts last and is the one worth keeping.
        shown = rows[: _MAX_ENDPOINT_ROWS - 1] + rows[-1:]
        hidden = len(rows) - len(shown)
    # A collapsed block: the per-endpoint breakdown is for whoever goes looking,
    # and it would push the verdict off the screen of a pull request.
    lines = [
        "<details><summary>Endpoints</summary>",
        "",
        "| Endpoint | Requests | Failures | Avg, ms | p95, ms | p99, ms | RPS |",
        "|:--|--:|--:|--:|--:|--:|--:|",
    ]
    for row in shown:
        name = row.get("Name", "")
        if name == "Aggregated":
            label = "**Total**"
        else:
            label = _cell(f"{(row.get('Type') or '').strip()} {name}".strip())
        lines.append(
            f"| {label} | {_fmt(row.get('Request Count'))} | {_fmt(row.get('Failure Count'))} "
            f"| {_fmt(row.get('Average Response Time'))} | {_fmt(row.get('95%'))} "
            f"| {_fmt(row.get('99%'))} | {_fmt(row.get('Requests/s'))} |"
        )
    if hidden:
        lines.append(f"| …and {hidden} more | | | | | | |")
    lines += ["", "</details>"]
    return lines


def render_markdown_summary(
    *,
    run_id: str,
    metrics: Optional[Dict[str, Any]],
    analysis: Optional[Dict[str, Any]],
    baseline_id: Optional[str] = None,
    baseline_metrics: Optional[Dict[str, Any]] = None,
    run_meta: Optional[Dict[str, Any]] = None,
    endpoints: Optional[List[Dict[str, str]]] = None,
    rules_configured: bool = True,
    locust_exit_code: Any = None,
    title: str = "Load test",
) -> str:
    """A pull/merge request comment or job summary for one run.

    *rules_configured* says whether regression rules exist; only then is a
    run without a baseline worth warning about, because only then did it skip
    a comparison someone asked for.
    """
    metrics = metrics or {}
    status = overall_status(metrics, analysis, locust_exit_code)
    compare = bool(baseline_id and baseline_metrics)
    # A run that compared nothing is not the same green as one that compared
    # and passed, and the heading is the only line many people read.
    uncompared = bool(metrics) and not compare and rules_configured and status in ("PASS", "WARNING")

    icon = STATUS_ICONS.get(status)
    heading = f"{title}: {status}"
    if uncompared:
        heading += ", nothing to compare against"
    lines: List[str] = [f"### {icon} {heading}" if icon else f"### {heading}", ""]

    context = [f"Run {_code(run_id)}"]
    if compare:
        context[0] += f" vs baseline {_code(baseline_id)}"
    meta = (run_meta or {}).get("meta")
    ci = meta.get("ci") if isinstance(meta, dict) else None
    if isinstance(ci, dict):
        if ci.get("branch"):
            branch = _code(ci["branch"])
            if ci.get("target_branch"):
                branch += f" → {_code(ci['target_branch'])}"
            context.append(branch)
        if ci.get("commit"):
            context.append(_code(str(ci["commit"])[:12]))
        build = _link("Build", ci.get("build_url"))
        if build:
            context.append(build)
    lines.append(" · ".join(context))

    recorded, verdict = (analysis or {}).get("status"), (analysis or {}).get("verdict")
    if verdict and recorded and verdict != recorded:
        lines += [
            "",
            f"Regression rules are advisory here (`rules_advisory`): they came out {recorded}, "
            "and only the gate decides the build.",
        ]

    if not metrics:
        lines += [
            "",
            "The run produced no metrics: Locust wrote no statistics. The build log says why.",
        ]
        return "\n".join(lines) + "\n"

    results = _checks(analysis, locust_exit_code)
    results.sort(key=lambda r: _STATUS_ORDER.get(str(r.get("status")), len(_STATUS_ORDER)))

    tripped = [r for r in results if r.get("status") in _TRIPPED]
    if tripped:
        described = [f"{_cell(check_label(r))}: {_cell(describe_result(r))}" for r in tripped[:_MAX_TRIPPED]]
        if len(tripped) > _MAX_TRIPPED:
            described.append(f"and {len(tripped) - _MAX_TRIPPED} more")
        lines += ["", f"**What tripped:** {'; '.join(described)}"]

    lines.append("")
    if compare:
        lines += ["| Metric | Baseline | Current | Change |", "|:--|--:|--:|--:|"]
    else:
        lines += ["| Metric | Current |", "|:--|--:|"]
    for key, label in SUMMARY_METRICS:
        if key not in metrics:
            continue
        if compare:
            base = baseline_metrics.get(key)
            lines.append(
                f"| {label} | {_fmt(base)} | {_fmt(metrics[key])} | {_change(metrics[key], base)} |"
            )
        else:
            lines.append(f"| {label} | {_fmt(metrics[key])} |")

    if uncompared:
        lines += [
            "",
            "> **No baseline was used**, so regression rules were skipped and only absolute "
            "thresholds ran: a slowdown against the previous run would not have been caught. "
            "On a first run that is expected; otherwise the baseline was missing, expired, or "
            "not downloaded, and the build log says which.",
        ]

    if results:
        lines += ["", "| Check | Status | Details |", "|:--|:--|:--|"]
        for res in results:
            lines.append(
                f"| {_cell(check_label(res))} | {_cell(res.get('status', ''))} "
                f"| {_cell(describe_result(res))} |"
            )
    else:
        lines += ["", "No rules or gate thresholds are configured, so nothing was checked."]

    if endpoints:
        lines += [""] + _endpoint_table(endpoints)
    return "\n".join(lines) + "\n"


# ── JUnit XML ─────────────────────────────────────────────────────────


def _is_failure(status: str, fail_on: str) -> bool:
    if status in ("FAILED", "DEGRADATION", "NO_DATA"):
        return True
    return status == "WARNING" and fail_on == "WARNING"


def render_junit(
    *,
    run_id: str,
    metrics: Optional[Dict[str, Any]],
    analysis: Optional[Dict[str, Any]],
    fail_on: str = "DEGRADATION",
    locust_exit_code: Any = None,
) -> str:
    """Every check as a test case: failures are what would fail the build.

    A WARNING is a failure only under ``fail_on: WARNING``; otherwise it passes
    and says so in its output. Anything that fails a rule is still reported as
    a failure under ``rules_advisory`` — the file describes the checks, the
    exit code decides the build. A non-zero exit from Locust is a failed
    ``locust_exit_code`` case, since it fails the build on its own.
    """
    analysis_results = list((analysis or {}).get("results") or [])
    results = _checks(analysis, locust_exit_code)
    suites = ET.Element("testsuites", {"name": "locomotive"})
    suite = ET.SubElement(suites, "testsuite", {"name": "locomotive"})
    props = ET.SubElement(suite, "properties")
    ET.SubElement(props, "property", {"name": "run_id", "value": str(run_id)})

    failures = skipped = 0

    def case(classname: str, name: str) -> ET.Element:
        return ET.SubElement(suite, "testcase", {"classname": classname, "name": name, "time": "0"})

    if not metrics:
        tc = case("locomotive.run", "metrics")
        failure = ET.SubElement(tc, "failure", {"type": "NO_DATA", "message": "the run produced no metrics"})
        failure.text = "Locust wrote no statistics; the build log says why."
        failures += 1
    elif not analysis_results:
        tc = case("locomotive.run", "checks")
        ET.SubElement(tc, "skipped", {"message": "no rules or gate thresholds configured"})
        skipped += 1

    for res in results:
        status = str(res.get("status") or "")
        detail = describe_result(res)
        tc = case(f"locomotive.{check_kind(res)}", check_label(res))
        if _is_failure(status, fail_on):
            failure = ET.SubElement(tc, "failure", {"type": status, "message": f"{status}: {detail}"})
            failure.text = detail
            failures += 1
        elif status == "SKIP":
            ET.SubElement(tc, "skipped", {"message": detail or "skipped"})
            skipped += 1
        else:
            out = ET.SubElement(tc, "system-out")
            out.text = f"{status}: {detail}" if detail else status

    counts = {
        "tests": str(len(suite.findall("testcase"))),
        "failures": str(failures),
        "errors": "0",
        "skipped": str(skipped),
    }
    for element in (suites, suite):
        for key, value in counts.items():
            element.set(key, value)
    ET.indent(suites)
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(suites, encoding="unicode") + "\n"
