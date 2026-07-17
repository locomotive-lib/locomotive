from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Dict, FrozenSet, Set, Union

from .scenario import RUNTIME_FUNCTIONS

_ENV_RE = re.compile(r"\$\{([^}]+)\}|\$([A-Za-z_][A-Za-z0-9_]*)")

# Built-in runtime placeholders that should NOT be resolved at config load time.
# These are handled at runtime by the generated locustfile's _resolve() method.
# The registry lives in scenario.py — the single source of truth shared by the
# loader and the code generator.
_RUNTIME_PLACEHOLDERS = RUNTIME_FUNCTIONS

# Namespaced placeholders resolved at runtime (captured variables, data pools).
_RUNTIME_NAMESPACES = ("var:", "data:")


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


def is_runtime_placeholder(ref: str, capture_names: FrozenSet[str] = frozenset()) -> bool:
    """Return True if a ${...} reference must be left for runtime resolution."""
    if ref.startswith(_RUNTIME_NAMESPACES):
        return True
    # Function calls: name before the first ':' (handles args that contain
    # ':-', e.g. ${randint:1:-100}, which _parse_env_ref would misparse).
    if ref.partition(":")[0] in _RUNTIME_PLACEHOLDERS:
        return True
    name, _ = _parse_env_ref(ref)
    return name in capture_names


def _resolve_env_value(value: Any, capture_names: FrozenSet[str] = frozenset()) -> Any:
    if isinstance(value, str):
        def repl(match: re.Match) -> str:
            ref = match.group(1) or match.group(2) or ""
            if is_runtime_placeholder(ref, capture_names):
                return match.group(0)  # preserve as-is
            if ref.startswith("env:"):
                # Explicit env namespace: ${env:NAME} / ${env:NAME:-default}
                ref = ref[4:]
            name, default = _parse_env_ref(ref)
            return os.environ.get(name, default)

        return _ENV_RE.sub(repl, value)
    if isinstance(value, list):
        return [_resolve_env_value(item, capture_names) for item in value]
    if isinstance(value, dict):
        return {key: _resolve_env_value(item, capture_names) for key, item in value.items()}
    return value


def _resolve_path(base_dir: Path, value: Any) -> Any:
    if not isinstance(value, str) or value.strip() == "":
        return value
    path = Path(value)
    if path.is_absolute():
        return str(path)
    return str((base_dir / path).resolve())


def _resolve_paths(config: Dict[str, Any], base_dir: Path) -> Dict[str, Any]:
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
    scenario = config.get("scenario")
    if isinstance(scenario, dict):
        data = scenario.get("data")
        if isinstance(data, dict):
            for pool in data.values():
                if isinstance(pool, dict) and "source" in pool:
                    pool["source"] = _resolve_path(base_dir, pool["source"])
    return config


def _load_data_file(path: Path) -> Dict[str, Any]:
    raw = path.read_text(encoding="utf-8")
    if path.suffix.lower() in {".yml", ".yaml"}:
        try:
            import yaml  # type: ignore
        except ImportError as exc:
            raise RuntimeError("PyYAML is required for YAML configs") from exc
        data = yaml.safe_load(raw)
    else:
        data = json.loads(raw)
    return data or {}


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


def load_config(path: Union[str, Path]) -> Dict[str, Any]:
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")
    data = _load_data_file(config_path)
    data = _process_includes(data, config_path.parent)
    capture_names = frozenset(_collect_capture_names(data))
    data = _resolve_env_value(data, capture_names)
    return _resolve_paths(data, config_path.parent)
