import base64
import sys
import types

import pytest

from locomotive.scenario import ScenarioGenerator, _slugify, _safe_int, _safe_float


# ── helpers: execute generated code with a stubbed locust ─────────────


class StubResponse:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class StubClient:
    """Records request() calls; returns a canned JSON payload."""

    def __init__(self, payload=None):
        self.calls = []
        self._payload = payload if payload is not None else {}

    def request(self, method, url, **kwargs):
        call = {"method": method, "url": url}
        call.update(kwargs)
        self.calls.append(call)
        return StubResponse(self._payload)


def _generate(tmp_path, scenario, target=None):
    gen = ScenarioGenerator(scenario, target or {})
    path = gen.generate(tmp_path)
    content = path.read_text()
    # Invariant: every generated file must be valid Python.
    compile(content, str(path), "exec")
    return content


def _exec_generated(tmp_path, scenario, target=None):
    """Generate a locustfile and exec it with a fake `locust` module."""
    content = _generate(tmp_path, scenario, target)

    fake = types.ModuleType("locust")
    fake.HttpUser = type("HttpUser", (), {})
    fake.task = lambda weight=1: (lambda f: f)
    fake.tag = lambda *tags: (lambda f: f)
    fake.between = lambda a, b: (a, b)

    saved = sys.modules.get("locust")
    sys.modules["locust"] = fake
    try:
        namespace = {}
        exec(compile(content, "generated_locustfile.py", "exec"), namespace)
    finally:
        if saved is not None:
            sys.modules["locust"] = saved
        else:
            sys.modules.pop("locust", None)
    return namespace


def _make_user(namespace, payload=None):
    user = namespace["GeneratedUser"]()
    user.client = StubClient(payload)
    return user


def _task_methods(user):
    return [name for name in dir(user) if name.startswith("task_")]


# ── _slugify ──────────────────────────────────────────────────────────


class TestSlugify:
    @pytest.mark.parametrize("input_val,expected", [
        ("Get Users", "get_users"),
        ("POST /api/v2", "post_api_v2"),
        ("  spaces  ", "spaces"),
        ("special!@#chars", "special_chars"),
        ("", "task"),
    ])
    def test_slugify(self, input_val, expected):
        assert _slugify(input_val) == expected


# ── _safe_int / _safe_float ───────────────────────────────────────────


class TestSafeConversions:
    def test_safe_int_valid(self):
        assert _safe_int("10", 0) == 10

    def test_safe_int_invalid(self):
        assert _safe_int("abc", 5) == 5

    def test_safe_int_none(self):
        assert _safe_int(None, 1) == 1

    def test_safe_float_valid(self):
        assert _safe_float("3.14", 0.0) == pytest.approx(3.14)

    def test_safe_float_invalid(self):
        assert _safe_float("bad", 1.0) == 1.0


# ── ScenarioGenerator.load_requests ──────────────────────────────────


class TestLoadRequests:
    def _gen(self, scenario, target=None):
        gen = ScenarioGenerator(scenario, target or {})
        gen.load_requests()
        return gen.requests

    def test_basic_load(self):
        scenario = {"requests": [{"method": "GET", "path": "/health"}]}
        assert len(self._gen(scenario)) == 1

    def test_include_tags(self):
        scenario = {
            "requests": [
                {"method": "GET", "path": "/a", "tags": ["api"]},
                {"method": "GET", "path": "/b", "tags": ["smoke"]},
            ]
        }
        result = self._gen(scenario, {"tags": ["api"]})
        assert len(result) == 1
        assert result[0]["path"] == "/a"

    def test_exclude_tags(self):
        scenario = {
            "requests": [
                {"method": "GET", "path": "/a", "tags": ["slow"]},
                {"method": "GET", "path": "/b", "tags": ["api"]},
            ]
        }
        result = self._gen(scenario, {"exclude_tags": ["slow"]})
        assert len(result) == 1
        assert result[0]["path"] == "/b"

    def test_requests_not_list(self):
        scenario = {"requests": "bad"}
        assert self._gen(scenario) == []


# ── ScenarioGenerator.generate ────────────────────────────────────────


class TestGenerate:
    def test_generates_file(self, tmp_path):
        scenario = {
            "requests": [{"method": "GET", "path": "/health", "name": "Health"}]
        }
        gen = ScenarioGenerator(scenario, {})
        path = gen.generate(tmp_path)
        assert path.exists()
        content = path.read_text()
        assert "class GeneratedUser" in content
        assert "@task" in content
        assert "/health" in content

    def test_empty_requests_raises(self, tmp_path):
        scenario = {"requests": []}
        gen = ScenarioGenerator(scenario, {})
        with pytest.raises(ValueError, match="non-empty"):
            gen.generate(tmp_path)

    def test_bearer_auth(self, tmp_path):
        scenario = {
            "auth": {"type": "bearer", "token": "test-token"},
            "requests": [{"method": "GET", "path": "/api"}],
        }
        content = _generate(tmp_path, scenario)
        assert "Bearer" in content
        assert "test-token" in content

    def test_api_key_auth(self, tmp_path):
        scenario = {
            "auth": {"type": "api_key", "header": "X-API-Key", "key": "my-key"},
            "requests": [{"method": "GET", "path": "/api"}],
        }
        content = _generate(tmp_path, scenario)
        assert "X-API-Key" in content

    def test_think_time_dict(self, tmp_path):
        scenario = {
            "think_time": {"min": 1.0, "max": 3.0},
            "requests": [{"method": "GET", "path": "/api"}],
        }
        content = _generate(tmp_path, scenario)
        assert "between(1.0, 3.0)" in content

    def test_task_weight(self, tmp_path):
        scenario = {
            "requests": [{"method": "GET", "path": "/api", "weight": 5}],
        }
        content = _generate(tmp_path, scenario)
        assert "@task(5)" in content

    def test_tag_decorator(self, tmp_path):
        scenario = {
            "requests": [{"method": "GET", "path": "/api", "tags": ["smoke"]}],
        }
        content = _generate(tmp_path, scenario)
        assert "@tag('smoke')" in content


# ── validation: fail fast instead of silently skipping ────────────────


class TestValidation:
    def test_request_missing_path_raises(self, tmp_path):
        scenario = {"requests": [{"method": "GET", "name": "No Path"}]}
        gen = ScenarioGenerator(scenario, {})
        with pytest.raises(ValueError, match=r"requests\[1\].*No Path.*'path'"):
            gen.generate(tmp_path)

    def test_request_not_dict_raises(self, tmp_path):
        scenario = {"requests": [{"method": "GET", "path": "/ok"}, "oops"]}
        gen = ScenarioGenerator(scenario, {})
        with pytest.raises(ValueError, match=r"requests\[2\] must be an object"):
            gen.generate(tmp_path)

    def test_on_start_missing_path_raises(self, tmp_path):
        scenario = {
            "on_start": [{"method": "POST", "name": "Login"}],
            "requests": [{"method": "GET", "path": "/ok"}],
        }
        gen = ScenarioGenerator(scenario, {})
        with pytest.raises(ValueError, match=r"on_start\[1\].*Login"):
            gen.generate(tmp_path)


# ── capture → variable injection (runtime behaviour) ──────────────────


class TestCaptureInjection:
    SCENARIO = {
        "headers": {"Authorization": "Bearer ${auth_token}"},
        "on_start": [
            {
                "name": "Login",
                "method": "POST",
                "path": "/auth/login",
                "json": {"username": "u", "password": "p"},
                "capture": {"auth_token": "data.token"},
            }
        ],
        "requests": [{"name": "Get API", "method": "GET", "path": "/api"}],
    }

    def test_capture_stores_variable(self, tmp_path):
        ns = _exec_generated(tmp_path, self.SCENARIO)
        user = _make_user(ns, {"data": {"token": "tok123"}})
        user.on_start()
        assert user._vars["auth_token"] == "tok123"

    def test_captured_variable_injected_into_headers(self, tmp_path):
        ns = _exec_generated(tmp_path, self.SCENARIO)
        user = _make_user(ns, {"data": {"token": "tok123"}})
        user.on_start()
        user.task_1_get_api()
        headers = user.client.calls[-1]["headers"]
        assert headers["Authorization"] == "Bearer tok123"

    def test_var_namespace(self, tmp_path):
        scenario = {
            "headers": {"Authorization": "Bearer ${var:auth_token}"},
            "on_start": [
                {
                    "method": "POST",
                    "path": "/login",
                    "capture": {"auth_token": "token"},
                }
            ],
            "requests": [{"name": "Ping", "method": "GET", "path": "/ping"}],
        }
        ns = _exec_generated(tmp_path, scenario)
        user = _make_user(ns, {"token": "abc"})
        user.on_start()
        user.task_1_ping()
        assert user.client.calls[-1]["headers"]["Authorization"] == "Bearer abc"

    def test_capture_missing_key_stores_none(self, tmp_path):
        ns = _exec_generated(tmp_path, self.SCENARIO)
        user = _make_user(ns, {"unexpected": "shape"})
        user.on_start()
        assert user._vars["auth_token"] is None
        user.task_1_get_api()
        # Unresolvable placeholder degrades to empty string, not a crash
        assert user.client.calls[-1]["headers"]["Authorization"] == "Bearer "


# ── basic auth: base64-encoded at runtime ─────────────────────────────


class TestBasicAuth:
    def test_header_is_base64(self, tmp_path):
        scenario = {
            "auth": {"type": "basic", "username": "admin", "password": "secret"},
            "requests": [{"name": "Ping", "method": "GET", "path": "/ping"}],
        }
        ns = _exec_generated(tmp_path, scenario)
        user = _make_user(ns)
        user.on_start()
        expected = "Basic " + base64.b64encode(b"admin:secret").decode("ascii")
        assert user._base_headers["Authorization"] == expected

    def test_no_plaintext_basic_header_in_file(self, tmp_path):
        scenario = {
            "auth": {"type": "basic", "username": "admin", "password": "secret"},
            "requests": [{"name": "Ping", "method": "GET", "path": "/ping"}],
        }
        content = _generate(tmp_path, scenario)
        assert "Basic admin:secret" not in content

    def test_credentials_from_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("API_USER", "env-user")
        monkeypatch.setenv("API_PASSWORD", "env-pass")
        scenario = {
            "auth": {"type": "basic"},
            "requests": [{"name": "Ping", "method": "GET", "path": "/ping"}],
        }
        ns = _exec_generated(tmp_path, scenario)
        user = _make_user(ns)
        user.on_start()
        expected = "Basic " + base64.b64encode(b"env-user:env-pass").decode("ascii")
        assert user._base_headers["Authorization"] == expected

    def test_header_sent_with_requests(self, tmp_path):
        scenario = {
            "auth": {"type": "basic", "username": "u", "password": "p"},
            "requests": [{"name": "Ping", "method": "GET", "path": "/ping"}],
        }
        ns = _exec_generated(tmp_path, scenario)
        user = _make_user(ns)
        user.on_start()
        user.task_1_ping()
        assert user.client.calls[-1]["headers"]["Authorization"].startswith("Basic ")


# ── path parameters ───────────────────────────────────────────────────


class TestPathParams:
    def test_path_placeholder_resolved(self, tmp_path):
        scenario = {
            "on_start": [
                {"method": "GET", "path": "/whoami", "capture": {"uid": "id"}}
            ],
            "requests": [
                {"name": "Get User", "method": "GET", "path": "/users/${var:uid}"}
            ],
        }
        ns = _exec_generated(tmp_path, scenario)
        user = _make_user(ns, {"id": 42})
        user.on_start()
        user.task_1_get_user()
        call = user.client.calls[-1]
        assert call["url"] == "/users/42"
        # Stats name stays the template so calls group per endpoint
        assert call["name"] == "Get User"

    def test_default_name_keeps_template(self, tmp_path):
        scenario = {
            "requests": [{"method": "GET", "path": "/users/${var:uid}"}],
        }
        ns = _exec_generated(tmp_path, scenario)
        user = _make_user(ns)
        user._vars = {"uid": 7}
        methods = _task_methods(user)
        getattr(user, methods[0])()
        call = user.client.calls[-1]
        assert call["url"] == "/users/7"
        assert call["name"] == "GET /users/${var:uid}"

    def test_env_placeholder_in_path(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PATH_ID", "99")
        scenario = {
            "requests": [
                {"name": "One", "method": "GET", "path": "/items/${env:PATH_ID}"}
            ],
        }
        ns = _exec_generated(tmp_path, scenario)
        user = _make_user(ns)
        user.task_1_one()
        assert user.client.calls[-1]["url"] == "/items/99"

    def test_static_path_unchanged(self, tmp_path):
        scenario = {"requests": [{"name": "H", "method": "GET", "path": "/health"}]}
        ns = _exec_generated(tmp_path, scenario)
        user = _make_user(ns)
        user.task_1_h()
        assert user.client.calls[-1]["url"] == "/health"


# ── runtime resolver details ──────────────────────────────────────────


class TestRuntimeResolver:
    def _user(self, tmp_path):
        scenario = {"requests": [{"name": "P", "method": "GET", "path": "/p"}]}
        ns = _exec_generated(tmp_path, scenario)
        return _make_user(ns)

    def test_env_default(self, tmp_path, monkeypatch):
        monkeypatch.delenv("NOPE_VAR", raising=False)
        user = self._user(tmp_path)
        assert user._resolve("${env:NOPE_VAR:-fallback}") == "fallback"
        assert user._resolve("${NOPE_VAR:-fallback}") == "fallback"

    def test_captured_wins_over_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("token", "from-env")
        user = self._user(tmp_path)
        user._vars = {"token": "from-capture"}
        assert user._resolve("${token}") == "from-capture"

    def test_builtin_generators(self, tmp_path):
        user = self._user(tmp_path)
        assert user._resolve("${timestamp}").isdigit()
        assert len(user._resolve("${random}")) == 8
        first = int(user._resolve("${iteration}"))
        second = int(user._resolve("${iteration}"))
        assert second == first + 1

    def test_non_string_passthrough(self, tmp_path):
        user = self._user(tmp_path)
        assert user._resolve(42) == 42
        assert user._resolve_dict({"n": 1}) == {"n": 1}


# ── generated code robustness ─────────────────────────────────────────


class TestGeneratedCodeRobustness:
    def test_quote_in_tag_compiles(self, tmp_path):
        scenario = {
            "requests": [
                {"name": "T", "method": "GET", "path": "/t", "tags": ["o'brien"]}
            ],
        }
        ns = _exec_generated(tmp_path, scenario)
        assert "GeneratedUser" in ns

    def test_quotes_and_newlines_in_values_compile(self, tmp_path):
        scenario = {
            "headers": {"X-Note": "it's \"quoted\"\nnewline"},
            "requests": [
                {
                    "name": "Tricky 'name'",
                    "method": "POST",
                    "path": "/echo",
                    "json": {"text": "line1\nline2 'quoted'"},
                }
            ],
        }
        ns = _exec_generated(tmp_path, scenario)
        user = _make_user(ns)
        methods = _task_methods(user)
        getattr(user, methods[0])()
        assert user.client.calls[-1]["json"]["text"] == "line1\nline2 'quoted'"

    def test_on_start_query_used(self, tmp_path):
        scenario = {
            "on_start": [
                {"method": "GET", "path": "/init", "query": {"warm": "1"}}
            ],
            "requests": [{"name": "P", "method": "GET", "path": "/p"}],
        }
        ns = _exec_generated(tmp_path, scenario)
        user = _make_user(ns)
        user.on_start()
        assert user.client.calls[0]["params"] == {"warm": "1"}
