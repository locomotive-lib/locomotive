from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from .utils import utc_now


@dataclass
class Rule:
    metric: str
    mode: str
    direction: str
    warn: float
    fail: float


# SKIP is deliberately absent: it means "this check did not apply" (typically a
# first run with no baseline yet) and must never raise the overall status.
# NO_DATA is different — it means the check *should* have run but there was
# nothing to measure, which is a failure of the run, not an exemption.
STATUS_SEVERITY = {
    "PASS": 0,
    "WARNING": 1,
    "NO_DATA": 2,
    "DEGRADATION": 3,
}

STATUS_NAMES = ("PASS", "WARNING", "NO_DATA", "DEGRADATION", "SKIP")


def _summarize(results: List[Dict[str, Any]]) -> Dict[str, int]:
    return {
        name: sum(1 for res in results if res.get("status") == name)
        for name in STATUS_NAMES
    }


def no_data_result(metric: str, reason: str) -> Dict[str, Any]:
    """A result marking that a check could not be evaluated for lack of data."""
    return {
        "metric": metric,
        "mode": "sanity",
        "direction": "increase",
        "warn": None,
        "fail": None,
        "current": None,
        "baseline": None,
        "delta_percent": None,
        "status": "NO_DATA",
        "reason": reason,
    }


def sanity_results(metrics: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Flag a run that produced nothing worth analysing.

    Without this, a run where locust never reached the target still yields a
    stats CSV full of zeros, every latency threshold passes because 0 < limit,
    and the build goes green on a test that never happened.
    """
    requests = _safe_float((metrics or {}).get("requests"))
    if requests is None:
        return [no_data_result("requests", "run produced no request metrics")]
    if requests <= 0:
        return [no_data_result("requests", "run recorded 0 requests")]
    return []


def _load_generators(run_meta: Optional[Dict[str, Any]]) -> Optional[int]:
    topology = (run_meta or {}).get("topology")
    if not isinstance(topology, dict):
        return None
    try:
        return int(topology.get("load_generators") or 1)
    except (TypeError, ValueError):
        return None


def topology_results(
    run_meta: Optional[Dict[str, Any]],
    baseline_meta: Optional[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Warn when this run and its baseline used different numbers of processes.

    Comparing throughput across a change in load generators measures the
    change in load generators. The regression rules cannot see it — the
    metrics look the same shape either way — so a run that went from one
    process to eight reports a magnificent RPS improvement and a p95
    regression, and neither number is about the code.

    A warning rather than an error: re-baselining after deliberately scaling
    the generator up is exactly the right move, and the run that does it has
    to be allowed to happen.
    """
    current = _load_generators(run_meta)
    baseline = _load_generators(baseline_meta)
    if current is None or baseline is None or current == baseline:
        return []
    return [{
        "metric": "load_generators",
        "mode": "sanity",
        "direction": "increase",
        "warn": None,
        "fail": None,
        "current": current,
        "baseline": baseline,
        "delta_percent": None,
        "status": "WARNING",
        "reason": (
            f"this run used {current} load generator(s), the baseline used "
            f"{baseline}; throughput and latency are not comparable across "
            "that change — re-baseline on the new topology"
        ),
    }]


def load_rules(data: Optional[Dict[str, Any]] = None) -> List[Rule]:
    if not data:
        return []
    rules_raw = data.get("rules") if isinstance(data, dict) else None
    if not isinstance(rules_raw, list):
        return []
    rules: List[Rule] = []
    for item in rules_raw:
        if not isinstance(item, dict):
            continue
        rule = Rule(
            metric=str(item.get("metric")),
            mode=str(item.get("mode")),
            direction=str(item.get("direction")),
            warn=float(item.get("warn")),
            fail=float(item.get("fail")),
        )
        rules.append(rule)
    return rules


def _safe_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _relative_change(current: float, baseline: float, direction: str) -> Tuple[Optional[float], Optional[float]]:
    if baseline == 0:
        return None, None
    delta = (current - baseline) / baseline * 100
    if direction == "increase":
        magnitude = max(0.0, delta)
    else:
        magnitude = max(0.0, -delta)
    return delta, magnitude


def evaluate_rule(rule: Rule, current: Dict[str, Any], baseline: Dict[str, Any]) -> Dict[str, Any]:
    current_value = _safe_float(current.get(rule.metric))
    baseline_value = _safe_float(baseline.get(rule.metric))

    result = {
        "metric": rule.metric,
        "mode": rule.mode,
        "direction": rule.direction,
        "warn": rule.warn,
        "fail": rule.fail,
        "current": current_value,
        "baseline": baseline_value,
        "delta_percent": None,
        "status": "PASS",
        "reason": None,
    }

    if current_value is None:
        # The metric this rule names was never measured — either the run
        # produced nothing or the metric name is wrong. Both are failures to
        # check, not exemptions from checking.
        result["status"] = "NO_DATA"
        result["reason"] = "missing current value"
        return result

    if rule.mode == "relative":
        if baseline_value in (None, 0):
            result["status"] = "SKIP"
            result["reason"] = "missing baseline value"
            return result
        delta, magnitude = _relative_change(current_value, baseline_value, rule.direction)
        result["delta_percent"] = delta
        if magnitude is None:
            result["status"] = "SKIP"
            result["reason"] = "unable to compute relative change"
            return result
        if magnitude >= rule.fail:
            result["status"] = "DEGRADATION"
        elif magnitude >= rule.warn:
            result["status"] = "WARNING"
        return result

    if rule.mode == "absolute":
        if rule.direction == "increase":
            if current_value >= rule.fail:
                result["status"] = "DEGRADATION"
            elif current_value >= rule.warn:
                result["status"] = "WARNING"
        else:
            if current_value <= rule.fail:
                result["status"] = "DEGRADATION"
            elif current_value <= rule.warn:
                result["status"] = "WARNING"
        if baseline_value is not None and baseline_value != 0:
            result["delta_percent"] = (current_value - baseline_value) / baseline_value * 100
        return result

    result["status"] = "SKIP"
    result["reason"] = "unsupported rule mode"
    return result


def analyze(current: Dict[str, Any], baseline: Dict[str, Any], rules: List[Rule]) -> Dict[str, Any]:
    results = [evaluate_rule(rule, current, baseline) for rule in rules]

    worst = "PASS"
    for res in results:
        status = res["status"]
        if status in STATUS_SEVERITY and STATUS_SEVERITY[status] > STATUS_SEVERITY[worst]:
            worst = status

    return {
        "status": worst,
        "evaluated_at": utc_now(),
        "summary": _summarize(results),
        "results": results,
    }


def merge_results(result_sets: List[List[Dict[str, Any]]]) -> Dict[str, Any]:
    results: List[Dict[str, Any]] = []
    for items in result_sets:
        if items:
            results.extend(items)

    worst = "PASS"
    for res in results:
        status = res.get("status")
        if status in STATUS_SEVERITY and STATUS_SEVERITY[status] > STATUS_SEVERITY[worst]:
            worst = status

    return {
        "status": worst,
        "evaluated_at": utc_now(),
        "summary": _summarize(results),
        "results": results,
    }
