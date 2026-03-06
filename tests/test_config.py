import json

import pytest

from locomotive.config import _parse_env_ref, _resolve_env_value, load_config


# ── _parse_env_ref ────────────────────────────────────────────────────


class TestParseEnvRef:
    def test_plain_var(self):
        assert _parse_env_ref("MY_VAR") == ("MY_VAR", "")

    def test_var_with_default(self):
        assert _parse_env_ref("MY_VAR:-fallback") == ("MY_VAR", "fallback")

    def test_var_colon_default(self):
        assert _parse_env_ref("MY_VAR:fallback") == ("MY_VAR", "fallback")

    def test_empty_default(self):
        assert _parse_env_ref("MY_VAR:-") == ("MY_VAR", "")


# ── _resolve_env_value ────────────────────────────────────────────────


class TestResolveEnvValue:
    def test_string_with_env_var(self, monkeypatch):
        monkeypatch.setenv("TEST_HOST", "localhost")
        result = _resolve_env_value("http://${TEST_HOST}:8080")
        assert result == "http://localhost:8080"

    def test_default_when_unset(self, monkeypatch):
        monkeypatch.delenv("UNSET_VAR", raising=False)
        result = _resolve_env_value("${UNSET_VAR:-default_val}")
        assert result == "default_val"

    def test_multiple_vars(self, monkeypatch):
        monkeypatch.setenv("A", "hello")
        monkeypatch.setenv("B", "world")
        result = _resolve_env_value("${A} ${B}")
        assert result == "hello world"

    def test_nested_dict(self, monkeypatch):
        monkeypatch.setenv("TOKEN", "abc123")
        data = {"auth": {"token": "${TOKEN}"}}
        result = _resolve_env_value(data)
        assert result["auth"]["token"] == "abc123"

    def test_list_resolved(self, monkeypatch):
        monkeypatch.setenv("TAG", "api")
        result = _resolve_env_value(["${TAG}", "other"])
        assert result == ["api", "other"]

    def test_non_string_unchanged(self):
        assert _resolve_env_value(42) == 42
        assert _resolve_env_value(None) is None
        assert _resolve_env_value(True) is True


# ── load_config ───────────────────────────────────────────────────────


class TestLoadConfig:
    def test_nonexistent_file(self):
        with pytest.raises(FileNotFoundError, match="Config file not found"):
            load_config("/nonexistent/path.json")

    def test_json_config(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MY_HOST", "example.com")
        config = {"load": {"host": "http://${MY_HOST}"}}
        config_path = tmp_path / "loconfig.json"
        config_path.write_text(json.dumps(config))
        result = load_config(config_path)
        assert result["load"]["host"] == "http://example.com"

    def test_relative_paths_resolved(self, tmp_path):
        config = {
            "locust": {"locustfile": "scripts/test.py"},
            "artifacts": {"storage": "artifacts"},
        }
        config_path = tmp_path / "loconfig.json"
        config_path.write_text(json.dumps(config))
        result = load_config(config_path)
        assert str(tmp_path) in result["locust"]["locustfile"]
        assert str(tmp_path) in result["artifacts"]["storage"]

    def test_absolute_paths_unchanged(self, tmp_path):
        config = {"locust": {"locustfile": "/absolute/path/test.py"}}
        config_path = tmp_path / "loconfig.json"
        config_path.write_text(json.dumps(config))
        result = load_config(config_path)
        assert result["locust"]["locustfile"] == "/absolute/path/test.py"
