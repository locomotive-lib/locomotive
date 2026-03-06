from __future__ import annotations

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


def summarize_history(history: List[Dict[str, Any]], warmup_seconds: int) -> Optional[Dict[str, float]]:
    if not history:
        return None
    start_ts = history[0].get("timestamp")
    if start_ts is None:
        start_ts = 0.0
    total_requests = 0.0
    total_failures = 0.0
    for idx, row in enumerate(history):
        timestamp = row.get("timestamp")
        if timestamp is None:
            timestamp = float(idx)
        if timestamp - start_ts < warmup_seconds:
            continue
        rps = _safe_float(row.get("rps")) or 0.0
        failures_s = _safe_float(row.get("failures_s")) or 0.0
        total_requests += rps
        total_failures += failures_s
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
        result["status"] = "SKIP"
        result["reason"] = skip_reason or "gate not eligible"
        return result

    if current is None:
        result["status"] = "SKIP"
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

    eligible = True
    skip_reason = None
    if warmup_seconds and history_summary is None:
        eligible = False
        skip_reason = "missing stats history for warmup"
    if min_requests is not None and (requests_used is None or requests_used < min_requests):
        eligible = False
        skip_reason = "min_requests not met"

    results = []
    for metric_name, rule in thresholds.items():
        current = _safe_float(gate_metrics.get(metric_name))
        results.append(_evaluate_threshold(metric_name, current, rule, mode, eligible, skip_reason))

    combined = merge_results([results])
    combined["gate"] = {
        "mode": mode,
        "min_requests": min_requests,
        "warmup_seconds": warmup_seconds,
        "requests_used": requests_used,
        "failures_used": failures_used,
        "evaluated_at": utc_now(),
    }
    return combined
