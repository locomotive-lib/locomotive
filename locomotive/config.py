from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Dict, Union

_ENV_RE = re.compile(r"\$\{([^}]+)\}|\$([A-Za-z_][A-Za-z0-9_]*)")

# Built-in runtime placeholders that should NOT be resolved at config load time.
# These are handled at runtime by the generated locustfile's _resolve() function.
_RUNTIME_PLACEHOLDERS = frozenset({"random", "timestamp", "iteration"})


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


def _resolve_env_value(value: Any) -> Any:
    if isinstance(value, str):
        def repl(match: re.Match) -> str:
            ref = match.group(1) or match.group(2) or ""
            name, _ = _parse_env_ref(ref)
            if name in _RUNTIME_PLACEHOLDERS:
                return match.group(0)  # preserve as-is
            name, default = _parse_env_ref(ref)
            return os.environ.get(name, default)

        return _ENV_RE.sub(repl, value)
    if isinstance(value, list):
        return [_resolve_env_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _resolve_env_value(item) for key, item in value.items()}
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


def load_config(path: Union[str, Path]) -> Dict[str, Any]:
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")
    data = _load_data_file(config_path)
    data = _resolve_env_value(data)
    return _resolve_paths(data, config_path.parent)
