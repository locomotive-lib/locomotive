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


def _slugify(value: Any) -> str:
    # Names, tags and flow titles come straight from YAML/JSON and are not
    # necessarily strings (`name: 2024` is an int).
    value = str(value).strip().lower()
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


def _tag_set(value: Any) -> set:
    """Normalise a `tags` value into a set of tag names.

    ``tags: "purchase"`` is a natural thing to write, and ``set("purchase")``
    is the set of its *letters* — which intersects nothing, so the request was
    silently dropped from every tag-filtered run. A string is treated as one
    tag, or as several when it is comma-separated (the same shape the CLI's
    ``--tags`` flag accepts).
    """
    if value is None:
        return set()
    if isinstance(value, str):
        return {part.strip() for part in value.split(",") if part.strip()}
    if isinstance(value, (list, tuple, set, frozenset)):
        tags: set = set()
        for item in value:
            tags |= _tag_set(item)
        return tags
    return {str(value)}


def _literal(value: Any) -> str:
    """Render a config value as a Python literal for the generated file.

    ``repr()`` alone is not safe here. A YAML config can hold values whose
    repr is not valid inside a standalone module (``datetime.date(2024, 1, 1)``
    needs an import that is not there) or not valid Python at all (``inf``,
    ``nan`` — YAML's ``.inf`` and ``.nan`` — render as bare names and raise
    NameError when locust imports the file). Everything that is not a JSON
    scalar is rendered as the string an HTTP body would have carried anyway.
    """
    if value is None or isinstance(value, (bool, int, str)):
        # bool before int matters: repr(True) is 'True', not '1'.
        return repr(value)
    if isinstance(value, float):
        if value != value:
            return "float('nan')"
        if value == float("inf"):
            return "float('inf')"
        if value == float("-inf"):
            return "float('-inf')"
        return repr(value)
    if isinstance(value, bytes):
        return repr(value)
    if isinstance(value, dict):
        # HTTP header/JSON keys are strings; a YAML key like `2024: x` would
        # otherwise become an int key that json.dumps renders differently.
        items = ", ".join(
            f"{_literal(k if isinstance(k, str) else str(k))}: {_literal(v)}"
            for k, v in value.items()
        )
        return "{" + items + "}"
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_literal(v) for v in value) + "]"
    if isinstance(value, (set, frozenset)):
        # Sorted so the generated file (and `loco diff`) stays deterministic.
        return "[" + ", ".join(_literal(v) for v in sorted(value, key=str)) + "]"
    # datetime.date / datetime.datetime / datetime.time / Decimal / UUID / ...
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        try:
            return repr(isoformat())
        except (TypeError, ValueError):
            pass
    return repr(str(value))


# ── module-level emitters (shared by all personas) ────────────────────


def _emit_imports() -> List[str]:
    """Generate import statements."""
    return [
        "import base64",
        "import csv",
        "import json",
        "import logging",
        "import os",
        "import re",
        "import threading",
        "import time",
        "import random",
        "import uuid",
        "from locust import HttpUser, SequentialTaskSet, task, between, tag",
        "from locust import events",
        "",
    ]


def _emit_helpers(shard_data: bool = True) -> List[str]:
    """Generate module-level helper functions for dynamic values.

    ``shard_data=False`` keeps the shard machinery — it still tells the run
    how many processes there are, and still seeds ``${iteration}`` so ids do
    not collide — but stops it dividing the pools. That is the right answer
    when every worker is meant to see the whole pool, e.g. a read-only pool
    of search terms that is smaller than the worker count.
    """
    return [
        "",
        "_LOG = logging.getLogger('locomotive')",
        "",
        "# ── this process's share of the load ──────────────────────────────",
        "#",
        "# Under `--processes N` (or a master with N workers) locust forks after",
        "# this file is imported, so every worker starts with the same empty pool",
        "# state and the same counters. Left alone, eight workers hand out",
        "# rows[0], rows[1], ... in parallel: every account in the pool is used",
        "# eight times over, and a login-per-user scenario has eight virtual users",
        "# sharing one session. `_shard()` is how a worker learns which slice of",
        "# each pool is its own.",
        "_SHARD = {'id': 0, 'count': 1, 'resolved': False}",
        "_SHARD_ENV = {}",
        f"_SHARD_DATA = {bool(shard_data)!r}",
        "",
        "",
        "@events.init.add_listener",
        "def _remember_environment(environment, **_kwargs):",
        "    _SHARD_ENV['environment'] = environment",
        "",
        "",
        "def _shard():",
        "    '''Which of how many load-generating processes this one is.",
        "",
        "    Resolved on first use rather than at import: the worker index",
        "    arrives from the master on connect, and the worker count arrives",
        "    with the spawn message. Both are in place before the first user",
        "    starts, and neither exists at import time.",
        "    '''",
        "    if _SHARD['resolved']:",
        "        return _SHARD",
        "    environment = _SHARD_ENV.get('environment')",
        "    runner = getattr(environment, 'runner', None)",
        "    if runner is None:",
        "        # No runner yet — answer 'one process' without caching it, so",
        "        # the real answer is still picked up once there is one.",
        "        return _SHARD",
        "    index = getattr(runner, 'worker_index', 0)",
        "    options = getattr(environment, 'parsed_options', None)",
        "    count = getattr(options, 'expect_workers', 1)",
        "    try:",
        "        count = int(count)",
        "    except (TypeError, ValueError):",
        "        count = 1",
        "    try:",
        "        index = int(index)",
        "    except (TypeError, ValueError):",
        "        index = -1",
        "    if index < 0:",
        "        # Masters up to 2.10.2 never send an index. Sharding on a guess",
        "        # would hand the same rows to several workers, which is the",
        "        # failure this exists to prevent — so it declines to shard, and",
        "        # says why.",
        "        _LOG.warning(",
        "            'locomotive: this locust master does not report worker '",
        "            'indexes (needs locust >= 2.11); data pools will not be '",
        "            'split between workers'",
        "        )",
        "        index, count = 0, 1",
        "    count = max(1, count)",
        "    _SHARD.update({'id': index % count, 'count': count, 'resolved': True})",
        "    return _SHARD",
        "",
        "",
        "def _shard_pool(name, rows, mode, generated):",
        "    '''This worker's slice of a pool, or the whole pool.",
        "",
        "    A stride (rows[id::count]) rather than a contiguous block: the",
        "    slices differ in length by at most one row whatever the pool size,",
        "    they never overlap, and together they are exactly the pool — so the",
        "    uniqueness `unique_per_user` promises holds across the whole cluster,",
        "    for the same len(pool) users as in a single process.",
        "    '''",
        "    shard = _shard()",
        "    if not _SHARD_DATA:",
        "        # load.shard_data: false — every process sees the whole pool.",
        "        # _shard() is still called first: it is what marks the shard",
        "        # resolved, and the caller will not cache a pool until it is.",
        "        return rows",
        "    if shard['count'] < 2 or not rows:",
        "        return rows",
        "    if mode not in ('unique_per_user', 'round_robin'):",
        "        # 'once' has to keep seeing rows[0] or it stops meaning one row;",
        "        # 'random' is already correct, and splitting it would only",
        "        # narrow the values each worker can draw from.",
        "        return rows",
        "    if generated:",
        "        # Synthesised rows are drawn independently in every process, so",
        "        # there is no shared sequence to divide — splitting them would",
        "        # just throw away seven eighths of the pool.",
        "        return rows",
        "    sliced = rows[shard['id']::shard['count']]",
        "    if not sliced:",
        "        _LOG.warning(",
        "            'locomotive: data pool %r has %d row(s) for %d workers; '",
        "            'worker %d would get none, so it is using the whole pool — '",
        "            'rows will repeat across workers',",
        "            name, len(rows), shard['count'], shard['id'],",
        "        )",
        "        return rows",
        "    return sliced",
        "",
        "",
        "# Dynamic value generators",
        "_iteration_counter = 0",
        "_iteration_seeded = False",
        "",
        "_PLACEHOLDER_RE = re.compile(r'\\$\\{([^}]+)\\}')",
        "# A value that is *only* a placeholder can keep the placeholder's own",
        "# type when it lands in a JSON body — see _RuntimeMixin._resolve_json.",
        "_ONLY_PLACEHOLDER_RE = re.compile(r'^\\$\\{([^}]+)\\}$')",
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
        "    '''Incrementing counter, unique across workers.",
        "",
        "    Each worker starts a billion apart, so N workers produce N",
        "    non-overlapping runs of numbers instead of N interleaved copies of",
        "    1, 2, 3 — which matters the moment ${iteration} ends up in an",
        "    order number or an idempotency key.",
        "    '''",
        "    global _iteration_counter, _iteration_seeded",
        "    if not _iteration_seeded:",
        "        shard = _shard()",
        "        if shard['count'] > 1:",
        "            _iteration_counter = shard['id'] * 1000000000",
        "        _iteration_seeded = shard['resolved']",
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
        "_JSON_INDEX_RE = re.compile(r'\\[(-?\\d+)\\]')",
        "",
        "",
        "def _json_path(node, path):",
        "    '''Walk a dot path through a decoded JSON body.",
        "",
        "    Supports object keys and list indices, in either notation:",
        "    'data.items.0.id' and 'data.items[0].id' are the same path.",
        "    Returns (found, value) so a captured null is not confused with a",
        "    path that does not exist.",
        "    '''",
        "    parts = [p for p in _JSON_INDEX_RE.sub(r'.\\1', str(path)).split('.') if p != '']",
        "    for part in parts:",
        "        if isinstance(node, dict):",
        "            if part not in node:",
        "                return False, None",
        "            node = node[part]",
        "        elif isinstance(node, (list, tuple)):",
        "            try:",
        "                idx = int(part)",
        "            except ValueError:",
        "                return False, None",
        "            if idx >= len(node) or idx < -len(node):",
        "                return False, None",
        "            node = node[idx]",
        "        else:",
        "            return False, None",
        "    return True, node",
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
        "",
        "_FAKE_FIRST = ['James', 'Mary', 'John', 'Patricia', 'Robert', 'Jennifer', 'Michael', 'Linda', 'David', 'Elizabeth', 'William', 'Barbara', 'Richard', 'Susan', 'Joseph', 'Jessica', 'Thomas', 'Karen', 'Daniel', 'Nancy', 'Anna', 'Ivan', 'Olga', 'Sergei', 'Maria', 'Dmitry']",
        "_FAKE_LAST = ['Smith', 'Johnson', 'Williams', 'Brown', 'Jones', 'Garcia', 'Miller', 'Davis', 'Rodriguez', 'Martinez', 'Wilson', 'Anderson', 'Taylor', 'Moore', 'Petrov', 'Ivanov', 'Sidorov', 'Kim', 'Lee', 'Wang', 'Nguyen', 'Muller', 'Rossi', 'Novak']",
        "_FAKE_CITY = ['London', 'Paris', 'Berlin', 'Madrid', 'Rome', 'Moscow', 'Tokyo', 'Austin', 'Denver', 'Boston', 'Toronto', 'Sydney', 'Oslo', 'Prague', 'Vienna', 'Dublin']",
        "_FAKE_COUNTRY = ['USA', 'UK', 'Germany', 'France', 'Spain', 'Italy', 'Russia', 'Japan', 'Canada', 'Australia', 'Norway', 'Brazil', 'India', 'Poland', 'Sweden', 'Ireland']",
        "_FAKE_STREET = ['Main St', 'Oak Ave', 'Pine Rd', 'Maple Dr', 'Cedar Ln', 'Elm St', 'Park Ave', 'Lake Rd', 'Hill St', 'River Rd', 'King St', 'Queen St']",
        "_FAKE_DOMAIN = ['example.com', 'mail.com', 'test.org', 'demo.net', 'acme.io', 'sample.co', 'inbox.com', 'fastmail.dev']",
        "_FAKE_LOREM = 'lorem ipsum dolor sit amet consectetur adipiscing elit sed do eiusmod tempor incididunt ut labore et dolore magna aliqua enim ad minim veniam quis nostrud'.split()",
        "",
        "",
        "def _fake(kind, args):",
        "    '''Synthetic data generator for ${fake:kind[:args]} placeholders.'''",
        "    if kind in ('first_name', 'firstname'):",
        "        return random.choice(_FAKE_FIRST)",
        "    if kind in ('last_name', 'lastname'):",
        "        return random.choice(_FAKE_LAST)",
        "    if kind in ('name', 'full_name', 'fullname'):",
        "        return random.choice(_FAKE_FIRST) + ' ' + random.choice(_FAKE_LAST)",
        "    if kind == 'username':",
        "        return (random.choice(_FAKE_FIRST) + '.' + random.choice(_FAKE_LAST)).lower() + str(random.randint(1, 999))",
        "    if kind == 'email':",
        "        user = (random.choice(_FAKE_FIRST) + '.' + random.choice(_FAKE_LAST)).lower()",
        "        return user + str(random.randint(1, 9999)) + '@' + random.choice(_FAKE_DOMAIN)",
        "    if kind == 'domain':",
        "        return random.choice(_FAKE_DOMAIN)",
        "    if kind == 'phone':",
        "        return '+1-{:03d}-{:03d}-{:04d}'.format(random.randint(200, 999), random.randint(200, 999), random.randint(0, 9999))",
        "    if kind == 'city':",
        "        return random.choice(_FAKE_CITY)",
        "    if kind == 'country':",
        "        return random.choice(_FAKE_COUNTRY)",
        "    if kind == 'address':",
        "        return str(random.randint(1, 9999)) + ' ' + random.choice(_FAKE_STREET)",
        "    if kind == 'word':",
        "        return random.choice(_FAKE_LOREM)",
        "    if kind == 'words':",
        "        try:",
        "            n = int(args) if args else 3",
        "        except ValueError:",
        "            n = 3",
        "        return ' '.join(random.choice(_FAKE_LOREM) for _ in range(max(1, n)))",
        "    if kind == 'sentence':",
        "        words = [random.choice(_FAKE_LOREM) for _ in range(random.randint(4, 9))]",
        "        return words[0].capitalize() + ' ' + ' '.join(words[1:]) + '.'",
        "    if kind == 'digits':",
        "        try:",
        "            n = int(args) if args else 6",
        "        except ValueError:",
        "            n = 6",
        "        return ''.join(str(random.randint(0, 9)) for _ in range(max(1, n)))",
        "    if kind == 'bool':",
        "        return random.choice(('true', 'false'))",
        "    return ''",
        "",
        "",
        "def _resolve_static(value):",
        "    '''Resolve ${fake:}, ${env:} and builtin function placeholders without a user context.'''",
        "    if not isinstance(value, str):",
        "        return value",
        "",
        "    def replace(match):",
        "        key = match.group(1)",
        "        if key.startswith('fake:'):",
        "            kind, _, fargs = key[5:].partition(':')",
        "            return _fake(kind, fargs)",
        "        if key.startswith('env:'):",
        "            name, default = _parse_env_ref(key[4:])",
        "            return os.environ.get(name, default)",
        "        func_name, _, func_args = key.partition(':')",
        "        if func_name in _RUNTIME_FUNCTIONS:",
        "            return _call_function(func_name, func_args)",
        "        return ''",
        "",
        "    return _PLACEHOLDER_RE.sub(replace, value)",
        "",
    ]


def _emit_data_layer(data_specs: Dict[str, Dict[str, Any]]) -> List[str]:
    """Generate the data pool machinery (specs, lazy loader, counters)."""
    return [
        "",
        "# Data pools (${data:pool.field})",
        f"_DATA_SPECS = {_literal(data_specs)}",
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
        "        generate = spec.get('generate')",
        "        _generated = generate is not None",
        "        if generate is not None:",
        "            count = generate.get('count', 100)",
        "            try:",
        "                count = int(count)",
        "            except (TypeError, ValueError):",
        "                count = 100",
        "            fields = generate.get('fields') or {}",
        "            rows = [{k: _resolve_static(v) for k, v in fields.items()} for _ in range(max(0, count))]",
        "        elif spec.get('inline') is not None:",
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
        "        rows = _shard_pool(name, rows, spec.get('mode', 'unique_per_user'), _generated)",
        "        if _SHARD['resolved'] or _SHARD['count'] > 1:",
        "            # Caching before the shard is known would pin the whole",
        "            # pool for the run. Only locust can answer that question,",
        "            # and by the time a user exists it already has.",
        "            _DATA_POOLS[name] = rows",
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
        "            ${fake:name}        - synthetic data (name, email, city, words:N, digits:N, ...)",
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
        "            if key.startswith('fake:'):",
        "                kind, _, fake_args = key[5:].partition(':')",
        "                return _fake(kind, fake_args)",
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
        "        '''Recursively resolve placeholders in dict keys and values.",
        "",
        "        Keys are resolved too: a query dict like",
        "        {'${env:PARAM_NAME}': 'x'} or a body keyed by ${var:field} used",
        "        to be sent with the placeholder text as the literal key.",
        "        '''",
        "        if isinstance(d, dict):",
        "            return {",
        "                (self._resolve(k) if isinstance(k, str) else k):",
        "                self._resolve_dict(v) for k, v in d.items()",
        "            }",
        "        if isinstance(d, list):",
        "            return [self._resolve_dict(v) for v in d]",
        "        return self._resolve(d)",
        "",
        "    def _resolve_json(self, value):",
        "        '''Resolve placeholders in a JSON body, keeping non-string types.",
        "",
        "        A JSON body is the one place where the type on the wire matters.",
        "        {'quantity': '${randint:1:10}'} used to send the string '7', and",
        "        an API that declares quantity as an integer answers 422 to that —",
        "        which looks like a broken service in the report, not a broken",
        "        config. When a value is exactly one placeholder, with no text",
        "        around it, the placeholder's own type survives; anything mixed",
        "        with other text is a string, as it has to be.",
        "",
        "        Headers, query params and form bodies keep using _resolve_dict:",
        "        those are strings on the wire whatever the config says.",
        "        '''",
        "        if isinstance(value, dict):",
        "            return {",
        "                (self._resolve(k) if isinstance(k, str) else k):",
        "                self._resolve_json(v) for k, v in value.items()",
        "            }",
        "        if isinstance(value, list):",
        "            return [self._resolve_json(v) for v in value]",
        "        if isinstance(value, str):",
        "            match = _ONLY_PLACEHOLDER_RE.match(value)",
        "            if match is not None:",
        "                typed, handled = self._resolve_typed(match.group(1))",
        "                if handled:",
        "                    return typed",
        "        return self._resolve(value)",
        "",
        "    def _resolve_typed(self, key):",
        "        '''Native value for a whole-string placeholder: (value, handled).",
        "",
        "        Only placeholders whose type is beyond doubt are converted.",
        "        ${uuid}, ${random}, ${choice:...}, ${now:...}, ${env:} and every",
        "        ${fake:} but bool stay strings — a code that happens to be all",
        "        digits is still a code.",
        "        '''",
        "        if key.startswith('var:'):",
        "            variables = getattr(self, '_vars', {})",
        "            name = key[4:]",
        "            if name in variables:",
        "                found = variables[name]",
        "                # A captured number stays a number: the response said 42,",
        "                # so the follow-up request sends 42, not '42'. A failed",
        "                # capture stores None; '' keeps parity with _resolve",
        "                # rather than turning the field into a JSON null.",
        "                return ('' if found is None else found), True",
        "            return None, False",
        "        if key.startswith('data:'):",
        "            pool_name, _, field_path = key[5:].partition('.')",
        "            found = self._data_raw(pool_name, field_path)",
        "            return ('' if found is None else found), True",
        "        if key == 'fake:bool':",
        "            return random.choice((True, False)), True",
        "        func_name, _, func_args = key.partition(':')",
        "        if func_name in ('randint', 'iteration', 'timestamp'):",
        "            text = _call_function(func_name, func_args)",
        "            try:",
        "                return int(text), True",
        "            except (TypeError, ValueError):",
        "                return text, True",
        "        return None, False",
        "",
        "    def _begin_request(self):",
        "        '''Open a new per-request data scope.",
        "",
        "        'random' and 'round_robin' pools pick their row once per request",
        "        and reuse it for every ${data:} placeholder in that request, so a",
        "        login body cannot mix the username of one row with the password",
        "        of another. This clears the previous request's choice.",
        "        '''",
        "        self._request_rows = {}",
        "",
        "    def _data_value(self, pool_name, field_path):",
        "        '''Resolve ${data:pool.field} for this user, as a string.'''",
        "        value = self._data_raw(pool_name, field_path)",
        "        return '' if value is None else str(value)",
        "",
        "    def _data_raw(self, pool_name, field_path):",
        "        '''The pool value as it was loaded, before any stringification.'''",
        "        rows = _load_pool(pool_name)",
        "        if not rows:",
        "            return None",
        "        spec = _DATA_SPECS.get(pool_name) or {}",
        "        mode = spec.get('mode', 'unique_per_user')",
        "        if mode in ('random', 'round_robin'):",
        "            request_rows = getattr(self, '_request_rows', None)",
        "            if request_rows is None:",
        "                request_rows = self._request_rows = {}",
        "            row = request_rows.get(pool_name)",
        "            if row is None:",
        "                if mode == 'random':",
        "                    row = random.choice(rows)",
        "                else:",
        "                    row = rows[_next_index(pool_name + ':access', len(rows))]",
        "                request_rows[pool_name] = row",
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
        "        return value",
        "",
        "    def _capture(self, response, spec):",
        "        '''Store {name: json.path} values from a response body.",
        "",
        "        Returns a list of failure messages. A capture that finds nothing",
        "        is a failed request: the alternative is an empty ${var:} that",
        "        turns the next step into a puzzling 404 several lines later.",
        "        The body is decoded once, however many variables are captured.",
        "        '''",
        "        variables = getattr(self, '_vars', None)",
        "        if variables is None:",
        "            variables = self._vars = {}",
        "        try:",
        "            body = response.json()",
        "        except Exception:",
        "            for name in spec:",
        "                variables[name] = None",
        "            return ['capture: response body is not JSON']",
        "        fails = []",
        "        for name, path in spec.items():",
        "            found, value = _json_path(body, path)",
        "            variables[name] = value if found else None",
        "            if not found:",
        "                fails.append('capture %s: no value at %s' % (name, path))",
        "        return fails",
        "",
        "    def _assert_response(self, response, expect):",
        "        '''Check a response against an \"expect\" block.",
        "",
        "        Returns a list of human-readable failure messages (empty if the",
        "        response satisfies every expectation). Supported keys:",
        "            status:   int or list of ints  - allowed status codes",
        "            contains: str or list of str   - substrings the body must contain",
        "            json:     {dot.path: expected} - JSON fields (loose str compare)",
        "            max_ms:   number               - max response time in milliseconds",
        "",
        "        When 'status' is omitted the response still has to be one Locust",
        "        itself would consider successful: an 'expect' block never turns a",
        "        transport error or a 4xx/5xx into a passing sample.",
        "        '''",
        "        fails = []",
        "        if not isinstance(expect, dict):",
        "            expect = {}",
        "        code = getattr(response, 'status_code', 0) or 0",
        "        status = expect.get('status')",
        "        if status is not None:",
        "            allowed = status if isinstance(status, (list, tuple)) else [status]",
        "            codes = []",
        "            for s in allowed:",
        "                try:",
        "                    codes.append(int(s))",
        "                except (TypeError, ValueError):",
        "                    pass",
        "            if codes and code not in codes:",
        "                fails.append('status %s not in %s' % (code, codes))",
        "        elif code == 0:",
        "            # No response at all: connection refused, DNS failure, timeout.",
        "            fails.append('no response received (connection error or timeout)')",
        "        elif code >= 400:",
        "            # 'expect' runs under catch_response, which suppresses Locust's own",
        "            # raise_for_status(). Without an explicit 'status' expectation we keep",
        "            # Locust's default notion of failure rather than silently passing.",
        "            fails.append('status %s' % (code,))",
        "        contains = expect.get('contains')",
        "        if contains is not None:",
        "            needles = contains if isinstance(contains, (list, tuple)) else [contains]",
        "            try:",
        "                text = response.text or ''",
        "            except Exception:",
        "                text = ''",
        "            for needle in needles:",
        "                if str(needle) not in text:",
        "                    fails.append('body is missing %r' % (str(needle),))",
        "        json_expect = expect.get('json')",
        "        if isinstance(json_expect, dict) and json_expect:",
        "            decoded = True",
        "            try:",
        "                body = response.json()",
        "            except Exception:",
        "                body = None",
        "                decoded = False",
        "                fails.append('response body is not valid JSON')",
        "            if decoded:",
        "                for path, expected in json_expect.items():",
        "                    # _json_path reports absence separately, so",
        "                    # `expect: {json: {x: null}}` no longer passes on a",
        "                    # response that has no 'x' at all. Array indices",
        "                    # work here exactly as they do in 'capture'.",
        "                    found, actual = _json_path(body, path)",
        "                    if not found:",
        "                        fails.append('json %s: no value at that path' % (path,))",
        "                    elif str(actual) != str(expected):",
        "                        fails.append('json %s: expected %r, got %r' % (path, str(expected), actual))",
        "        max_ms = expect.get('max_ms')",
        "        if max_ms is not None:",
        "            try:",
        "                limit = float(max_ms)",
        "            except (TypeError, ValueError):",
        "                limit = None",
        "            elapsed_ms = None",
        "            try:",
        "                elapsed_ms = response.elapsed.total_seconds() * 1000",
        "            except Exception:",
        "                elapsed_ms = None",
        "            if limit is not None and elapsed_ms is not None and elapsed_ms > limit:",
        "                fails.append('response took %.0fms > %sms' % (elapsed_ms, max_ms))",
        "        return fails",
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

    @property
    def _needs_request_scope(self) -> bool:
        """True when some pool re-picks a row and needs a per-request scope.

        'unique_per_user' and 'once' pin one row for the lifetime of the user,
        so they never need the scope reset; only 'random' and 'round_robin' do.
        Scenarios without such a pool generate exactly the code they did before.
        """
        return any(
            (spec or {}).get("mode") in ("random", "round_robin")
            for spec in self.data_specs.values()
        )

    def _filter_by_tags(self, items: List[Any]) -> List[Any]:
        """Filter requests/flows by target include/exclude tags."""
        include_set = _tag_set(self.target.get("tags")) or None
        exclude_set = _tag_set(self.target.get("exclude_tags"))
        if include_set is None and not exclude_set:
            return items

        filtered = []
        for item in items:
            item_tags = _tag_set(item.get("tags")) if isinstance(item, dict) else set()
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
            generate = spec.get("generate")
            if generate is not None:
                if not isinstance(generate, dict):
                    raise ValueError(
                        f"{self.section_prefix}.data.{pool_name}.generate must be an object"
                    )
                fields = generate.get("fields")
                if not isinstance(fields, dict) or not fields:
                    raise ValueError(
                        f"{self.section_prefix}.data.{pool_name}.generate.fields "
                        "must be a non-empty object"
                    )
                count = generate.get("count", 100)
                try:
                    count = int(count)
                except (TypeError, ValueError):
                    raise ValueError(
                        f"{self.section_prefix}.data.{pool_name}.generate.count must be an integer"
                    )
                if count < 1:
                    raise ValueError(
                        f"{self.section_prefix}.data.{pool_name}.generate.count must be >= 1"
                    )
                generate = {"count": count, "fields": {str(k): v for k, v in fields.items()}}
            elif inline is not None:
                if not isinstance(inline, list) or not all(
                    isinstance(row, dict) for row in inline
                ):
                    raise ValueError(
                        f"{self.section_prefix}.data.{pool_name}.inline must be a list of objects"
                    )
            elif not (isinstance(source, str) and source.strip()):
                raise ValueError(
                    f"{self.section_prefix}.data.{pool_name} must define 'source' (file path), "
                    "'inline' (rows), or 'generate' (synthetic)"
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
                "generate": generate,
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
        # Read from this generator's own target: used directly as a library
        # entry point, this is the only place `shard_data` can come from —
        # `generate_locustfile` passes it explicitly for the persona case,
        # where several targets could disagree.
        shard_data = (self.target or {}).get("shard_data", True)
        return _write_locustfile([self], output_dir, shard_data=shard_data is not False)

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
        """Build a between(...) expression from a think_time config value.

        Bounds are ordered and non-negative before they reach locust:
        ``between(2, 0.5)`` makes ``random.uniform`` return values outside the
        range the config asked for, and a negative wait makes locust sleep for
        a nonsensical duration. Swapping is friendlier than failing — the
        intent of ``min: 2, max: 0.5`` is not in doubt — and ``loco validate``
        warns about it separately.
        """
        if isinstance(think_time, dict):
            min_wait = max(0.0, _safe_float(think_time.get("min"), 0.5))
            max_wait = max(0.0, _safe_float(think_time.get("max"), min_wait))
            if max_wait < min_wait:
                min_wait, max_wait = max_wait, min_wait
            return f"between({min_wait}, {max_wait})"
        if think_time is not None:
            value = max(0.0, _safe_float(think_time, 1.0))
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
        # A YAML name like `name: 2024` arrives as an int; locust needs a str
        # to group stats rows under.
        name = str(req.get("name") or f"{method} {path}")

        # Resolve dynamic path segments at runtime, but keep the template
        # string as the stats name so Locust groups all calls together.
        if "${" in path:
            path_expr = f"{user_expr}._resolve({_literal(path)})"
        else:
            path_expr = _literal(path)

        req_headers = req.get("headers") if isinstance(req.get("headers"), dict) else {}
        params = req.get("query") if isinstance(req.get("query"), dict) else None
        json_body = req.get("json")
        data_body = req.get("data")
        timeout = req.get("timeout")

        args: List[str] = [_literal(method), path_expr]
        kwargs: List[str] = [f"name={_literal(name)}"]

        if req_headers:
            kwargs.append(
                f"headers={user_expr}._resolve_dict({{**{user_expr}._base_headers, **{_literal(req_headers)}}})"
            )
        else:
            kwargs.append(f"headers={user_expr}._resolve_dict({user_expr}._base_headers)")
        if params:
            kwargs.append(f"params={user_expr}._resolve_dict({_literal(params)})")
        if json_body is not None:
            # _resolve_json, not _resolve_dict: a JSON body is the one place
            # where "7" and 7 are different things to the server.
            kwargs.append(f"json={user_expr}._resolve_json({_literal(json_body)})")
        # 'json' and 'data' both fill the request body; requests would send the
        # form body and drop the JSON one silently. 'loco validate' rejects the
        # combination, and here 'json' wins so the two never disagree.
        if data_body is not None and json_body is None:
            kwargs.append(f"data={user_expr}._resolve_dict({_literal(data_body)})")
        if timeout is not None:
            # A YAML `timeout: "5"` is a string; requests raises a TypeError on
            # it mid-run, after the whole load test has already started.
            seconds = _safe_float(timeout, 0.0)
            if seconds > 0:
                kwargs.append(f"timeout={seconds}")

        return ", ".join(args + kwargs)

    def _generate_request_stmt(
        self,
        req: Dict[str, Any],
        indent: int,
        user_expr: str = "self",
    ) -> List[str]:
        """Generate the request call plus optional capture / assertion handling.

        Two shapes:
          - plain request (no capture, no expect): a bare ``client.request(...)``
            whose success is judged by Locust itself
          - capture and/or expect: a ``catch_response=True`` block, so a failed
            assertion *or* a capture that found nothing marks the sample as a
            failure instead of quietly poisoning a later step with an empty
            variable
        """
        pad = " " * indent
        call = self._build_request_call(req, user_expr)

        # Every ${data:} placeholder in this request resolves against the same
        # pool row; the scope is reset here, right before the call is built.
        prelude: List[str] = []
        if self._needs_request_scope:
            prelude.append(f"{pad}{user_expr}._begin_request()")

        capture = req.get("capture")
        has_capture = isinstance(capture, dict) and bool(capture)
        expect = req.get("expect")
        has_expect = isinstance(expect, dict) and bool(expect)

        if not has_capture and not has_expect:
            return prelude + [f"{pad}self.client.request({call})"]

        inner = pad + "    "
        lines = prelude + [f"{pad}with self.client.request({call}, catch_response=True) as resp:"]

        # catch_response suppresses Locust's own status check, so the implicit
        # one runs even when there is no 'expect' block.
        expect_arg = (
            f"{user_expr}._resolve_dict({_literal(expect)})" if has_expect else "{}"
        )
        lines.append(f"{inner}_fails = {user_expr}._assert_response(resp, {expect_arg})")

        if has_capture:
            spec = {str(k): str(v) for k, v in capture.items()}
            lines.append(f"{inner}_capture_fails = {user_expr}._capture(resp, {_literal(spec)})")
            lines.append(
                f"{inner}# A response that already failed explains itself; "
                "capture errors on top of it are noise."
            )
            lines.append(f"{inner}_fails = _fails or _capture_fails")

        lines.append(f"{inner}if _fails:")
        lines.append(f"{inner}    resp.failure('; '.join(_fails))")
        lines.append(f"{inner}else:")
        lines.append(f"{inner}    resp.success()")
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
        lines.append(f"    _base_headers = {_literal(base_headers)}")

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


def _write_locustfile(
    generators: List[ScenarioGenerator],
    output_dir: Path,
    shard_data: bool = True,
) -> Path:
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
    lines.extend(_emit_helpers(shard_data))
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
    shard_data = (target or {}).get("shard_data", True)
    return _write_locustfile(generators, output_dir, shard_data=shard_data is not False)
