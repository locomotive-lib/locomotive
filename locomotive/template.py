"""Template generator for Locomotive configuration.

This module generates a starter configuration file that users then edit manually.
It can optionally read an OpenAPI spec to pre-populate the requests section.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional


def _load_openapi(path: Path) -> Dict[str, Any]:
    """Load OpenAPI specification from file."""
    content = path.read_text(encoding="utf-8")
    if path.suffix.lower() in {".yml", ".yaml"}:
        try:
            import yaml
        except ImportError as exc:
            raise RuntimeError("PyYAML is required for YAML OpenAPI specs") from exc
        return yaml.safe_load(content) or {}
    return json.loads(content)


def _extract_endpoints(spec: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Extract endpoint definitions from OpenAPI specification."""
    endpoints = []
    paths = spec.get("paths", {})
    
    for path, path_item in paths.items():
        if not isinstance(path_item, dict):
            continue
        
        for method in ["get", "post", "put", "patch", "delete"]:
            operation = path_item.get(method)
            if not isinstance(operation, dict):
                continue
            
            summary = operation.get("summary", "")
            operation_id = operation.get("operationId", "")
            tags = operation.get("tags", [])
            
            # Basic request template
            req: Dict[str, Any] = {
                "name": summary or operation_id or f"{method.upper()} {path}",
                "method": method.upper(),
                "path": path,
                "weight": 1,
            }
            
            # Add tags if present
            if tags:
                req["tags"] = tags
            
            # Check for query parameters
            params = operation.get("parameters", [])
            query_params = {}
            for param in params:
                if isinstance(param, dict) and param.get("in") == "query":
                    param_name = param.get("name", "")
                    if param_name:
                        query_params[param_name] = f"${{PARAM_{param_name.upper()}}}"
            
            if query_params:
                req["query"] = query_params
            
            # Check for request body
            request_body = operation.get("requestBody", {})
            if isinstance(request_body, dict):
                content = request_body.get("content", {})
                if "application/json" in content:
                    req["json"] = {"_comment": "TODO: add request body"}
            
            # Add comment about auth if security is specified
            security = operation.get("security", [])
            if security:
                req["_requires_auth"] = True
            
            endpoints.append(req)
    
    return endpoints


def generate_template(
    output_path: Path,
    host: str = "http://localhost:8000",
    openapi_path: Optional[Path] = None,
) -> None:
    """Generate a Locomotive configuration template.
    
    Args:
        output_path: Where to write the config file.
        host: Default host URL.
        openapi_path: Optional path to OpenAPI spec for pre-populating requests.
    """
    requests = []
    
    if openapi_path and openapi_path.exists():
        spec = _load_openapi(openapi_path)
        requests = _extract_endpoints(spec)
    
    # If no requests from OpenAPI, add example placeholders
    if not requests:
        requests = [
            {
                "name": "Health Check",
                "method": "GET",
                "path": "/health",
                "weight": 1,
                "tags": ["smoke"],
                "_comment": "Remove this example and add your actual endpoints"
            },
            {
                "name": "Example POST",
                "method": "POST",
                "path": "/api/resource",
                "weight": 2,
                "json": {
                    "field": "value"
                },
                "tags": ["api"],
                "_comment": "Example POST request with JSON body"
            }
        ]
    
    config: Dict[str, Any] = {
        "_comment": "Locomotive Configuration - edit this file for your project",
        "load": {
            "host": host,
            "users": 10,
            "spawn_rate": 2,
            "run_time": "1m"
        },
        "scenario": {
            "think_time": {
                "min": 0.5,
                "max": 2.0
            },
            "headers": {
                "Accept": "application/json",
                "Content-Type": "application/json"
            },
            "_auth_examples": {
                "_comment": "Uncomment and configure one of these auth methods:",
                "bearer": {
                    "type": "bearer",
                    "token": "${API_TOKEN}"
                },
                "api_key": {
                    "type": "api_key",
                    "header": "X-API-Key",
                    "key": "${API_KEY}"
                },
                "basic": {
                    "type": "basic",
                    "username": "${API_USER}",
                    "password": "${API_PASSWORD}"
                }
            },
            "_on_start_example": [
                {
                    "_comment": "Uncomment to run login at start of each user session",
                    "name": "Login",
                    "method": "POST",
                    "path": "/auth/login",
                    "json": {
                        "username": "${TEST_USER}",
                        "password": "${TEST_PASSWORD}"
                    },
                    "capture": {
                        "auth_token": "token"
                    }
                }
            ],
            "requests": requests
        },
        "artifacts": {
            "storage": "artifacts",
            "run_id": "${GITHUB_SHA:-local}",
            "history": 30,
            "_comment_history": "Number of recent runs to keep in history.json for trend charts (0 = disabled)"
        },
        "analysis": {
            "mode": "resilience",
            "gate": {
                "min_requests": 100,
                "thresholds": {
                    "error_rate": {
                        "fail": 5,
                        "_comment": "Fail if error rate exceeds 5%"
                    }
                }
            },
            "rules": [
                {
                    "metric": "p95_ms",
                    "mode": "relative",
                    "direction": "increase",
                    "warn": 10,
                    "fail": 25,
                    "_comment": "Fail if p95 latency increases >25% vs baseline"
                },
                {
                    "metric": "error_rate",
                    "mode": "absolute",
                    "direction": "increase",
                    "warn": 1,
                    "fail": 5,
                    "_comment": "Fail if error rate exceeds 5%"
                },
                {
                    "metric": "rps",
                    "mode": "relative",
                    "direction": "decrease",
                    "warn": 10,
                    "fail": 20,
                    "_comment": "Fail if throughput drops >20% vs baseline"
                }
            ],
            "fail_on": "DEGRADATION"
        },
        "report": {
            "title": "Load Test Report",
            "output": "artifacts/report.html",
            "_comment_preset": "Preset: 'default', 'latency', 'throughput', or 'errors'. Applied first, then overrides below.",
            "theme": {
                "mode": "light",
                "_comment_mode": "'light' or 'dark'"
            },
            "branding": {
                "name": "Locomotive",
                "_comment_name": "Company/project name shown in footer. Set 'color' to change brand name color."
            },
            "sections": ["kpi", "charts", "regression", "endpoints", "trends"],
            "_comment_sections": "Controls which sections appear and in what order. Remove to hide.",
            "timezone": "UTC",
            "_comment_timezone": "Timezone for dates in report. Examples: 'UTC', 'UTC+3', 'UTC-5:30'",
            "trends": {
                "metrics": ["p95_ms", "rps", "error_rate"],
                "_comment": "Metrics to show on trend charts. Requires artifacts.history > 0."
            }
        }
    }
    
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")


def generate_rules_template(output_path: Path) -> None:
    """Generate a rules.json template."""
    rules = {
        "_comment": "Performance regression rules",
        "rules": [
            {
                "metric": "p95_ms",
                "mode": "relative",
                "direction": "increase",
                "warn": 10,
                "fail": 25
            },
            {
                "metric": "p99_ms",
                "mode": "relative",
                "direction": "increase",
                "warn": 15,
                "fail": 30
            },
            {
                "metric": "avg_ms",
                "mode": "relative",
                "direction": "increase",
                "warn": 10,
                "fail": 20
            },
            {
                "metric": "rps",
                "mode": "relative",
                "direction": "decrease",
                "warn": 10,
                "fail": 20
            },
            {
                "metric": "error_rate",
                "mode": "absolute",
                "direction": "increase",
                "warn": 0.5,
                "fail": 2.0
            }
        ]
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

      # Set baseline on main branch
      - name: Set baseline
        if: github.ref == 'refs/heads/main' && github.event_name == 'push'
        run: loco --config {config_name} ci --set-baseline
'''
    
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(workflow, encoding="utf-8")
