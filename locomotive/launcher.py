from __future__ import annotations

import csv
import os
import re
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .storage import Storage
from .utils import utc_now


def _normalize_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


_STATUS_RE = re.compile(r"\b([1-5]\d{2})\b")

# Locust's failure CSV holds free text, and any three digits in it used to be
# read as an HTTP status. "[Errno 111] Connection refused" became a 111 —
# a code no bucket claims, so a connection-refused run reported zero 4xx,
# zero 5xx and zero other failures while every single request had failed.
# These patterns look for a status where one is actually announced.
_EXPLICIT_STATUS_PATTERNS = (
    # Our own generated wording: "status 503", "status 503 not in [200]",
    # and — crucially — "status 0 not in [200]" for a request that never got
    # a response at all.
    re.compile(r"\bstatus(?:[ _-]?code)?\b\D{0,3}(\d{1,3})\b", re.I),
    # requests' HTTPError: "503 Server Error: Service Unavailable for url: ..."
    re.compile(r"\b(\d{3})\s+(?:client|server)\s+error\b", re.I),
    re.compile(r"\bHTTP[/ ](?:1\.[01]\s+)?(\d{3})\b", re.I),
)

# Numbers that look like statuses but are not, removed before the last-resort
# scan: errnos, ports, and any URL (whose path may well end in /404).
_STATUS_NOISE_RE = re.compile(
    r"\[Errno \d+\]"
    r"|\berrno[ =:]+\d+"
    r"|\bport[ =:]+\d+"
    r"|\bhttps?://\S+"
    # ConnectionRefusedError(111, 'Connection refused') — the bare errno form.
    # HTTPError('503 ...') is untouched: its number sits behind a quote.
    r"|\b\w*Error\(\s*\d+",
    re.I,
)


def _safe_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        if isinstance(value, (int, float)):
            return float(value)
        text = str(value).strip()
        if text == "" or text.upper() == "N/A":
            return None
        return float(text)
    except ValueError:
        return None


def _safe_int(value: Any) -> Optional[int]:
    num = _safe_float(value)
    if num is None:
        return None
    return int(num)


def _clear_previous_csvs(raw_dir: Path) -> None:
    """Remove CSVs left by an earlier run into the same directory.

    run_id defaults to the commit sha, so re-running the same commit reuses the
    directory. If locust then fails to start, the old CSVs are still there and
    get parsed as if they were this run's results.
    """
    if not raw_dir.exists():
        return
    for path in raw_dir.glob("locust*.csv"):
        try:
            path.unlink()
        except OSError:
            pass


def _find_stats_csv(raw_dir: Path) -> Optional[Path]:
    preferred = raw_dir / "locust_stats.csv"
    if preferred.exists():
        return preferred
    matches = sorted(raw_dir.glob("*_stats.csv"))
    if matches:
        return matches[0]
    return None


def _find_failures_csv(raw_dir: Path) -> Optional[Path]:
    preferred = raw_dir / "locust_failures.csv"
    if preferred.exists():
        return preferred
    matches = sorted(raw_dir.glob("*_failures.csv"))
    if matches:
        return matches[0]
    return None


def find_stats_history_csv(raw_dir: Path) -> Optional[Path]:
    preferred = raw_dir / "locust_stats_history.csv"
    if preferred.exists():
        return preferred
    matches = sorted(raw_dir.glob("*_stats_history.csv"))
    if matches:
        return matches[0]
    return None


def _select_aggregate_row(rows: List[Dict[str, str]]) -> Optional[Dict[str, str]]:
    for row in rows:
        name = (row.get("Name") or "").strip().lower()
        typ = (row.get("Type") or "").strip().lower()
        if name == "aggregated" or typ == "aggregated":
            return row
    for row in rows:
        name = (row.get("Name") or "").strip().lower()
        if name in {"total", "overall"}:
            return row
    if rows:
        return rows[0]
    return None


def parse_locust_stats(path: Path) -> Dict[str, Any]:
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)
    row = _select_aggregate_row(rows)
    if not row:
        return {}

    norm_map = {_normalize_key(key): key for key in row.keys()}

    def fetch(*candidates: str) -> Optional[str]:
        for cand in candidates:
            key = norm_map.get(_normalize_key(cand))
            if key is not None:
                return row.get(key)
        return None

    requests = _safe_int(fetch("Requests", "Request Count"))
    failures = _safe_int(fetch("Failures", "Failure Count"))
    error_rate = _safe_float(fetch("Failure%", "Failure %"))
    if error_rate is None and requests:
        error_rate = (failures or 0) / requests * 100

    metrics: Dict[str, Any] = {
        "requests": requests,
        "failures": failures,
        "error_rate": error_rate,
        "avg_ms": _safe_float(fetch("Average Response Time", "Average Response Time (ms)")),
        "median_ms": _safe_float(fetch("Median Response Time", "Median Response Time (ms)")),
        "min_ms": _safe_float(fetch("Min Response Time", "Min Response Time (ms)")),
        "max_ms": _safe_float(fetch("Max Response Time", "Max Response Time (ms)")),
        "p95_ms": _safe_float(fetch("95%", "95% Response Time")),
        "p99_ms": _safe_float(fetch("99%", "99% Response Time")),
        "rps": _safe_float(fetch("Requests/s", "Requests/s")),
    }

    return metrics


def _extract_status_code(text: str) -> Optional[int]:
    """Read the HTTP status out of a Locust failure message, if it has one."""
    if not text:
        return None
    for pattern in _EXPLICIT_STATUS_PATTERNS:
        match = pattern.search(text)
        if match:
            # An explicit marker is the answer even when the answer is "no
            # status": "status 0 not in [200]" is our wording for a request
            # that never got a response, and reading the 200 out of the
            # expectation would file a connection failure as a success code.
            code = int(match.group(1))
            return code if 100 <= code <= 599 else None
    match = _STATUS_RE.search(_STATUS_NOISE_RE.sub(" ", text))
    if not match:
        return None
    code = int(match.group(1))
    if 100 <= code <= 599:
        return code
    return None


def parse_locust_failures(path: Path) -> Dict[str, Any]:
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)
    if not rows:
        return {}

    norm_map = {_normalize_key(key): key for key in rows[0].keys()}

    def fetch(row: Dict[str, str], *candidates: str) -> Optional[str]:
        for cand in candidates:
            key = norm_map.get(_normalize_key(cand))
            if key is not None:
                return row.get(key)
        return None

    failures_4xx = 0
    failures_5xx = 0
    failures_503 = 0
    failures_other = 0

    for row in rows:
        error_text = fetch(row, "Error", "Error Type", "Exception", "Message") or ""
        occurrences = _safe_int(fetch(row, "Occurrences", "Count", "Number"))
        if occurrences is None or occurrences <= 0:
            occurrences = 1

        status = _extract_status_code(error_text)
        if status is None or not 400 <= status < 600:
            # Connection refused, DNS failure, timeout, a failed `expect`
            # on a 200 — none of them is a 4xx or a 5xx, and all of them
            # used to fall out of every bucket, so the breakdown silently
            # added up to less than the failure count beside it.
            failures_other += occurrences
            continue
        if status < 500:
            failures_4xx += occurrences
        else:
            failures_5xx += occurrences
            if status == 503:
                failures_503 += occurrences

    return {
        "failures_4xx": failures_4xx,
        "failures_5xx": failures_5xx,
        "failures_503": failures_503,
        "failures_other": failures_other,
    }


def parse_locust_stats_history(path: Path) -> List[Dict[str, Any]]:
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)
    if not rows:
        return []

    norm_map = {_normalize_key(key): key for key in rows[0].keys()}

    def fetch(row: Dict[str, str], *candidates: str) -> Optional[str]:
        for cand in candidates:
            key = norm_map.get(_normalize_key(cand))
            if key is not None:
                return row.get(key)
        return None

    history: List[Dict[str, Any]] = []
    for row in rows:
        timestamp = _safe_float(fetch(row, "Timestamp", "Time", "Epoch"))
        rps = _safe_float(fetch(row, "Requests/s", "Total RPS", "Total Requests/s", "RPS"))
        failures_s = _safe_float(fetch(row, "Failures/s", "Total Failures/s", "Failure/s"))
        history.append(
            {
                "timestamp": timestamp,
                "rps": rps,
                "failures_s": failures_s,
            }
        )
    return history


# Разбивка отказов для прогона, в котором отказов не было.
_ZERO_FAILURE_BREAKDOWN = {
    "failures_4xx": 0,
    "failures_5xx": 0,
    "failures_503": 0,
    "failures_other": 0,
}


def _apply_failure_rates(metrics: Dict[str, Any], breakdown: Dict[str, Any]) -> None:
    requests = _safe_float(metrics.get("requests"))
    if not requests:
        return

    def rate(count: Optional[int]) -> Optional[float]:
        if count is None:
            return None
        return count / requests * 100

    failures_4xx = breakdown.get("failures_4xx")
    failures_5xx = breakdown.get("failures_5xx")
    failures_503 = breakdown.get("failures_503")

    metrics["error_rate_4xx"] = rate(failures_4xx)
    metrics["error_rate_5xx"] = rate(failures_5xx)
    metrics["error_rate_503"] = rate(failures_503)

    failures_total = _safe_int(metrics.get("failures"))
    if failures_total is not None and failures_503 is not None:
        failures_non_503 = max(0, failures_total - failures_503)
        metrics["failures_non_503"] = failures_non_503
        metrics["error_rate_non_503"] = rate(failures_non_503)


# ── process control ───────────────────────────────────────────────────

_DURATION_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
_DURATION_RE = re.compile(r"(\d+(?:\.\d+)?)\s*([smhd]?)", re.I)

# How long locust may take to shut down after being asked, before it is
# killed. Locust's own --stop-timeout is added on top when one is set.
GRACE_SECONDS = 30
# `None` is a meaningful timeout (wait forever), so absence needs its own value.
_MISSING = object()
# How much longer than --run-time the whole run may take before we call it
# hung. Ramp-up, shutdown and CSV flushing all happen outside run-time.
_TIMEOUT_MARGIN_SECONDS = 120


def parse_duration(value: Any) -> Optional[float]:
    """Parse locust's duration syntax ("30s", "5m", "1h30m") into seconds."""
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    total = 0.0
    matched = False
    for number, unit in _DURATION_RE.findall(text):
        total += float(number) * _DURATION_UNITS[unit.lower() or "s"]
        matched = True
    return total if matched else None


def _default_timeout(run_time: Any, stop_timeout: Any) -> Optional[float]:
    """The wall-clock budget for one locust process.

    ``subprocess.run`` with no timeout means a locust that never honours
    ``--run-time`` — a hung greenlet, a worker that never connects — pins a
    CI job until the CI platform kills it, with no artifacts and no report.
    """
    seconds = parse_duration(run_time)
    if seconds is None:
        return None
    stop = parse_duration(stop_timeout) or 0.0
    return seconds + stop + max(_TIMEOUT_MARGIN_SECONDS, seconds * 0.5)


def resolve_topology(config: Dict[str, Any]) -> Dict[str, Any]:
    """How many processes generate load, and what this one's role is.

    Three shapes, and locust spells them differently:

    * one process — nothing on the command line;
    * ``processes: N`` — locust forks N children itself and appoints the
      parent master, setting ``expect_workers`` from the same number
      (``main.py``), so an explicit ``--expect-workers`` here would only be a
      second chance to get it wrong;
    * ``master``/``worker`` — separate hosts (or containers), where the worker
      count is something only the operator knows, so ``expect_workers`` is
      how the master is told.

    The returned dict is also what lands in ``run.json``: a report that says
    "p95 got worse" is a different sentence when the previous run had four
    load generators and this one has one.
    """
    processes = _safe_int(config.get("processes"))
    master = bool(config.get("master"))
    worker = bool(config.get("worker"))
    expect_workers = _safe_int(config.get("expect_workers"))

    if worker:
        role = "worker"
    elif master or (processes is not None and processes > 1):
        role = "master"
    else:
        role = "standalone"

    if role in ("standalone", "worker"):
        # A worker is one generator and does not know how many others there
        # are — the master owns that number and only tells it at connect
        # time, long after this runs.
        generators = 1
    elif processes is not None and processes > 1:
        generators = processes
    else:
        generators = expect_workers or 1

    topology: Dict[str, Any] = {"role": role, "load_generators": generators}
    if processes is not None:
        topology["processes"] = processes
    if expect_workers is not None:
        topology["expect_workers"] = expect_workers
    if role == "worker":
        topology["master_host"] = config.get("master_host") or "127.0.0.1"
    return topology


def _topology_args(config: Dict[str, Any], topology: Dict[str, Any]) -> List[str]:
    """The locust flags for this role."""
    args: List[str] = []
    processes = topology.get("processes")
    if processes is not None and processes > 1:
        args += ["--processes", str(processes)]
    if topology["role"] == "master":
        if "--processes" not in args:
            args.append("--master")
        expect_workers = topology.get("expect_workers")
        if expect_workers and processes is None:
            args += ["--expect-workers", str(expect_workers)]
    elif topology["role"] == "worker":
        args.append("--worker")
        args += ["--master-host", str(topology["master_host"])]
        port = _safe_int(config.get("master_port"))
        if port:
            args += ["--master-port", str(port)]
    return args


def _signal_process(proc: "subprocess.Popen", sig: int, own_group: bool) -> None:
    try:
        if own_group and os.name == "posix":
            # The whole group: locust's workers are children of the process
            # we started, and signalling only the parent leaves them running.
            os.killpg(os.getpgid(proc.pid), sig)
        else:
            proc.send_signal(sig)
    except (OSError, ProcessLookupError, ValueError):
        pass  # already gone


def _shutdown(proc: "subprocess.Popen", sig: int, own_group: bool, grace: float) -> int:
    """Ask locust to stop, then insist. Returns its exit code."""
    _signal_process(proc, sig, own_group)
    try:
        return proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        pass
    _signal_process(proc, signal.SIGKILL if hasattr(signal, "SIGKILL") else signal.SIGTERM, own_group)
    try:
        return proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        return -9


class LocustLauncher:
    def __init__(self, storage: Storage, run_id: str, config: Dict[str, Any]) -> None:
        self.storage = storage
        self.run_id = run_id
        self.config = config

    def run(self) -> Dict[str, Any]:
        self.storage.ensure_run(self.run_id)
        raw_dir = self.storage.raw_dir(self.run_id)
        csv_prefix = raw_dir / "locust"

        locust_cmd = self.config.get("locust_cmd") or "locust"
        locustfile = self.config.get("locustfile")
        host = self.config.get("host")
        users = self.config.get("users")
        spawn_rate = self.config.get("spawn_rate")
        run_time = self.config.get("run_time")
        tags = self.config.get("tags") or []
        exclude_tags = self.config.get("exclude_tags") or []
        stop_timeout = self.config.get("stop_timeout")
        extra_args = self.config.get("extra_args") or []

        topology = resolve_topology(self.config)
        is_worker = topology["role"] == "worker"

        if not locustfile:
            raise ValueError("locustfile is required")
        # A worker is told what to run by its master. `-u`, `-r` and
        # `--run-time` are cluster-wide totals the master owns, and locust
        # ignores them on a worker — so requiring them here would only be
        # asking for numbers that cannot mean anything.
        if not is_worker:
            if users is None:
                raise ValueError("users is required")
            if spawn_rate is None:
                raise ValueError("spawn_rate is required")
            if not run_time:
                raise ValueError("run_time is required")

        cmd: List[str] = [locust_cmd, "-f", str(locustfile), "--headless"]
        if not is_worker:
            cmd += [
                "-u",
                str(users),
                "-r",
                str(spawn_rate),
                "--run-time",
                str(run_time),
                # Only the master aggregates, so only the master has stats to
                # write. A worker given --csv writes nothing and the empty
                # files would look like a run that measured zero.
                "--csv",
                str(csv_prefix),
            ]

        cmd += _topology_args(self.config, topology)

        if host:
            cmd += ["--host", str(host)]
        if tags:
            cmd += ["--tags", ",".join(tags)]
        if exclude_tags:
            cmd += ["--exclude-tags", ",".join(exclude_tags)]
        if stop_timeout:
            cmd += ["--stop-timeout", str(stop_timeout)]
        if extra_args:
            cmd += [str(arg) for arg in extra_args]

        _clear_previous_csvs(raw_dir)

        started_at = utc_now()
        returncode, interrupted, timed_out = self._spawn(cmd, stop_timeout)
        finished_at = utc_now()

        stats_path = _find_stats_csv(raw_dir)
        metrics: Dict[str, Any] = {}
        if stats_path:
            metrics = parse_locust_stats(stats_path)
            failures_path = _find_failures_csv(raw_dir)
            breakdown = parse_locust_failures(failures_path) if failures_path else {}
            if not breakdown and _safe_int(metrics.get("failures")) == 0:
                # Locust пишет файл ошибок с одной шапкой, когда отказов не
                # было, и разбор возвращает пустой словарь. Это не «данных
                # нет», а «отказов нет»: без явных нулей пороги по
                # подкатегориям ошибок отвечают NO_DATA на безупречном
                # прогоне, а NO_DATA запрещает сохранить его как baseline.
                breakdown = _ZERO_FAILURE_BREAKDOWN.copy()
            if breakdown:
                metrics.update(breakdown)
                _apply_failure_rates(metrics, breakdown)
            self.storage.save_json(self.storage.metrics_path(self.run_id), metrics)

        run_meta = {
            "run_id": self.run_id,
            "started_at": started_at,
            "finished_at": finished_at,
            "returncode": returncode,
            "command": cmd,
            "locustfile": str(locustfile),
            "host": host,
            "users": users,
            "spawn_rate": spawn_rate,
            "run_time": run_time,
            "topology": topology,
        }
        if interrupted:
            run_meta["interrupted"] = True
        if timed_out:
            run_meta["timed_out"] = True
        extra_meta = self.config.get("meta") or {}
        if extra_meta:
            run_meta["meta"] = extra_meta
        self.storage.save_json(self.storage.run_meta_path(self.run_id), run_meta)

        result = {
            "returncode": returncode,
            "metrics": metrics,
            "stats_path": str(stats_path) if stats_path else None,
            "topology": topology,
        }
        if interrupted:
            result["interrupted"] = True
        if timed_out:
            result["timed_out"] = True
        return result

    def _spawn(self, cmd: List[str], stop_timeout: Any) -> Tuple[int, bool, bool]:
        """Run locust to completion. Returns (returncode, interrupted, timed_out).

        Neither Ctrl-C nor a hung locust used to leave anything behind:
        ``subprocess.run(cmd)`` let the KeyboardInterrupt unwind straight past
        the code that writes ``run.json`` and parses the CSVs, so a run the
        user stopped a minute early produced no artifacts at all — even
        though locust had already flushed everything it measured. Both paths
        now return normally, and the caller records what the run did produce.
        """
        timeout = self.config.get("timeout", _MISSING)
        if timeout is _MISSING:
            timeout = _default_timeout(self.config.get("run_time"),
                                       stop_timeout)
        else:
            timeout = parse_duration(timeout)
        grace = GRACE_SECONDS + (parse_duration(stop_timeout) or 0.0)

        own_group = os.name == "posix"
        popen_kwargs: Dict[str, Any] = {}
        if own_group:
            # A session of its own means the terminal's Ctrl-C reaches locust
            # only when we forward it, so exactly one shutdown is in flight.
            # Sharing the group let the signal hit both processes at once:
            # locust began shutting down while our own KeyboardInterrupt was
            # still unwinding, and the two raced over the same CSV files.
            popen_kwargs["start_new_session"] = True

        proc = subprocess.Popen(cmd, **popen_kwargs)
        try:
            return proc.wait(timeout=timeout), False, False
        except KeyboardInterrupt:
            print("\nStopping locust (Ctrl-C again to kill it)...", file=sys.stderr)
            return _shutdown(proc, signal.SIGINT, own_group, grace), True, False
        except subprocess.TimeoutExpired:
            print(
                f"locust exceeded its {timeout:.0f}s budget and was stopped; "
                "results below cover only what it had already written",
                file=sys.stderr,
            )
            return _shutdown(proc, signal.SIGTERM, own_group, grace), False, True
        except BaseException:
            # Anything else — SystemExit from a signal handler, an OSError
            # while waiting — still must not leave locust running.
            _shutdown(proc, signal.SIGTERM, own_group, grace)
            raise
