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

# Allowed data pool selection modes (scenario.data.<pool>.mode)
DATA_MODES = frozenset({"unique_per_user", "round_robin", "random", "once"})

_POOL_NAME_RE = re.compile(r"^[A-Za-z0-9_]+$")


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


# ── module-level emitters (shared by all personas) ────────────────────


def _emit_imports() -> List[str]:
    """Generate import statements."""
    return [
        "import base64",
        "import csv",
        "import json",
        "import os",
        "import re",
        "import threading",
        "import time",
        "import random",
        "import uuid",
        "from locust import HttpUser, SequentialTaskSet, task, between, tag",
        "",
    ]


def _emit_helpers() -> List[str]:
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


def _emit_data_layer(data_specs: Dict[str, Dict[str, Any]]) -> List[str]:
    """Generate the data pool machinery (specs, lazy loader, counters)."""
    return [
        "",
        "# Data pools (${data:pool.field})",
        f"_DATA_SPECS = {repr(data_specs)}",
        "_DATA_POOLS = {}",
        "_DATA_COUNTERS = {}",
        "_DATA_LOCK = threading.Lock()",
        "",
        "",
        "def _load_pool(name):",
        "    '''Load a data pool once per process (CSV/JSON file or inline rows).'''",
        "    if name in _DATA_POOLS:",
        "        return _DATA_POOLS[name]",
        "    with _DATA_LOCK:",
        "        if name in _DATA_POOLS:",
        "            return _DATA_POOLS[name]",
        "        spec = _DATA_SPECS.get(name) or {}",
        "        rows = []",
        "        if spec.get('inline') is not None:",
        "            rows = [row for row in spec['inline'] if isinstance(row, dict)]",
        "        else:",
        "            path = spec.get('source') or ''",
        "            try:",
        "                if path.lower().endswith('.json'):",
        "                    with open(path, encoding='utf-8') as handle:",
        "                        data = json.load(handle)",
        "                    if isinstance(data, list):",
        "                        rows = [row for row in data if isinstance(row, dict)]",
        "                else:",
        "                    with open(path, newline='', encoding='utf-8') as handle:",
        "                        rows = list(csv.DictReader(handle))",
        "            except (OSError, ValueError):",
        "                rows = []",
        "        _DATA_POOLS[name] = rows",
        "        return rows",
        "",
        "",
        "def _next_index(counter_key, size):",
        "    '''Thread-safe cycling counter (wraps around when exhausted).'''",
        "    with _DATA_LOCK:",
        "        idx = _DATA_COUNTERS.get(counter_key, 0)",
        "        _DATA_COUNTERS[counter_key] = idx + 1",
        "    return idx % size",
        "",
    ]


def _emit_runtime_mixin() -> List[str]:
    """Generate the mixin with the placeholder resolver, shared by all users."""
    return [
        "",
        "",
        "class _RuntimeMixin:",
        "    '''Placeholder resolution shared by all generated user classes.'''",
        "",
        "    def _resolve(self, value):",
        "        '''Resolve dynamic placeholders in string values.",
        "",
        "        Supports:",
        "            ${var:name}         - captured variable (see \"capture\")",
        "            ${env:NAME}         - environment variable",
        "            ${env:NAME:-def}    - environment variable with default",
        "            ${data:pool.field}  - field of this user's data pool row",
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
        "            if key.startswith('data:'):",
        "                pool_name, _, field_path = key[5:].partition('.')",
        "                return self._data_value(pool_name, field_path)",
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
        "    def _data_value(self, pool_name, field_path):",
        "        '''Resolve ${data:pool.field} for this user.'''",
        "        rows = _load_pool(pool_name)",
        "        if not rows:",
        "            return ''",
        "        spec = _DATA_SPECS.get(pool_name) or {}",
        "        mode = spec.get('mode', 'unique_per_user')",
        "        if mode == 'random':",
        "            row = random.choice(rows)",
        "        elif mode == 'round_robin':",
        "            row = rows[_next_index(pool_name + ':access', len(rows))]",
        "        else:",
        "            data_rows = getattr(self, '_data_rows', None)",
        "            if data_rows is None:",
        "                data_rows = self._data_rows = {}",
        "            row = data_rows.get(pool_name)",
        "            if row is None:",
        "                if mode == 'once':",
        "                    row = rows[0]",
        "                else:  # unique_per_user",
        "                    row = rows[_next_index(pool_name, len(rows))]",
        "                data_rows[pool_name] = row",
        "        value = row",
        "        for part in field_path.split('.'):",
        "            if isinstance(value, dict):",
        "                value = value.get(part)",
        "            else:",
        "                value = None",
        "                break",
        "        return '' if value is None else str(value)",
    ]


class ScenarioGenerator:
    """Generates Locust user classes from scenario configuration.

    The scenario config format:
    {
        "think_time": {"min": 0.5, "max": 2.0},  # or just a number
        "headers": {"Authorization": "Bearer ${TOKEN}"},
        "auth": {
            "type": "bearer",
            "token": "${API_TOKEN}"
        },
        "data": {  # data pools for data-driven values
            "accounts": {
                "source": "data/accounts.csv",   # CSV/JSON file, or "inline": [{...}]
                "mode": "unique_per_user"        # round_robin | random | once
            }
        },
        "on_start": [  # requests to run once per user at start
            {"method": "POST", "path": "/login",
             "capture": {"auth_token": "data.token"}, ...}
        ],
        "on_stop": [  # requests to run once per user at stop (teardown)
            {"method": "POST", "path": "/logout"}
        ],
        "flows": [  # ordered multi-step user journeys
            {
                "name": "Checkout",
                "weight": 3,
                "think_time": 1.0,           # optional, overrides user's
                "tags": ["purchase"],        # optional
                "steps": [
                    {"name": "Create order", "method": "POST", "path": "/orders",
                     "capture": {"order_id": "id"}},
                    {"name": "Pay", "method": "POST",
                     "path": "/orders/${var:order_id}/pay"}
                ]
            }
        ],
        "requests": [  # flat weighted-random tasks (can coexist with flows)
            {
                "name": "Get Users",
                "method": "GET",
                "path": "/users/${var:user_id}",
                "weight": 5,
                "headers": {},
                "query": {},
                "json": {},
                "tags": ["api"],
                "capture": {"first_id": "items.0"}   # optional
            }
        ]
    }

    Placeholder namespaces resolved at runtime by the generated file:
        ${var:name}   - variable captured via "capture"
        ${env:NAME}   - environment variable (also ${env:NAME:-default})
        ${data:pool.field} - field of the user's row from a data pool
        ${NAME}       - captured variable first, then environment variable
        ${timestamp}, ${random[:N]}, ${iteration}, ${uuid},
        ${randint:A:B}, ${choice:a,b,c}, ${now:fmt} - built-in generators

    Multiple personas: see generate_locustfile(users=...). Each persona is an
    independent scenario rendered as its own HttpUser class with a weight.
    """

    def __init__(
        self,
        scenario: Dict[str, Any],
        target: Dict[str, Any],
        class_name: str = "GeneratedUser",
        weight: Optional[int] = None,
        flow_prefix: str = "Flow",
        section_prefix: str = "scenario",
    ) -> None:
        self.scenario = scenario
        self.target = target
        self.class_name = class_name
        self.weight = weight
        self.flow_prefix = flow_prefix
        self.section_prefix = section_prefix
        self.requests: List[Dict[str, Any]] = []
        self.flows: List[Dict[str, Any]] = []
        self.data_specs: Dict[str, Dict[str, Any]] = {}

    def _filter_by_tags(self, items: List[Any]) -> List[Any]:
        """Filter requests/flows by target include/exclude tags."""
        include_tags = self.target.get("tags") or []
        exclude_tags = self.target.get("exclude_tags") or []
        if not include_tags and not exclude_tags:
            return items

        include_set = set(include_tags) if include_tags else None
        exclude_set = set(exclude_tags) if exclude_tags else set()

        filtered = []
        for item in items:
            item_tags = set(item.get("tags", [])) if isinstance(item, dict) else set()
            if item_tags & exclude_set:
                continue
            if include_set is not None and not (item_tags & include_set):
                continue
            filtered.append(item)
        return filtered

    def load_requests(self) -> None:
        """Load flat requests from config."""
        requests = self.scenario.get("requests", [])
        if not isinstance(requests, list):
            requests = []
        self.requests = self._filter_by_tags(requests)

    def load_flows(self) -> None:
        """Load flows from config."""
        flows = self.scenario.get("flows", [])
        if not isinstance(flows, list):
            flows = []
        self.flows = self._filter_by_tags(flows)

    def load_data_specs(self) -> None:
        """Load and validate data pool specs from config."""
        data = self.scenario.get("data")
        if data is None:
            self.data_specs = {}
            return
        if not isinstance(data, dict):
            raise ValueError(f"{self.section_prefix}.data must be an object of pool specs")

        specs: Dict[str, Dict[str, Any]] = {}
        for name, spec in data.items():
            pool_name = str(name)
            if not _POOL_NAME_RE.match(pool_name):
                raise ValueError(
                    f"{self.section_prefix}.data pool name {pool_name!r} must match [A-Za-z0-9_]+"
                )
            if not isinstance(spec, dict):
                raise ValueError(
                    f"{self.section_prefix}.data.{pool_name} must be an object, got {type(spec).__name__}"
                )
            source = spec.get("source")
            inline = spec.get("inline")
            if inline is not None:
                if not isinstance(inline, list) or not all(
                    isinstance(row, dict) for row in inline
                ):
                    raise ValueError(
                        f"{self.section_prefix}.data.{pool_name}.inline must be a list of objects"
                    )
            elif not (isinstance(source, str) and source.strip()):
                raise ValueError(
                    f"{self.section_prefix}.data.{pool_name} must define 'source' (file path) or 'inline' (rows)"
                )
            mode = str(spec.get("mode", "unique_per_user"))
            if mode not in DATA_MODES:
                raise ValueError(
                    f"{self.section_prefix}.data.{pool_name}.mode must be one of "
                    f"{sorted(DATA_MODES)}, got {mode!r}"
                )
            specs[pool_name] = {
                "source": source if isinstance(source, str) else None,
                "inline": inline,
                "mode": mode,
            }
        self.data_specs = specs

    def prepare(self) -> None:
        """Load and validate all scenario pieces. Must run before emit_classes."""
        self.load_requests()
        self.load_flows()
        self.load_data_specs()

        if not self.requests and not self.flows:
            raise ValueError(
                f"{self.section_prefix} must define a non-empty 'requests' or 'flows' list"
            )

        self._validate_requests(self.requests, f"{self.section_prefix}.requests")
        self._validate_flows(self.flows)
        for section in ("on_start", "on_stop"):
            entries = self.scenario.get(section)
            if isinstance(entries, list):
                self._validate_requests(entries, f"{self.section_prefix}.{section}")

    def emit_classes(self) -> List[str]:
        """Emit flow classes and the user class for this persona."""
        lines: List[str] = []
        flow_entries: List[Tuple[str, int]] = []
        for idx, flow in enumerate(self.flows, start=1):
            class_name, weight, flow_lines = self._generate_flow_class(idx, flow)
            flow_entries.append((class_name, weight))
            lines.extend(flow_lines)
        lines.extend(self._generate_user_class(flow_entries))
        return lines

    def generate(self, output_dir: Path) -> Path:
        """Generate a complete locustfile for this single scenario."""
        return _write_locustfile([self], output_dir)

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

    def _validate_flows(self, flows: List[Any]) -> None:
        for idx, flow in enumerate(flows, start=1):
            if not isinstance(flow, dict):
                raise ValueError(
                    f"{self.section_prefix}.flows[{idx}] must be an object, got {type(flow).__name__}"
                )
            label = flow.get("name") or f"flow_{idx}"
            steps = flow.get("steps")
            if not isinstance(steps, list) or not steps:
                raise ValueError(
                    f"{self.section_prefix}.flows[{idx}] ({label!r}) must define a non-empty 'steps' list"
                )
            self._validate_requests(steps, f"{self.section_prefix}.flows[{idx}].steps")

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

    @staticmethod
    def _think_time_expr(think_time: Any) -> Optional[str]:
        """Build a between(...) expression from a think_time config value."""
        if isinstance(think_time, dict):
            min_wait = _safe_float(think_time.get("min"), 0.5)
            max_wait = _safe_float(think_time.get("max"), min_wait)
            return f"between({min_wait}, {max_wait})"
        if think_time is not None:
            value = _safe_float(think_time, 1.0)
            return f"between({value}, {value})"
        return None

    def _build_request_call(self, req: Dict[str, Any], user_expr: str = "self") -> str:
        """Build the argument list for a self.client.request(...) call.

        user_expr is the expression that reaches the User instance from the
        generated context: "self" inside the User class, "self.user" inside
        a flow (SequentialTaskSet).
        """
        method = str(req.get("method", "GET")).upper()
        path = str(req.get("path"))
        name = req.get("name") or f"{method} {path}"

        # Resolve dynamic path segments at runtime, but keep the template
        # string as the stats name so Locust groups all calls together.
        if "${" in path:
            path_expr = f"{user_expr}._resolve({repr(path)})"
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
                f"headers={user_expr}._resolve_dict({{**{user_expr}._base_headers, **{repr(req_headers)}}})"
            )
        else:
            kwargs.append(f"headers={user_expr}._resolve_dict({user_expr}._base_headers)")
        if params:
            kwargs.append(f"params={user_expr}._resolve_dict({repr(params)})")
        if json_body is not None:
            kwargs.append(f"json={user_expr}._resolve_dict({repr(json_body)})")
        if data_body is not None:
            kwargs.append(f"data={user_expr}._resolve_dict({repr(data_body)})")
        if timeout is not None:
            kwargs.append(f"timeout={repr(timeout)}")

        return ", ".join(args + kwargs)

    def _generate_request_stmt(
        self,
        req: Dict[str, Any],
        indent: int,
        user_expr: str = "self",
    ) -> List[str]:
        """Generate the request call plus optional capture handling."""
        pad = " " * indent
        call = self._build_request_call(req, user_expr)

        capture = req.get("capture")
        if not (isinstance(capture, dict) and capture):
            return [f"{pad}self.client.request({call})"]

        lines = [f"{pad}resp = self.client.request({call})"]
        for var_name, json_path in capture.items():
            # Simple json path like "token" or "data.access_token"
            accessor = "data"
            for part in str(json_path).split("."):
                accessor += f"[{repr(part)}]"
            lines.append(f"{pad}try:")
            lines.append(f"{pad}    data = resp.json()")
            lines.append(f"{pad}    {user_expr}._vars[{repr(str(var_name))}] = {accessor}")
            lines.append(f"{pad}except Exception:")
            lines.append(f"{pad}    {user_expr}._vars[{repr(str(var_name))}] = None")
        return lines

    def _generate_flow_class(
        self, idx: int, flow: Dict[str, Any]
    ) -> Tuple[str, int, List[str]]:
        """Generate a SequentialTaskSet class for a flow.

        Returns (class_name, weight, lines).
        """
        name = str(flow.get("name") or f"flow_{idx}")
        class_name = f"{self.flow_prefix}_{idx}_{_slugify(name)}"
        weight = _safe_int(flow.get("weight"), 1)
        if weight < 1:
            weight = 1

        lines = ["", ""]
        tags = flow.get("tags") if isinstance(flow.get("tags"), list) else []
        for t in tags:
            lines.append(f"@tag({repr(str(t))})")
        lines.append(f"class {class_name}(SequentialTaskSet):")

        think_expr = self._think_time_expr(flow.get("think_time"))
        if think_expr:
            lines.append(f"    wait_time = {think_expr}")

        for sidx, step in enumerate(flow["steps"], start=1):
            func_name = _slugify(step.get("name") or f"step_{sidx}")
            step_tags = step.get("tags") if isinstance(step.get("tags"), list) else []
            lines.append("")
            for t in step_tags:
                lines.append(f"    @tag({repr(str(t))})")
            lines.append("    @task")
            lines.append(f"    def step_{sidx}_{func_name}(self):")
            lines.extend(self._generate_request_stmt(step, indent=8, user_expr="self.user"))

        # Return control to the User after the last step so other flows and
        # flat tasks get scheduled (otherwise the SequentialTaskSet loops).
        lines.append("")
        lines.append("    @task")
        lines.append("    def _flow_complete(self):")
        lines.append("        self.interrupt(reschedule=False)")

        return class_name, weight, lines

    def _generate_user_class(self, flow_entries: List[Tuple[str, int]]) -> List[str]:
        """Generate the user class for this persona."""
        lines = ["", "", f"class {self.class_name}(_RuntimeMixin, HttpUser):"]

        if self.weight is not None:
            lines.append(f"    weight = {self.weight}")

        # Wait time
        think_expr = self._think_time_expr(self.scenario.get("think_time"))
        lines.append(f"    wait_time = {think_expr or 'between(0.5, 2.0)'}")

        # Gather headers
        scenario_headers = self.scenario.get("headers") if isinstance(self.scenario.get("headers"), dict) else {}
        target_headers = self.target.get("headers") if isinstance(self.target.get("headers"), dict) else {}
        base_headers = {**target_headers, **scenario_headers}

        basic_auth = self._auth_config(base_headers)

        # Store base headers as class attribute (placeholders resolved per request)
        lines.append(f"    _base_headers = {repr(base_headers)}")

        # Flows participate in scheduling alongside flat @task methods
        if flow_entries:
            tasks_repr = "{" + ", ".join(f"{cn}: {w}" for cn, w in flow_entries) + "}"
            lines.append(f"    tasks = {tasks_repr}")

        # on_start: always generated — initializes the per-user variables
        # store used by capture and the resolver.
        on_start = self.scenario.get("on_start")
        on_start_requests = on_start if isinstance(on_start, list) else []
        lines.extend(self._generate_on_start(on_start_requests, basic_auth))

        # on_stop: teardown requests (logout etc.)
        on_stop = self.scenario.get("on_stop")
        on_stop_requests = on_stop if isinstance(on_stop, list) else []
        if on_stop_requests:
            lines.extend(self._generate_on_stop(on_stop_requests))

        # Flat weighted-random tasks
        for idx, req in enumerate(self.requests, start=1):
            lines.extend(self._generate_task(idx, req))

        return lines

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

        if self.data_specs:
            pool_names = sorted(self.data_specs)
            lines.extend([
                "        # Pin per-user data rows (unique_per_user / once modes)",
                "        self._data_rows = {}",
                f"        for _pool_name in {pool_names!r}:",
                "            _spec = _DATA_SPECS.get(_pool_name) or {}",
                "            _mode = _spec.get('mode', 'unique_per_user')",
                "            _rows = _load_pool(_pool_name)",
                "            if not _rows:",
                "                continue",
                "            if _mode == 'once':",
                "                self._data_rows[_pool_name] = _rows[0]",
                "            elif _mode == 'unique_per_user':",
                "                self._data_rows[_pool_name] = _rows[_next_index(_pool_name, len(_rows))]",
            ])

        if basic_auth:
            user, password = basic_auth
            lines.append("        self._base_headers = dict(self._base_headers)")
            lines.append(
                "        self._base_headers['Authorization'] = _basic_auth("
                f"self._resolve({repr(user)}), self._resolve({repr(password)}))"
            )

        for req in requests:
            lines.extend(self._generate_request_stmt(req, indent=8, user_expr="self"))

        return lines

    def _generate_on_stop(self, requests: List[Dict[str, Any]]) -> List[str]:
        """Generate on_stop method for user teardown."""
        lines = [
            "",
            "    def on_stop(self):",
            "        '''Run once per user at stop (logout, cleanup, etc.).'''",
        ]
        for req in requests:
            lines.extend(self._generate_request_stmt(req, indent=8, user_expr="self"))
        return lines

    def _generate_task(self, idx: int, req: Dict[str, Any]) -> List[str]:
        """Generate a single flat task method."""
        method = str(req.get("method", "GET")).upper()
        path = str(req.get("path"))
        weight = _safe_int(req.get("weight"), 1)
        if weight < 1:
            weight = 1

        tags = req.get("tags") if isinstance(req.get("tags"), list) else []

        func_name = _slugify(req.get("name") or f"{method}_{path}")
        func_name = f"task_{idx}_{func_name}"

        lines = [""]
        for t in tags:
            lines.append(f"    @tag({repr(str(t))})")
        lines.append(f"    @task({weight})")
        lines.append(f"    def {func_name}(self):")
        lines.extend(self._generate_request_stmt(req, indent=8, user_expr="self"))

        return lines


def _write_locustfile(generators: List[ScenarioGenerator], output_dir: Path) -> Path:
    """Prepare all personas, merge data pools, and write the locustfile."""
    merged_specs: Dict[str, Dict[str, Any]] = {}
    for gen in generators:
        gen.prepare()
        for name, spec in gen.data_specs.items():
            existing = merged_specs.get(name)
            if existing is not None and existing != spec:
                raise ValueError(
                    f"data pool {name!r} is defined differently in multiple personas; "
                    "pool names are global — use one shared definition or distinct names"
                )
            merged_specs[name] = spec

    lines = _emit_imports()
    lines.extend(_emit_helpers())
    lines.extend(_emit_data_layer(merged_specs))
    lines.extend(_emit_runtime_mixin())
    for gen in generators:
        lines.extend(gen.emit_classes())

    output_path = output_dir / "generated_locustfile.py"
    ensure_dir(output_dir)
    write_text(output_path, "\n".join(lines) + "\n")
    return output_path


def _build_persona_generators(
    users: List[Any],
    target: Dict[str, Any],
) -> List[ScenarioGenerator]:
    """Build one ScenarioGenerator per persona from a users list.

    Each entry is either {"weight": N, "name": ..., "scenario": {...}} or a
    flat form where the scenario fields live directly on the entry (as
    produced by "include" of a bare scenario file).
    """
    generators: List[ScenarioGenerator] = []
    for idx, entry in enumerate(users, start=1):
        if not isinstance(entry, dict):
            raise ValueError(
                f"users[{idx}] must be an object, got {type(entry).__name__}"
            )
        weight = _safe_int(entry.get("weight"), 1)
        if weight < 1:
            weight = 1
        scenario = entry.get("scenario")
        if not isinstance(scenario, dict):
            scenario = {
                key: value
                for key, value in entry.items()
                if key not in ("weight", "name", "scenario")
            }
        name = str(entry.get("name") or f"user_{idx}")
        class_name = f"User_{idx}_{_slugify(name)}"
        generators.append(
            ScenarioGenerator(
                scenario,
                target,
                class_name=class_name,
                weight=weight,
                flow_prefix=f"Flow{idx}",
                section_prefix=f"users[{idx}]",
            )
        )
    return generators


def generate_locustfile(
    scenario: Dict[str, Any],
    target: Dict[str, Any],
    output_dir: Path,
    users: Optional[List[Any]] = None,
) -> Path:
    """Generate a locustfile from scenario configuration.

    Args:
        scenario: The scenario configuration dict containing requests/flows.
            Ignored when `users` is provided.
        target: The target/load configuration with host, headers, tags, etc.
        output_dir: Directory to write the generated file.
        users: Optional list of personas. Each persona gets its own HttpUser
            class with a scheduling weight:
            [{"weight": 4, "name": "reader", "scenario": {...}},
             {"weight": 1, "name": "buyer", "scenario": {...}}]

    Returns:
        Path to the generated locustfile.
    """
    if users:
        generators = _build_persona_generators(users, target)
    else:
        generators = [ScenarioGenerator(scenario or {}, target)]
    return _write_locustfile(generators, output_dir)
