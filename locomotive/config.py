from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Dict, FrozenSet, Set, Union

from .scenario import RUNTIME_FUNCTIONS

# Only the braced form is a placeholder. The bare `$NAME` form used to be
# accepted here, but it also swallowed every `$ref` in a JSON-Schema body and
# every `$` in a jq-style path, and the generated locustfile never supported
# it anyway — the two resolvers disagreed about what a config meant.
_ENV_RE = re.compile(r"\$\{([^}]+)\}")

# Built-in runtime placeholders that should NOT be resolved at config load time.
# These are handled at runtime by the generated locustfile's _resolve() method.
# The registry lives in scenario.py — the single source of truth shared by the
# loader and the code generator.
_RUNTIME_PLACEHOLDERS = RUNTIME_FUNCTIONS

# Namespaced placeholders resolved at runtime (captured variables, data pools).
_RUNTIME_NAMESPACES = ("var:", "data:", "fake:")

# Sections whose values are consumed by the generated locustfile, so ${env:}
# inside them can be left for runtime. Keeping secrets out of the generated
# file matters: it is written into the artifacts directory and routinely
# uploaded as a CI build artifact.
_DEFERRED_ENV_SECTIONS = ("scenario", "users")

# ...except under these keys, which the generator itself reads while building
# the file (file paths, counters, method names). Nothing resolves them later,
# so they must still be substituted at load time.
_BUILD_TIME_KEYS = frozenset(
    {
        "source",
        "mode",
        "count",
        "weight",
        "tags",
        "exclude_tags",
        "think_time",
        "name",
        "include",
        "locustfile",
    }
)


def _parse_env_ref(ref: str) -> tuple:
    """Parse environment variable reference, supporting ${VAR:-default} syntax."""
    if ":-" in ref:
        name, default = ref.split(":-", 1)
        return name, default
    if ":" in ref:
        # ${VAR:default} also supported
        name, default = ref.split(":", 1)
        return name, default
    return ref, ""


def _collect_capture_names(data: Any) -> Set[str]:
    """Collect variable names defined via "capture" anywhere in the config.

    Placeholders referencing captured variables (e.g. ${auth_token}) must
    survive config loading so the generated locustfile can resolve them at
    runtime from the values captured per virtual user.
    """
    names: Set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            capture = node.get("capture")
            if isinstance(capture, dict):
                names.update(str(key) for key in capture.keys())
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(data)
    return names


def is_runtime_placeholder(
    ref: str,
    capture_names: FrozenSet[str] = frozenset(),
    defer_env: bool = False,
) -> bool:
    """Return True if a ${...} reference must be left for runtime resolution."""
    if ref.startswith(_RUNTIME_NAMESPACES):
        return True
    if defer_env and ref.startswith("env:"):
        return True
    # Function calls: name before the first ':' (handles args that contain
    # ':-', e.g. ${randint:1:-100}, which _parse_env_ref would misparse).
    if ref.partition(":")[0] in _RUNTIME_PLACEHOLDERS:
        return True
    name, _ = _parse_env_ref(ref)
    return name in capture_names


def _resolve_env_value(
    value: Any,
    capture_names: FrozenSet[str] = frozenset(),
    defer_env: bool = False,
) -> Any:
    if isinstance(value, str):
        def repl(match: re.Match) -> str:
            ref = match.group(1) or ""
            if is_runtime_placeholder(ref, capture_names, defer_env):
                return match.group(0)  # preserve as-is
            if ref.startswith("env:"):
                # Explicit env namespace: ${env:NAME} / ${env:NAME:-default}
                ref = ref[4:]
            name, default = _parse_env_ref(ref)
            return os.environ.get(name, default)

        return _ENV_RE.sub(repl, value)
    if isinstance(value, list):
        return [_resolve_env_value(item, capture_names, defer_env) for item in value]
    if isinstance(value, dict):
        return {
            key: _resolve_env_value(
                item, capture_names, defer_env and key not in _BUILD_TIME_KEYS
            )
            for key, item in value.items()
        }
    return value


def _resolve_path(base_dir: Path, value: Any) -> Any:
    if not isinstance(value, str) or value.strip() == "":
        return value
    path = Path(value)
    if path.is_absolute():
        return str(path)
    return str((base_dir / path).resolve())


def _resolve_scenario_paths(scenario: Any, base_dir: Path) -> None:
    """Make every data pool 'source' in one scenario absolute."""
    if not isinstance(scenario, dict):
        return
    data = scenario.get("data")
    if isinstance(data, dict):
        for pool in data.values():
            if isinstance(pool, dict) and "source" in pool:
                pool["source"] = _resolve_path(base_dir, pool["source"])


def _resolve_paths(config: Any, base_dir: Path) -> Any:
    """Resolve relative file paths against the file the config came from.

    Personas are covered as well as the single scenario: a pool declared under
    ``users[i].scenario.data`` used to be left relative, so it resolved against
    whatever directory ``loco`` happened to be run from and vanished in CI.
    """
    if not isinstance(config, dict):
        return config
    locust = config.get("locust")
    if isinstance(locust, dict) and "locustfile" in locust:
        locust["locustfile"] = _resolve_path(base_dir, locust["locustfile"])
    artifacts = config.get("artifacts")
    if isinstance(artifacts, dict) and "storage" in artifacts:
        artifacts["storage"] = _resolve_path(base_dir, artifacts["storage"])
    analysis = config.get("analysis")
    if isinstance(analysis, dict) and "rules_file" in analysis:
        analysis["rules_file"] = _resolve_path(base_dir, analysis["rules_file"])
    report = config.get("report")
    if isinstance(report, dict) and "output" in report:
        report["output"] = _resolve_path(base_dir, report["output"])
    _resolve_scenario_paths(config.get("scenario"), base_dir)
    users = config.get("users")
    if isinstance(users, list):
        for entry in users:
            if isinstance(entry, dict):
                # Both persona shapes: {"scenario": {...}} and the flat one
                # where the persona *is* the scenario.
                _resolve_scenario_paths(entry.get("scenario"), base_dir)
                _resolve_scenario_paths(entry, base_dir)
    return config


def _load_data_file(path: Path) -> Dict[str, Any]:
    """Read one config file.

    Parse failures come back as ValueError naming the file. A raw
    ``yaml.scanner.ScannerError`` reaching the CLI prints a traceback through
    PyYAML's internals, which reads like a crash in Locomotive rather than
    what it is: a tab where the file wanted spaces, on a line the message
    already knows.
    """
    if path.suffix.lower() in {".yml", ".yaml"}:
        try:
            import yaml  # type: ignore
        except ImportError as exc:
            raise ValueError(
                f"{path}: reading YAML configs needs PyYAML — pip install pyyaml"
            ) from exc
        try:
            # The open file rather than its text: PyYAML names the stream in
            # its own error messages, and a stream with no name is reported
            # as `in "<unicode string>"`, which sends the reader looking for
            # a file that does not exist.
            with path.open(encoding="utf-8") as handle:
                data = yaml.safe_load(handle)
        except yaml.YAMLError as exc:
            raise ValueError(f"{path}: could not parse YAML: {exc}") from exc
    else:
        raw = path.read_text(encoding="utf-8")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"{path}: could not parse JSON: {exc.msg} "
                f"(line {exc.lineno}, column {exc.colno})"
            ) from exc
    return data if data is not None else {}


def _require_mapping(data: Any, path: Path) -> Dict[str, Any]:
    """The top level of a config file, or a message saying why it isn't one."""
    if not isinstance(data, dict):
        raise ValueError(
            f"{path}: a config must be an object at the top level, got "
            f"{type(data).__name__}"
        )
    return data


_MAX_INCLUDE_DEPTH = 10


def _process_includes(node: Any, base_dir: Path, depth: int = 0) -> Any:
    """Recursively expand "include" directives.

    A dict node {"include": "personas/reader.yaml", ...other} is replaced by
    the included file's content merged with the sibling keys — sibling keys
    win. Included files may include further files (paths are relative to the
    file that contains the directive). Runs BEFORE placeholder resolution so
    included capture names are preserved correctly.
    """
    if depth > _MAX_INCLUDE_DEPTH:
        raise ValueError(
            f"include nesting deeper than {_MAX_INCLUDE_DEPTH} levels "
            "(possible include cycle)"
        )
    if isinstance(node, dict):
        include_ref = node.get("include")
        if isinstance(include_ref, str) and include_ref.strip():
            include_path = Path(include_ref)
            if not include_path.is_absolute():
                include_path = (base_dir / include_path).resolve()
            if not include_path.exists():
                raise ValueError(f"Included file not found: {include_path}")
            content = _load_data_file(include_path)
            content = _process_includes(content, include_path.parent, depth + 1)
            if not isinstance(content, dict):
                raise ValueError(
                    f"Included file must contain an object, got "
                    f"{type(content).__name__}: {include_path}"
                )
            # Paths inside the fragment are written relative to the fragment,
            # not to the config that includes it. Resolving them here — while
            # the including file's directory is still known — is the only
            # moment that information exists. The fragment may be a whole
            # config, a scenario, or a persona, so both shapes are tried;
            # each only touches keys that are actually present, and the later
            # top-level pass leaves the now-absolute paths alone.
            _resolve_paths(content, include_path.parent)
            _resolve_scenario_paths(content, include_path.parent)
            rest = {
                key: _process_includes(value, base_dir, depth)
                for key, value in node.items()
                if key != "include"
            }
            return {**content, **rest}
        return {
            key: _process_includes(value, base_dir, depth)
            for key, value in node.items()
        }
    if isinstance(node, list):
        return [_process_includes(item, base_dir, depth) for item in node]
    return node


def load_config_raw(path: Union[str, Path]) -> Dict[str, Any]:
    """Load a config with ``include`` expanded but no ``${...}`` substituted.

    ``loco diff`` compares what the user *wrote* against the spec, and a path
    written as ``/users/${PATH_ID:-1}`` is a parameterized path. Substituting
    it to ``/users/1`` first — which is what ``load_config`` does, correctly,
    for a run — turns every scaffolded path param into a literal segment that
    matches no spec operation. The result was that ``loco init --openapi
    spec.json`` followed immediately by ``loco diff --openapi spec.json``
    reported breaking drift against the very spec it had just generated from.
    """
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")
    data = _require_mapping(_load_data_file(config_path), config_path)
    data = _process_includes(data, config_path.parent)
    return _resolve_paths(data, config_path.parent)


def load_config(path: Union[str, Path]) -> Dict[str, Any]:
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")
    data = _require_mapping(_load_data_file(config_path), config_path)
    data = _process_includes(data, config_path.parent)
    capture_names = frozenset(_collect_capture_names(data))
    if isinstance(data, dict):
        # ${env:} survives load only inside the sections the generated
        # locustfile can resolve itself; everywhere else (host, storage paths,
        # rules files) it is substituted here as before.
        data = {
            key: _resolve_env_value(
                value, capture_names, defer_env=key in _DEFERRED_ENV_SECTIONS
            )
            for key, value in data.items()
        }
    else:
        data = _resolve_env_value(data, capture_names)
    return _resolve_paths(data, config_path.parent)
