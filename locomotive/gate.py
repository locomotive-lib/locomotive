from __future__ import annotations

import statistics
from typing import Any, Dict, List, Optional

from .analyzer import merge_results
from .utils import utc_now


ERROR_METRICS = {
    "error_rate",
    "error_rate_4xx",
    "error_rate_5xx",
    "error_rate_503",
    "error_rate_non_503",
    "failures",
    "failures_4xx",
    "failures_5xx",
    "failures_503",
    "failures_non_503",
}


def _safe_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_int(value: Any) -> Optional[int]:
    num = _safe_float(value)
    if num is None:
        return None
    return int(num)


def _parse_thresholds(raw: Any) -> Dict[str, Dict[str, Any]]:
    if not isinstance(raw, dict):
        return {}
    thresholds: Dict[str, Dict[str, Any]] = {}
    for metric, cfg in raw.items():
        if isinstance(cfg, dict):
            thresholds[str(metric)] = cfg
        elif cfg is not None:
            thresholds[str(metric)] = {"fail": cfg}
    return thresholds


def _row_timestamps(history: List[Dict[str, Any]]) -> List[float]:
    """The timestamp of each row, with the row index standing in when absent."""
    stamps: List[float] = []
    for idx, row in enumerate(history):
        stamp = _safe_float(row.get("timestamp"))
        stamps.append(float(idx) if stamp is None else stamp)
    return stamps


def _row_durations(stamps: List[float]) -> List[float]:
    """How many seconds each stats-history row covers.

    ``rps`` is a rate, so turning it into a request count means multiplying by
    the length of the window it describes — and that window is a setting, not
    a constant. Locust writes one row per CSV interval, so a run recorded at
    five-second intervals used to report a fifth of the requests it actually
    made, which is enough for ``min_requests`` to reject a perfectly healthy
    run as too small to judge.
    """
    if not stamps:
        return []
    gaps = [later - earlier for earlier, later in zip(stamps, stamps[1:]) if later > earlier]
    typical = statistics.median(gaps) if gaps else 1.0
    durations: List[float] = []
    for idx in range(len(stamps)):
        # A row is stamped at the end of the window it summarises, so its own
        # width is the distance back to the row before it. The first row has
        # nothing before it and takes the typical width.
        gap = stamps[idx] - stamps[idx - 1] if idx else typical
        if gap <= 0:
            # Duplicate stamps, a clock stepping backwards, two runs merged
            # into one file: none of it says anything about the window.
            gap = typical
        elif gap > typical * 2:
            # A hole in the history means locust stopped reporting, not that
            # this one sample covers the whole silence. Crediting it for the
            # gap would invent requests nobody measured.
            gap = typical
        durations.append(gap)
    return durations


def summarize_history(history: List[Dict[str, Any]], warmup_seconds: int) -> Optional[Dict[str, float]]:
    if not history:
        return None
    stamps = _row_timestamps(history)
    durations = _row_durations(stamps)
    start_ts = stamps[0]
    total_requests = 0.0
    total_failures = 0.0
    for idx, row in enumerate(history):
        if stamps[idx] - start_ts < warmup_seconds:
            continue
        seconds = durations[idx]
        rps = _safe_float(row.get("rps")) or 0.0
        failures_s = _safe_float(row.get("failures_s")) or 0.0
        total_requests += rps * seconds
        total_failures += failures_s * seconds
    if total_requests <= 0:
        return {
            "requests": 0.0,
            "failures": total_failures,
            "error_rate": None,
        }
    return {
        "requests": total_requests,
        "failures": total_failures,
        "error_rate": total_failures / total_requests * 100,
    }


def _evaluate_threshold(
    metric_name: str,
    current: Optional[float],
    rule: Dict[str, Any],
    mode: str,
    eligible: bool,
    skip_reason: Optional[str],
) -> Dict[str, Any]:
    warn = _safe_float(rule.get("warn"))
    fail = _safe_float(rule.get("fail"))
    direction = str(rule.get("direction") or "increase").lower()

    if warn is None and metric_name in ERROR_METRICS and fail is not None and mode == "resilience":
        warn = 0.0

    result = {
        "metric": f"gate.{metric_name}",
        "mode": "gate",
        "direction": direction,
        "warn": warn,
        "fail": fail,
        "current": current,
        "baseline": None,
        "delta_percent": None,
        "status": "PASS",
        "reason": None,
    }

    if not eligible:
        # A gate that could not evaluate is not a gate that passed. Reporting
        # this as SKIP is how a run with 0 requests, or one that never met
        # min_requests, used to come out green.
        result["status"] = "NO_DATA"
        result["reason"] = skip_reason or "gate not eligible"
        return result

    if current is None:
        result["status"] = "NO_DATA"
        result["reason"] = "missing current value"
        return result

    if warn is None and fail is None:
        result["status"] = "SKIP"
        result["reason"] = "missing thresholds"
        return result

    if direction == "decrease":
        if fail is not None and current <= fail:
            result["status"] = "DEGRADATION"
        elif warn is not None and current <= warn:
            result["status"] = "WARNING"
        return result

    # For "increase" direction, use strict inequality (>) to avoid false positives
    # when current value equals threshold (e.g., error_rate_non_503 = 0 with fail = 0)
    if fail is not None and current > fail:
        result["status"] = "DEGRADATION"
    elif warn is not None and current > warn:
        result["status"] = "WARNING"
    return result


def evaluate_gate(
    metrics: Dict[str, Any],
    gate_cfg: Dict[str, Any],
    mode: str,
    history_summary: Optional[Dict[str, float]] = None,
) -> Optional[Dict[str, Any]]:
    thresholds = _parse_thresholds(gate_cfg.get("thresholds"))
    if not thresholds:
        return None

    min_requests = _safe_int(gate_cfg.get("min_requests"))
    warmup_seconds = _safe_int(gate_cfg.get("warmup_seconds"))

    gate_metrics = dict(metrics)
    requests_used = _safe_int(gate_metrics.get("requests"))
    failures_used = _safe_int(gate_metrics.get("failures"))

    if warmup_seconds and history_summary:
        if history_summary.get("requests") is not None:
            requests_used = int(round(history_summary["requests"]))
            gate_metrics["requests"] = requests_used
        if history_summary.get("failures") is not None:
            failures_used = int(round(history_summary["failures"]))
            gate_metrics["failures"] = failures_used
        if requests_used:
            gate_metrics["error_rate"] = (failures_used or 0) / requests_used * 100

    # A run has to record at least one request before any threshold means
    # anything, so min_requests defaults to 1 rather than to "no floor".
    effective_min = min_requests if min_requests is not None else 1

    eligible = True
    skip_reason = None
    if warmup_seconds and history_summary is None:
        eligible = False
        skip_reason = "missing stats history for warmup"
    if requests_used is None:
        eligible = False
        skip_reason = "no request count in metrics"
    elif requests_used <= 0:
        eligible = False
        skip_reason = "run recorded 0 requests"
    elif requests_used < effective_min:
        eligible = False
        skip_reason = f"min_requests not met ({requests_used} < {effective_min})"

    results = []
    for metric_name, rule in thresholds.items():
        current = _safe_float(gate_metrics.get(metric_name))
        results.append(_evaluate_threshold(metric_name, current, rule, mode, eligible, skip_reason))

    combined = merge_results([results])
    combined["gate"] = {
        "mode": mode,
        "min_requests": min_requests,
        "min_requests_effective": effective_min,
        "warmup_seconds": warmup_seconds,
        "requests_used": requests_used,
        "failures_used": failures_used,
        "evaluated_at": utc_now(),
    }
    return combined
