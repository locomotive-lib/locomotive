"""Static validation for Locomotive configs.

``loco validate`` checks a config without running Locust and reports all
problems at once — structural mistakes as errors, likely-but-not-fatal issues
as warnings — with precise locations and friendly messages.

The request-traversal here (``iter_requests``) is intentionally reusable so
``loco diff`` can walk the same config structure.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Iterator, List, Tuple

from .scenario import DATA_MODES

_VAR_RE = re.compile(r"\$\{var:([^}]+)\}")
_DATA_RE = re.compile(r"\$\{data:([^.}]+)(?:\.[^}]*)?\}")

_HTTP_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
_AUTH_TYPES = {"bearer", "basic", "api_key"}

KNOWN_METRICS = {
    "rps", "avg_ms", "median_ms", "min_ms", "max_ms", "p95_ms", "p99_ms",
    "error_rate", "error_rate_4xx", "error_rate_5xx", "error_rate_503",
    "error_rate_non_503", "requests", "failures", "failures_4xx",
    "failures_5xx", "failures_503", "failures_non_503",
}

ERROR = "error"
WARNING = "warning"


class Issue:
    __slots__ = ("level", "location", "message")

    def __init__(self, level: str, location: str, message: str) -> None:
        self.level = level
        self.location = location
        self.message = message

    def __repr__(self) -> str:
        return f"Issue({self.level!r}, {self.location!r}, {self.message!r})"


# ── reusable traversal ────────────────────────────────────────────────


def iter_scenarios(config: Dict[str, Any]) -> Iterator[Tuple[str, Dict[str, Any]]]:
    """Yield (location, scenario_dict) for the single scenario or each persona."""
    users = config.get("users")
    if isinstance(users, list) and users:
        for idx, entry in enumerate(users, start=1):
            if isinstance(entry, dict):
                scenario = entry.get("scenario")
                if not isinstance(scenario, dict):
                    scenario = {k: v for k, v in entry.items() if k not in ("weight", "name", "scenario")}
                yield f"users[{idx}]", scenario
        return
    scenario = config.get("scenario")
    if isinstance(scenario, dict):
        yield "scenario", scenario


def iter_requests(scenario: Dict[str, Any], prefix: str = "scenario") -> Iterator[Tuple[str, Dict[str, Any]]]:
    """Yield (location, request_dict) for on_start, on_stop, flows steps, requests."""
    for section in ("on_start", "on_stop"):
        entries = scenario.get(section)
        if isinstance(entries, list):
            for i, req in enumerate(entries, start=1):
                if isinstance(req, dict):
                    yield f"{prefix}.{section}[{i}]", req
    flows = scenario.get("flows")
    if isinstance(flows, list):
        for fi, flow in enumerate(flows, start=1):
            if isinstance(flow, dict) and isinstance(flow.get("steps"), list):
                for si, step in enumerate(flow["steps"], start=1):
                    if isinstance(step, dict):
                        yield f"{prefix}.flows[{fi}].steps[{si}]", step
    requests = scenario.get("requests")
    if isinstance(requests, list):
        for i, req in enumerate(requests, start=1):
            if isinstance(req, dict):
                yield f"{prefix}.requests[{i}]", req


# ── helpers ───────────────────────────────────────────────────────────


def _walk_strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _walk_strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _walk_strings(v)


def _is_int(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    if isinstance(value, str):
        try:
            int(value)
            return True
        except ValueError:
            return False
    return False


# ── validation ────────────────────────────────────────────────────────


def _validate_request(req: Dict[str, Any], loc: str, issues: List[Issue]) -> None:
    path = req.get("path")
    if not isinstance(path, str) or not path.strip():
        issues.append(Issue(ERROR, loc, "missing required 'path' (string)"))
    method = req.get("method", "GET")
    if isinstance(method, str) and method.upper() not in _HTTP_METHODS:
        issues.append(Issue(WARNING, loc, f"unusual HTTP method {method!r}"))
    if "weight" in req and not _is_int(req["weight"]):
        issues.append(Issue(ERROR, loc, "'weight' must be an integer"))
    for key in ("headers", "query", "json_headers"):
        if key in req and req[key] is not None and not isinstance(req[key], dict):
            issues.append(Issue(ERROR, loc, f"'{key}' must be an object"))
    if "tags" in req and not isinstance(req["tags"], list):
        issues.append(Issue(ERROR, loc, "'tags' must be a list"))
    capture = req.get("capture")
    if capture is not None and not isinstance(capture, dict):
        issues.append(Issue(ERROR, loc, "'capture' must be an object {name: json.path}"))
    if "expect" in req:
        _validate_expect(req["expect"], f"{loc}.expect", issues)


_EXPECT_KEYS = {"status", "contains", "json", "max_ms"}


def _validate_expect(expect: Any, loc: str, issues: List[Issue]) -> None:
    if not isinstance(expect, dict):
        issues.append(Issue(ERROR, loc, "'expect' must be an object with keys "
                                        "status/contains/json/max_ms"))
        return
    unknown = [k for k in expect if k not in _EXPECT_KEYS and not str(k).startswith("_")]
    if unknown:
        issues.append(Issue(WARNING, loc, f"unknown 'expect' key(s) {sorted(unknown)}; "
                                          f"known keys are {sorted(_EXPECT_KEYS)}"))
    if "status" in expect:
        status = expect["status"]
        codes = status if isinstance(status, list) else [status]
        if not codes or not all(_is_int(c) for c in codes):
            issues.append(Issue(ERROR, loc, "'expect.status' must be an integer or list of integers"))
    if "contains" in expect:
        contains = expect["contains"]
        items = contains if isinstance(contains, list) else [contains]
        if not items or not all(isinstance(s, str) for s in items):
            issues.append(Issue(ERROR, loc, "'expect.contains' must be a string or list of strings"))
    if "json" in expect and not isinstance(expect["json"], dict):
        issues.append(Issue(ERROR, loc, "'expect.json' must be an object {dot.path: expected}"))
    if "max_ms" in expect:
        max_ms = expect["max_ms"]
        ok = isinstance(max_ms, (int, float)) and not isinstance(max_ms, bool)
        if not ok:
            try:
                float(max_ms)
                ok = True
            except (TypeError, ValueError):
                ok = False
        if not ok:
            issues.append(Issue(ERROR, loc, "'expect.max_ms' must be a number (milliseconds)"))


def _validate_data(data: Any, loc: str, issues: List[Issue]) -> None:
    if not isinstance(data, dict):
        issues.append(Issue(ERROR, loc, "'data' must be an object of pool specs"))
        return
    for name, spec in data.items():
        ploc = f"{loc}.{name}"
        if not isinstance(spec, dict):
            issues.append(Issue(ERROR, ploc, "pool spec must be an object"))
            continue
        has_source = isinstance(spec.get("source"), str) and spec["source"].strip()
        inline = spec.get("inline")
        generate = spec.get("generate")
        if generate is not None:
            if not isinstance(generate, dict) or not isinstance(generate.get("fields"), dict) or not generate.get("fields"):
                issues.append(Issue(ERROR, ploc, "'generate.fields' must be a non-empty object"))
            if "count" in generate and not _is_int(generate["count"]):
                issues.append(Issue(ERROR, ploc, "'generate.count' must be an integer"))
        elif inline is not None:
            if not isinstance(inline, list) or not all(isinstance(r, dict) for r in inline):
                issues.append(Issue(ERROR, ploc, "'inline' must be a list of objects"))
        elif not has_source:
            issues.append(Issue(ERROR, ploc, "must define 'source', 'inline', or 'generate'"))
        mode = spec.get("mode", "unique_per_user")
        if mode not in DATA_MODES:
            issues.append(Issue(ERROR, ploc, f"'mode' must be one of {sorted(DATA_MODES)}, got {mode!r}"))


def _validate_auth(auth: Any, loc: str, issues: List[Issue]) -> None:
    if not isinstance(auth, dict):
        issues.append(Issue(ERROR, loc, "'auth' must be an object"))
        return
    atype = str(auth.get("type", "")).lower()
    if atype not in _AUTH_TYPES:
        issues.append(Issue(ERROR, loc, f"'auth.type' must be one of {sorted(_AUTH_TYPES)}, got {auth.get('type')!r}"))
    elif atype == "api_key" and not auth.get("header"):
        issues.append(Issue(WARNING, loc, "api_key auth without 'header' defaults to 'X-API-Key'"))


def _validate_analysis(analysis: Any, issues: List[Issue]) -> None:
    if not isinstance(analysis, dict):
        return
    rules = analysis.get("rules")
    if isinstance(rules, list):
        for i, rule in enumerate(rules, start=1):
            loc = f"analysis.rules[{i}]"
            if not isinstance(rule, dict):
                issues.append(Issue(ERROR, loc, "rule must be an object"))
                continue
            if rule.get("metric") not in KNOWN_METRICS:
                issues.append(Issue(WARNING, loc, f"unknown metric {rule.get('metric')!r}"))
            if rule.get("mode") not in ("relative", "absolute"):
                issues.append(Issue(ERROR, loc, "'mode' must be 'relative' or 'absolute'"))
            if rule.get("direction") not in ("increase", "decrease"):
                issues.append(Issue(ERROR, loc, "'direction' must be 'increase' or 'decrease'"))
    gate = analysis.get("gate")
    if isinstance(gate, dict):
        thresholds = gate.get("thresholds")
        if isinstance(thresholds, dict):
            for metric in thresholds:
                if metric not in KNOWN_METRICS:
                    issues.append(Issue(WARNING, f"analysis.gate.thresholds", f"unknown metric {metric!r}"))


def validate_config(config: Dict[str, Any]) -> List[Issue]:
    """Return all validation issues (errors and warnings) for a config."""
    issues: List[Issue] = []
    if not isinstance(config, dict):
        return [Issue(ERROR, "<root>", "config must be an object")]

    load = config.get("load")
    if not isinstance(load, dict):
        issues.append(Issue(WARNING, "load", "missing 'load' section"))
        load = {}
    for field in ("host", "users", "spawn_rate", "run_time"):
        if field not in load and not load.get("locustfile"):
            issues.append(Issue(WARNING, "load", f"'{field}' missing (required for a run unless passed via CLI)"))
    for field in ("users", "spawn_rate", "stop_timeout"):
        if field in load and not _is_int(load[field]):
            issues.append(Issue(ERROR, "load", f"'{field}' must be an integer"))

    scenarios = list(iter_scenarios(config))
    has_source = bool(scenarios) or bool(load.get("locustfile"))
    if not has_source:
        issues.append(Issue(
            ERROR, "<root>",
            "no scenario source: define scenario.requests / scenario.flows, "
            "a 'users' list, or load.locustfile",
        ))

    for scope, scenario in scenarios:
        reqs = list(iter_requests(scenario, scope))
        has_reqs = bool(scenario.get("requests")) or bool(scenario.get("flows"))
        if not has_reqs and not load.get("locustfile"):
            issues.append(Issue(ERROR, scope, "scenario has no 'requests' or 'flows'"))

        for loc, req in reqs:
            _validate_request(req, loc, issues)

        if "data" in scenario:
            _validate_data(scenario["data"], f"{scope}.data", issues)
        if "auth" in scenario:
            _validate_auth(scenario["auth"], f"{scope}.auth", issues)

        # semantic: captured vars, data pools, auth requirement
        captured = set()
        for _loc, req in reqs:
            cap = req.get("capture")
            if isinstance(cap, dict):
                captured.update(str(k) for k in cap)
        declared_pools = set(scenario["data"].keys()) if isinstance(scenario.get("data"), dict) else set()
        has_auth = isinstance(scenario.get("auth"), dict)

        for loc, req in reqs:
            for text in _walk_strings(req):
                for var in _VAR_RE.findall(text):
                    if var not in captured:
                        issues.append(Issue(WARNING, loc, f"references ${{var:{var}}} but nothing captures {var!r}"))
                for pool in _DATA_RE.findall(text):
                    if pool not in declared_pools:
                        issues.append(Issue(WARNING, loc, f"references data pool {pool!r} not declared in {scope}.data"))
            if req.get("_requires_auth") and not has_auth:
                issues.append(Issue(WARNING, loc, "endpoint needs auth but no 'auth' block is configured"))

    _validate_analysis(config.get("analysis"), issues)

    # De-duplicate identical issues (same var referenced in many strings).
    seen = set()
    unique: List[Issue] = []
    for issue in issues:
        key = (issue.level, issue.location, issue.message)
        if key not in seen:
            seen.add(key)
            unique.append(issue)
    return unique


def format_issues(issues: List[Issue]) -> str:
    """Human-readable report of validation issues."""
    if not issues:
        return "✓ Config is valid."
    errors = [i for i in issues if i.level == ERROR]
    warnings = [i for i in issues if i.level == WARNING]
    lines: List[str] = []
    for issue in errors:
        lines.append(f"  ERROR   {issue.location}: {issue.message}")
    for issue in warnings:
        lines.append(f"  WARNING {issue.location}: {issue.message}")
    summary = f"{len(errors)} error(s), {len(warnings)} warning(s)"
    lines.append(summary)
    return "\n".join(lines)
