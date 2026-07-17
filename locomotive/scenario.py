from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .utils import ensure_dir, write_text


_NAME_RE = re.compile(r"[^a-zA-Z0-9_]+")

# Canonical registry of runtime placeholder functions. This is the single
# source of truth shared with the config loader (config.py imports it), so
# ${uuid}, ${randint:1:100} etc. survive load-time env resolution and are
# resolved at runtime by the generated locustfile.
RUNTIME_FUNCTIONS = frozenset(
    {"timestamp", "random", "iteration", "uuid", "randint", "choice", "now"}
)


def _slugify(value: str) -> str:
    value = value.strip().lower()
    value = _NAME_RE.sub("_", value)
    value = value.strip("_")
    return value or "task"


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class ScenarioGenerator:
    """Generates Locust test files from scenario configuration.

    The scenario config format:
    {
        "think_time": {"min": 0.5, "max": 2.0},  # or just a number
        "headers": {"Authorization": "Bearer ${TOKEN}"},
        "auth": {
            "type": "bearer",
            "token": "${API_TOKEN}"
        },
        "on_start": [  # requests to run once per user at start
            {"method": "POST", "path": "/login",
             "capture": {"auth_token": "data.token"}, ...}
        ],
        "requests": [
            {
                "name": "Get Users",
                "method": "GET",
                "path": "/users/${var:user_id}",
                "weight": 5,
                "headers": {},
                "query": {},
                "json": {},
                "tags": ["api"]
            }
        ]
    }

    Placeholder namespaces resolved at runtime by the generated file:
        ${var:name}   - variable captured via "capture"
        ${env:NAME}   - environment variable (also ${env:NAME:-default})
        ${NAME}       - captured variable first, then environment variable
        ${timestamp}, ${random[:N]}, ${iteration}, ${uuid},
        ${randint:A:B}, ${choice:a,b,c}, ${now:fmt} - built-in generators
    """

    def __init__(
        self,
        scenario: Dict[str, Any],
        target: Dict[str, Any],
    ) -> None:
        self.scenario = scenario
        self.target = target
        self.requests: List[Dict[str, Any]] = []

    def load_requests(self) -> None:
        """Load requests from config."""
        self.requests = self.scenario.get("requests", [])
        if not isinstance(self.requests, list):
            self.requests = []

        # Filter by tags if specified
        include_tags = self.target.get("tags") or []
        exclude_tags = self.target.get("exclude_tags") or []

        if include_tags or exclude_tags:
            include_set = set(include_tags) if include_tags else None
            exclude_set = set(exclude_tags) if exclude_tags else set()

            filtered = []
            for req in self.requests:
                req_tags = set(req.get("tags", [])) if isinstance(req, dict) else set()
                if req_tags & exclude_set:
                    continue
                if include_set is not None and not (req_tags & include_set):
                    continue
                filtered.append(req)
            self.requests = filtered

    def generate(self, output_dir: Path) -> Path:
        """Generate the locustfile and return its path."""
        self.load_requests()

        if not self.requests:
            raise ValueError("scenario.requests must be a non-empty list")

        self._validate_requests(self.requests, "scenario.requests")
        on_start = self.scenario.get("on_start")
        if isinstance(on_start, list):
            self._validate_requests(on_start, "scenario.on_start")

        lines = self._generate_imports()
        lines.extend(self._generate_helpers())
        lines.extend(self._generate_user_class())

        output_path = output_dir / "generated_locustfile.py"
        ensure_dir(output_dir)
        write_text(output_path, "\n".join(lines) + "\n")
        return output_path

    @staticmethod
    def _validate_requests(requests: List[Any], section: str) -> None:
        """Fail early with a clear message instead of silently skipping entries."""
        for idx, req in enumerate(requests, start=1):
            if not isinstance(req, dict):
                raise ValueError(
                    f"{section}[{idx}] must be an object, got {type(req).__name__}"
                )
            path = req.get("path")
            if not path or not isinstance(path, str):
                label = req.get("name") or req.get("method") or "request"
                raise ValueError(
                    f"{section}[{idx}] ({label!r}) is missing required 'path'"
                )

    def _generate_imports(self) -> List[str]:
        """Generate import statements."""
        return [
            "import base64",
            "import os",
            "import re",
            "import time",
            "import random",
            "import uuid",
            "from locust import HttpUser, task, between, tag",
            "",
        ]

    def _generate_helpers(self) -> List[str]:
        """Generate module-level helper functions for dynamic values."""
        return [
            "",
            "# Dynamic value generators",
            "_iteration_counter = 0",
            "",
            "_PLACEHOLDER_RE = re.compile(r'\\$\\{([^}]+)\\}')",
            "",
            "",
            "def _timestamp():",
            "    '''Current timestamp in milliseconds.'''",
            "    return str(int(time.time() * 1000))",
            "",
            "",
            "def _random_string(length=8):",
            "    '''Random alphanumeric string.'''",
            "    chars = 'abcdefghijklmnopqrstuvwxyz0123456789'",
            "    return ''.join(random.choice(chars) for _ in range(length))",
            "",
            "",
            "def _iteration():",
            "    '''Incrementing counter.'''",
            "    global _iteration_counter",
            "    _iteration_counter += 1",
            "    return _iteration_counter",
            "",
            "",
            "def _basic_auth(username, password):",
            "    '''Build a base64-encoded Basic Authorization header value.'''",
            "    raw = f'{username}:{password}'.encode('utf-8')",
            "    return 'Basic ' + base64.b64encode(raw).decode('ascii')",
            "",
            "",
            "def _parse_env_ref(ref):",
            "    '''Split a VAR:-default (or VAR:default) reference into (name, default).'''",
            "    if ':-' in ref:",
            "        name, default = ref.split(':-', 1)",
            "        return name, default",
            "    if ':' in ref:",
            "        name, default = ref.split(':', 1)",
            "        return name, default",
            "    return ref, ''",
            "",
            "",
            f"_RUNTIME_FUNCTIONS = frozenset({sorted(RUNTIME_FUNCTIONS)!r})",
            "",
            "",
            "def _call_function(name, args):",
            "    '''Dispatch a ${func:args} runtime placeholder.",
            "",
            "    Argument errors degrade gracefully (defaults or empty string)",
            "    instead of crashing the load test.",
            "    '''",
            "    if name == 'timestamp':",
            "        return _timestamp()",
            "    if name == 'iteration':",
            "        return str(_iteration())",
            "    if name == 'uuid':",
            "        return str(uuid.uuid4())",
            "    if name == 'random':",
            "        try:",
            "            length = int(args) if args else 8",
            "        except ValueError:",
            "            length = 8",
            "        return _random_string(max(1, length))",
            "    if name == 'randint':",
            "        lo, _, hi = args.partition(':')",
            "        try:",
            "            a, b = int(lo), int(hi)",
            "        except ValueError:",
            "            return ''",
            "        if a > b:",
            "            a, b = b, a",
            "        return str(random.randint(a, b))",
            "    if name == 'choice':",
            "        options = [item for item in args.split(',') if item != '']",
            "        return random.choice(options) if options else ''",
            "    if name == 'now':",
            "        try:",
            "            return time.strftime(args or '%Y-%m-%dT%H:%M:%S')",
            "        except ValueError:",
            "            return time.strftime('%Y-%m-%dT%H:%M:%S')",
            "    return ''",
            "",
        ]

    def _generate_resolver_methods(self) -> List[str]:
        """Generate the instance-bound placeholder resolver."""
        return [
            "",
            "    def _resolve(self, value):",
            "        '''Resolve dynamic placeholders in string values.",
            "",
            "        Supports:",
            "            ${var:name}         - captured variable (see \"capture\")",
            "            ${env:NAME}         - environment variable",
            "            ${env:NAME:-def}    - environment variable with default",
            "            ${NAME}             - captured variable, then env variable",
            "            ${timestamp}        - current timestamp ms",
            "            ${random}           - random string (${random:N} for length N)",
            "            ${iteration}        - incrementing counter",
            "            ${uuid}             - random UUID4",
            "            ${randint:A:B}      - random integer between A and B",
            "            ${choice:a,b,c}     - random element of a comma-separated list",
            "            ${now:%Y-%m-%d}     - current time via strftime (default ISO)",
            "        '''",
            "        if not isinstance(value, str):",
            "            return value",
            "",
            "        def replace(match):",
            "            key = match.group(1)",
            "            if key.startswith('var:'):",
            "                variables = getattr(self, '_vars', {})",
            "                found = variables.get(key[4:])",
            "                return '' if found is None else str(found)",
            "            if key.startswith('env:'):",
            "                name, default = _parse_env_ref(key[4:])",
            "                return os.environ.get(name, default)",
            "            func_name, _, func_args = key.partition(':')",
            "            if func_name in _RUNTIME_FUNCTIONS:",
            "                return _call_function(func_name, func_args)",
            "            variables = getattr(self, '_vars', {})",
            "            name, default = _parse_env_ref(key)",
            "            if name in variables:",
            "                found = variables[name]",
            "                return '' if found is None else str(found)",
            "            return os.environ.get(name, default)",
            "",
            "        return _PLACEHOLDER_RE.sub(replace, value)",
            "",
            "    def _resolve_dict(self, d):",
            "        '''Recursively resolve dynamic values in dict/list.'''",
            "        if isinstance(d, dict):",
            "            return {k: self._resolve_dict(v) for k, v in d.items()}",
            "        if isinstance(d, list):",
            "            return [self._resolve_dict(v) for v in d]",
            "        return self._resolve(d)",
            "",
        ]

    def _auth_config(self, base_headers: Dict[str, str]) -> Optional[Tuple[str, str]]:
        """Apply auth config to base headers.

        Bearer and api_key auth become static header templates (resolved per
        request). Basic auth is returned as (username, password) so the header
        can be base64-encoded at runtime after placeholder resolution — the
        raw credentials are never embedded as a ready-made header.
        """
        auth = self.scenario.get("auth")
        if not isinstance(auth, dict):
            return None
        auth_type = str(auth.get("type", "")).lower()
        if auth_type == "bearer":
            token = auth.get("token", "${API_TOKEN}")
            base_headers["Authorization"] = f"Bearer {token}"
        elif auth_type == "basic":
            user = auth.get("username", "${API_USER}")
            password = auth.get("password", "${API_PASSWORD}")
            return str(user), str(password)
        elif auth_type == "api_key":
            header_name = auth.get("header", "X-API-Key")
            key = auth.get("key", "${API_KEY}")
            base_headers[header_name] = key
        return None

    def _generate_user_class(self) -> List[str]:
        """Generate the main User class."""
        lines = ["", "class GeneratedUser(HttpUser):"]

        # Wait time
        think_time = self.scenario.get("think_time")
        if isinstance(think_time, dict):
            min_wait = _safe_float(think_time.get("min"), 0.5)
            max_wait = _safe_float(think_time.get("max"), min_wait)
            lines.append(f"    wait_time = between({min_wait}, {max_wait})")
        elif think_time is not None:
            value = _safe_float(think_time, 1.0)
            lines.append(f"    wait_time = between({value}, {value})")
        else:
            lines.append("    wait_time = between(0.5, 2.0)")

        # Gather headers
        scenario_headers = self.scenario.get("headers") if isinstance(self.scenario.get("headers"), dict) else {}
        target_headers = self.target.get("headers") if isinstance(self.target.get("headers"), dict) else {}
        base_headers = {**target_headers, **scenario_headers}

        basic_auth = self._auth_config(base_headers)

        # Store base headers as class attribute (placeholders resolved per request)
        lines.append(f"    _base_headers = {repr(base_headers)}")

        lines.extend(self._generate_resolver_methods())

        # on_start for setup (variables store, basic auth, login, etc.)
        on_start = self.scenario.get("on_start")
        on_start_requests = on_start if isinstance(on_start, list) else []
        if on_start_requests or basic_auth:
            lines.extend(self._generate_on_start(on_start_requests, basic_auth))

        # Generate tasks
        for idx, req in enumerate(self.requests, start=1):
            task_lines = self._generate_task(idx, req)
            lines.extend(task_lines)

        return lines

    def _build_request_call(self, req: Dict[str, Any]) -> str:
        """Build the argument list for a self.client.request(...) call."""
        method = str(req.get("method", "GET")).upper()
        path = str(req.get("path"))
        name = req.get("name") or f"{method} {path}"

        # Resolve dynamic path segments at runtime, but keep the template
        # string as the stats name so Locust groups all calls together.
        if "${" in path:
            path_expr = f"self._resolve({repr(path)})"
        else:
            path_expr = repr(path)

        req_headers = req.get("headers") if isinstance(req.get("headers"), dict) else {}
        params = req.get("query") if isinstance(req.get("query"), dict) else None
        json_body = req.get("json")
        data_body = req.get("data")
        timeout = req.get("timeout")

        args: List[str] = [repr(method), path_expr]
        kwargs: List[str] = [f"name={repr(name)}"]

        if req_headers:
            kwargs.append(
                f"headers=self._resolve_dict({{**self._base_headers, **{repr(req_headers)}}})"
            )
        else:
            kwargs.append("headers=self._resolve_dict(self._base_headers)")
        if params:
            kwargs.append(f"params=self._resolve_dict({repr(params)})")
        if json_body is not None:
            kwargs.append(f"json=self._resolve_dict({repr(json_body)})")
        if data_body is not None:
            kwargs.append(f"data=self._resolve_dict({repr(data_body)})")
        if timeout is not None:
            kwargs.append(f"timeout={repr(timeout)}")

        return ", ".join(args + kwargs)

    def _generate_on_start(
        self,
        requests: List[Dict[str, Any]],
        basic_auth: Optional[Tuple[str, str]],
    ) -> List[str]:
        """Generate on_start method for user initialization."""
        lines = [
            "",
            "    def on_start(self):",
            "        '''Run once per user at start (login, setup, etc.).'''",
            "        self._vars = {}",
        ]

        if basic_auth:
            user, password = basic_auth
            lines.append("        self._base_headers = dict(self._base_headers)")
            lines.append(
                "        self._base_headers['Authorization'] = _basic_auth("
                f"self._resolve({repr(user)}), self._resolve({repr(password)}))"
            )

        for req in requests:
            call = self._build_request_call(req)

            capture = req.get("capture")
            if isinstance(capture, dict) and capture:
                lines.append(f"        resp = self.client.request({call})")
                for var_name, json_path in capture.items():
                    # Simple json path like "token" or "data.access_token"
                    accessor = "data"
                    for part in str(json_path).split("."):
                        accessor += f"[{repr(part)}]"
                    lines.append("        try:")
                    lines.append("            data = resp.json()")
                    lines.append(f"            self._vars[{repr(str(var_name))}] = {accessor}")
                    lines.append("        except Exception:")
                    lines.append(f"            self._vars[{repr(str(var_name))}] = None")
            else:
                lines.append(f"        self.client.request({call})")

        return lines

    def _generate_task(self, idx: int, req: Dict[str, Any]) -> List[str]:
        """Generate a single task method."""
        method = str(req.get("method", "GET")).upper()
        path = str(req.get("path"))
        weight = _safe_int(req.get("weight"), 1)
        if weight < 1:
            weight = 1

        tags = req.get("tags") if isinstance(req.get("tags"), list) else []

        call = self._build_request_call(req)
        func_name = _slugify(req.get("name") or f"{method}_{path}")
        func_name = f"task_{idx}_{func_name}"

        lines = [""]
        for t in tags:
            lines.append(f"    @tag({repr(str(t))})")
        lines.append(f"    @task({weight})")
        lines.append(f"    def {func_name}(self):")
        lines.append(f"        self.client.request({call})")

        return lines


def generate_locustfile(
    scenario: Dict[str, Any],
    target: Dict[str, Any],
    output_dir: Path,
) -> Path:
    """Generate a locustfile from scenario configuration.

    Args:
        scenario: The scenario configuration dict containing requests.
        target: The target/load configuration with host, headers, tags, etc.
        output_dir: Directory to write the generated file.

    Returns:
        Path to the generated locustfile.
    """
    generator = ScenarioGenerator(scenario, target)
    return generator.generate(output_dir)
