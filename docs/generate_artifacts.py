"""Generate synthetic artifacts for documentation screenshots.

Creates realistic test data with baseline comparisons showing
PASS, WARNING, and DEGRADATION scenarios.
"""
import csv
import json
import math
import os
import random
from pathlib import Path

BASE = Path(__file__).parent / "artifacts"

# ── Scenario definitions ──────────────────────────────────────────────
# Each run has different metrics to produce different regression results.

RUNS = {
    "baseline": {
        "rps": 145.2,
        "avg_ms": 42.3,
        "median_ms": 38.0,
        "min_ms": 2.1,
        "max_ms": 187.4,
        "p95_ms": 95.0,
        "p99_ms": 142.0,
        "error_rate": 0.8,
        "requests": 8712,
        "failures": 70,
        "started_at": "2026-03-01T10:00:00+00:00",
        "finished_at": "2026-03-01T10:01:00+00:00",
        "users": 50,
        "spawn_rate": 10,
        "run_time": "60s",
    },
    "run-pass": {
        # Slightly better than baseline → PASS with nice green deltas
        "rps": 152.8,
        "avg_ms": 39.1,
        "median_ms": 35.0,
        "min_ms": 1.8,
        "max_ms": 165.2,
        "p95_ms": 88.0,
        "p99_ms": 131.0,
        "error_rate": 0.5,
        "requests": 9168,
        "failures": 46,
        "started_at": "2026-03-03T10:00:00+00:00",
        "finished_at": "2026-03-03T10:01:00+00:00",
        "users": 50,
        "spawn_rate": 10,
        "run_time": "60s",
    },
    "run-warn": {
        # Moderately worse → WARNING on p95, p99
        "rps": 138.4,
        "avg_ms": 48.7,
        "median_ms": 43.0,
        "min_ms": 2.5,
        "max_ms": 245.8,
        "p95_ms": 112.0,
        "p99_ms": 198.0,
        "error_rate": 1.2,
        "requests": 8304,
        "failures": 100,
        "started_at": "2026-03-04T10:00:00+00:00",
        "finished_at": "2026-03-04T10:01:00+00:00",
        "users": 50,
        "spawn_rate": 10,
        "run_time": "60s",
    },
    "run-degrade": {
        # Significantly worse → DEGRADATION on latency, error rate
        "rps": 98.5,
        "avg_ms": 78.4,
        "median_ms": 65.0,
        "min_ms": 3.8,
        "max_ms": 524.1,
        "p95_ms": 185.0,
        "p99_ms": 412.0,
        "error_rate": 6.3,
        "requests": 5910,
        "failures": 372,
        "started_at": "2026-03-05T10:00:00+00:00",
        "finished_at": "2026-03-05T10:01:00+00:00",
        "users": 50,
        "spawn_rate": 10,
        "run_time": "60s",
    },
}

# Endpoint breakdown (per-run multipliers applied later)
ENDPOINTS = [
    {"type": "GET", "name": "List Users", "weight": 5, "base_avg": 35, "base_p95": 78},
    {"type": "POST", "name": "Create Order", "weight": 3, "base_avg": 55, "base_p95": 120},
    {"type": "GET", "name": "Get Product", "weight": 4, "base_avg": 28, "base_p95": 62},
    {"type": "GET", "name": "Health Check", "weight": 1, "base_avg": 5, "base_p95": 12},
    {"type": "PUT", "name": "Update Profile", "weight": 2, "base_avg": 48, "base_p95": 105},
]

# Rules for analysis
RULES = [
    {"metric": "p95_ms", "mode": "relative", "direction": "increase", "warn": 10, "fail": 25},
    {"metric": "p99_ms", "mode": "relative", "direction": "increase", "warn": 15, "fail": 30},
    {"metric": "avg_ms", "mode": "relative", "direction": "increase", "warn": 10, "fail": 25},
    {"metric": "error_rate", "mode": "absolute", "direction": "increase", "warn": 1.0, "fail": 5.0},
    {"metric": "rps", "mode": "relative", "direction": "decrease", "warn": 10, "fail": 20},
]


def _run_multiplier(run_id: str) -> float:
    """How much to scale endpoint metrics relative to baseline."""
    return {
        "baseline": 1.0,
        "run-pass": 0.92,
        "run-warn": 1.18,
        "run-degrade": 1.85,
    }[run_id]


def _error_multiplier(run_id: str) -> float:
    return {
        "baseline": 1.0,
        "run-pass": 0.6,
        "run-warn": 1.5,
        "run-degrade": 7.5,
    }[run_id]


def write_metrics(run_id: str, data: dict) -> None:
    path = BASE / "runs" / run_id / "metrics.json"
    metrics = {
        "avg_ms": data["avg_ms"],
        "error_rate": data["error_rate"],
        "failures": data["failures"],
        "max_ms": data["max_ms"],
        "median_ms": data["median_ms"],
        "min_ms": data["min_ms"],
        "p95_ms": data["p95_ms"],
        "p99_ms": data["p99_ms"],
        "requests": data["requests"],
        "rps": data["rps"],
    }
    path.write_text(json.dumps(metrics, indent=2) + "\n")


def write_run_json(run_id: str, data: dict) -> None:
    path = BASE / "runs" / run_id / "run.json"
    meta = {
        "run_id": run_id,
        "host": "https://staging.myapp.com",
        "users": data["users"],
        "spawn_rate": data["spawn_rate"],
        "run_time": data["run_time"],
        "started_at": data["started_at"],
        "finished_at": data["finished_at"],
        "returncode": 0,
        "locustfile": f"runs/{run_id}/generated/generated_locustfile.py",
        "command": ["locust", "-f", "generated_locustfile.py", "--headless",
                     f"--users={data['users']}", f"--spawn-rate={data['spawn_rate']}",
                     f"--run-time={data['run_time']}", f"--host=https://staging.myapp.com"],
        "meta": {"ci": {}},
    }
    path.write_text(json.dumps(meta, indent=2) + "\n")


def write_stats_csv(run_id: str, data: dict) -> None:
    """Write locust_stats.csv with per-endpoint rows."""
    path = BASE / "runs" / run_id / "raw" / "locust_stats.csv"
    mult = _run_multiplier(run_id)
    err_mult = _error_multiplier(run_id)
    total_weight = sum(e["weight"] for e in ENDPOINTS)

    rows = []
    for ep in ENDPOINTS:
        reqs = int(data["requests"] * ep["weight"] / total_weight)
        fails = int(reqs * (data["error_rate"] / 100) * (err_mult / _error_multiplier(run_id) if run_id == "baseline" else 1.0))
        avg = ep["base_avg"] * mult
        p95 = ep["base_p95"] * mult
        med = avg * 0.88
        minv = avg * 0.05
        maxv = p95 * 1.8
        p50 = med
        p66 = avg * 0.95
        p75 = avg * 1.05
        p80 = avg * 1.12
        p90 = p95 * 0.88
        p98 = p95 * 1.15
        p99 = p95 * 1.3
        p999 = maxv * 0.9
        p9999 = maxv * 0.95
        rps = reqs / 60.0

        rows.append({
            "Type": ep["type"],
            "Name": ep["name"],
            "Request Count": reqs,
            "Failure Count": fails,
            "Median Response Time": med,
            "Average Response Time": avg,
            "Min Response Time": minv,
            "Max Response Time": maxv,
            "Average Content Size": 256,
            "Requests/s": rps,
            "Failures/s": fails / 60.0,
            "50%": p50, "66%": p66, "75%": p75, "80%": p80,
            "90%": p90, "95%": p95, "98%": p98, "99%": p99,
            "99.9%": p999, "99.99%": p9999, "100%": maxv,
        })

    # Aggregated row
    total_reqs = sum(r["Request Count"] for r in rows)
    total_fails = sum(r["Failure Count"] for r in rows)
    rows.append({
        "Type": "",
        "Name": "Aggregated",
        "Request Count": total_reqs,
        "Failure Count": total_fails,
        "Median Response Time": data["median_ms"],
        "Average Response Time": data["avg_ms"],
        "Min Response Time": data["min_ms"],
        "Max Response Time": data["max_ms"],
        "Average Content Size": 256,
        "Requests/s": data["rps"],
        "Failures/s": total_fails / 60.0,
        "50%": data["median_ms"], "66%": data["median_ms"] * 1.1,
        "75%": data["median_ms"] * 1.2, "80%": data["median_ms"] * 1.3,
        "90%": data["p95_ms"] * 0.85, "95%": data["p95_ms"],
        "98%": data["p99_ms"] * 0.9, "99%": data["p99_ms"],
        "99.9%": data["max_ms"] * 0.9, "99.99%": data["max_ms"] * 0.95,
        "100%": data["max_ms"],
    })

    cols = list(rows[0].keys())
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)


def write_history_csv(run_id: str, data: dict) -> None:
    """Write locust_stats_history.csv with time series data."""
    path = BASE / "runs" / run_id / "raw" / "locust_stats_history.csv"
    cols = [
        "Timestamp", "User Count", "Type", "Name",
        "Requests/s", "Failures/s",
        "50%", "66%", "75%", "80%", "90%", "95%", "98%", "99%", "99.9%", "99.99%", "100%",
        "Total Request Count", "Total Failure Count",
        "Total Median Response Time", "Total Average Response Time",
        "Total Min Response Time", "Total Max Response Time", "Total Average Content Size",
    ]

    # Parse start timestamp
    from datetime import datetime, timezone
    start = datetime.fromisoformat(data["started_at"])
    base_ts = int(start.timestamp())
    duration_s = int(data["run_time"].rstrip("s"))
    users = data["users"]

    rows = []
    for i in range(duration_s + 1):
        ts = base_ts + i
        # Ramp up users in first 5 seconds
        uc = min(users, int(users * (i / 5.0))) if i < 5 else users
        # Gradually increasing RPS and cumulative requests
        progress = i / duration_s if duration_s > 0 else 1
        curr_rps = data["rps"] * (0.3 + 0.7 * min(1.0, i / 10.0)) if i > 0 else 0
        cum_reqs = int(data["requests"] * progress)
        cum_fails = int(data["failures"] * progress)

        # Add some variation to response times
        jitter = 1.0 + 0.15 * math.sin(i * 0.5)
        p50 = data["median_ms"] * jitter
        p95 = data["p95_ms"] * jitter
        p99 = data["p99_ms"] * jitter

        row = {
            "Timestamp": ts,
            "User Count": uc,
            "Type": "",
            "Name": "Aggregated",
            "Requests/s": f"{curr_rps:.6f}",
            "Failures/s": f"{(cum_fails / max(i, 1)):.6f}",
            "50%": int(p50) if i > 0 else "N/A",
            "66%": int(p50 * 1.08) if i > 0 else "N/A",
            "75%": int(p50 * 1.15) if i > 0 else "N/A",
            "80%": int(p50 * 1.22) if i > 0 else "N/A",
            "90%": int(p95 * 0.88) if i > 0 else "N/A",
            "95%": int(p95) if i > 0 else "N/A",
            "98%": int(p99 * 0.92) if i > 0 else "N/A",
            "99%": int(p99) if i > 0 else "N/A",
            "99.9%": int(data["max_ms"] * 0.85) if i > 0 else "N/A",
            "99.99%": int(data["max_ms"] * 0.95) if i > 0 else "N/A",
            "100%": int(data["max_ms"]) if i > 0 else "N/A",
            "Total Request Count": cum_reqs,
            "Total Failure Count": cum_fails,
            "Total Median Response Time": p50,
            "Total Average Response Time": data["avg_ms"] * jitter,
            "Total Min Response Time": data["min_ms"],
            "Total Max Response Time": data["max_ms"],
            "Total Average Content Size": 256,
        }
        rows.append(row)

    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)


def write_empty_csvs(run_id: str) -> None:
    """Write empty failures and exceptions CSVs."""
    raw = BASE / "runs" / run_id / "raw"
    (raw / "locust_failures.csv").write_text("Method,Name,Error,Occurrences\n")
    (raw / "locust_exceptions.csv").write_text("Count,Message,Traceback\n")


def evaluate_rules(current: dict, baseline: dict) -> dict:
    """Run regression analysis rules and produce analysis.json data."""
    results = []
    for rule in RULES:
        m = rule["metric"]
        curr_val = current[m]
        base_val = baseline[m]

        if rule["mode"] == "relative":
            if rule["direction"] == "increase":
                delta_pct = ((curr_val - base_val) / base_val) * 100
                magnitude = max(0, delta_pct)
            else:  # decrease
                delta_pct = ((curr_val - base_val) / base_val) * 100
                magnitude = max(0, -delta_pct)

            if magnitude >= rule["fail"]:
                status = "DEGRADATION"
            elif magnitude >= rule["warn"]:
                status = "WARNING"
            else:
                status = "PASS"

            results.append({
                "metric": m,
                "mode": "relative",
                "direction": rule["direction"],
                "warn": rule["warn"],
                "fail": rule["fail"],
                "baseline": base_val,
                "current": curr_val,
                "delta_percent": delta_pct,
                "status": status,
                "reason": None,
            })
        else:  # absolute
            if rule["direction"] == "increase":
                if curr_val >= rule["fail"]:
                    status = "DEGRADATION"
                elif curr_val >= rule["warn"]:
                    status = "WARNING"
                else:
                    status = "PASS"
            else:
                if curr_val <= rule["fail"]:
                    status = "DEGRADATION"
                elif curr_val <= rule["warn"]:
                    status = "WARNING"
                else:
                    status = "PASS"

            delta_pct = ((curr_val - base_val) / base_val * 100) if base_val else 0

            results.append({
                "metric": m,
                "mode": "absolute",
                "direction": rule["direction"],
                "warn": rule["warn"],
                "fail": rule["fail"],
                "baseline": base_val,
                "current": curr_val,
                "delta_percent": delta_pct,
                "status": status,
                "reason": None,
            })

    summary = {"PASS": 0, "WARNING": 0, "DEGRADATION": 0, "SKIP": 0}
    for r in results:
        summary[r["status"]] += 1

    overall = "PASS"
    if summary["DEGRADATION"] > 0:
        overall = "DEGRADATION"
    elif summary["WARNING"] > 0:
        overall = "WARNING"

    return {
        "run_id": None,  # will be set per-run
        "baseline_id": "baseline",
        "evaluated_at": None,  # will be set per-run
        "status": overall,
        "summary": summary,
        "results": results,
    }


def write_analysis(run_id: str, data: dict, baseline_data: dict) -> None:
    path = BASE / "runs" / run_id / "analysis.json"
    analysis = evaluate_rules(data, baseline_data)
    analysis["run_id"] = run_id
    analysis["evaluated_at"] = data["finished_at"]
    path.write_text(json.dumps(analysis, indent=2) + "\n")


def write_baseline_json() -> None:
    path = BASE / "baseline.json"
    path.write_text(json.dumps({"baseline_id": "baseline"}, indent=2) + "\n")


def write_history_json() -> None:
    path = BASE / "history.json"
    runs = []
    for run_id in ["baseline", "run-pass", "run-warn", "run-degrade"]:
        d = RUNS[run_id]
        runs.append({
            "run_id": run_id,
            "started_at": d["started_at"],
            "rps": d["rps"],
            "avg_ms": d["avg_ms"],
            "median_ms": d["median_ms"],
            "p95_ms": d["p95_ms"],
            "p99_ms": d["p99_ms"],
            "max_ms": d["max_ms"],
            "error_rate": d["error_rate"],
            "error_rate_4xx": None,
            "error_rate_5xx": None,
            "error_rate_503": None,
            "requests": d["requests"],
            "failures": d["failures"],
        })
    path.write_text(json.dumps({"runs": runs}, indent=2) + "\n")


def main():
    baseline_data = RUNS["baseline"]

    for run_id, data in RUNS.items():
        print(f"Generating {run_id}...")
        write_metrics(run_id, data)
        write_run_json(run_id, data)
        write_stats_csv(run_id, data)
        write_history_csv(run_id, data)
        write_empty_csvs(run_id)

        if run_id != "baseline":
            write_analysis(run_id, data, baseline_data)

    write_baseline_json()
    write_history_json()
    print("Done! All artifacts generated.")


if __name__ == "__main__":
    main()
