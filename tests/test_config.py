import json

import pytest

from locomotive.config import (
    _collect_capture_names,
    _parse_env_ref,
    _resolve_env_value,
    is_runtime_placeholder,
    load_config,
)


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


# ── runtime placeholder preservation ──────────────────────────────────


class TestRuntimePlaceholderPreservation:
    def test_builtin_runtime_preserved(self):
        value = "ts=${timestamp} r=${random} i=${iteration}"
        assert _resolve_env_value(value) == value

    def test_var_namespace_preserved(self, monkeypatch):
        monkeypatch.setenv("order_id", "should-not-leak")
        assert _resolve_env_value("/orders/${var:order_id}") == "/orders/${var:order_id}"

    def test_data_namespace_preserved(self):
        assert _resolve_env_value("${data:accounts.login}") == "${data:accounts.login}"

    def test_captured_names_preserved(self, monkeypatch):
        monkeypatch.setenv("auth_token", "should-not-leak")
        value = "Bearer ${auth_token}"
        assert _resolve_env_value(value, frozenset({"auth_token"})) == value

    def test_uncaptured_name_still_resolved(self, monkeypatch):
        monkeypatch.setenv("PLAIN", "resolved")
        assert _resolve_env_value("${PLAIN}", frozenset({"auth_token"})) == "resolved"

    def test_env_namespace_resolved_at_load(self, monkeypatch):
        monkeypatch.setenv("MY_TOKEN", "xyz")
        assert _resolve_env_value("${env:MY_TOKEN}") == "xyz"

    def test_env_namespace_default(self, monkeypatch):
        monkeypatch.delenv("UNSET_VAR", raising=False)
        assert _resolve_env_value("${env:UNSET_VAR:-fallback}") == "fallback"


class TestCollectCaptureNames:
    def test_collects_from_on_start(self):
        config = {
            "scenario": {
                "on_start": [
                    {"path": "/login", "capture": {"auth_token": "data.token"}},
                    {"path": "/profile", "capture": {"uid": "id", "org": "org.id"}},
                ]
            }
        }
        assert _collect_capture_names(config) == {"auth_token", "uid", "org"}

    def test_empty_config(self):
        assert _collect_capture_names({}) == set()

    def test_capture_not_dict_ignored(self):
        config = {"scenario": {"on_start": [{"path": "/x", "capture": "bad"}]}}
        assert _collect_capture_names(config) == set()


class TestIsRuntimePlaceholder:
    @pytest.mark.parametrize("ref,expected", [
        ("timestamp", True),
        ("random", True),
        ("iteration", True),
        ("var:token", True),
        ("data:accounts.login", True),
        ("MY_VAR", False),
        ("MY_VAR:-default", False),
        ("env:MY_VAR", False),
    ])
    def test_refs(self, ref, expected):
        assert is_runtime_placeholder(ref) is expected

    def test_captured_name(self):
        assert is_runtime_placeholder("auth_token", frozenset({"auth_token"})) is True


class TestLoadConfigCapturePreservation:
    def test_end_to_end(self, tmp_path, monkeypatch):
        monkeypatch.delenv("auth_token", raising=False)
        config = {
            "scenario": {
                "headers": {"Authorization": "Bearer ${auth_token}"},
                "on_start": [
                    {"path": "/login", "capture": {"auth_token": "data.token"}}
                ],
                "requests": [{"name": "P", "method": "GET", "path": "/p"}],
            }
        }
        config_path = tmp_path / "loconfig.json"
        config_path.write_text(json.dumps(config))
        result = load_config(config_path)
        headers = result["scenario"]["headers"]
        assert headers["Authorization"] == "Bearer ${auth_token}"


class TestRuntimeFunctionPreservation:
    """D2: new function placeholders must survive config loading."""

    @pytest.mark.parametrize("value", [
        "${uuid}",
        "${randint:1:100}",
        "${randint:1:-100}",
        "${choice:a,b,c}",
        "${now:%H:%M}",
        "${random:16}",
    ])
    def test_preserved(self, value):
        assert _resolve_env_value(value) == value

    def test_similar_env_name_still_resolved(self, monkeypatch):
        monkeypatch.setenv("UUID_SUFFIX", "abc")
        assert _resolve_env_value("${UUID_SUFFIX}") == "abc"

    @pytest.mark.parametrize("ref", [
        "uuid", "randint:1:100", "randint:1:-100", "choice:a,b", "now:%H:%M", "random:4",
    ])
    def test_is_runtime_placeholder(self, ref):
        assert is_runtime_placeholder(ref) is True

    def test_registry_shared_with_generator(self):
        from locomotive.config import _RUNTIME_PLACEHOLDERS
        from locomotive.scenario import RUNTIME_FUNCTIONS
        assert _RUNTIME_PLACEHOLDERS is RUNTIME_FUNCTIONS


class TestDataSourcePathResolution:
    def test_relative_source_resolved(self, tmp_path):
        config = {
            "scenario": {
                "data": {"accounts": {"source": "data/accounts.csv"}},
                "requests": [{"name": "P", "method": "GET", "path": "/p"}],
            }
        }
        config_path = tmp_path / "loconfig.json"
        config_path.write_text(json.dumps(config))
        result = load_config(config_path)
        source = result["scenario"]["data"]["accounts"]["source"]
        assert str(tmp_path) in source and source.endswith("accounts.csv")

    def test_absolute_source_unchanged(self, tmp_path):
        config = {
            "scenario": {
                "data": {"accounts": {"source": "/abs/accounts.csv"}},
                "requests": [{"name": "P", "method": "GET", "path": "/p"}],
            }
        }
        config_path = tmp_path / "loconfig.json"
        config_path.write_text(json.dumps(config))
        result = load_config(config_path)
        assert result["scenario"]["data"]["accounts"]["source"] == "/abs/accounts.csv"

    def test_inline_pool_untouched(self, tmp_path):
        config = {
            "scenario": {
                "data": {"accounts": {"inline": [{"login": "u1"}]}},
                "requests": [{"name": "P", "method": "GET", "path": "/p"}],
            }
        }
        config_path = tmp_path / "loconfig.json"
        config_path.write_text(json.dumps(config))
        result = load_config(config_path)
        assert result["scenario"]["data"]["accounts"]["inline"] == [{"login": "u1"}]


# ── include directive ─────────────────────────────────────────────────


class TestInclude:
    def test_persona_include_merges_scenario(self, tmp_path):
        (tmp_path / "personas").mkdir()
        (tmp_path / "personas" / "reader.json").write_text(json.dumps({
            "scenario": {
                "think_time": 0.5,
                "requests": [{"name": "Read", "method": "GET", "path": "/articles"}],
            }
        }))
        config = {
            "users": [
                {"weight": 4, "name": "reader", "include": "personas/reader.json"}
            ]
        }
        config_path = tmp_path / "loconfig.json"
        config_path.write_text(json.dumps(config))
        result = load_config(config_path)
        entry = result["users"][0]
        assert entry["weight"] == 4            # sibling keys survive
        assert entry["name"] == "reader"
        assert entry["scenario"]["requests"][0]["path"] == "/articles"
        assert "include" not in entry

    def test_sibling_keys_override_included(self, tmp_path):
        (tmp_path / "base.json").write_text(json.dumps({"weight": 1, "a": "base"}))
        config = {"users": [{"include": "base.json", "weight": 9}]}
        config_path = tmp_path / "loconfig.json"
        config_path.write_text(json.dumps(config))
        result = load_config(config_path)
        assert result["users"][0]["weight"] == 9
        assert result["users"][0]["a"] == "base"

    def test_nested_includes_relative_to_parent(self, tmp_path):
        sub = tmp_path / "personas"
        sub.mkdir()
        (sub / "shared_headers.json").write_text(json.dumps(
            {"headers": {"X-Shared": "1"}}
        ))
        (sub / "reader.json").write_text(json.dumps({
            "scenario": {
                "include": "shared_headers.json",   # relative to personas/
                "requests": [{"name": "R", "method": "GET", "path": "/r"}],
            }
        }))
        config = {"users": [{"weight": 1, "include": "personas/reader.json"}]}
        config_path = tmp_path / "loconfig.json"
        config_path.write_text(json.dumps(config))
        result = load_config(config_path)
        scenario = result["users"][0]["scenario"]
        assert scenario["headers"] == {"X-Shared": "1"}
        assert scenario["requests"][0]["path"] == "/r"

    def test_missing_include_raises(self, tmp_path):
        config = {"users": [{"include": "nope.json"}]}
        config_path = tmp_path / "loconfig.json"
        config_path.write_text(json.dumps(config))
        with pytest.raises(ValueError, match="Included file not found"):
            load_config(config_path)

    def test_include_cycle_raises(self, tmp_path):
        (tmp_path / "a.json").write_text(json.dumps({"include": "b.json"}))
        (tmp_path / "b.json").write_text(json.dumps({"include": "a.json"}))
        config_path = tmp_path / "loconfig.json"
        config_path.write_text(json.dumps({"scenario": {"include": "a.json"}}))
        with pytest.raises(ValueError, match="include nesting"):
            load_config(config_path)

    def test_include_non_object_raises(self, tmp_path):
        (tmp_path / "list.json").write_text(json.dumps([1, 2]))
        config_path = tmp_path / "loconfig.json"
        config_path.write_text(json.dumps({"scenario": {"include": "list.json"}}))
        with pytest.raises(ValueError, match="must contain an object"):
            load_config(config_path)

    def test_captures_inside_included_file_preserved(self, tmp_path, monkeypatch):
        monkeypatch.delenv("tok", raising=False)
        (tmp_path / "persona.json").write_text(json.dumps({
            "scenario": {
                "headers": {"Authorization": "Bearer ${tok}"},
                "on_start": [{"path": "/login", "capture": {"tok": "t"}}],
                "requests": [{"name": "P", "method": "GET", "path": "/p"}],
            }
        }))
        config_path = tmp_path / "loconfig.json"
        config_path.write_text(json.dumps(
            {"users": [{"weight": 1, "include": "persona.json"}]}
        ))
        result = load_config(config_path)
        headers = result["users"][0]["scenario"]["headers"]
        assert headers["Authorization"] == "Bearer ${tok}"
