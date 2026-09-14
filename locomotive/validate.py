"""Static validation for Locomotive configs.

``loco validate`` checks a config without running Locust and reports all
problems at once — structural mistakes as errors, likely-but-not-fatal issues
as warnings — with precise locations and friendly messages.

The request-traversal here (``iter_requests``) is intentionally reusable so
``loco diff`` can walk the same config structure.
"""
from __future__ import annotations

import csv
import json
import os
import re
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Set, Tuple

from .launcher import parse_duration
from .report_config import css_name, css_value
from .scenario import DATA_MODES, _POOL_NAME_RE

_VAR_RE = re.compile(r"\$\{var:([^}]+)\}")
_DATA_RE = re.compile(r"\$\{data:([^.}]+)(?:\.([^}]*))?\}")
_ENV_REF_RE = re.compile(r"\$\{env:([^}]+)\}")
# The bare `$NAME` form is no longer substituted. Only shout about it when the
# name looks like an environment variable *and* is actually set — otherwise
# every `$ref` in a JSON-Schema body would be flagged.
_BARE_ENV_RE = re.compile(r"(?<![\w$])\$([A-Z][A-Z0-9_]+)")

_HTTP_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
_AUTH_TYPES = {"bearer", "basic", "api_key"}

# Duplicated from cli.FAIL_ON_LEVELS rather than imported: cli imports this
# module, and a validator that cannot be imported without the CLI is a
# validator nobody can call from a test.
_FAIL_ON_LEVELS = ("WARNING", "DEGRADATION")

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


def _iter_located_strings(node: Any, prefix: str = "") -> Iterator[Tuple[str, str]]:
    """Like ``_walk_strings`` but keeps the dotted location of every string."""
    if isinstance(node, str):
        yield prefix or "<root>", node
    elif isinstance(node, dict):
        for key, value in node.items():
            child = f"{prefix}.{key}" if prefix else str(key)
            yield from _iter_located_strings(value, child)
    elif isinstance(node, list):
        for index, value in enumerate(node, start=1):
            yield from _iter_located_strings(value, f"{prefix}[{index}]")


def _split_env_ref(ref: str) -> Tuple[str, bool]:
    """Split an ${env:...} body into (name, has_default).

    Mirrors ``config._parse_env_ref``'s syntax — ``NAME``, ``NAME:-default``,
    ``NAME:default`` — but reports whether a default was written at all, which
    ``${env:NAME:-}`` (an explicit empty default) makes indistinguishable from
    the returned value.
    """
    if ":-" in ref:
        return ref.split(":-", 1)[0], True
    if ":" in ref:
        return ref.split(":", 1)[0], True
    return ref, False


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
    if not isinstance(method, str):
        # A YAML `method: 200` reaches the generated locustfile as
        # ``self.client.request(200, ...)`` and dies inside requests, with a
        # traceback pointing at library code rather than at this line.
        issues.append(Issue(ERROR, loc, f"'method' must be a string, got "
                                        f"{type(method).__name__}"))
    elif method.upper() not in _HTTP_METHODS:
        issues.append(Issue(WARNING, loc, f"unusual HTTP method {method!r}"))
    if "weight" in req:
        if not _is_int(req["weight"]):
            issues.append(Issue(ERROR, loc, "'weight' must be an integer"))
        elif int(req["weight"]) < 1:
            # The generator clamps to 1. Saying so beats a user concluding
            # that weight 0 disables a request — it does not.
            issues.append(Issue(
                WARNING, loc,
                f"'weight' is {req['weight']}; weights below 1 run as 1 — "
                "to switch a request off, remove it or use tags",
            ))
    for key in ("headers", "query", "json_headers"):
        if key in req and req[key] is not None and not isinstance(req[key], dict):
            issues.append(Issue(ERROR, loc, f"'{key}' must be an object"))
    if "tags" in req and not isinstance(req["tags"], (list, str)):
        issues.append(Issue(ERROR, loc, "'tags' must be a list of strings "
                                        "(a comma-separated string also works)"))
    if req.get("json") is not None and req.get("data") is not None:
        # requests sends one body; the JSON one would be dropped without a word.
        issues.append(Issue(ERROR, loc, "'json' and 'data' both set a request "
                                        "body — keep one ('json' is what runs)"))
    if "timeout" in req and req["timeout"] is not None:
        timeout = req["timeout"]
        try:
            seconds = float(timeout)
        except (TypeError, ValueError):
            issues.append(Issue(ERROR, loc, f"'timeout' must be a number of "
                                            f"seconds, got {timeout!r}"))
        else:
            if seconds <= 0:
                issues.append(Issue(ERROR, loc, "'timeout' must be greater than 0"))
            elif isinstance(timeout, str):
                issues.append(Issue(WARNING, loc, f"'timeout' is the string "
                                                  f"{timeout!r}; write it as a number"))
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


def _probe_pool_file(path_str: str) -> Tuple[Optional[str], Optional[Set[str]]]:
    """Open a pool file far enough to know it yields rows.

    Returns (error, field_names). ``error`` is a ready-to-print message when
    the file cannot serve as a pool; ``field_names`` is the key set of the
    first row when it can, and None when the fields are unknown. Only the
    first row is read, so a million-row CSV costs one line of I/O.
    """
    path = Path(path_str)
    if not path.exists():
        return f"data file not found: {path}", None
    if path.is_dir():
        return f"data source is a directory, not a file: {path}", None
    try:
        if path.suffix.lower() == ".json":
            with path.open(encoding="utf-8") as handle:
                data = json.load(handle)
            if not isinstance(data, list):
                return (
                    f"JSON data file must contain a list of objects, got "
                    f"{type(data).__name__}: {path}",
                    None,
                )
            rows = [row for row in data if isinstance(row, dict)]
            if not rows:
                return f"data file has no usable rows: {path}", None
            return None, set(str(key) for key in rows[0])
        with path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            first = next(reader, None)
        if first is None:
            return f"data file has no rows (header only or empty): {path}", None
        return None, set(str(key) for key in first if key is not None)
    except OSError as exc:
        return f"cannot read data file {path}: {exc}", None
    except (ValueError, UnicodeDecodeError) as exc:
        return f"cannot parse data file {path}: {exc}", None


def _count_rows_up_to(path_str: str, limit: int) -> Optional[int]:
    """How many rows a pool file has, counted no further than ``limit``.

    The question being asked is only "are there at least as many rows as
    there are workers", so a ten-million-row CSV costs eight lines of I/O
    rather than ten million. Returns None when the file cannot be read —
    that is already reported elsewhere and should not be reported twice.
    """
    path = Path(path_str)
    try:
        if path.suffix.lower() == ".json":
            with path.open(encoding="utf-8") as handle:
                data = json.load(handle)
            if not isinstance(data, list):
                return None
            return min(sum(1 for row in data if isinstance(row, dict)), limit)
        with path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            count = 0
            for _ in reader:
                count += 1
                if count >= limit:
                    break
            return count
    except (OSError, ValueError, UnicodeDecodeError):
        return None


def _validate_pool_capacity(
    spec: Dict[str, Any], ploc: str, shard_count: int, issues: List[Issue]
) -> None:
    """Warn when a pool has fewer rows than there are workers to divide it.

    The generated file degrades loudly here — a worker whose slice comes out
    empty falls back to the whole pool and logs it — but that log line lands
    in a worker's stderr in the middle of a run. The condition is knowable
    before anything starts, and this is where the reader is looking.
    """
    mode = spec.get("mode", "unique_per_user")
    if mode not in ("unique_per_user", "round_robin"):
        return
    if spec.get("generate") is not None:
        # Synthesised per process, never sharded.
        return
    inline = spec.get("inline")
    if isinstance(inline, list) and inline:
        rows: Optional[int] = len(inline)
    else:
        source = spec.get("source")
        if not isinstance(source, str) or not source.strip() or "$" in source:
            return
        rows = _count_rows_up_to(source.strip(), shard_count)
    if rows is None or rows >= shard_count:
        return
    issues.append(Issue(
        WARNING, ploc,
        f"{rows} row(s) for {shard_count} load generators; at least one "
        "worker would get an empty slice, so every worker falls back to the "
        "whole pool and rows repeat across workers — add rows, lower "
        "expect_workers/processes, or set load.shard_data: false to say the "
        "repetition is intended",
    ))


def _validate_data(
    data: Any,
    loc: str,
    issues: List[Issue],
    shard_count: int = 1,
) -> Dict[str, Optional[Set[str]]]:
    """Validate the pool specs and report the fields each pool can offer.

    The returned mapping is ``{pool_name: fields or None}``; None means the
    fields could not be determined (generated pools, unreadable file), so
    callers must not treat a missing name as a typo.
    """
    fields_by_pool: Dict[str, Optional[Set[str]]] = {}
    if not isinstance(data, dict):
        issues.append(Issue(ERROR, loc, "'data' must be an object of pool specs"))
        return fields_by_pool
    for name, spec in data.items():
        ploc = f"{loc}.{name}"
        fields_by_pool[str(name)] = None
        if not isinstance(spec, dict):
            issues.append(Issue(ERROR, ploc, "pool spec must be an object"))
            continue
        source = spec.get("source")
        has_source = isinstance(source, str) and source.strip()
        inline = spec.get("inline")
        generate = spec.get("generate")
        if generate is not None:
            if not isinstance(generate, dict) or not isinstance(generate.get("fields"), dict) or not generate.get("fields"):
                issues.append(Issue(ERROR, ploc, "'generate.fields' must be a non-empty object"))
            elif isinstance(generate, dict):
                fields_by_pool[str(name)] = set(str(k) for k in generate["fields"])
            if isinstance(generate, dict) and "count" in generate:
                if not _is_int(generate["count"]):
                    issues.append(Issue(ERROR, ploc, "'generate.count' must be an integer"))
                elif int(generate["count"]) < 1:
                    # A zero-row pool makes every ${data:} in the run an empty
                    # string; better to say so now than to debug 404s later.
                    issues.append(Issue(ERROR, ploc, "'generate.count' must be at least 1"))
        elif inline is not None:
            if not isinstance(inline, list) or not all(isinstance(r, dict) for r in inline):
                issues.append(Issue(ERROR, ploc, "'inline' must be a list of objects"))
            elif not inline:
                issues.append(Issue(ERROR, ploc, "'inline' has no rows"))
            else:
                fields_by_pool[str(name)] = set(str(k) for k in inline[0])
        elif has_source:
            # The run reads this file the moment the first user starts; a
            # missing or empty file becomes silent empty strings, so it is
            # checked here, before locust is launched. A path still holding a
            # placeholder is left alone — only load_config knows its value.
            if "${" not in source and "$" not in source:
                error, fields = _probe_pool_file(str(source).strip())
                if error:
                    issues.append(Issue(ERROR, ploc, error))
                fields_by_pool[str(name)] = fields
        else:
            issues.append(Issue(ERROR, ploc, "must define 'source', 'inline', or 'generate'"))
        mode = spec.get("mode", "unique_per_user")
        if mode not in DATA_MODES:
            issues.append(Issue(ERROR, ploc, f"'mode' must be one of {sorted(DATA_MODES)}, got {mode!r}"))
        elif shard_count > 1:
            _validate_pool_capacity(spec, ploc, shard_count, issues)
    return fields_by_pool


def _validate_think_time(value: Any, loc: str, issues: List[Issue]) -> None:
    """Check a think_time value: a number, or {min, max} in that order."""
    if value is None:
        return
    if isinstance(value, dict):
        bounds: Dict[str, float] = {}
        for key in ("min", "max"):
            if key not in value:
                continue
            try:
                bounds[key] = float(value[key])
            except (TypeError, ValueError):
                issues.append(Issue(ERROR, loc, f"'{key}' must be a number of seconds, "
                                                f"got {value[key]!r}"))
        if any(seconds < 0 for seconds in bounds.values()):
            issues.append(Issue(ERROR, loc, "think_time cannot be negative"))
        if "min" in bounds and "max" in bounds and bounds["min"] > bounds["max"]:
            issues.append(Issue(
                WARNING, loc,
                f"min ({bounds['min']}) is greater than max ({bounds['max']}); "
                "the bounds are swapped when the locustfile is generated",
            ))
        return
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        issues.append(Issue(ERROR, loc, f"'think_time' must be a number of seconds "
                                        f"or an object {{min, max}}, got {value!r}"))
        return
    if seconds < 0:
        issues.append(Issue(ERROR, loc, "think_time cannot be negative"))


def _validate_auth(auth: Any, loc: str, issues: List[Issue]) -> None:
    if not isinstance(auth, dict):
        issues.append(Issue(ERROR, loc, "'auth' must be an object"))
        return
    atype = str(auth.get("type", "")).lower()
    if atype not in _AUTH_TYPES:
        issues.append(Issue(ERROR, loc, f"'auth.type' must be one of {sorted(_AUTH_TYPES)}, got {auth.get('type')!r}"))
    elif atype == "api_key" and not auth.get("header"):
        issues.append(Issue(WARNING, loc, "api_key auth without 'header' defaults to 'X-API-Key'"))


# ── structure ─────────────────────────────────────────────────────────
#
# Everything below answers one question: does ``loco validate`` accept a
# config that ``loco run`` then refuses? The traversals in ``iter_requests``
# and ``iter_scenarios`` skip anything that isn't the shape they expect,
# which is right for *reading* a config but meant a request list written as
# a mapping produced zero issues and a ValueError one second later. The
# messages here deliberately mirror the generator's own wording so the two
# read as one voice.


def _check_object_list(value: Any, loc: str, issues: List[Issue], label: str) -> bool:
    """Report and return whether *value* is a list of objects."""
    if not isinstance(value, list):
        issues.append(Issue(ERROR, loc, f"must be a list of {label}, got "
                                        f"{type(value).__name__}"))
        return False
    ok = True
    for idx, entry in enumerate(value, start=1):
        if not isinstance(entry, dict):
            issues.append(Issue(ERROR, f"{loc}[{idx}]", f"must be an object, got "
                                                        f"{type(entry).__name__}"))
            ok = False
    return ok


def _validate_scenario_structure(scenario: Dict[str, Any], scope: str, issues: List[Issue]) -> None:
    for section in ("requests", "on_start", "on_stop"):
        if section in scenario and scenario[section] is not None:
            # on_start/on_stop written as a single object is valid YAML and
            # was silently ignored — the login it described never ran, and
            # the whole run came back 401.
            _check_object_list(scenario[section], f"{scope}.{section}", issues, "requests")

    flows = scenario.get("flows")
    if flows is not None and _check_object_list(flows, f"{scope}.flows", issues, "flows"):
        for fi, flow in enumerate(flows, start=1):
            if not isinstance(flow, dict):
                continue
            floc = f"{scope}.flows[{fi}]"
            label = flow.get("name") or f"flow_{fi}"
            steps = flow.get("steps")
            if steps is None or (isinstance(steps, list) and not steps):
                issues.append(Issue(ERROR, floc, f"({label!r}) must define a "
                                                 f"non-empty 'steps' list"))
            else:
                _check_object_list(steps, f"{floc}.steps", issues, "requests")
            if "weight" in flow:
                if not _is_int(flow["weight"]):
                    issues.append(Issue(ERROR, floc, "'weight' must be an integer"))
                elif int(flow["weight"]) < 1:
                    issues.append(Issue(
                        WARNING, floc,
                        f"'weight' is {flow['weight']}; weights below 1 run as 1",
                    ))

    headers = scenario.get("headers")
    if headers is not None and not isinstance(headers, dict):
        # Dropped without a word by the generator, which is how a scenario
        # loses its Authorization header and gains a wall of 401s.
        issues.append(Issue(ERROR, f"{scope}.headers", f"must be an object of "
                                                       f"header names to values, got "
                                                       f"{type(headers).__name__}"))

    data = scenario.get("data")
    if isinstance(data, dict):
        for name in data:
            if not _POOL_NAME_RE.match(str(name)):
                issues.append(Issue(
                    ERROR, f"{scope}.data",
                    f"pool name {str(name)!r} must match [A-Za-z0-9_]+ — it "
                    f"becomes a Python identifier in the generated locustfile",
                ))

    seen_names: Dict[str, str] = {}
    for loc, req in iter_requests(scenario, scope):
        name = req.get("name")
        if not isinstance(name, str) or not name:
            continue
        first = seen_names.get(name)
        if first is None:
            seen_names[name] = loc
        else:
            issues.append(Issue(
                WARNING, loc,
                f"request name {name!r} is already used at {first}; Locust "
                f"merges both into a single statistics row",
            ))


def _validate_structure(config: Dict[str, Any], issues: List[Issue]) -> None:
    users = config.get("users")
    if users is not None and not isinstance(users, list):
        issues.append(Issue(ERROR, "users", f"must be a list of personas, got "
                                            f"{type(users).__name__}"))
    elif isinstance(users, list):
        for idx, entry in enumerate(users, start=1):
            if not isinstance(entry, dict):
                issues.append(Issue(ERROR, f"users[{idx}]", f"must be an object, "
                                                            f"got {type(entry).__name__}"))
        if not users:
            issues.append(Issue(WARNING, "users", "is empty; no personas will be generated"))

    scenario = config.get("scenario")
    if scenario is not None and not isinstance(scenario, dict):
        issues.append(Issue(ERROR, "scenario", f"must be an object, got "
                                               f"{type(scenario).__name__}"))

    for name in ("load", "target", "analysis", "report", "artifacts"):
        section = config.get(name)
        if section is not None and not isinstance(section, dict):
            issues.append(Issue(ERROR, name, f"must be an object, got "
                                             f"{type(section).__name__}"))

    analysis = config.get("analysis")
    if isinstance(analysis, dict) and "fail_on" in analysis:
        if str(analysis["fail_on"]).strip().upper() not in _FAIL_ON_LEVELS:
            issues.append(Issue(
                WARNING, "analysis.fail_on",
                f"unknown value {analysis['fail_on']!r}; expected one of "
                f"{', '.join(_FAIL_ON_LEVELS)} — DEGRADATION is used instead",
            ))
    if isinstance(analysis, dict) and analysis.get("warning_exit_code") is not None:
        code = analysis["warning_exit_code"]
        # Read after the run has finished, so a bad value would end `loco ci`
        # with an error once the load test had already spent its minutes.
        if isinstance(code, bool) or not _is_int(code) or not 0 <= int(code) <= 255:
            issues.append(Issue(
                ERROR, "analysis.warning_exit_code",
                f"must be an integer from 0 to 255, got {code!r}",
            ))

    report_section = config.get("report")
    if isinstance(report_section, dict) and "chart_js_url" in report_section:
        if not isinstance(report_section["chart_js_url"], str):
            issues.append(Issue(
                WARNING, "report.chart_js_url",
                f"must be a URL string, got {report_section['chart_js_url']!r} — the default is used",
            ))

    artifacts = config.get("artifacts")
    if isinstance(artifacts, dict) and artifacts.get("history") is not None:
        history = artifacts["history"]
        if not _is_int(history):
            # ``ci`` calls int() on this without a guard, so a typo here ends
            # the run *after* locust has already spent its minutes.
            issues.append(Issue(ERROR, "artifacts.history", f"must be an integer "
                                                            f"number of runs, got {history!r}"))
        elif int(history) < 0:
            issues.append(Issue(ERROR, "artifacts.history", "cannot be negative"))

    report = config.get("report")
    if isinstance(report, dict):
        _validate_report_colors(report, issues)

    for scope, scn in iter_scenarios(config):
        _validate_scenario_structure(scn, scope, issues)


def _validate_report_colors(report: Dict[str, Any], issues: List[Issue]) -> None:
    """Colours the report will refuse to write into its stylesheet.

    The renderer drops anything that isn't recognisably a colour rather than
    letting it close the ``<style>`` element, which is right — but a theme
    that silently comes out the default shade of blue is a mystery worth two
    lines here instead of an afternoon there.
    """
    theme = report.get("theme")
    if isinstance(theme, dict):
        colors = theme.get("colors")
        if colors is not None and not isinstance(colors, dict):
            issues.append(Issue(ERROR, "report.theme.colors", f"must be an object of "
                                                              f"names to colours, got "
                                                              f"{type(colors).__name__}"))
        elif isinstance(colors, dict):
            for name, value in colors.items():
                if str(name).startswith("_"):
                    continue
                if css_name(name) is None:
                    issues.append(Issue(
                        WARNING, "report.theme.colors",
                        f"name {str(name)!r} is not usable as a CSS variable and "
                        f"will be ignored",
                    ))
                elif css_value(value) is None:
                    issues.append(Issue(
                        WARNING, f"report.theme.colors.{name}",
                        f"{value!r} is not a colour the report can write; it will "
                        f"be ignored and the default kept",
                    ))
        if theme.get("color") is not None and css_value(theme["color"]) is None:
            issues.append(Issue(WARNING, "report.theme.color",
                                f"{theme['color']!r} is not a colour the report can write"))

    branding = report.get("branding")
    if isinstance(branding, dict) and branding.get("color") is not None:
        if css_value(branding["color"]) is None:
            issues.append(Issue(WARNING, "report.branding.color",
                                f"{branding['color']!r} is not a colour the report "
                                f"can write; it will be ignored"))


def _validate_analysis(analysis: Any, issues: List[Issue]) -> None:
    if not isinstance(analysis, dict):
        return
    rules = analysis.get("rules")
    if rules is not None and not isinstance(rules, list):
        issues.append(Issue(ERROR, "analysis.rules", f"must be a list of rules, "
                                                     f"got {type(rules).__name__}"))
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
            for bound in ("warn", "fail"):
                # analyzer.load_rules does float(item.get(bound)) with no
                # guard: a rule missing one of these raises TypeError after
                # the run, when the numbers it was meant to judge already exist.
                if bound not in rule:
                    issues.append(Issue(ERROR, loc, f"missing '{bound}' threshold"))
                    continue
                try:
                    float(rule[bound])
                except (TypeError, ValueError):
                    issues.append(Issue(ERROR, loc, f"'{bound}' must be a number, "
                                                    f"got {rule[bound]!r}"))
    gate = analysis.get("gate")
    if isinstance(gate, dict):
        thresholds = gate.get("thresholds")
        if isinstance(thresholds, dict):
            for metric in thresholds:
                if metric not in KNOWN_METRICS:
                    issues.append(Issue(WARNING, f"analysis.gate.thresholds", f"unknown metric {metric!r}"))


def _validate_env_refs(config: Dict[str, Any], issues: List[Issue]) -> None:
    """Every ${env:NAME} left in the config must resolve to something.

    Inside ``scenario`` and ``users`` these references survive config loading
    on purpose, so the secret never lands in the generated locustfile — which
    means nothing has checked them yet. An unset variable with no default
    would silently become an empty password at request time, and the run would
    fail as a wall of 401s a minute later.
    """
    for loc, text in _iter_located_strings(config):
        for ref in _ENV_REF_RE.findall(text):
            name, has_default = _split_env_ref(ref)
            if has_default or name in os.environ:
                continue
            issues.append(Issue(
                ERROR, loc,
                f"${{env:{name}}} is not set in the environment and has no "
                f"default — export {name}, or write ${{env:{name}:-fallback}}",
            ))
        for name in _BARE_ENV_RE.findall(text):
            if name in os.environ:
                issues.append(Issue(
                    WARNING, loc,
                    f"'${name}' is sent literally: the bare $NAME form is no "
                    f"longer substituted — write ${{{name}}} or ${{env:{name}}}",
                ))


def _validate_run_budget(load: Dict[str, Any], issues: List[Issue]) -> None:
    """``load.timeout`` — the wall-clock budget for the locust process.

    Absent, it is derived from ``run_time``. ``null`` means wait forever,
    which is a real answer for a long soak run and so has to stay expressible.
    Anything else has to be a duration locust would recognise, and it has to
    leave room for ramp-up and shutdown: a budget shorter than ``run_time``
    guarantees every run is killed mid-flight.
    """
    if "timeout" not in load or load["timeout"] is None:
        return
    budget = parse_duration(load["timeout"])
    if budget is None or budget <= 0:
        issues.append(Issue(
            ERROR, "load.timeout",
            f"{load['timeout']!r} is not a duration; use seconds (300) or "
            "locust's syntax (\"5m\"), or null to wait indefinitely",
        ))
        return
    run_time = parse_duration(load.get("run_time"))
    if run_time and budget <= run_time:
        issues.append(Issue(
            WARNING, "load.timeout",
            f"budget ({budget:g}s) is not longer than run_time ({run_time:g}s); "
            "ramp-up and shutdown happen outside run_time, so the run will be "
            "stopped before it finishes",
        ))


def _validate_distribution(load: Dict[str, Any], issues: List[Issue]) -> int:
    """The distribution keys, and how many processes they add up to.

    Returns the number of load-generating processes the config describes —
    the denominator every data pool will be divided by — or 1 when it
    describes a single process. Sharding is only checked against that number
    when the config is otherwise coherent, so a config with a contradiction
    in it does not also collect a pile of downstream warnings.
    """
    for field in ("processes", "expect_workers", "master_port"):
        if field in load and load[field] is not None and not _is_int(load[field]):
            issues.append(Issue(ERROR, f"load.{field}", "must be an integer"))
    for field in ("master", "worker", "shard_data"):
        if field in load and not isinstance(load[field], bool):
            issues.append(Issue(
                ERROR, f"load.{field}", f"must be true or false, got {load[field]!r}"
            ))

    processes = int(load["processes"]) if _is_int(load.get("processes")) else None
    expect_workers = (
        int(load["expect_workers"]) if _is_int(load.get("expect_workers")) else None
    )
    master = load.get("master") is True
    worker = load.get("worker") is True

    if processes is not None and processes < 1:
        issues.append(Issue(
            ERROR, "load.processes",
            f"must be at least 1, got {processes} — omit the key for a single process",
        ))
        processes = None
    if expect_workers is not None and expect_workers < 1:
        issues.append(Issue(
            ERROR, "load.expect_workers", f"must be at least 1, got {expect_workers}"
        ))
        expect_workers = None

    if master and worker:
        issues.append(Issue(
            ERROR, "load",
            "'master' and 'worker' are both true; one process is one role — "
            "run the master and the workers as separate commands",
        ))
        return 1
    if processes is not None and processes > 1:
        if worker:
            issues.append(Issue(
                ERROR, "load",
                "'worker' with 'processes' — a worker takes its work from the "
                "master and cannot fan out again; put 'processes' on the master",
            ))
            return 1
        if master:
            issues.append(Issue(
                ERROR, "load",
                "'master' with 'processes' — locust's --processes already makes "
                "this process the master of the workers it forks; drop 'master'",
            ))
            return 1
        if expect_workers is not None and expect_workers != processes:
            issues.append(Issue(
                ERROR, "load",
                f"'expect_workers' ({expect_workers}) contradicts 'processes' "
                f"({processes}); with 'processes' locust sets both from one "
                "number, so drop 'expect_workers'",
            ))
            return 1
        if os.name == "nt":
            issues.append(Issue(
                ERROR, "load.processes",
                "locust forks its worker processes, which Windows cannot do; "
                "start the workers yourself with 'worker: true' (WSL or "
                "containers are the usual answer)",
            ))
            return 1
        return processes

    if master:
        if expect_workers is None:
            issues.append(Issue(
                ERROR, "load.expect_workers",
                "a master needs to know how many workers to wait for; it is "
                "also the number each data pool is divided by, so guessing it "
                "would hand the same rows to several workers",
            ))
            return 1
        return expect_workers

    if worker:
        if not load.get("master_host"):
            issues.append(Issue(
                WARNING, "load.master_host",
                "not set, so this worker will look for a master on 127.0.0.1 — "
                "correct on one machine, and nothing a worker in its own "
                "container will ever find",
            ))
        # A worker's shard count arrives from the master at runtime; nothing
        # here can know it, and the config that does is the master's.
        return 1

    if expect_workers is not None and expect_workers > 1:
        issues.append(Issue(
            WARNING, "load.expect_workers",
            "set without 'master' or 'processes', so this is a single "
            "standalone process and the value has no effect",
        ))
    return 1


def validate_config(config: Dict[str, Any]) -> List[Issue]:
    """Return all validation issues (errors and warnings) for a config."""
    issues: List[Issue] = []
    if not isinstance(config, dict):
        return [Issue(ERROR, "<root>", "config must be an object")]

    load = config.get("load")
    if not isinstance(load, dict):
        issues.append(Issue(WARNING, "load", "missing 'load' section"))
        load = {}
    # A worker is told what to run by its master: `users`, `spawn_rate` and
    # `run_time` are cluster-wide totals the master owns, and locust ignores
    # whatever a worker was given. Asking a worker config for them would be
    # three warnings on every worker in the cluster for fields that would do
    # nothing if they were there.
    required = ("host",) if load.get("worker") else ("host", "users", "spawn_rate", "run_time")
    for field in required:
        if field not in load and not load.get("locustfile"):
            issues.append(Issue(WARNING, "load", f"'{field}' missing (required for a run unless passed via CLI)"))
    for field in ("users", "spawn_rate", "stop_timeout"):
        if field in load and not _is_int(load[field]):
            issues.append(Issue(ERROR, "load", f"'{field}' must be an integer"))
    _validate_run_budget(load, issues)
    shard_count = _validate_distribution(load, issues)
    if load.get("shard_data") is False:
        # Every worker keeps the whole pool by request, so "fewer rows than
        # workers" is not a finding — it is the stated intent.
        shard_count = 1

    _validate_structure(config, issues)

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
        # Only a *list* counts: `requests:` written as a mapping is already
        # reported by _validate_structure, and saying "has no requests" on top
        # of that sends the reader looking for a second, imaginary problem.
        has_reqs = any(
            isinstance(scenario.get(key), list) and scenario[key]
            for key in ("requests", "flows")
        )
        malformed = any(
            key in scenario and scenario[key] is not None
            and not isinstance(scenario[key], list)
            for key in ("requests", "flows")
        )
        if not has_reqs and not malformed and not load.get("locustfile"):
            issues.append(Issue(ERROR, scope, "scenario has no 'requests' or 'flows'"))

        for loc, req in reqs:
            _validate_request(req, loc, issues)
            if "think_time" in req:
                _validate_think_time(req["think_time"], f"{loc}.think_time", issues)

        _validate_think_time(scenario.get("think_time"), f"{scope}.think_time", issues)
        flows = scenario.get("flows")
        if isinstance(flows, list):
            for fi, flow in enumerate(flows, start=1):
                if isinstance(flow, dict) and "think_time" in flow:
                    _validate_think_time(
                        flow["think_time"], f"{scope}.flows[{fi}].think_time", issues
                    )

        pool_fields: Dict[str, Optional[Set[str]]] = {}
        if "data" in scenario:
            pool_fields = _validate_data(
                scenario["data"], f"{scope}.data", issues, shard_count=shard_count
            )
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
                for pool, field in _DATA_RE.findall(text):
                    if pool not in declared_pools:
                        issues.append(Issue(WARNING, loc, f"references data pool {pool!r} not declared in {scope}.data"))
                        continue
                    known = pool_fields.get(pool)
                    # Only flat names are checked: a dotted path may reach into
                    # a nested JSON row, whose shape the first-row key set does
                    # not describe.
                    if known and field and "." not in field and field not in known:
                        issues.append(Issue(
                            WARNING, loc,
                            f"pool {pool!r} has no field {field!r} "
                            f"(available: {', '.join(sorted(known))})",
                        ))
            if req.get("_requires_auth") and not has_auth:
                issues.append(Issue(WARNING, loc, "endpoint needs auth but no 'auth' block is configured"))

    _validate_analysis(config.get("analysis"), issues)
    _validate_env_refs(config, issues)

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
