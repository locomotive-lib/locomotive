from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import __version__
from .analyzer import analyze as analyze_metrics
from .analyzer import load_rules, merge_results, sanity_results, topology_results
from .ci import default_run_id, detect_ci
from .config import load_config, load_config_raw
from .gate import evaluate_gate, summarize_history
from .launcher import LocustLauncher, find_stats_history_csv, parse_locust_stats_history
from .reporter import render_report, load_stats_history, load_endpoint_stats
from .scenario import generate_locustfile
from .storage import Storage
from .template import generate_github_workflow, generate_jenkinsfile, generate_template
from .utils import write_text


DEFAULT_CONFIG = "loconfig.json"


def _get_section(config: Dict[str, Any], name: str) -> Dict[str, Any]:
    value = config.get(name)
    return value if isinstance(value, dict) else {}


def _normalize_mode(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip().lower()
    # Accept legacy mode names for backward compatibility
    if text in {"resilience", "acceptance"}:
        return "resilience"
    return ""


def _resolve_gate_config(analysis_cfg: Dict[str, Any]) -> tuple[str, Dict[str, Any]]:
    gate_cfg = _get_section(analysis_cfg, "gate")
    mode = _normalize_mode(analysis_cfg.get("mode") or gate_cfg.get("mode"))
    thresholds = gate_cfg.get("thresholds")
    has_thresholds = isinstance(thresholds, dict) and bool(thresholds)
    if not mode and has_thresholds:
        mode = "resilience"
    if mode and not has_thresholds:
        mode = ""
    return mode, gate_cfg


def _load_history_summary(storage: Storage, run_id: str, warmup_seconds: Optional[int]) -> Optional[Dict[str, float]]:
    if not warmup_seconds:
        return None
    history_path = find_stats_history_csv(storage.raw_dir(run_id))
    if not history_path:
        return None
    history = parse_locust_stats_history(history_path)
    return summarize_history(history, warmup_seconds)


def _parse_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return [str(value)]


def _parse_int(value: Any, name: str) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be an integer")


def _default_run_id() -> str:
    return default_run_id(detect_ci())


def _collect_ci_meta() -> Dict[str, Any]:
    return detect_ci().to_meta()


def _load_rules_from_sources(rules_path: Optional[str], inline_rules: Optional[List[Dict[str, Any]]]) -> List[Any]:
    if rules_path:
        data = json.loads(Path(rules_path).read_text(encoding="utf-8"))
        return load_rules(data)
    if inline_rules:
        return load_rules({"rules": inline_rules})
    return []


def _load_run_meta(storage: Storage, run_id: str) -> Dict[str, Any]:
    """A run's ``run.json``, or an empty dict for a run that has none.

    Runs recorded before a given field existed simply do not have it, and
    that has to read as "unknown", not as a difference.
    """
    path = storage.run_meta_path(run_id)
    return storage.load_json(path) if path.exists() else {}


def _build_storage(args: argparse.Namespace, config: Dict[str, Any]) -> Storage:
    artifacts = _get_section(config, "artifacts")
    storage_root = args.storage or artifacts.get("storage") or "artifacts"
    return Storage.from_root(storage_root)


def _build_run_id(args: argparse.Namespace, config: Dict[str, Any]) -> str:
    artifacts = _get_section(config, "artifacts")
    return args.run_id or artifacts.get("run_id") or _default_run_id()


def _resolve_baseline_id(
    args: argparse.Namespace, config: Dict[str, Any], storage: Storage
) -> Optional[str]:
    analysis_cfg = _get_section(config, "analysis")
    return args.baseline or analysis_cfg.get("baseline") or storage.get_baseline()


def _avoid_baseline_collision(storage: Storage, run_id: str, baseline_id: Optional[str]) -> str:
    """Give a run whose id is already the baseline's a fresh id instead.

    Writing that run into the baseline's directory overwrites the numbers it
    is about to be compared against, and the comparison that follows is the
    run against itself: every delta is zero and every rule passes. A constant
    run id does exactly that — ``${GITHUB_SHA:-local}`` anywhere but GitHub,
    or a re-run of the commit that set the baseline — and it used to turn a
    fourfold latency regression green.

    Existing run directories are skipped as well, so nothing already recorded
    is overwritten.
    """
    if not baseline_id or run_id != baseline_id:
        return run_id
    suffix = 2
    while storage.run_dir(f"{run_id}-{suffix}").exists():
        suffix += 1
    fresh = f"{run_id}-{suffix}"
    print(
        f"Warning: run id '{run_id}' is the current baseline; recording this run "
        f"as '{fresh}' so it is compared against the baseline instead of overwriting it. "
        "Remove artifacts.run_id from the config to get a unique id per CI build."
    )
    return fresh


def _build_locust_config(args: argparse.Namespace, config: Dict[str, Any]) -> Dict[str, Any]:
    locust_cfg = _get_section(config, "load")
    
    scenario_cfg = _get_section(config, "scenario")
    scenario_headers = scenario_cfg.get("headers") if isinstance(scenario_cfg.get("headers"), dict) else {}
    locust_headers = locust_cfg.get("headers") if isinstance(locust_cfg.get("headers"), dict) else {}
    headers = {**locust_headers, **scenario_headers}
    
    merged: Dict[str, Any] = {
        "locustfile": args.locustfile or locust_cfg.get("locustfile"),
        "host": args.host or locust_cfg.get("host"),
        "users": _parse_int(args.users, "users") if args.users is not None else _parse_int(locust_cfg.get("users"), "users"),
        "spawn_rate": _parse_int(args.spawn_rate, "spawn_rate") if args.spawn_rate is not None else _parse_int(locust_cfg.get("spawn_rate"), "spawn_rate"),
        "run_time": args.run_time or locust_cfg.get("run_time"),
        "tags": _parse_list(args.tags) if args.tags is not None else _parse_list(locust_cfg.get("tags")),
        "exclude_tags": _parse_list(args.exclude_tags) if args.exclude_tags is not None else _parse_list(locust_cfg.get("exclude_tags")),
        "stop_timeout": _parse_int(args.stop_timeout, "stop_timeout") if args.stop_timeout is not None else _parse_int(locust_cfg.get("stop_timeout"), "stop_timeout"),
        "extra_args": _parse_list(args.extra_arg) if args.extra_arg is not None else _parse_list(locust_cfg.get("extra_args")),
        "locust_cmd": args.locust_cmd or locust_cfg.get("locust_cmd"),
        "headers": headers,
        "meta": {"ci": _collect_ci_meta()},
    }
    # Absent and `null` mean different things — no key means "derive a budget
    # from run_time", `null` means "wait as long as it takes" — so the key is
    # only set when the config actually carries one.
    if "timeout" in locust_cfg:
        merged["timeout"] = locust_cfg["timeout"]
    merged.update(_merge_topology(args, locust_cfg))
    return merged


def _merge_topology(
    args: argparse.Namespace, locust_cfg: Dict[str, Any]
) -> Dict[str, Any]:
    """Distribution settings, flag beating config.

    ``--master`` and ``--worker`` are store_true, so the flag can only ever
    turn a role on; ``master: false`` in the config with ``--master`` on the
    command line means the command line. Everything else is "None means the
    config had its say".
    """
    merged: Dict[str, Any] = {
        "processes": (
            _parse_int(args.processes, "processes")
            if getattr(args, "processes", None) is not None
            else _parse_int(locust_cfg.get("processes"), "processes")
        ),
        "expect_workers": (
            _parse_int(args.expect_workers, "expect_workers")
            if getattr(args, "expect_workers", None) is not None
            else _parse_int(locust_cfg.get("expect_workers"), "expect_workers")
        ),
        "master": bool(getattr(args, "master", False)) or bool(locust_cfg.get("master")),
        "worker": bool(getattr(args, "worker", False)) or bool(locust_cfg.get("worker")),
        "master_host": getattr(args, "master_host", None) or locust_cfg.get("master_host"),
        "master_port": (
            _parse_int(args.master_port, "master_port")
            if getattr(args, "master_port", None) is not None
            else _parse_int(locust_cfg.get("master_port"), "master_port")
        ),
        # Read by the generator, not the launcher: it decides whether the
        # generated file divides its data pools between workers.
        "shard_data": locust_cfg.get("shard_data", True) is not False,
    }
    if getattr(args, "no_shard_data", False):
        merged["shard_data"] = False
    return merged


def _maybe_generate_locustfile(
    storage: Storage,
    run_id: str,
    locust_config: Dict[str, Any],
    config: Dict[str, Any],
) -> None:
    """Generate locustfile from scenario config if no locustfile is specified."""
    if locust_config.get("locustfile"):
        return
    
    scenario = _get_section(config, "scenario")
    users = config.get("users")
    users = users if isinstance(users, list) and users else None
    if not scenario and not users:
        raise ValueError(
            "Either 'locustfile' in load config, or a 'scenario'/'users' section is required"
        )

    if not users and not scenario.get("requests") and not scenario.get("flows"):
        raise ValueError("scenario must define a non-empty 'requests' or 'flows' list")

    output_dir = storage.run_dir(run_id) / "generated"
    locustfile_path = generate_locustfile(scenario, locust_config, output_dir, users=users)
    locust_config["locustfile"] = str(locustfile_path)


def _run(storage: Storage, run_id: str, locust_config: Dict[str, Any]) -> Dict[str, Any]:
    launcher = LocustLauncher(storage, run_id, locust_config)
    return launcher.run()


def _analyze(
    storage: Storage,
    run_id: str,
    baseline_id: str,
    rules_path: Optional[str],
    inline_rules: Optional[List[Dict[str, Any]]],
    save: bool = True,
) -> Dict[str, Any]:
    current_metrics = storage.load_json(storage.metrics_path(run_id))
    baseline_metrics = {}
    baseline_path = storage.metrics_path(baseline_id)
    if baseline_path.exists():
        baseline_metrics = storage.load_json(baseline_path)
    rules = _load_rules_from_sources(rules_path, inline_rules)
    analysis = analyze_metrics(current_metrics, baseline_metrics, rules)
    analysis["run_id"] = run_id
    analysis["baseline_id"] = baseline_id
    if save:
        storage.save_json(storage.analysis_path(run_id), analysis)
    return analysis


def _report(
    storage: Storage,
    run_id: str,
    baseline_id: Optional[str],
    title: str,
    output_path: Optional[str],
    report_cfg: Optional[Dict[str, Any]] = None,
    history_runs: Optional[List[Dict[str, Any]]] = None,
) -> str:
    from .report_config import resolve_report_config

    run_meta_path = storage.run_meta_path(run_id)
    if run_meta_path.exists():
        run_meta = storage.load_json(run_meta_path)
    else:
        run_meta = {"run_id": run_id}
    if baseline_id:
        run_meta["baseline_id"] = baseline_id

    current_metrics = {}
    metrics_path = storage.metrics_path(run_id)
    if metrics_path.exists():
        current_metrics = storage.load_json(metrics_path)

    baseline_metrics = None
    if baseline_id:
        baseline_path = storage.metrics_path(baseline_id)
        if baseline_path.exists():
            baseline_metrics = storage.load_json(baseline_path)

    analysis = None
    analysis_path = storage.analysis_path(run_id)
    if analysis_path.exists():
        analysis = storage.load_json(analysis_path)

    raw_dir = storage.run_dir(run_id) / "raw"
    stats_history = load_stats_history(raw_dir / "locust_stats_history.csv")
    endpoint_stats = load_endpoint_stats(raw_dir / "locust_stats.csv")

    cfg = resolve_report_config(report_cfg or {})
    if title:
        cfg.title = title

    html = render_report(
        run_meta,
        current_metrics,
        baseline_metrics,
        analysis,
        title,
        stats_history=stats_history,
        endpoint_stats=endpoint_stats,
        report_config=cfg,
        history_runs=history_runs,
    )

    # Always save report in the run directory
    run_report = storage.report_path(run_id)
    storage.save_text(run_report, html)

    # Also save to custom output path if specified
    if output_path:
        output = Path(output_path)
        if output.resolve() != run_report.resolve():
            storage.save_text(output, html)

    return str(output_path or run_report)


def _write_exports(
    args: argparse.Namespace,
    storage: Storage,
    run_id: str,
    baseline_id: Optional[str],
    analysis_cfg: Dict[str, Any],
    title: str,
) -> None:
    """Write the markdown summary and JUnit XML asked for on the command line."""
    summary_path = getattr(args, "summary", None)
    junit_path = getattr(args, "junit", None)
    if not summary_path and not junit_path:
        return
    from .export import load_endpoint_rows, render_junit, render_markdown_summary

    def load(path: Path) -> Optional[Dict[str, Any]]:
        return storage.load_json(path) if path.exists() else None

    metrics = load(storage.metrics_path(run_id))
    analysis = load(storage.analysis_path(run_id))
    if summary_path:
        baseline_metrics = load(storage.metrics_path(baseline_id)) if baseline_id else None
        rules_configured = bool(
            getattr(args, "rules", None) or analysis_cfg.get("rules_file") or analysis_cfg.get("rules")
        )
        write_text(Path(summary_path), render_markdown_summary(
            run_id=run_id,
            metrics=metrics,
            analysis=analysis,
            baseline_id=baseline_id,
            baseline_metrics=baseline_metrics,
            run_meta=_load_run_meta(storage, run_id),
            endpoints=load_endpoint_rows(storage.raw_dir(run_id) / "locust_stats.csv"),
            rules_configured=rules_configured,
            title=title,
        ))
    if junit_path:
        fail_on = _resolve_fail_on(getattr(args, "fail_on", None), analysis_cfg)
        write_text(Path(junit_path), render_junit(
            run_id=run_id, metrics=metrics, analysis=analysis, fail_on=fail_on,
        ))


def _prune_runs(storage: Storage, run_id: str) -> None:
    """Keep only this run and the baseline, so the stored artifact stays small.

    The artifacts directory is what a CI system archives and hands to the next
    build as its baseline, and every run left in it is carried forward again.
    """
    keep = {run_id, storage.get_baseline()}
    removed = storage.prune_runs(keep)
    if removed:
        kept = ", ".join(sorted(k for k in keep if k))
        print(f"Pruned {len(removed)} stored run(s); kept {kept}.")


FAIL_ON_LEVELS = ("WARNING", "DEGRADATION")


def _is_true(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _resolve_fail_on(args_value: Any, analysis_cfg: Dict[str, Any]) -> str:
    """Resolve fail_on from CLI/config, case-insensitively.

    A lowercase 'degradation' in the config used to compare unequal to
    'DEGRADATION' and silently disable the build ever failing.
    """
    raw = args_value or analysis_cfg.get("fail_on") or "DEGRADATION"
    fail_on = str(raw).strip().upper()
    if fail_on not in FAIL_ON_LEVELS:
        print(
            f"Warning: unknown fail_on {raw!r}; expected one of "
            f"{', '.join(FAIL_ON_LEVELS)}. Falling back to DEGRADATION."
        )
        return "DEGRADATION"
    return fail_on


def _resolve_warning_exit_code(args_value: Any, analysis_cfg: Dict[str, Any]) -> int:
    """The exit code for a run whose worst result is WARNING: 0 unless asked.

    CI systems can show a third state between green and red — GitLab's
    ``allow_failure: exit_codes`` turns a job orange, a Jenkins pipeline marks
    the build UNSTABLE — but only if the process says so with its exit code.
    0 stays the default so pipelines that pass on WARNING today keep passing.
    """
    raw = args_value if args_value is not None else analysis_cfg.get("warning_exit_code")
    if raw is None or raw == "":
        return 0
    if isinstance(raw, bool):
        raise ValueError(f"warning_exit_code must be an integer from 0 to 255, got {raw!r}")
    code = _parse_int(raw, "warning_exit_code")
    if code is None or not 0 <= code <= 255:
        raise ValueError(f"warning_exit_code must be an integer from 0 to 255, got {raw!r}")
    return code


def _downgrade_no_data(result_sets: List[List[Dict[str, Any]]]) -> None:
    """Turn NO_DATA into SKIP, for users who opt into analysis.allow_no_data."""
    for results in result_sets:
        for res in results or []:
            if res.get("status") == "NO_DATA":
                res["status"] = "SKIP"
                reason = res.get("reason") or "no data"
                res["reason"] = f"{reason} (allowed by allow_no_data)"


def _gate_status(gate_eval: Dict[str, Any]) -> str:
    """Derive a single status from gate evaluation results only."""
    results = gate_eval.get("results") or []
    statuses = [r.get("status") for r in results if r.get("status") not in (None, "SKIP")]
    if not statuses:
        # Thresholds were configured but not one of them was actually
        # evaluated. That is the opposite of a pass.
        return "NO_DATA"
    for level in ("DEGRADATION", "NO_DATA", "WARNING"):
        if level in statuses:
            return level
    return "PASS"


def _exit_code_for_status(status: str, fail_on: str) -> int:
    if status == "NO_DATA":
        # Independent of fail_on: nothing was measured, so nothing was proven.
        return 1
    if fail_on == "WARNING" and status in {"WARNING", "DEGRADATION"}:
        return 1
    if fail_on == "DEGRADATION" and status == "DEGRADATION":
        return 1
    return 0


def cmd_validate(args: argparse.Namespace, config: Dict[str, Any]) -> int:
    """Statically validate the config; exit 1 if there are errors."""
    from .validate import ERROR, format_issues, validate_config

    issues = validate_config(config)
    print(format_issues(issues))
    return 1 if any(i.level == ERROR for i in issues) else 0


def cmd_diff(args: argparse.Namespace, config: Dict[str, Any]) -> int:
    """Compare the config against an OpenAPI spec; exit 1 on breaking drift."""
    from .diff import diff_config_spec, format_findings, has_breaking
    from .openapi import load_spec

    spec_path = Path(args.openapi)
    if not spec_path.exists():
        print(f"Error: OpenAPI spec not found: {spec_path}")
        return 1
    spec = load_spec(spec_path)
    # Re-read the config unsubstituted: a path param written as
    # ${PATH_ID:-1} is a parameter, and comparing the resolved "/users/1"
    # against the spec's "/users/{id}" made every scaffolded config drift.
    try:
        config = load_config_raw(args.config)
    except (OSError, ValueError):
        pass  # fall back to the already-loaded config
    findings = diff_config_spec(config, spec)
    print(format_findings(findings))
    if args.exit_zero:
        return 0
    return 1 if has_breaking(findings) else 0


def _preflight_validate(config: Dict[str, Any]) -> int:
    """Validate before a run. Print issues; return 1 if there are errors."""
    from .validate import ERROR, format_issues, validate_config

    issues = validate_config(config)
    if issues:
        print(format_issues(issues))
    if any(i.level == ERROR for i in issues):
        print("Config validation failed. Fix the errors above, or pass --no-validate to skip.")
        return 1
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    """Initialize a new loconfig configuration."""
    output_path = Path(args.output)
    openapi_path = Path(args.openapi) if args.openapi else None
    # Left as None when the flag was not given, so a host declared by the spec
    # can win over the localhost fallback.
    host = args.host

    if output_path.exists() and not args.force:
        print(f"Error: {output_path} already exists. Use --force to overwrite.")
        return 1
    
    generate_template(output_path, host=host, openapi_path=openapi_path)
    print(f"Created: {output_path}")
    
    # Optionally generate GitHub workflow
    if args.github_workflow:
        workflow_path = Path(".github/workflows/loadtest.yml")
        if workflow_path.exists() and not args.force:
            print(f"Skipped: {workflow_path} already exists")
        else:
            generate_github_workflow(workflow_path, config_name=output_path.name)
            print(f"Created: {workflow_path}")

    if args.jenkinsfile:
        jenkinsfile_path = Path("Jenkinsfile")
        if jenkinsfile_path.exists() and not args.force:
            print(f"Skipped: {jenkinsfile_path} already exists")
        else:
            generate_jenkinsfile(jenkinsfile_path, config_name=output_path.as_posix())
            print(f"Created: {jenkinsfile_path}")
    
    print()
    print("Next steps:")
    print(f"  1. Edit {output_path} to configure your endpoints")
    print("  2. Run: loco ci --config", output_path)
    
    return 0


def cmd_run(args: argparse.Namespace, config: Dict[str, Any]) -> int:
    if not getattr(args, "no_validate", False):
        code = _preflight_validate(config)
        if code:
            return code
    storage = _build_storage(args, config)
    run_id = _avoid_baseline_collision(
        storage, _build_run_id(args, config), _resolve_baseline_id(args, config, storage)
    )
    locust_config = _build_locust_config(args, config)

    _maybe_generate_locustfile(storage, run_id, locust_config, config)
    result = _run(storage, run_id, locust_config)

    metrics_exist = storage.metrics_path(run_id).exists()
    if args.set_baseline and metrics_exist:
        storage.set_baseline(run_id)

    return int(result.get("returncode") or 0)


def cmd_analyze(args: argparse.Namespace, config: Dict[str, Any]) -> int:
    storage = _build_storage(args, config)
    run_id = _build_run_id(args, config)

    analysis_cfg = _get_section(config, "analysis")
    mode, gate_cfg = _resolve_gate_config(analysis_cfg)
    baseline_id = _resolve_baseline_id(args, config, storage)
    if baseline_id and baseline_id == run_id:
        # Comparing a run with itself proves nothing and passes every rule.
        print(f"Warning: run '{run_id}' is the baseline itself; skipping the baseline comparison.")
        baseline_id = None
    if not baseline_id and not mode:
        raise ValueError("baseline run id is required")

    rules_path = args.rules or analysis_cfg.get("rules_file")
    inline_rules = analysis_cfg.get("rules")

    current_metrics = storage.load_json(storage.metrics_path(run_id))

    result_sets: List[List[Dict[str, Any]]] = []
    baseline_results = None
    if baseline_id:
        baseline_results = _analyze(storage, run_id, baseline_id, rules_path, inline_rules, save=False)
        result_sets.append(baseline_results.get("results") or [])

    gate_eval = None
    if mode:
        warmup_seconds = gate_cfg.get("warmup_seconds")
        history_summary = _load_history_summary(storage, run_id, _parse_int(warmup_seconds, "warmup_seconds") if warmup_seconds is not None else None)
        gate_eval = evaluate_gate(current_metrics, gate_cfg, mode, history_summary)
        if gate_eval:
            result_sets.append(gate_eval.get("results") or [])

    # A run that recorded nothing must not pass by virtue of having no numbers
    # to compare. This check applies even when no rules and no gate are set.
    sanity = sanity_results(current_metrics)
    if sanity:
        result_sets.append(sanity)
    if baseline_id:
        drift = topology_results(
            _load_run_meta(storage, run_id), _load_run_meta(storage, baseline_id)
        )
        if drift:
            result_sets.append(drift)

    if _is_true(analysis_cfg.get("allow_no_data")):
        _downgrade_no_data(result_sets)

    if result_sets:
        combined = merge_results(result_sets)
        combined["run_id"] = run_id
        if baseline_id:
            combined["baseline_id"] = baseline_id
        if gate_eval:
            combined["gate"] = gate_eval.get("gate")
        storage.save_json(storage.analysis_path(run_id), combined)

        fail_on = _resolve_fail_on(args.fail_on, analysis_cfg)
        warning_code = _resolve_warning_exit_code(getattr(args, "warning_exit_code", None), analysis_cfg)
        code = _exit_code_for_status(combined.get("status"), fail_on)
        if not code and combined.get("status") == "WARNING":
            return warning_code
        return code

    return 0


def cmd_report(args: argparse.Namespace, config: Dict[str, Any]) -> int:
    storage = _build_storage(args, config)
    run_id = _build_run_id(args, config)

    analysis_cfg = _get_section(config, "analysis")
    report_cfg = _get_section(config, "report")
    baseline_id = _resolve_baseline_id(args, config, storage)
    if baseline_id == run_id:
        baseline_id = None
    title = args.title or report_cfg.get("title") or "CI Load Test Report"
    output_path = args.output or report_cfg.get("output")

    history_runs = storage.load_history().get("runs", [])
    _report(storage, run_id, baseline_id, title, output_path,
            report_cfg=report_cfg, history_runs=history_runs)
    _write_exports(args, storage, run_id, baseline_id, analysis_cfg, title)
    return 0


def cmd_ci(args: argparse.Namespace, config: Dict[str, Any]) -> int:
    """Run full CI pipeline: run tests, analyze, generate report."""
    if not getattr(args, "no_validate", False):
        code = _preflight_validate(config)
        if code:
            return code
    # Resolved before the run: a bad value must not wait for the load test to end.
    warning_code = _resolve_warning_exit_code(
        getattr(args, "warning_exit_code", None), _get_section(config, "analysis")
    )
    storage = _build_storage(args, config)
    # Resolved before the run, which must not be written over the baseline.
    # The run itself never moves baseline.json, so this stays the baseline.
    baseline_id = _resolve_baseline_id(args, config, storage)
    run_id = _avoid_baseline_collision(storage, _build_run_id(args, config), baseline_id)
    locust_config = _build_locust_config(args, config)

    _maybe_generate_locustfile(storage, run_id, locust_config, config)
    run_result = _run(storage, run_id, locust_config)

    if (run_result.get("topology") or {}).get("role") == "worker":
        # A worker generates load and reports it to the master; the master
        # writes the CSVs, so there is nothing here to analyse, gate or
        # report on. Falling through would reach `if not metrics_exist:
        # return locust_code or 1` and turn a worker that did its job into a
        # failed CI step.
        code = int(run_result.get("returncode") or 0)
        print(f"Worker finished (exit {code}); the master holds the results.")
        return code

    analysis_cfg = _get_section(config, "analysis")
    report_cfg = _get_section(config, "report")
    mode, gate_cfg = _resolve_gate_config(analysis_cfg)

    metrics_path = storage.metrics_path(run_id)
    metrics_exist = metrics_path.exists()
    analysis = None
    result_sets: List[List[Dict[str, Any]]] = []
    baseline_results = None

    if baseline_id and metrics_exist:
        rules_path = args.rules or analysis_cfg.get("rules_file")
        inline_rules = analysis_cfg.get("rules")
        baseline_results = _analyze(storage, run_id, baseline_id, rules_path, inline_rules, save=False)
        result_sets.append(baseline_results.get("results") or [])
    else:
        baseline_id = None

    gate_eval = None
    if mode and metrics_exist:
        current_metrics = storage.load_json(metrics_path)
        warmup_seconds = gate_cfg.get("warmup_seconds")
        history_summary = _load_history_summary(storage, run_id, _parse_int(warmup_seconds, "warmup_seconds") if warmup_seconds is not None else None)
        gate_eval = evaluate_gate(current_metrics, gate_cfg, mode, history_summary)
        if gate_eval:
            result_sets.append(gate_eval.get("results") or [])

    # A run that recorded nothing must not pass by virtue of having no numbers
    # to compare — see sanity_results().
    sanity: List[Dict[str, Any]] = []
    if metrics_exist:
        sanity = sanity_results(storage.load_json(metrics_path))
        if sanity:
            result_sets.append(sanity)
        if baseline_id:
            drift = topology_results(
                _load_run_meta(storage, run_id), _load_run_meta(storage, baseline_id)
            )
            if drift:
                result_sets.append(drift)

    if _is_true(analysis_cfg.get("allow_no_data")):
        _downgrade_no_data(result_sets)

    if result_sets:
        combined = merge_results(result_sets)
        combined["run_id"] = run_id
        if baseline_id:
            combined["baseline_id"] = baseline_id
        if gate_eval:
            combined["gate"] = gate_eval.get("gate")
        storage.save_json(storage.analysis_path(run_id), combined)
        analysis = combined

    locust_code = int(run_result.get("returncode") or 0)
    rules_advisory = _is_true(analysis_cfg.get("rules_advisory"))
    set_baseline = False
    sanity_failed = any(res.get("status") == "NO_DATA" for res in sanity)
    if args.set_baseline and metrics_exist and not sanity_failed:
        if gate_eval:
            # When gate is configured, use gate status for baseline eligibility.
            # Regression rules may fluctuate between runs and should not block baseline.
            set_baseline = _gate_status(gate_eval) in ("PASS", "WARNING")
        elif analysis:
            set_baseline = analysis.get("status") == "PASS"
        else:
            set_baseline = locust_code == 0
    if set_baseline:
        storage.set_baseline(run_id)

    # Append run to history for trend tracking
    artifacts_cfg = _get_section(config, "artifacts")
    max_history = int(artifacts_cfg.get("history", 0))
    if max_history > 0 and metrics_exist:
        current_metrics = storage.load_json(metrics_path)
        run_meta_path = storage.run_meta_path(run_id)
        run_meta = storage.load_json(run_meta_path) if run_meta_path.exists() else {}
        storage.append_to_history(run_id, current_metrics, run_meta, max_history)

    history_runs = storage.load_history().get("runs", [])
    title = args.title or report_cfg.get("title") or "CI Load Test Report"
    output_path = args.output or report_cfg.get("output")
    _report(storage, run_id, baseline_id, title, output_path,
            report_cfg=report_cfg, history_runs=history_runs)
    _write_exports(args, storage, run_id, baseline_id, analysis_cfg, title)
    if getattr(args, "prune", False):
        _prune_runs(storage, run_id)

    if not metrics_exist:
        return locust_code or 1

    code = 0
    status = None
    if analysis:
        fail_on = _resolve_fail_on(args.fail_on, analysis_cfg)
        if rules_advisory and gate_eval:
            # Opt-in legacy behaviour: baseline regression rules are reported
            # but only the gate (plus sanity checks) decides the exit code.
            status = _gate_status(gate_eval)
            if sanity_failed:
                status = "NO_DATA"
        else:
            # Default: everything that was evaluated counts. 'loco ci' and
            # 'loco analyze' now agree on the same artifacts.
            status = analysis.get("status")
        code = _exit_code_for_status(status, fail_on)

    if code:
        return code
    # locust's own exit code (non-zero on --exit-code-on-error, interrupted or
    # crashed runs) must not be swallowed just because the analysis was clean.
    if locust_code:
        return locust_code
    if status == "WARNING":
        return warning_code
    return 0


def cmd_comment(args: argparse.Namespace) -> int:
    """Post a summary to the build's pull/merge request, editing the earlier one."""
    from .comment import CommentError, post_comment

    body_path = Path(args.body)
    if not body_path.is_file():
        print(f"Error: summary file not found: {body_path}", file=sys.stderr)
        return 1
    body = body_path.read_text(encoding="utf-8")
    try:
        result = post_comment(
            body,
            key=args.key,
            host=args.host,
            api_url=args.api_url,
            project=args.project,
            number=args.number,
            token_env=args.token_env,
            dry_run=args.dry_run,
        )
    except CommentError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    if result is None:
        print("Not a pull or merge request build; nothing to comment on.")
        return 0
    print(result.describe())
    if result.action == "dry-run":
        print(body)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="loco",
        description="Locomotive - CI/CD load testing runner and regression analyzer for Locust",
    )
    parser.add_argument("--config", default=DEFAULT_CONFIG, help="Path to config JSON/YAML")
    parser.add_argument("--version", action="version", version=f"locomotive {__version__}")
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Show the full traceback when a command fails (also LOCO_DEBUG=1)",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    # init command
    init_parser = subparsers.add_parser("init", help="Initialize a new loconfig configuration")
    init_parser.add_argument("--output", "-o", default="loconfig.json", help="Output config file path")
    init_parser.add_argument("--openapi", help="Path to OpenAPI spec to generate request templates")
    init_parser.add_argument("--host", help="Default host URL")
    init_parser.add_argument("--github-workflow", action="store_true", help="Also generate GitHub Actions workflow")
    init_parser.add_argument(
        "--jenkinsfile", action="store_true",
        help="Also generate a Jenkinsfile that uses the Locomotive shared library",
    )
    init_parser.add_argument("--force", "-f", action="store_true", help="Overwrite existing files")

    # run command
    run_parser = subparsers.add_parser("run", help="Run locust and store metrics")
    _add_storage_args(run_parser)
    _add_run_args(run_parser)

    # analyze command
    analyze_parser = subparsers.add_parser("analyze", help="Analyze metrics vs baseline")
    _add_storage_args(analyze_parser)
    _add_analyze_args(analyze_parser)

    # report command
    report_parser = subparsers.add_parser("report", help="Generate HTML report")
    _add_storage_args(report_parser)
    _add_report_args(report_parser)

    # ci command (all-in-one)
    ci_parser = subparsers.add_parser("ci", help="Run, analyze, and report (full CI pipeline)")
    _add_storage_args(ci_parser)
    _add_run_args(ci_parser)
    _add_analyze_args(ci_parser)
    _add_report_args(ci_parser)
    ci_parser.add_argument(
        "--prune", action="store_true",
        help="After reporting, delete stored runs other than this one and the baseline",
    )

    # comment command
    comment_parser = subparsers.add_parser(
        "comment", help="Post or update the run summary on the pull/merge request",
    )
    comment_parser.add_argument("--body", required=True, help="Markdown file to post (from `ci --summary`)")
    comment_parser.add_argument(
        "--key", default="loadtest",
        help="Keeps this comment apart from other Locomotive comments on the same request",
    )
    comment_parser.add_argument(
        "--host", choices=["github", "gitlab"],
        help="Code host, when it cannot be told from the CI environment",
    )
    comment_parser.add_argument(
        "--api-url",
        help="API base URL (GitHub Enterprise: https://host/api/v3; GitLab: https://host/api/v4)",
    )
    comment_parser.add_argument("--project", help="owner/repo on GitHub; group/project or numeric id on GitLab")
    comment_parser.add_argument("--number", help="Pull request number or merge request IID")
    comment_parser.add_argument("--token-env", help="Environment variable that holds the token")
    comment_parser.add_argument(
        "--dry-run", action="store_true",
        help="Print where the comment would go and what it says, without posting",
    )

    # validate command
    subparsers.add_parser("validate", help="Statically validate the config without running")

    # diff command
    diff_parser = subparsers.add_parser("diff", help="Compare the config against an OpenAPI spec")
    diff_parser.add_argument("--openapi", required=True, help="Path to the OpenAPI spec to compare against")
    diff_parser.add_argument("--exit-zero", action="store_true", help="Always exit 0 (report only, don't fail on breaking drift)")

    return parser


def _add_run_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--locustfile", help="Path to locustfile (optional if scenario is in config)")
    parser.add_argument("--host", help="Target host URL")
    parser.add_argument("--users", help="Number of concurrent users")
    parser.add_argument("--spawn-rate", help="Users spawned per second")
    parser.add_argument("--run-time", help="Test duration (e.g., 1m, 30s)")
    parser.add_argument("--tags", help="Only run tasks with these tags (comma-separated)")
    parser.add_argument("--exclude-tags", help="Exclude tasks with these tags")
    parser.add_argument("--stop-timeout", help="Timeout for stopping users")
    parser.add_argument("--extra-arg", action="append", help="Extra arguments to pass to locust")
    parser.add_argument("--locust-cmd", help="Custom locust command")
    parser.add_argument("--set-baseline", action="store_true", help="Set this run as baseline")
    parser.add_argument("--no-validate", action="store_true", help="Skip static config validation before running")
    _add_distributed_args(parser)


def _add_distributed_args(parser: argparse.ArgumentParser) -> None:
    """Flags for spreading the load over more than one process.

    ``--users`` and ``--spawn-rate`` stay cluster-wide totals under all of
    these: locust divides them between workers itself, so `-u 500
    --processes 8` is five hundred users in total, not four thousand.
    """
    group = parser.add_argument_group("distributed load")
    group.add_argument(
        "--processes",
        help="Fork N load-generating processes on this machine (locust does the split)",
    )
    group.add_argument(
        "--master", action="store_true",
        help="Run as the master of a cluster: aggregates, gates and reports, generates no load",
    )
    group.add_argument(
        "--worker", action="store_true",
        help="Run as a worker: generates load and reports to a master, writes no artifacts",
    )
    group.add_argument("--master-host", help="Master address for --worker (default 127.0.0.1)")
    group.add_argument("--master-port", help="Master port for --worker (default 5557)")
    group.add_argument(
        "--expect-workers",
        help="How many workers the master waits for; also the denominator for data pool sharding",
    )
    group.add_argument(
        "--no-shard-data", action="store_true",
        help="Give every worker the whole data pool instead of a disjoint slice",
    )


def _add_storage_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--storage", help="Artifacts storage directory")
    parser.add_argument("--run-id", help="Unique run identifier")
    parser.add_argument("--baseline", help="Baseline run ID to compare against")


def _add_analyze_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--rules", help="Path to rules JSON file")
    parser.add_argument("--fail-on", choices=["WARNING", "DEGRADATION"], help="Exit code 1 threshold")
    parser.add_argument(
        "--warning-exit-code", type=int, metavar="CODE",
        help="Exit with CODE instead of 0 when the worst result is WARNING "
             "(e.g. 2 for GitLab allow_failure:exit_codes or Jenkins UNSTABLE)",
    )


def _add_report_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--title", help="Report title")
    parser.add_argument("--output", help="Report output path")
    parser.add_argument(
        "--summary", metavar="PATH",
        help="Also write a markdown summary (pull/merge request comment, job summary)",
    )
    parser.add_argument(
        "--junit", metavar="PATH",
        help="Also write every check as JUnit XML (GitLab MR widget, Jenkins junit step)",
    )


# ── error reporting ───────────────────────────────────────────────────
#
# Everything below exists so that a mistake in a config file reads like a
# message and not like a bug in Locomotive. A stray `weight: "ten"` used to
# come back as forty lines of traceback ending in a ValueError raised deep in
# ``scenario.py`` — accurate, and useless to the person whose CI job just went
# red. Validation catches most of these before the run now; this is the net
# under everything it cannot.

_TRACEBACK_HINT = "Run with --debug (or LOCO_DEBUG=1) for the full traceback."


def _debug_enabled(args: argparse.Namespace) -> bool:
    if getattr(args, "debug", False):
        return True
    return os.environ.get("LOCO_DEBUG", "").strip().lower() not in ("", "0", "false", "no", "off")


def _error_message(exc: BaseException) -> str:
    if isinstance(exc, FileNotFoundError):
        target = exc.filename or (exc.args[1] if len(exc.args) > 1 else exc)
        return f"file not found: {target}"
    if isinstance(exc, IsADirectoryError):
        return f"expected a file but found a directory: {exc.filename}"
    if isinstance(exc, PermissionError):
        return f"permission denied: {exc.filename}"
    if isinstance(exc, json.JSONDecodeError):
        return f"could not parse JSON: {exc}"
    if isinstance(exc, OSError):
        # Disk full, too many open files, a read-only artifacts directory.
        where = f" ({exc.filename})" if exc.filename else ""
        return f"{exc.strerror or exc}{where}"
    text = str(exc).strip()
    if type(exc).__module__.startswith("yaml"):
        # yaml's own messages already carry line and column.
        return f"could not parse YAML: {text}"
    return text or type(exc).__name__


def _run_command(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    # init writes a config; it is the one command that must not need one.
    if args.command == "init":
        return cmd_init(args)
    # comment only needs the summary file and the environment.
    if args.command == "comment":
        return cmd_comment(args)

    try:
        config = load_config(args.config)
    except FileNotFoundError:
        print(f"Error: config file not found: {args.config}", file=sys.stderr)
        print("Run 'loco init' to create a default config.", file=sys.stderr)
        return 1

    handlers = {
        "validate": cmd_validate,
        "diff": cmd_diff,
        "run": cmd_run,
        "analyze": cmd_analyze,
        "report": cmd_report,
        "ci": cmd_ci,
    }
    handler = handlers.get(args.command)
    if handler is None:
        parser.print_help()
        return 1
    return handler(args, config)


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        return _run_command(args, parser)
    except KeyboardInterrupt:
        # The launcher handles Ctrl-C during a run and still writes artifacts;
        # this is for the seconds on either side of it.
        print("\nInterrupted.", file=sys.stderr)
        return 130
    except (ValueError, OSError) as exc:
        if _debug_enabled(args):
            traceback.print_exc()
        print(f"Error: {_error_message(exc)}", file=sys.stderr)
        if not _debug_enabled(args):
            print(_TRACEBACK_HINT, file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - last line before a traceback
        if _debug_enabled(args):
            traceback.print_exc()
            return 1
        print(f"Error: {_error_message(exc)}", file=sys.stderr)
        print(
            f"This one is unexpected — {_TRACEBACK_HINT[0].lower()}{_TRACEBACK_HINT[1:]}",
            file=sys.stderr,
        )
        return 1
