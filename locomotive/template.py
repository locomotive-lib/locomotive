"""Template generator for Locomotive configuration.

This module generates a starter configuration file that users then edit manually.
It can optionally read an OpenAPI spec to pre-populate the scenario (requests,
auth, and a login step) via the openapi module.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from .openapi import convert_path_params as _convert_path_params
from .openapi import extract_requests as _extract_requests
from .openapi import load_spec as _load_openapi
from .openapi import scaffold_scenario as _scaffold_scenario


def _extract_endpoints(spec: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Extract endpoint definitions from an OpenAPI specification.

    Delegates to the openapi module, which synthesizes request bodies and
    query params from the schema (see locomotive/openapi.py).
    """
    return _extract_requests(spec)


def _auth_examples() -> Dict[str, Any]:
    return {
        "_comment": "Uncomment and configure one of these auth methods:",
        "bearer": {"type": "bearer", "token": "${API_TOKEN}"},
        "api_key": {"type": "api_key", "header": "X-API-Key", "key": "${API_KEY}"},
        "basic": {"type": "basic", "username": "${API_USER}", "password": "${API_PASSWORD}"},
    }


def _on_start_example() -> List[Dict[str, Any]]:
    return [
        {
            "_comment": "Uncomment to run login at start of each user session",
            "name": "Login",
            "method": "POST",
            "path": "/auth/login",
            "json": {"username": "${TEST_USER}", "password": "${TEST_PASSWORD}"},
            "capture": {"auth_token": "token"},
        }
    ]


def _data_example() -> Dict[str, Any]:
    return {
        "_comment": "Rename to 'data' for data-driven values: ${data:accounts.login}",
        "accounts": {"source": "data/accounts.csv", "mode": "unique_per_user"},
        "people": {
            "_comment": "Synthetic pool: consistent fake identity per user, no file needed",
            "generate": {"count": 1000, "fields": {"email": "${fake:email}", "full_name": "${fake:name}"}},
            "mode": "unique_per_user",
        },
    }


def _flows_example() -> List[Dict[str, Any]]:
    return [
        {
            "_comment": "Rename to 'flows' for ordered multi-step journeys; captured vars chain between steps",
            "name": "Checkout",
            "weight": 2,
            "steps": [
                {"name": "Create order", "method": "POST", "path": "/orders",
                 "json": {"product_id": "${randint:1:100}"}, "capture": {"order_id": "id"}},
                {"name": "Pay", "method": "POST", "path": "/orders/${var:order_id}/pay"},
            ],
        }
    ]


def generate_template(
    output_path: Path,
    host: str = "http://localhost:8000",
    openapi_path: Optional[Path] = None,
) -> None:
    """Generate a Locomotive configuration template.

    Args:
        output_path: Where to write the config file.
        host: Default host URL.
        openapi_path: Optional path to OpenAPI spec for pre-populating the scenario.
    """
    requests: List[Dict[str, Any]] = []
    auth_block: Optional[Dict[str, Any]] = None
    on_start: Optional[List[Dict[str, Any]]] = None
    flows: Optional[List[Dict[str, Any]]] = None

    if openapi_path and openapi_path.exists():
        spec = _load_openapi(openapi_path)
        scaffold = _scaffold_scenario(spec)
        requests = scaffold.get("requests") or []
        auth_block = scaffold.get("auth")
        on_start = scaffold.get("on_start")
        flows = scaffold.get("flows")

    # If no requests from OpenAPI, add example placeholders
    if not requests:
        requests = [
            {
                "name": "Health Check",
                "method": "GET",
                "path": "/health",
                "weight": 1,
                "tags": ["smoke"],
                "_comment": "Remove this example and add your actual endpoints",
            },
            {
                "name": "Example POST",
                "method": "POST",
                "path": "/api/resource",
                "weight": 2,
                "json": {"field": "value"},
                "tags": ["api"],
                "_comment": "Example POST request with JSON body",
            },
        ]

    # Assemble the scenario: use detected auth / login when available,
    # otherwise fall back to commented example scaffolds.
    scenario: Dict[str, Any] = {
        "think_time": {"min": 0.5, "max": 2.0},
        "headers": {"Accept": "application/json", "Content-Type": "application/json"},
    }
    if auth_block:
        scenario["auth"] = auth_block
    else:
        scenario["_auth_examples"] = _auth_examples()
    if on_start:
        scenario["on_start"] = on_start
    else:
        scenario["_on_start_example"] = _on_start_example()
    scenario["_data_example"] = _data_example()
    if flows:
        scenario["flows"] = flows
    else:
        scenario["_flows_example"] = _flows_example()
    scenario["requests"] = requests

    config: Dict[str, Any] = {
        "_comment": "Locomotive Configuration - edit this file for your project",
        "load": {
            "host": host,
            "users": 10,
            "spawn_rate": 2,
            "run_time": "1m",
        },
        "scenario": scenario,
        "artifacts": {
            "storage": "artifacts",
            "run_id": "${GITHUB_SHA:-local}",
            "history": 30,
            "_comment_history": "Number of recent runs to keep in history.json for trend charts (0 = disabled)",
        },
        "analysis": {
            "mode": "resilience",
            "gate": {
                "min_requests": 100,
                "thresholds": {
                    "error_rate": {"fail": 5, "_comment": "Fail if error rate exceeds 5%"}
                },
            },
            "rules": [
                {"metric": "p95_ms", "mode": "relative", "direction": "increase",
                 "warn": 10, "fail": 25,
                 "_comment": "Fail if p95 latency increases >25% vs baseline"},
                {"metric": "error_rate", "mode": "absolute", "direction": "increase",
                 "warn": 1, "fail": 5, "_comment": "Fail if error rate exceeds 5%"},
                {"metric": "rps", "mode": "relative", "direction": "decrease",
                 "warn": 10, "fail": 20, "_comment": "Fail if throughput drops >20% vs baseline"},
            ],
            "fail_on": "DEGRADATION",
        },
        "report": {
            "title": "Load Test Report",
            "output": "artifacts/report.html",
            "_comment_preset": "Preset: 'default', 'latency', 'throughput', or 'errors'. Applied first, then overrides below.",
            "theme": {"mode": "light", "_comment_mode": "'light' or 'dark'"},
            "branding": {
                "name": "Locomotive",
                "_comment_name": "Company/project name shown in footer. Set 'color' to change brand name color.",
            },
            "sections": ["kpi", "charts", "regression", "endpoints", "trends"],
            "_comment_sections": "Controls which sections appear and in what order. Remove to hide.",
            "timezone": "UTC",
            "_comment_timezone": "Timezone for dates in report. Examples: 'UTC', 'UTC+3', 'UTC-5:30'",
            "trends": {
                "metrics": ["p95_ms", "rps", "error_rate"],
                "_comment": "Metrics to show on trend charts. Requires artifacts.history > 0.",
            },
        },
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")


def generate_rules_template(output_path: Path) -> None:
    """Generate a rules.json template."""
    rules = {
        "_comment": "Performance regression rules",
        "rules": [
            {"metric": "p95_ms", "mode": "relative", "direction": "increase", "warn": 10, "fail": 25},
            {"metric": "p99_ms", "mode": "relative", "direction": "increase", "warn": 15, "fail": 30},
            {"metric": "avg_ms", "mode": "relative", "direction": "increase", "warn": 10, "fail": 20},
            {"metric": "rps", "mode": "relative", "direction": "decrease", "warn": 10, "fail": 20},
            {"metric": "error_rate", "mode": "absolute", "direction": "increase", "warn": 0.5, "fail": 2.0},
        ],
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(rules, indent=2), encoding="utf-8")


def generate_github_workflow(output_path: Path, config_name: str = "loconfig.json") -> None:
    """Generate a GitHub Actions workflow template."""
    workflow = f'''name: Load Test

on:
  push:
    branches: [main, master]
  pull_request:
    branches: [main, master]

jobs:
  loadtest:
    runs-on: ubuntu-latest

    steps:
      - uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.11'

      - name: Install dependencies
        run: |
          pip install locomotive locust

      # TODO: Add step to start your service here
      # - name: Start service
      #   run: docker-compose up -d

      - name: Run load test
        run: loco --config {config_name} ci
        env:
          # Add your environment variables here
          # API_TOKEN: ${{{{ secrets.API_TOKEN }}}}
          DUMMY_SERVICE_URL: http://localhost:8000

      - name: Upload artifacts
        uses: actions/upload-artifact@v4
        if: always()
        with:
          name: loadtest-results
          path: artifacts/

      # Set baseline on push (each branch listed in push.branches maintains its own baseline)
      - name: Set baseline
        if: github.event_name == 'push'
        run: loco --config {config_name} ci --set-baseline
'''

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(workflow, encoding="utf-8")
