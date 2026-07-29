import base64
import json
import sys
import types

import pytest

from locomotive.scenario import (
    ScenarioGenerator,
    _literal,
    _slugify,
    _safe_int,
    _safe_float,
)


# ── helpers: execute generated code with a stubbed locust ─────────────


class _Elapsed:
    def __init__(self, ms):
        self._ms = ms

    def total_seconds(self):
        return self._ms / 1000.0


class StubResponse:
    """A canned response that also supports Locust's catch_response protocol.

    Usable both directly (``resp = client.request(...)``) and as a context
    manager (``with client.request(..., catch_response=True) as resp:``).
    """

    def __init__(self, payload, status_code=200, text=None, elapsed_ms=0.0):
        self._payload = payload
        self.status_code = status_code
        if text is None:
            try:
                text = json.dumps(payload)
            except (TypeError, ValueError):
                text = str(payload)
        self.text = text
        self.elapsed = _Elapsed(elapsed_ms)
        self.succeeded = False
        self.failed = False
        self.failure_msg = None

    def json(self):
        return self._payload

    def success(self):
        self.succeeded = True

    def failure(self, msg):
        self.failed = True
        self.failure_msg = msg

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class StubClient:
    """Records request() calls; returns a canned JSON payload."""

    def __init__(self, payload=None, status_code=200, text=None, elapsed_ms=0.0):
        self.calls = []
        self.responses = []
        self._payload = payload if payload is not None else {}
        self._status_code = status_code
        self._text = text
        self._elapsed_ms = elapsed_ms

    def request(self, method, url, **kwargs):
        call = {"method": method, "url": url}
        call.update(kwargs)
        self.calls.append(call)
        resp = StubResponse(self._payload, self._status_code, self._text, self._elapsed_ms)
        self.responses.append(resp)
        return resp


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

    def fake_task(arg=1):
        if callable(arg):  # bare @task
            return arg
        return lambda f: f  # @task(N)

    class FakeSequentialTaskSet:
        def __init__(self, parent=None):
            self.parent = parent
            self.user = getattr(parent, "user", parent)
            self.client = getattr(self.user, "client", None)
            self.interrupted = False

        def interrupt(self, reschedule=True):
            self.interrupted = True

    class FakeEventHook:
        """Locust's EventHook, as far as the generated file uses it."""

        def __init__(self):
            self.listeners = []

        def add_listener(self, func):
            self.listeners.append(func)
            return func

        def fire(self, **kwargs):
            for listener in self.listeners:
                listener(**kwargs)

    fake = types.ModuleType("locust")
    fake.HttpUser = type("HttpUser", (), {})
    fake.SequentialTaskSet = FakeSequentialTaskSet
    fake.task = fake_task
    fake.tag = lambda *tags: (lambda f: f)
    fake.between = lambda a, b: (a, b)
    fake.events = types.SimpleNamespace(init=FakeEventHook())

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
    # The fake module is uninstalled again above, but the generated file's
    # init listener is registered on *this* hook and tests still need to fire
    # it. Hand the module back with the namespace rather than leaving tests to
    # reach for a `sys.modules` entry that no longer exists.
    namespace["_fake_locust"] = fake
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

    def test_env_placeholder_in_auth_resolves_at_request_time(
        self, tmp_path, monkeypatch
    ):
        # The config loader now leaves ${env:} in place; the locust process
        # reads it, so the token never lands in the generated file.
        monkeypatch.setenv("API_TOKEN", "s3cret")
        scenario = {
            "auth": {"type": "bearer", "token": "${env:API_TOKEN}"},
            "requests": [{"name": "P", "method": "GET", "path": "/api"}],
        }
        ns = _exec_generated(tmp_path, scenario)
        user = _make_user(ns)
        user.on_start()
        user.task_1_p()
        assert user.client.calls[-1]["headers"]["Authorization"] == "Bearer s3cret"

    def test_env_placeholder_in_body_resolves_at_request_time(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.delenv("MISSING_KEY", raising=False)
        monkeypatch.setenv("TENANT", "acme")
        scenario = {
            "requests": [{
                "name": "P", "method": "POST", "path": "/p/${env:TENANT}",
                "json": {"k": "${env:MISSING_KEY:-fallback}"},
            }],
        }
        ns = _exec_generated(tmp_path, scenario)
        user = _make_user(ns)
        user.on_start()
        user.task_1_p()
        call = user.client.calls[-1]
        assert call["url"] == "/p/acme"
        assert call["json"]["k"] == "fallback"

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


# ── D2: rich dynamic functions ────────────────────────────────────────


class TestDynamicFunctions:
    def _user(self, tmp_path):
        scenario = {"requests": [{"name": "P", "method": "GET", "path": "/p"}]}
        ns = _exec_generated(tmp_path, scenario)
        return _make_user(ns)

    def test_uuid(self, tmp_path):
        user = self._user(tmp_path)
        value = user._resolve("${uuid}")
        assert len(value) == 36
        assert value.count("-") == 4
        # Two calls produce different values
        assert user._resolve("${uuid}") != value

    def test_random_with_length(self, tmp_path):
        user = self._user(tmp_path)
        assert len(user._resolve("${random:16}")) == 16
        assert len(user._resolve("${random}")) == 8

    def test_random_bad_length_falls_back(self, tmp_path):
        user = self._user(tmp_path)
        assert len(user._resolve("${random:abc}")) == 8

    def test_randint_in_range(self, tmp_path):
        user = self._user(tmp_path)
        for _ in range(20):
            value = int(user._resolve("${randint:1:6}"))
            assert 1 <= value <= 6

    def test_randint_negative_bounds(self, tmp_path):
        user = self._user(tmp_path)
        value = int(user._resolve("${randint:-5:-1}"))
        assert -5 <= value <= -1

    def test_randint_swapped_bounds(self, tmp_path):
        user = self._user(tmp_path)
        value = int(user._resolve("${randint:10:1}"))
        assert 1 <= value <= 10

    def test_randint_bad_args_empty(self, tmp_path):
        user = self._user(tmp_path)
        assert user._resolve("${randint:a:b}") == ""
        assert user._resolve("${randint:1}") == ""

    def test_choice(self, tmp_path):
        user = self._user(tmp_path)
        for _ in range(10):
            assert user._resolve("${choice:red,green,blue}") in {"red", "green", "blue"}

    def test_choice_empty_list(self, tmp_path):
        user = self._user(tmp_path)
        assert user._resolve("${choice:}") == ""

    def test_now_default_iso(self, tmp_path):
        user = self._user(tmp_path)
        value = user._resolve("${now}")
        assert len(value) == 19 and value[4] == "-" and value[10] == "T"

    def test_now_custom_format_with_colons(self, tmp_path):
        user = self._user(tmp_path)
        value = user._resolve("${now:%H:%M}")
        assert len(value) == 5 and value[2] == ":"

    def test_now_year_only(self, tmp_path):
        user = self._user(tmp_path)
        assert user._resolve("${now:%Y}").isdigit()

    def test_functions_inside_json_body(self, tmp_path):
        scenario = {
            "requests": [
                {
                    "name": "Create",
                    "method": "POST",
                    "path": "/items",
                    "json": {"id": "${uuid}", "qty": "${randint:1:3}"},
                }
            ],
        }
        ns = _exec_generated(tmp_path, scenario)
        user = _make_user(ns)
        user.task_1_create()
        body = user.client.calls[-1]["json"]
        assert len(body["id"]) == 36
        assert int(body["qty"]) in {1, 2, 3}

    def test_function_names_reserved_over_env(self, tmp_path, monkeypatch):
        # Function names win over env vars of the same name
        monkeypatch.setenv("uuid", "not-a-uuid")
        user = self._user(tmp_path)
        assert user._resolve("${uuid}") != "not-a-uuid"


# ── B: multi-step flows ───────────────────────────────────────────────


def _make_flow(namespace, class_name, user):
    """Instantiate a generated flow bound to a user."""
    flow = namespace[class_name](user)
    flow.user = user
    flow.client = user.client
    return flow


CHECKOUT_SCENARIO = {
    "flows": [
        {
            "name": "Checkout",
            "weight": 3,
            "steps": [
                {"name": "Browse", "method": "GET", "path": "/catalog"},
                {
                    "name": "Create order",
                    "method": "POST",
                    "path": "/orders",
                    "json": {"product": "x"},
                    "capture": {"order_id": "id"},
                },
                {
                    "name": "Pay",
                    "method": "POST",
                    "path": "/orders/${var:order_id}/pay",
                },
            ],
        }
    ],
    "requests": [{"name": "Health", "method": "GET", "path": "/health", "weight": 2}],
}


class TestFlowGeneration:
    def test_flow_class_generated(self, tmp_path):
        content = _generate(tmp_path, CHECKOUT_SCENARIO)
        assert "class Flow_1_checkout(SequentialTaskSet):" in content
        assert "def step_1_browse(self):" in content
        assert "def step_2_create_order(self):" in content
        assert "def step_3_pay(self):" in content
        assert "def _flow_complete(self):" in content
        assert "self.interrupt(reschedule=False)" in content

    def test_steps_in_declaration_order(self, tmp_path):
        content = _generate(tmp_path, CHECKOUT_SCENARIO)
        assert (
            content.index("step_1_browse")
            < content.index("step_2_create_order")
            < content.index("step_3_pay")
            < content.index("_flow_complete")
        )

    def test_tasks_dict_with_weight(self, tmp_path):
        content = _generate(tmp_path, CHECKOUT_SCENARIO)
        assert "tasks = {Flow_1_checkout: 3}" in content

    def test_flat_requests_coexist(self, tmp_path):
        content = _generate(tmp_path, CHECKOUT_SCENARIO)
        assert "@task(2)" in content
        assert "def task_1_health(self):" in content

    def test_multiple_flows(self, tmp_path):
        scenario = {
            "flows": [
                {"name": "A", "weight": 1, "steps": [{"method": "GET", "path": "/a"}]},
                {"name": "B", "weight": 4, "steps": [{"method": "GET", "path": "/b"}]},
            ]
        }
        content = _generate(tmp_path, scenario)
        assert "tasks = {Flow_1_a: 1, Flow_2_b: 4}" in content

    def test_flow_think_time(self, tmp_path):
        scenario = {
            "flows": [
                {
                    "name": "Slow",
                    "think_time": {"min": 2.0, "max": 5.0},
                    "steps": [{"method": "GET", "path": "/s"}],
                }
            ]
        }
        content = _generate(tmp_path, scenario)
        # wait_time on the flow class overrides the user's
        flow_part = content[content.index("class Flow_1_slow") :]
        assert "wait_time = between(2.0, 5.0)" in flow_part

    def test_flow_tags_on_class_and_steps(self, tmp_path):
        scenario = {
            "flows": [
                {
                    "name": "Tagged",
                    "tags": ["journey"],
                    "steps": [
                        {"method": "GET", "path": "/x", "tags": ["smoke"]}
                    ],
                }
            ]
        }
        content = _generate(tmp_path, scenario)
        flow_part = content[content.index("@tag('journey')") :]
        assert flow_part.splitlines()[1].startswith("class Flow_1_tagged")
        assert "@tag('smoke')" in flow_part


class TestFlowRuntime:
    def test_variable_chaining_between_steps(self, tmp_path):
        ns = _exec_generated(tmp_path, CHECKOUT_SCENARIO)
        user = _make_user(ns, {"id": 555})
        user.on_start()
        flow = _make_flow(ns, "Flow_1_checkout", user)

        flow.step_1_browse()
        flow.step_2_create_order()
        assert user._vars["order_id"] == 555
        flow.step_3_pay()

        calls = user.client.calls
        assert calls[0]["url"] == "/catalog"
        assert calls[1]["url"] == "/orders"
        assert calls[2]["url"] == "/orders/555/pay"
        assert calls[2]["name"] == "Pay"

    def test_flow_complete_interrupts(self, tmp_path):
        ns = _exec_generated(tmp_path, CHECKOUT_SCENARIO)
        user = _make_user(ns)
        flow = _make_flow(ns, "Flow_1_checkout", user)
        flow._flow_complete()
        assert flow.interrupted is True

    def test_base_headers_reach_flow_steps(self, tmp_path):
        scenario = {
            "headers": {"X-Common": "yes"},
            "flows": [
                {"name": "F", "steps": [{"method": "GET", "path": "/a"}]}
            ],
        }
        ns = _exec_generated(tmp_path, scenario)
        user = _make_user(ns)
        user.on_start()
        flow = _make_flow(ns, "Flow_1_f", user)
        flow.step_1_step_1()
        assert user.client.calls[-1]["headers"]["X-Common"] == "yes"


class TestFlowFiltering:
    def test_exclude_flow_by_tag(self, tmp_path):
        scenario = {
            "flows": [
                {"name": "Keep", "steps": [{"method": "GET", "path": "/k"}]},
                {"name": "Drop", "tags": ["slow"],
                 "steps": [{"method": "GET", "path": "/d"}]},
            ]
        }
        content = _generate(tmp_path, scenario, {"exclude_tags": ["slow"]})
        assert "Flow_1_keep" in content
        assert "drop" not in content.lower()

    def test_include_only_tagged_flows(self, tmp_path):
        scenario = {
            "flows": [
                {"name": "Api", "tags": ["api"],
                 "steps": [{"method": "GET", "path": "/a"}]},
                {"name": "Other", "steps": [{"method": "GET", "path": "/o"}]},
            ]
        }
        content = _generate(tmp_path, scenario, {"tags": ["api"]})
        assert "Flow_1_api" in content
        assert "Flow_2_other" not in content


class TestFlowValidation:
    def test_flow_without_steps_raises(self, tmp_path):
        scenario = {"flows": [{"name": "Empty"}]}
        gen = ScenarioGenerator(scenario, {})
        with pytest.raises(ValueError, match=r"flows\[1\].*Empty.*'steps'"):
            gen.generate(tmp_path)

    def test_step_missing_path_raises(self, tmp_path):
        scenario = {
            "flows": [{"name": "F", "steps": [{"method": "POST", "name": "Bad"}]}]
        }
        gen = ScenarioGenerator(scenario, {})
        with pytest.raises(ValueError, match=r"flows\[1\]\.steps\[1\].*Bad"):
            gen.generate(tmp_path)

    def test_flows_only_config_valid(self, tmp_path):
        scenario = {
            "flows": [{"name": "F", "steps": [{"method": "GET", "path": "/x"}]}]
        }
        ns = _exec_generated(tmp_path, scenario)
        assert "GeneratedUser" in ns

    def test_no_requests_no_flows_raises(self, tmp_path):
        gen = ScenarioGenerator({}, {})
        with pytest.raises(ValueError, match="non-empty"):
            gen.generate(tmp_path)


# ── on_stop (teardown) ────────────────────────────────────────────────


class TestOnStop:
    def test_on_stop_generated_and_called(self, tmp_path):
        scenario = {
            "on_stop": [{"name": "Logout", "method": "POST", "path": "/logout"}],
            "requests": [{"name": "P", "method": "GET", "path": "/p"}],
        }
        ns = _exec_generated(tmp_path, scenario)
        user = _make_user(ns)
        user.on_start()
        user.on_stop()
        assert user.client.calls[-1]["url"] == "/logout"
        assert user.client.calls[-1]["method"] == "POST"

    def test_no_on_stop_by_default(self, tmp_path):
        scenario = {"requests": [{"name": "P", "method": "GET", "path": "/p"}]}
        content = _generate(tmp_path, scenario)
        assert "def on_stop" not in content


# ── capture in flat requests ──────────────────────────────────────────


class TestCaptureInFlatRequests:
    def test_capture_from_task(self, tmp_path):
        scenario = {
            "requests": [
                {"name": "List", "method": "GET", "path": "/items",
                 "capture": {"first": "items.0"}},
                {"name": "Use", "method": "GET", "path": "/items/${var:first}"},
            ],
        }
        ns = _exec_generated(tmp_path, scenario)
        user = _make_user(ns, {"items": {"0": "abc"}})
        user.on_start()
        user.task_1_list()
        assert user._vars["first"] == "abc"
        user.task_2_use()
        assert user.client.calls[-1]["url"] == "/items/abc"


class TestCaptureFailsTheSample:
    """A capture that finds nothing is a failed request.

    It used to store None silently, so the run stayed green and the next step
    hit `/orders/` instead of `/orders/42` — a 404 several lines away from the
    actual cause.
    """

    SCENARIO = {
        "requests": [
            {"name": "List", "method": "GET", "path": "/items",
             "capture": {"first": "items.0.id"}},
        ],
    }

    def _run(self, tmp_path, payload, status_code=200, text=None):
        ns = _exec_generated(tmp_path, self.SCENARIO)
        user = ns["GeneratedUser"]()
        user.client = StubClient(payload, status_code=status_code, text=text)
        user.on_start()
        user.task_1_list()
        return user, user.client.responses[-1]

    def test_present_value_succeeds(self, tmp_path):
        user, resp = self._run(tmp_path, {"items": [{"id": 42}]})
        assert user._vars["first"] == 42
        assert resp.succeeded and not resp.failed

    def test_missing_path_fails_the_sample(self, tmp_path):
        user, resp = self._run(tmp_path, {"items": []})
        assert user._vars["first"] is None
        assert resp.failed
        assert "first" in resp.failure_msg

    def test_non_json_body_fails_the_sample(self, tmp_path):
        ns = _exec_generated(tmp_path, self.SCENARIO)
        user = ns["GeneratedUser"]()

        class BrokenClient(StubClient):
            def request(self, method, url, **kwargs):
                resp = super().request(method, url, **kwargs)
                resp.json = lambda: (_ for _ in ()).throw(ValueError("not json"))
                return resp

        user.client = BrokenClient({})
        user.on_start()
        user.task_1_list()
        resp = user.client.responses[-1]
        assert resp.failed
        assert "not JSON" in resp.failure_msg

    def test_captured_null_is_not_a_failure(self, tmp_path):
        # A field that exists and is null is a real captured value.
        user, resp = self._run(tmp_path, {"items": [{"id": None}]})
        assert user._vars["first"] is None
        assert resp.succeeded

    def test_error_status_still_fails_without_expect(self, tmp_path):
        # catch_response suppresses locust's own check; the implicit one runs.
        user, resp = self._run(tmp_path, {"items": [{"id": 1}]}, status_code=500)
        assert resp.failed
        assert "500" in resp.failure_msg

    def test_status_failure_hides_capture_noise(self, tmp_path):
        user, resp = self._run(tmp_path, {}, status_code=500)
        assert "500" in resp.failure_msg
        assert "capture" not in resp.failure_msg

    def test_plain_request_is_not_wrapped(self, tmp_path):
        # No capture, no expect: locust judges the sample, no catch_response.
        # (The mixin mentions catch_response in a comment, so match the call.)
        content = _generate(
            tmp_path, {"requests": [{"name": "P", "method": "GET", "path": "/p"}]}
        )
        assert "catch_response=True" not in content
        assert "self.client.request(" in content


class TestCaptureJsonPaths:
    """B: capture paths reach into arrays and decode the body once."""

    def _capture(self, tmp_path, path, payload):
        scenario = {
            "requests": [
                {"name": "L", "method": "GET", "path": "/l", "capture": {"v": path}},
            ],
        }
        ns = _exec_generated(tmp_path, scenario)
        user = _make_user(ns, payload)
        user.on_start()
        user.task_1_l()
        return user._vars["v"]

    @pytest.mark.parametrize("path", ["items.0.id", "items[0].id"])
    def test_array_index_both_notations(self, tmp_path, path):
        assert self._capture(tmp_path, path, {"items": [{"id": "a"}]}) == "a"

    def test_negative_index(self, tmp_path):
        assert self._capture(tmp_path, "items.-1", {"items": ["a", "b"]}) == "b"

    def test_out_of_range_index_is_missing(self, tmp_path):
        assert self._capture(tmp_path, "items.5", {"items": ["a"]}) is None

    def test_root_array(self, tmp_path):
        assert self._capture(tmp_path, "[1].id", [{"id": "x"}, {"id": "y"}]) == "y"

    def test_nested_mix(self, tmp_path):
        payload = {"data": {"orders": [{"lines": [{"sku": "S1"}]}]}}
        assert self._capture(tmp_path, "data.orders[0].lines[0].sku", payload) == "S1"

    def test_body_is_decoded_once_per_request(self, tmp_path):
        scenario = {
            "requests": [
                {"name": "L", "method": "GET", "path": "/l",
                 "capture": {"a": "x", "b": "y", "c": "z"}},
            ],
        }
        ns = _exec_generated(tmp_path, scenario)
        user = _make_user(ns)
        calls = {"n": 0}

        class CountingClient(StubClient):
            def request(self, method, url, **kwargs):
                resp = super().request(method, url, **kwargs)
                payload = {"x": 1, "y": 2, "z": 3}

                def counted():
                    calls["n"] += 1
                    return payload

                resp.json = counted
                return resp

        user.client = CountingClient({})
        user.on_start()
        user.task_1_l()
        assert user._vars == {"a": 1, "b": 2, "c": 3}
        assert calls["n"] == 1


class TestTagNormalisation:
    """A string in 'tags' is one tag, not a bag of letters."""

    SCENARIO = {"requests": [
        {"name": "Buy", "method": "POST", "path": "/buy", "tags": "purchase"},
        {"name": "Read", "method": "GET", "path": "/read", "tags": ["browse"]},
    ]}

    def test_string_tag_is_selected_not_dropped(self, tmp_path):
        content = _generate(tmp_path, self.SCENARIO, {"tags": ["purchase"]})
        assert "/buy" in content
        assert "/read" not in content

    def test_string_target_tag_works(self, tmp_path):
        content = _generate(tmp_path, self.SCENARIO, {"tags": "purchase"})
        assert "/buy" in content and "/read" not in content

    def test_comma_separated_target_tags(self, tmp_path):
        content = _generate(tmp_path, self.SCENARIO, {"tags": "purchase,browse"})
        assert "/buy" in content and "/read" in content

    def test_string_tag_can_be_excluded(self, tmp_path):
        content = _generate(tmp_path, self.SCENARIO, {"exclude_tags": ["purchase"]})
        assert "/buy" not in content
        assert "/read" in content

    def test_letters_of_a_tag_match_nothing(self, tmp_path):
        # The old set("purchase") behaviour would have matched 'p' here; now
        # nothing matches, and an empty selection is a loud error.
        with pytest.raises(ValueError, match="non-empty"):
            _generate(tmp_path, self.SCENARIO, {"tags": ["p"]})


class TestPlaceholdersInDictKeys:
    """Keys carry placeholders as often as values do."""

    def _call(self, tmp_path, req, payload=None):
        ns = _exec_generated(tmp_path, {"requests": [req]})
        user = _make_user(ns, payload)
        user.on_start()
        user.task_1_r()
        return user.client.calls[0]

    def test_query_key_is_resolved(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LOCO_PARAM", "page")
        call = self._call(tmp_path, {
            "name": "R", "method": "GET", "path": "/r",
            "query": {"${env:LOCO_PARAM}": "1"},
        })
        assert call["params"] == {"page": "1"}

    def test_body_key_is_resolved(self, tmp_path):
        call = self._call(tmp_path, {
            "name": "R", "method": "POST", "path": "/r",
            "json": {"${var:field}": "v"},
        })
        # nothing captured 'field' yet, so it resolves to an empty key —
        # the point is that the placeholder text is not sent verbatim.
        assert "${var:field}" not in call["json"]

    def test_header_key_is_resolved(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LOCO_HEADER", "X-Tenant")
        call = self._call(tmp_path, {
            "name": "R", "method": "GET", "path": "/r",
            "headers": {"${env:LOCO_HEADER}": "acme"},
        })
        assert call["headers"]["X-Tenant"] == "acme"

    def test_plain_keys_are_untouched(self, tmp_path):
        call = self._call(tmp_path, {
            "name": "R", "method": "GET", "path": "/r", "query": {"page": "1"},
        })
        assert call["params"] == {"page": "1"}


class TestRequestBodyAndTimeout:
    """'json' wins over 'data'; a stringy timeout does not reach requests."""

    def _call(self, tmp_path, req):
        ns = _exec_generated(tmp_path, {"requests": [req]})
        user = _make_user(ns)
        user.on_start()
        user.task_1_r()
        return user.client.calls[0]

    def test_json_wins_over_data(self, tmp_path):
        call = self._call(tmp_path, {
            "name": "R", "method": "POST", "path": "/r",
            "json": {"a": 1}, "data": {"b": 2},
        })
        assert call["json"] == {"a": 1}
        assert "data" not in call

    def test_data_alone_still_works(self, tmp_path):
        call = self._call(tmp_path, {
            "name": "R", "method": "POST", "path": "/r", "data": {"b": 2},
        })
        assert call["data"] == {"b": 2}

    def test_string_timeout_becomes_a_number(self, tmp_path):
        call = self._call(tmp_path, {
            "name": "R", "method": "GET", "path": "/r", "timeout": "5",
        })
        assert call["timeout"] == 5.0

    def test_unparseable_timeout_is_dropped(self, tmp_path):
        call = self._call(tmp_path, {
            "name": "R", "method": "GET", "path": "/r", "timeout": "soon",
        })
        assert "timeout" not in call


class TestThinkTimeBounds:
    """between(min, max) must receive ordered, non-negative bounds."""

    def test_swapped_bounds_are_ordered(self, tmp_path):
        content = _generate(tmp_path, {
            "think_time": {"min": 2.0, "max": 0.5},
            "requests": [{"name": "R", "method": "GET", "path": "/r"}],
        })
        assert "between(0.5, 2.0)" in content

    def test_negative_bounds_are_clamped(self, tmp_path):
        content = _generate(tmp_path, {
            "think_time": {"min": -1, "max": 3},
            "requests": [{"name": "R", "method": "GET", "path": "/r"}],
        })
        assert "between(0.0, 3.0)" in content

    def test_ordered_bounds_are_untouched(self, tmp_path):
        content = _generate(tmp_path, {
            "think_time": {"min": 0.5, "max": 2.0},
            "requests": [{"name": "R", "method": "GET", "path": "/r"}],
        })
        assert "between(0.5, 2.0)" in content


# ── expect: response assertions ───────────────────────────────────────


class TestResponseAssertions:
    def _run(self, tmp_path, expect, payload=None, status_code=200,
             text=None, elapsed_ms=0.0):
        scenario = {"requests": [
            {"name": "Check", "method": "GET", "path": "/check", "expect": expect},
        ]}
        ns = _exec_generated(tmp_path, scenario)
        user = ns["GeneratedUser"]()
        user.client = StubClient(payload if payload is not None else {},
                                 status_code=status_code, text=text,
                                 elapsed_ms=elapsed_ms)
        user.on_start()
        user.task_1_check()
        return user.client.responses[-1]

    def test_generated_uses_catch_response(self, tmp_path):
        scenario = {"requests": [
            {"name": "Check", "method": "GET", "path": "/check",
             "expect": {"status": 200}},
        ]}
        content = _generate(tmp_path, scenario)
        assert "catch_response=True" in content
        assert "_assert_response" in content

    def test_no_expect_no_catch_response(self, tmp_path):
        scenario = {"requests": [{"name": "Plain", "method": "GET", "path": "/p"}]}
        content = _generate(tmp_path, scenario)
        assert "catch_response=True" not in content

    def test_status_ok(self, tmp_path):
        resp = self._run(tmp_path, {"status": 200}, status_code=200)
        assert resp.succeeded and not resp.failed

    def test_status_mismatch_fails(self, tmp_path):
        resp = self._run(tmp_path, {"status": 200}, status_code=500)
        assert resp.failed and "status 500" in resp.failure_msg

    def test_status_list(self, tmp_path):
        resp = self._run(tmp_path, {"status": [200, 201, 204]}, status_code=201)
        assert resp.succeeded

    def test_contains_ok(self, tmp_path):
        resp = self._run(tmp_path, {"contains": "hello"}, text="well hello there")
        assert resp.succeeded

    def test_contains_missing_fails(self, tmp_path):
        resp = self._run(tmp_path, {"contains": "hello"}, text="goodbye")
        assert resp.failed and "missing" in resp.failure_msg

    def test_contains_list_all_required(self, tmp_path):
        resp = self._run(tmp_path, {"contains": ["a", "z"]}, text="abc")
        assert resp.failed and "'z'" in resp.failure_msg

    def test_json_path_match(self, tmp_path):
        resp = self._run(tmp_path, {"json": {"data.status": "ok"}},
                         payload={"data": {"status": "ok"}})
        assert resp.succeeded

    def test_json_path_mismatch_fails(self, tmp_path):
        resp = self._run(tmp_path, {"json": {"data.status": "ok"}},
                         payload={"data": {"status": "error"}})
        assert resp.failed and "data.status" in resp.failure_msg

    def test_json_loose_str_compare(self, tmp_path):
        # int 9 in the body matches expected "9" via str() comparison
        resp = self._run(tmp_path, {"json": {"id": "9"}}, payload={"id": 9})
        assert resp.succeeded

    def test_json_missing_path_fails(self, tmp_path):
        resp = self._run(tmp_path, {"json": {"data.status": "ok"}}, payload={})
        assert resp.failed and "no value at that path" in resp.failure_msg

    def test_json_expected_null_needs_the_path_to_exist(self, tmp_path):
        # A missing path used to read as None and satisfy `expected: null`.
        resp = self._run(tmp_path, {"json": {"error": None}}, payload={"ok": 1})
        assert resp.failed and "no value at that path" in resp.failure_msg

    def test_json_actual_null_matches_expected_null(self, tmp_path):
        resp = self._run(tmp_path, {"json": {"error": None}}, payload={"error": None})
        assert resp.succeeded

    def test_json_array_index(self, tmp_path):
        resp = self._run(tmp_path, {"json": {"items[0].id": "a"}},
                         payload={"items": [{"id": "a"}]})
        assert resp.succeeded

    def test_missing_key_is_not_reported_as_a_decode_failure(self, tmp_path):
        resp = self._run(tmp_path, {"json": {"x": 1}}, payload={})
        assert resp.failed
        assert "not valid JSON" not in resp.failure_msg

    def test_max_ms_ok(self, tmp_path):
        resp = self._run(tmp_path, {"max_ms": 500}, elapsed_ms=120)
        assert resp.succeeded

    def test_max_ms_exceeded_fails(self, tmp_path):
        resp = self._run(tmp_path, {"max_ms": 100}, elapsed_ms=250)
        assert resp.failed and "250ms" in resp.failure_msg

    def test_multiple_failures_joined(self, tmp_path):
        resp = self._run(tmp_path, {"status": 200, "contains": "ok"},
                         status_code=500, text="fail")
        assert resp.failed and ";" in resp.failure_msg

    def test_placeholder_in_expect_resolved(self, tmp_path):
        # captured var is available to expect via _resolve_dict
        scenario = {
            "on_start": [{"method": "POST", "path": "/login",
                          "capture": {"want": "id"}}],
            "requests": [{"name": "Check", "method": "GET", "path": "/check",
                          "expect": {"contains": "${var:want}"}}],
        }
        ns = _exec_generated(tmp_path, scenario)
        user = ns["GeneratedUser"]()
        user.client = StubClient({"id": "tok"}, text="body has tok inside")
        user.on_start()
        user.task_1_check()
        assert user.client.responses[-1].succeeded

    def test_expect_with_capture_both_run(self, tmp_path):
        scenario = {"requests": [
            {"name": "Check", "method": "GET", "path": "/check",
             "capture": {"cid": "id"}, "expect": {"status": 200}},
        ]}
        ns = _exec_generated(tmp_path, scenario)
        user = ns["GeneratedUser"]()
        user.client = StubClient({"id": "xyz"}, status_code=200)
        user.on_start()
        user.task_1_check()
        assert user._vars["cid"] == "xyz"
        assert user.client.responses[-1].succeeded

    # B1: 'expect' must never turn an HTTP or transport error into a pass.
    def test_expect_without_status_still_fails_on_500(self, tmp_path):
        resp = self._run(tmp_path, {"max_ms": 5000}, status_code=500, elapsed_ms=10)
        assert resp.failed and "status 500" in resp.failure_msg

    def test_expect_without_status_still_fails_on_404(self, tmp_path):
        resp = self._run(tmp_path, {"contains": "x"}, text="x", status_code=404)
        assert resp.failed and "status 404" in resp.failure_msg

    def test_expect_without_status_fails_on_no_response(self, tmp_path):
        # status_code 0 is what Locust reports for a connection error.
        resp = self._run(tmp_path, {"max_ms": 5000}, status_code=0)
        assert resp.failed and "no response" in resp.failure_msg

    def test_expect_without_status_passes_on_2xx(self, tmp_path):
        resp = self._run(tmp_path, {"max_ms": 5000}, status_code=204, elapsed_ms=10)
        assert resp.succeeded

    def test_expect_without_status_passes_on_3xx(self, tmp_path):
        resp = self._run(tmp_path, {"max_ms": 5000}, status_code=302, elapsed_ms=10)
        assert resp.succeeded

    def test_explicit_status_still_wins(self, tmp_path):
        # Asking for 404 on purpose (negative test) must keep passing.
        resp = self._run(tmp_path, {"status": 404}, status_code=404)
        assert resp.succeeded

    def test_expect_in_flow_step(self, tmp_path):
        scenario = {"flows": [{"name": "F", "steps": [
            {"name": "S1", "method": "GET", "path": "/s1",
             "expect": {"status": 200}},
        ]}]}
        content = _generate(tmp_path, scenario)
        assert "catch_response=True" in content
        assert "self.user._assert_response" in content


# ── D1: data pools ────────────────────────────────────────────────────


class TestDataPools:
    def _inline_scenario(self, mode, rows=None):
        return {
            "data": {
                "accounts": {
                    "inline": rows or [
                        {"login": "u1", "password": "p1"},
                        {"login": "u2", "password": "p2"},
                        {"login": "u3", "password": "p3"},
                    ],
                    "mode": mode,
                }
            },
            "requests": [
                {"name": "Login", "method": "POST", "path": "/login",
                 "json": {"user": "${data:accounts.login}"}}
            ],
        }

    def test_unique_per_user_distinct_rows(self, tmp_path):
        ns = _exec_generated(tmp_path, self._inline_scenario("unique_per_user"))
        users = []
        for _ in range(3):
            user = _make_user(ns)
            user.on_start()
            users.append(user)
        logins = {u._data_rows["accounts"]["login"] for u in users}
        assert logins == {"u1", "u2", "u3"}

    def test_unique_per_user_wraps_when_exhausted(self, tmp_path):
        ns = _exec_generated(tmp_path, self._inline_scenario("unique_per_user"))
        users = []
        for _ in range(4):
            user = _make_user(ns)
            user.on_start()
            users.append(user)
        # 4th user wraps around to the first row
        assert users[3]._data_rows["accounts"]["login"] == "u1"

    def test_once_same_row_for_everyone(self, tmp_path):
        ns = _exec_generated(tmp_path, self._inline_scenario("once"))
        logins = set()
        for _ in range(3):
            user = _make_user(ns)
            user.on_start()
            user.task_1_login()
            logins.add(user.client.calls[-1]["json"]["user"])
        assert logins == {"u1"}

    def test_round_robin_cycles_per_access(self, tmp_path):
        ns = _exec_generated(tmp_path, self._inline_scenario("round_robin"))
        user = _make_user(ns)
        user.on_start()
        seen = []
        for _ in range(4):
            user.task_1_login()
            seen.append(user.client.calls[-1]["json"]["user"])
        assert seen == ["u1", "u2", "u3", "u1"]

    def test_random_membership(self, tmp_path):
        ns = _exec_generated(tmp_path, self._inline_scenario("random"))
        user = _make_user(ns)
        user.on_start()
        for _ in range(10):
            user.task_1_login()
            assert user.client.calls[-1]["json"]["user"] in {"u1", "u2", "u3"}

    def test_value_reaches_request_body(self, tmp_path):
        ns = _exec_generated(tmp_path, self._inline_scenario("unique_per_user"))
        user = _make_user(ns)
        user.on_start()
        user.task_1_login()
        assert user.client.calls[-1]["json"]["user"] == "u1"

    def test_csv_source(self, tmp_path):
        csv_path = tmp_path / "accounts.csv"
        csv_path.write_text("login,password\ncsv1,x\ncsv2,y\n")
        scenario = {
            "data": {"accounts": {"source": str(csv_path), "mode": "unique_per_user"}},
            "requests": [
                {"name": "L", "method": "POST", "path": "/login",
                 "json": {"user": "${data:accounts.login}"}}
            ],
        }
        ns = _exec_generated(tmp_path, scenario)
        user = _make_user(ns)
        user.on_start()
        user.task_1_l()
        assert user.client.calls[-1]["json"]["user"] == "csv1"

    def test_json_source_with_nested_field(self, tmp_path):
        import json as jsonlib
        json_path = tmp_path / "accounts.json"
        json_path.write_text(jsonlib.dumps(
            [{"login": "j1", "profile": {"city": "Moscow"}}]
        ))
        scenario = {
            "data": {"accounts": {"source": str(json_path), "mode": "once"}},
            "requests": [
                {"name": "L", "method": "GET",
                 "path": "/city/${data:accounts.profile.city}"}
            ],
        }
        ns = _exec_generated(tmp_path, scenario)
        user = _make_user(ns)
        user.on_start()
        user.task_1_l()
        assert user.client.calls[-1]["url"] == "/city/Moscow"

    def test_missing_pool_resolves_empty(self, tmp_path):
        scenario = {
            "requests": [
                {"name": "L", "method": "GET", "path": "/x/${data:nope.field}"}
            ],
        }
        ns = _exec_generated(tmp_path, scenario)
        user = _make_user(ns)
        user.on_start()
        user.task_1_l()
        assert user.client.calls[-1]["url"] == "/x/"

    def test_missing_field_resolves_empty(self, tmp_path):
        ns = _exec_generated(
            tmp_path,
            {
                "data": {"accounts": {"inline": [{"login": "u1"}], "mode": "once"}},
                "requests": [
                    {"name": "L", "method": "GET", "path": "/x/${data:accounts.nope}"}
                ],
            },
        )
        user = _make_user(ns)
        user.on_start()
        user.task_1_l()
        assert user.client.calls[-1]["url"] == "/x/"

    def test_missing_file_resolves_empty(self, tmp_path):
        scenario = {
            "data": {"accounts": {"source": str(tmp_path / "missing.csv")}},
            "requests": [
                {"name": "L", "method": "GET", "path": "/x/${data:accounts.login}"}
            ],
        }
        ns = _exec_generated(tmp_path, scenario)
        user = _make_user(ns)
        user.on_start()
        user.task_1_l()
        assert user.client.calls[-1]["url"] == "/x/"

    def test_data_in_flow_steps(self, tmp_path):
        scenario = {
            "data": {"accounts": {"inline": [{"login": "flowuser"}], "mode": "once"}},
            "flows": [
                {"name": "F", "steps": [
                    {"name": "S", "method": "POST", "path": "/login",
                     "json": {"user": "${data:accounts.login}"}}
                ]}
            ],
        }
        ns = _exec_generated(tmp_path, scenario)
        user = _make_user(ns)
        user.on_start()
        flow = _make_flow(ns, "Flow_1_f", user)
        flow.step_1_s()
        assert user.client.calls[-1]["json"]["user"] == "flowuser"


class TestLiteralSerialization:
    """B6: config values must become literals the generated file can evaluate.

    YAML turns `2024-01-01` into a datetime.date and `.inf` into a float whose
    repr is the bare name `inf`. Both used to be emitted through repr() and
    made locust fail at import time with NameError / no such module.
    """

    def _body_scenario(self, body):
        return {
            "requests": [
                {"name": "P", "method": "POST", "path": "/p", "json": body}
            ]
        }

    def test_scalars_round_trip(self):
        for value in ["x", "", "it's", 1, 0, True, False, None, 1.5, -0.25]:
            assert eval(_literal(value)) == value or value is None

    def test_infinity_and_nan_are_evaluable(self):
        import math

        assert eval(_literal(float("inf"))) == math.inf
        assert eval(_literal(float("-inf"))) == -math.inf
        assert math.isnan(eval(_literal(float("nan"))))

    def test_date_becomes_iso_string(self):
        import datetime

        assert _literal(datetime.date(2024, 1, 1)) == "'2024-01-01'"
        assert _literal(datetime.datetime(2024, 1, 1, 10, 30)) == "'2024-01-01T10:30:00'"

    def test_unknown_object_becomes_its_string_form(self):
        class Weird:
            def __str__(self):
                return "weird-value"

        assert _literal(Weird()) == "'weird-value'"

    def test_non_string_keys_become_strings(self):
        assert _literal({2024: "y"}) == "{'2024': 'y'}"

    def test_nesting_is_recursive(self):
        import datetime

        rendered = _literal({"a": [1, datetime.date(2024, 1, 1), {"b": float("inf")}]})
        assert eval(rendered) == {"a": [1, "2024-01-01", {"b": float("inf")}]}

    def test_sets_are_deterministic(self):
        assert _literal({"b", "a", "c"}) == "['a', 'b', 'c']"

    def test_yaml_date_body_generates_importable_file(self, tmp_path):
        import datetime

        # _generate compiles the output, which is where the old repr() broke.
        content = _generate(
            tmp_path, self._body_scenario({"from": datetime.date(2024, 1, 1)})
        )
        assert "'2024-01-01'" in content
        assert "datetime.date" not in content

    def test_yaml_infinity_body_generates_importable_file(self, tmp_path):
        ns = _exec_generated(tmp_path, self._body_scenario({"limit": float("inf")}))
        user = _make_user(ns)
        user.on_start()
        user.task_1_p()
        assert user.client.calls[-1]["json"]["limit"] == float("inf")

    def test_numeric_request_name_is_stringified(self, tmp_path):
        scenario = {"requests": [{"name": 2024, "method": "GET", "path": "/p"}]}
        ns = _exec_generated(tmp_path, scenario)
        user = _make_user(ns)
        user.on_start()
        user.task_1_2024()
        assert user.client.calls[-1]["name"] == "2024"

    def test_date_in_inline_pool_survives(self, tmp_path):
        import datetime

        scenario = {
            "data": {"d": {"inline": [{"day": datetime.date(2024, 1, 1)}],
                           "mode": "once"}},
            "requests": [{"name": "P", "method": "GET", "path": "/p/${data:d.day}"}],
        }
        ns = _exec_generated(tmp_path, scenario)
        user = _make_user(ns)
        user.on_start()
        user.task_1_p()
        assert user.client.calls[-1]["url"] == "/p/2024-01-01"


class TestRequestRowScope:
    """B2: every ${data:} in one request comes from the same pool row.

    Before this, `random` and `round_robin` re-picked a row per placeholder,
    so a login body could carry the username of one account and the password
    of another — every such request failed against a real service.
    """

    ROWS = [
        {"login": "u1", "password": "p1"},
        {"login": "u2", "password": "p2"},
        {"login": "u3", "password": "p3"},
    ]

    def _scenario(self, mode, **req_extra):
        req = {
            "name": "Login",
            "method": "POST",
            "path": "/login",
            "json": {
                "user": "${data:accounts.login}",
                "pass": "${data:accounts.password}",
            },
        }
        req.update(req_extra)
        return {
            "data": {"accounts": {"inline": self.ROWS, "mode": mode}},
            "requests": [req],
        }

    @pytest.mark.parametrize("mode", ["random", "round_robin"])
    def test_login_and_password_come_from_one_row(self, tmp_path, mode):
        ns = _exec_generated(tmp_path, self._scenario(mode))
        user = _make_user(ns)
        user.on_start()
        for _ in range(12):
            user.task_1_login()
            body = user.client.calls[-1]["json"]
            assert {"login": body["user"], "password": body["pass"]} in self.ROWS

    def test_round_robin_advances_once_per_request(self, tmp_path):
        # Two placeholders per request must consume one row, not two.
        ns = _exec_generated(tmp_path, self._scenario("round_robin"))
        user = _make_user(ns)
        user.on_start()
        seen = []
        for _ in range(4):
            user.task_1_login()
            seen.append(user.client.calls[-1]["json"]["user"])
        assert seen == ["u1", "u2", "u3", "u1"]

    def test_expect_sees_the_row_that_was_sent(self, tmp_path):
        scenario = self._scenario(
            "round_robin", expect={"contains": "${data:accounts.login}"}
        )
        ns = _exec_generated(tmp_path, scenario)
        user = _make_user(ns, payload={"echo": "u1"})
        user.on_start()
        user.task_1_login()
        sent = user.client.calls[-1]["json"]["user"]
        resp = user.client.responses[-1]
        # The body echoes u1; the assertion must be checked against the same
        # row the request used, so it passes only for the first row.
        assert (sent == "u1") == resp.succeeded

    def test_scope_is_reset_between_flow_steps(self, tmp_path):
        scenario = {
            "data": {"accounts": {"inline": self.ROWS, "mode": "round_robin"}},
            "flows": [
                {"name": "F", "steps": [
                    {"name": "A", "method": "POST", "path": "/a",
                     "json": {"user": "${data:accounts.login}",
                              "pass": "${data:accounts.password}"}},
                    {"name": "B", "method": "POST", "path": "/b",
                     "json": {"user": "${data:accounts.login}"}},
                ]}
            ],
        }
        ns = _exec_generated(tmp_path, scenario)
        user = _make_user(ns)
        user.on_start()
        flow = _make_flow(ns, "Flow_1_f", user)
        flow.step_1_a()
        flow.step_2_b()
        assert user.client.calls[0]["json"] == {"user": "u1", "pass": "p1"}
        assert user.client.calls[1]["json"] == {"user": "u2"}

    def test_pinned_modes_generate_no_scope_reset(self, tmp_path):
        # unique_per_user / once pin one row per user, so the generated file
        # stays exactly as it was before this change.
        for mode in ("unique_per_user", "once"):
            content = _generate(tmp_path, self._scenario(mode))
            assert "_begin_request()" not in content

    def test_pinned_row_survives_across_requests(self, tmp_path):
        ns = _exec_generated(tmp_path, self._scenario("unique_per_user"))
        user = _make_user(ns)
        user.on_start()
        user.task_1_login()
        user.task_1_login()
        assert user.client.calls[0]["json"] == user.client.calls[1]["json"]


class TestDataPoolValidation:
    def _gen(self, data):
        return ScenarioGenerator(
            {"data": data, "requests": [{"name": "P", "method": "GET", "path": "/p"}]},
            {},
        )

    def test_bad_mode_raises(self, tmp_path):
        gen = self._gen({"accounts": {"inline": [{"a": 1}], "mode": "weird"}})
        with pytest.raises(ValueError, match="mode must be one of"):
            gen.generate(tmp_path)

    def test_no_source_no_inline_raises(self, tmp_path):
        gen = self._gen({"accounts": {"mode": "random"}})
        with pytest.raises(ValueError, match="must define 'source'.*'generate'"):
            gen.generate(tmp_path)

    def test_bad_pool_name_raises(self, tmp_path):
        gen = self._gen({"bad.name": {"inline": [{"a": 1}]}})
        with pytest.raises(ValueError, match="pool name"):
            gen.generate(tmp_path)

    def test_inline_not_list_of_objects_raises(self, tmp_path):
        gen = self._gen({"accounts": {"inline": ["not-a-dict"]}})
        with pytest.raises(ValueError, match="list of objects"):
            gen.generate(tmp_path)

    def test_data_not_dict_raises(self, tmp_path):
        gen = self._gen("bad")
        with pytest.raises(ValueError, match="scenario.data must be an object"):
            gen.generate(tmp_path)


# ── personas (multiple user types) ────────────────────────────────────


from locomotive.scenario import generate_locustfile


def _generate_users(tmp_path, users, target=None):
    path = generate_locustfile({}, target or {}, tmp_path, users=users)
    content = path.read_text()
    compile(content, str(path), "exec")
    return content


def _exec_users(tmp_path, users, target=None):
    content = _generate_users(tmp_path, users, target)
    import types as _t
    # reuse the fake-locust exec machinery
    scenario_stub = {"requests": [{"name": "x", "method": "GET", "path": "/x"}]}
    ns_helper = _exec_generated  # for symmetry; we inline exec below

    def fake_task(arg=1):
        if callable(arg):
            return arg
        return lambda f: f

    class FakeSequentialTaskSet:
        def __init__(self, parent=None):
            self.parent = parent
            self.user = getattr(parent, "user", parent)
            self.client = getattr(self.user, "client", None)
            self.interrupted = False

        def interrupt(self, reschedule=True):
            self.interrupted = True

    class FakeEventHook:
        def __init__(self):
            self.listeners = []

        def add_listener(self, func):
            self.listeners.append(func)
            return func

        def fire(self, **kwargs):
            for listener in self.listeners:
                listener(**kwargs)

    fake = _t.ModuleType("locust")
    fake.HttpUser = type("HttpUser", (), {})
    fake.SequentialTaskSet = FakeSequentialTaskSet
    fake.task = fake_task
    fake.tag = lambda *tags: (lambda f: f)
    fake.between = lambda a, b: (a, b)
    fake.events = _t.SimpleNamespace(init=FakeEventHook())

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
    namespace["_fake_locust"] = fake
    return namespace


READER_BUYER = [
    {
        "weight": 4,
        "name": "reader",
        "scenario": {
            "think_time": 0.5,
            "headers": {"X-Persona": "reader"},
            "requests": [{"name": "Read", "method": "GET", "path": "/articles"}],
        },
    },
    {
        "weight": 1,
        "name": "buyer",
        "scenario": {
            "headers": {"X-Persona": "buyer"},
            "flows": [
                {"name": "Buy", "steps": [
                    {"name": "Order", "method": "POST", "path": "/orders",
                     "capture": {"oid": "id"}},
                    {"name": "Pay", "method": "POST", "path": "/orders/${var:oid}/pay"},
                ]}
            ],
        },
    },
]


class TestPersonas:
    def test_two_user_classes_with_weights(self, tmp_path):
        content = _generate_users(tmp_path, READER_BUYER)
        assert "class User_1_reader(_RuntimeMixin, HttpUser):" in content
        assert "class User_2_buyer(_RuntimeMixin, HttpUser):" in content
        reader_part = content[content.index("class User_1_reader"):content.index("class User_2_buyer")]
        assert "weight = 4" in reader_part
        buyer_part = content[content.index("class User_2_buyer"):]
        assert "weight = 1" in buyer_part

    def test_flow_prefix_no_collision(self, tmp_path):
        content = _generate_users(tmp_path, READER_BUYER)
        assert "class Flow2_1_buy(SequentialTaskSet):" in content
        buyer_part = content[content.index("class User_2_buyer"):]
        assert "tasks = {Flow2_1_buy: 1}" in buyer_part

    def test_personas_have_isolated_settings(self, tmp_path):
        ns = _exec_users(tmp_path, READER_BUYER)
        reader = ns["User_1_reader"]()
        buyer = ns["User_2_buyer"]()
        assert reader._base_headers == {"X-Persona": "reader"}
        assert buyer._base_headers == {"X-Persona": "buyer"}
        assert reader.wait_time == (0.5, 0.5)

    def test_personas_share_runtime_mixin(self, tmp_path):
        ns = _exec_users(tmp_path, READER_BUYER)
        # single mixin class, both users resolve placeholders
        reader = ns["User_1_reader"]()
        reader.client = StubClient()
        reader._vars = {"k": "v"}
        assert reader._resolve("${var:k}") == "v"
        assert ns["User_1_reader"].__mro__[1] is ns["_RuntimeMixin"]
        assert ns["User_2_buyer"].__mro__[1] is ns["_RuntimeMixin"]

    def test_buyer_flow_chaining_works(self, tmp_path):
        ns = _exec_users(tmp_path, READER_BUYER)
        buyer = ns["User_2_buyer"]()
        buyer.client = StubClient({"id": 9})
        buyer.on_start()
        flow = ns["Flow2_1_buy"](buyer)
        flow.user = buyer
        flow.client = buyer.client
        flow.step_1_order()
        flow.step_2_pay()
        assert buyer.client.calls[-1]["url"] == "/orders/9/pay"

    def test_flat_entry_form_without_scenario_key(self, tmp_path):
        # Entry carries scenario fields directly (the "include" shape)
        users = [
            {"weight": 2, "name": "simple",
             "requests": [{"name": "P", "method": "GET", "path": "/p"}]},
        ]
        content = _generate_users(tmp_path, users)
        assert "class User_1_simple(_RuntimeMixin, HttpUser):" in content
        assert "weight = 2" in content

    def test_single_scenario_backward_compat(self, tmp_path):
        # No users -> same GeneratedUser / Flow_1_x naming as before
        scenario = {
            "flows": [{"name": "F", "steps": [{"method": "GET", "path": "/x"}]}],
        }
        content = _generate(tmp_path, scenario)
        assert "class GeneratedUser(_RuntimeMixin, HttpUser):" in content
        assert "class Flow_1_f(SequentialTaskSet):" in content
        assert "weight =" not in content.split("class GeneratedUser")[1].split("wait_time")[0]

    def test_persona_validation_error_names_persona(self, tmp_path):
        users = [{"weight": 1, "name": "bad", "scenario": {}}]
        with pytest.raises(ValueError, match=r"users\[1\]"):
            generate_locustfile({}, {}, tmp_path, users=users)

    def test_persona_not_dict_raises(self, tmp_path):
        with pytest.raises(ValueError, match=r"users\[2\] must be an object"):
            generate_locustfile({}, {}, tmp_path, users=[
                {"weight": 1, "scenario": {"requests": [{"method": "GET", "path": "/x"}]}},
                "oops",
            ])


class TestPersonaDataPools:
    def test_shared_identical_pool_merged(self, tmp_path):
        pool = {"inline": [{"login": "u1"}], "mode": "once"}
        users = [
            {"weight": 1, "scenario": {
                "data": {"accounts": dict(pool)},
                "requests": [{"name": "A", "method": "POST", "path": "/a",
                              "json": {"u": "${data:accounts.login}"}}]}},
            {"weight": 1, "scenario": {
                "data": {"accounts": dict(pool)},
                "requests": [{"name": "B", "method": "POST", "path": "/b",
                              "json": {"u": "${data:accounts.login}"}}]}},
        ]
        ns = _exec_users(tmp_path, users)
        u1 = ns["User_1_user_1"]()
        u1.client = StubClient()
        u1.on_start()
        u1.task_1_a()
        assert u1.client.calls[-1]["json"]["u"] == "u1"

    def test_conflicting_pool_definitions_raise(self, tmp_path):
        users = [
            {"weight": 1, "scenario": {
                "data": {"accounts": {"inline": [{"a": "1"}], "mode": "once"}},
                "requests": [{"name": "A", "method": "GET", "path": "/a"}]}},
            {"weight": 1, "scenario": {
                "data": {"accounts": {"inline": [{"a": "2"}], "mode": "once"}},
                "requests": [{"name": "B", "method": "GET", "path": "/b"}]}},
        ]
        with pytest.raises(ValueError, match="defined differently"):
            generate_locustfile({}, {}, tmp_path, users=users)


# ── synthetic data: ${fake:...} generators ────────────────────────────


class TestFakeGenerators:
    def _user(self, tmp_path):
        scenario = {"requests": [{"name": "P", "method": "GET", "path": "/p"}]}
        ns = _exec_generated(tmp_path, scenario)
        return _make_user(ns)

    def test_name_has_two_parts(self, tmp_path):
        user = self._user(tmp_path)
        parts = user._resolve("${fake:name}").split(" ")
        assert len(parts) == 2 and all(p.isalpha() for p in parts)

    def test_first_and_last_name(self, tmp_path):
        user = self._user(tmp_path)
        assert user._resolve("${fake:first_name}").isalpha()
        assert user._resolve("${fake:last_name}").isalpha()

    def test_email_format(self, tmp_path):
        user = self._user(tmp_path)
        email = user._resolve("${fake:email}")
        assert email.count("@") == 1
        local, domain = email.split("@")
        assert local and "." in domain

    def test_username_nonempty(self, tmp_path):
        user = self._user(tmp_path)
        assert len(user._resolve("${fake:username}")) > 0

    def test_phone_pattern(self, tmp_path):
        import re as _re
        user = self._user(tmp_path)
        assert _re.fullmatch(r"\+1-\d{3}-\d{3}-\d{4}", user._resolve("${fake:phone}"))

    def test_digits_length(self, tmp_path):
        user = self._user(tmp_path)
        val = user._resolve("${fake:digits:5}")
        assert len(val) == 5 and val.isdigit()

    def test_digits_default(self, tmp_path):
        user = self._user(tmp_path)
        assert len(user._resolve("${fake:digits}")) == 6

    def test_words_count(self, tmp_path):
        user = self._user(tmp_path)
        assert len(user._resolve("${fake:words:4}").split(" ")) == 4

    def test_sentence_ends_with_period(self, tmp_path):
        user = self._user(tmp_path)
        s = user._resolve("${fake:sentence}")
        assert s.endswith(".") and s[0].isupper()

    def test_bool(self, tmp_path):
        user = self._user(tmp_path)
        assert user._resolve("${fake:bool}") in {"true", "false"}

    def test_city_country_address(self, tmp_path):
        user = self._user(tmp_path)
        assert user._resolve("${fake:city}").strip()
        assert user._resolve("${fake:country}").strip()
        assert user._resolve("${fake:address}")[0].isdigit()

    def test_unknown_kind_empty(self, tmp_path):
        user = self._user(tmp_path)
        assert user._resolve("${fake:nonsense}") == ""

    def test_variety_across_calls(self, tmp_path):
        user = self._user(tmp_path)
        emails = {user._resolve("${fake:email}") for _ in range(20)}
        assert len(emails) > 1  # not all identical

    def test_fake_reserved_over_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("fake:email", "should-not-be-read")
        user = self._user(tmp_path)
        assert "@" in user._resolve("${fake:email}")

    def test_fake_inside_request_body(self, tmp_path):
        scenario = {
            "requests": [
                {"name": "Reg", "method": "POST", "path": "/register",
                 "json": {"email": "${fake:email}", "name": "${fake:name}"}}
            ],
        }
        ns = _exec_generated(tmp_path, scenario)
        user = _make_user(ns)
        user.task_1_reg()
        body = user.client.calls[-1]["json"]
        assert "@" in body["email"] and " " in body["name"]


# ── synthetic data: generated data pools ──────────────────────────────


class TestGeneratedPools:
    def _scenario(self, count=50, mode="unique_per_user"):
        return {
            "data": {
                "people": {
                    "generate": {
                        "count": count,
                        "fields": {
                            "email": "${fake:email}",
                            "full_name": "${fake:name}",
                            "user_id": "${uuid}",
                        },
                    },
                    "mode": mode,
                }
            },
            "requests": [
                {"name": "Reg", "method": "POST", "path": "/register",
                 "json": {"email": "${data:people.email}", "name": "${data:people.full_name}"}}
            ],
        }

    def test_pool_generates_count_rows(self, tmp_path):
        ns = _exec_generated(tmp_path, self._scenario(count=25))
        rows = ns["_load_pool"]("people")
        assert len(rows) == 25
        assert all("@" in r["email"] and r["user_id"] for r in rows)

    def test_consistent_row_per_user(self, tmp_path):
        ns = _exec_generated(tmp_path, self._scenario())
        user = _make_user(ns)
        user.on_start()
        user.task_1_reg()
        first = user.client.calls[-1]["json"]
        user.task_1_reg()
        second = user.client.calls[-1]["json"]
        assert first == second  # same user keeps its synthetic identity

    def test_unique_per_user_distinct(self, tmp_path):
        ns = _exec_generated(tmp_path, self._scenario(count=50))
        emails = set()
        for _ in range(5):
            u = _make_user(ns)
            u.on_start()
            u.task_1_reg()
            emails.add(u.client.calls[-1]["json"]["email"])
        assert len(emails) == 5  # distinct rows (count >> users)

    def test_generated_pool_in_flow(self, tmp_path):
        scenario = {
            "data": {"acc": {"generate": {"count": 10, "fields": {"login": "${fake:username}"}}, "mode": "once"}},
            "flows": [
                {"name": "F", "steps": [
                    {"name": "S", "method": "POST", "path": "/login",
                     "json": {"u": "${data:acc.login}"}}
                ]}
            ],
        }
        ns = _exec_generated(tmp_path, scenario)
        user = _make_user(ns)
        user.on_start()
        flow = _make_flow(ns, "Flow_1_f", user)
        flow.step_1_s()
        assert user.client.calls[-1]["json"]["u"]


class TestGeneratedPoolValidation:
    def _gen(self, data):
        return ScenarioGenerator(
            {"data": data, "requests": [{"name": "P", "method": "GET", "path": "/p"}]},
            {},
        )

    def test_generate_without_fields_raises(self, tmp_path):
        gen = self._gen({"p": {"generate": {"count": 5}}})
        with pytest.raises(ValueError, match="generate.fields must be a non-empty object"):
            gen.generate(tmp_path)

    def test_generate_empty_fields_raises(self, tmp_path):
        gen = self._gen({"p": {"generate": {"fields": {}}}})
        with pytest.raises(ValueError, match="generate.fields"):
            gen.generate(tmp_path)

    def test_generate_bad_count_raises(self, tmp_path):
        gen = self._gen({"p": {"generate": {"fields": {"a": "${uuid}"}, "count": "lots"}}})
        with pytest.raises(ValueError, match="generate.count must be an integer"):
            gen.generate(tmp_path)

    def test_generate_zero_count_raises(self, tmp_path):
        gen = self._gen({"p": {"generate": {"fields": {"a": "${uuid}"}, "count": 0}}})
        with pytest.raises(ValueError, match="generate.count must be >= 1"):
            gen.generate(tmp_path)

    def test_generate_not_object_raises(self, tmp_path):
        gen = self._gen({"p": {"generate": "nope"}})
        with pytest.raises(ValueError, match="generate must be an object"):
            gen.generate(tmp_path)


class TestTypedJsonBodies:
    """A JSON body is the one place where "7" and 7 are different things."""

    def _call(self, tmp_path, req, scenario_extra=None, payload=None):
        scenario = {"requests": [req]}
        if scenario_extra:
            scenario.update(scenario_extra)
        ns = _exec_generated(tmp_path, scenario)
        user = _make_user(ns, payload)
        user.on_start()
        user.task_1_r()
        return user.client.calls[0]

    def _body(self, tmp_path, body, **kw):
        return self._call(tmp_path, {
            "name": "R", "method": "POST", "path": "/r", "json": body,
        }, **kw)["json"]

    def test_randint_is_an_int(self, tmp_path):
        body = self._body(tmp_path, {"quantity": "${randint:7:7}"})
        assert body["quantity"] == 7
        assert isinstance(body["quantity"], int)

    def test_iteration_is_an_int(self, tmp_path):
        body = self._body(tmp_path, {"n": "${iteration}"})
        assert isinstance(body["n"], int)

    def test_timestamp_is_an_int(self, tmp_path):
        body = self._body(tmp_path, {"ts": "${timestamp}"})
        assert isinstance(body["ts"], int)

    def test_fake_bool_is_a_bool(self, tmp_path):
        body = self._body(tmp_path, {"flag": "${fake:bool}"})
        assert body["flag"] in (True, False)
        assert isinstance(body["flag"], bool)

    def test_uuid_stays_a_string(self, tmp_path):
        body = self._body(tmp_path, {"id": "${uuid}"})
        assert isinstance(body["id"], str)

    def test_digits_from_fake_stay_a_string(self, tmp_path):
        # A code that happens to be all digits is still a code.
        body = self._body(tmp_path, {"code": "${random:6}"})
        assert isinstance(body["code"], str)

    def test_placeholder_mixed_with_text_stays_a_string(self, tmp_path):
        body = self._body(tmp_path, {"sku": "SKU-${randint:3:3}"})
        assert body["sku"] == "SKU-3"

    def test_captured_number_stays_a_number(self, tmp_path):
        scenario = {"requests": [
            {"name": "First", "method": "GET", "path": "/first",
             "capture": {"order_id": "id"}},
            {"name": "R", "method": "POST", "path": "/r",
             "json": {"order": "${var:order_id}"}},
        ]}
        ns = _exec_generated(tmp_path, scenario)
        user = _make_user(ns, {"id": 42})
        user.on_start()
        user.task_1_first()
        user.task_2_r()
        assert user.client.calls[1]["json"]["order"] == 42

    def test_uncaptured_var_is_an_empty_string_not_null(self, tmp_path):
        body = self._body(tmp_path, {"order": "${var:nope}"})
        assert body["order"] == ""

    def test_pool_value_keeps_its_loaded_type(self, tmp_path):
        call = self._call(
            tmp_path,
            {"name": "R", "method": "POST", "path": "/r",
             "json": {"qty": "${data:items.qty}"}},
            scenario_extra={"data": {"items": {
                "inline": [{"qty": 3}], "mode": "once"}}},
        )
        assert call["json"]["qty"] == 3

    def test_nested_structures_are_resolved(self, tmp_path):
        body = self._body(tmp_path, {
            "lines": [{"qty": "${randint:2:2}"}],
            "meta": {"n": "${randint:9:9}"},
        })
        assert body["lines"][0]["qty"] == 2
        assert body["meta"]["n"] == 9

    def test_non_string_literals_survive(self, tmp_path):
        body = self._body(tmp_path, {"a": 1, "b": None, "c": True, "d": 1.5})
        assert body == {"a": 1, "b": None, "c": True, "d": 1.5}

    def test_keys_are_still_resolved(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LOCO_FIELD", "quantity")
        body = self._body(tmp_path, {"${env:LOCO_FIELD}": "${randint:4:4}"})
        assert body == {"quantity": 4}

    def test_headers_still_stringify(self, tmp_path):
        call = self._call(tmp_path, {
            "name": "R", "method": "GET", "path": "/r",
            "headers": {"X-N": "${randint:5:5}"},
        })
        assert call["headers"]["X-N"] == "5"

    def test_query_still_stringifies(self, tmp_path):
        call = self._call(tmp_path, {
            "name": "R", "method": "GET", "path": "/r",
            "query": {"n": "${randint:5:5}"},
        })
        assert call["params"]["n"] == "5"

    def test_form_body_still_stringifies(self, tmp_path):
        call = self._call(tmp_path, {
            "name": "R", "method": "POST", "path": "/r",
            "data": {"n": "${randint:5:5}"},
        })
        assert call["data"]["n"] == "5"


# ── distributed runs: one slice of each pool per worker ────────────────


def _shard_namespace(tmp_path, scenario, worker_index, expect_workers, target=None):
    """Exec a generated locustfile as worker `worker_index` of `expect_workers`."""
    ns = _exec_generated(tmp_path, scenario, target)
    runner = types.SimpleNamespace(worker_index=worker_index)
    environment = types.SimpleNamespace(
        runner=runner,
        parsed_options=types.SimpleNamespace(expect_workers=expect_workers),
    )
    # The generated file registers its listener on the fake hook at import.
    ns["_fake_locust"].events.init.fire(environment=environment, runner=runner)
    return ns


def _accounts_csv(tmp_path, count):
    path = tmp_path / "accounts.csv"
    rows = "\n".join(f"user{i},pw{i}" for i in range(count))
    path.write_text(f"login,password\n{rows}\n", encoding="utf-8")
    return str(path)


def _pool_scenario(source, mode="unique_per_user"):
    return {
        "data": {"acc": {"source": source, "mode": mode}},
        "requests": [
            {"name": "Login", "method": "POST", "path": "/login",
             "json": {"login": "${data:acc.login}"}},
        ],
    }


class TestPoolSharding:
    def _rows(self, tmp_path, source, index, count, mode="unique_per_user"):
        ns = _shard_namespace(tmp_path, _pool_scenario(source, mode), index, count)
        return ns["_load_pool"]("acc")

    def test_one_process_sees_the_whole_pool(self, tmp_path):
        source = _accounts_csv(tmp_path, 10)
        rows = self._rows(tmp_path, source, 0, 1)
        assert [r["login"] for r in rows] == [f"user{i}" for i in range(10)]

    def test_workers_get_disjoint_slices(self, tmp_path):
        source = _accounts_csv(tmp_path, 12)
        slices = [
            [r["login"] for r in self._rows(tmp_path, source, i, 4)]
            for i in range(4)
        ]
        seen = [login for s in slices for login in s]
        assert len(seen) == len(set(seen)) == 12

    def test_slices_reassemble_into_the_whole_pool(self, tmp_path):
        source = _accounts_csv(tmp_path, 12)
        seen = set()
        for i in range(4):
            seen.update(r["login"] for r in self._rows(tmp_path, source, i, 4))
        assert seen == {f"user{i}" for i in range(12)}

    def test_slices_are_balanced_within_one_row(self, tmp_path):
        # A stride, not a contiguous block: 10 rows over 4 workers is
        # 3/3/2/2, never 3/3/3/1.
        source = _accounts_csv(tmp_path, 10)
        sizes = [len(self._rows(tmp_path, source, i, 4)) for i in range(4)]
        assert sorted(sizes) == [2, 2, 3, 3]

    def test_round_robin_is_sharded_too(self, tmp_path):
        source = _accounts_csv(tmp_path, 8)
        a = [r["login"] for r in self._rows(tmp_path, source, 0, 2, mode="round_robin")]
        b = [r["login"] for r in self._rows(tmp_path, source, 1, 2, mode="round_robin")]
        assert set(a).isdisjoint(b)
        assert len(a) + len(b) == 8

    def test_random_is_left_whole(self, tmp_path):
        # Splitting 'random' would only narrow what each worker can draw
        # from; it was never wrong to begin with.
        source = _accounts_csv(tmp_path, 8)
        assert len(self._rows(tmp_path, source, 1, 4, mode="random")) == 8

    def test_once_still_means_the_first_row(self, tmp_path):
        # 'once' has to see rows[0], or on worker 1 it silently means
        # "the second row" instead.
        source = _accounts_csv(tmp_path, 8)
        for index in range(4):
            rows = self._rows(tmp_path, source, index, 4, mode="once")
            assert rows[0]["login"] == "user0"

    def test_more_workers_than_rows_falls_back_to_the_whole_pool(self, tmp_path):
        # An empty shard means the worker generates no load at all, which is
        # a far worse failure than repeating rows — and it warns.
        source = _accounts_csv(tmp_path, 3)
        rows = self._rows(tmp_path, source, 7, 8)
        assert len(rows) == 3

    def test_inline_pools_are_sharded(self, tmp_path):
        scenario = {
            "data": {"acc": {"inline": [{"login": f"u{i}"} for i in range(6)],
                             "mode": "unique_per_user"}},
            "requests": [{"name": "L", "method": "POST", "path": "/l",
                          "json": {"login": "${data:acc.login}"}}],
        }
        ns = _shard_namespace(tmp_path, scenario, 1, 3)
        assert [r["login"] for r in ns["_load_pool"]("acc")] == ["u1", "u4"]

    def test_generated_pools_are_not_sharded(self, tmp_path):
        # Synthesised rows are drawn independently in every process, so there
        # is no shared sequence to divide — slicing would just discard rows.
        scenario = {
            "data": {"acc": {"generate": {"count": 12, "fields": {"u": "${fake:username}"}},
                             "mode": "unique_per_user"}},
            "requests": [{"name": "L", "method": "POST", "path": "/l",
                          "json": {"login": "${data:acc.u}"}}],
        }
        ns = _shard_namespace(tmp_path, scenario, 1, 4)
        assert len(ns["_load_pool"]("acc")) == 12

    def test_users_on_different_workers_do_not_share_a_row(self, tmp_path):
        # The whole point, stated end to end: eight users spread over two
        # workers get eight different logins.
        source = _accounts_csv(tmp_path, 8)
        logins = []
        for index in range(2):
            ns = _shard_namespace(tmp_path, _pool_scenario(source), index, 2)
            for _ in range(4):
                user = _make_user(ns)
                user.on_start()
                logins.append(user._data_rows["acc"]["login"])
        assert sorted(logins) == sorted(f"user{i}" for i in range(8))


class TestShardResolution:
    def _ns(self, tmp_path, **environment_kwargs):
        scenario = {"requests": [{"name": "H", "method": "GET", "path": "/h"}]}
        ns = _exec_generated(tmp_path, scenario)
        if environment_kwargs:
            ns["_fake_locust"].events.init.fire(**environment_kwargs)
        return ns

    def test_without_a_runner_it_answers_one_process(self, tmp_path):
        ns = self._ns(tmp_path)
        assert ns["_shard"]() == {"id": 0, "count": 1, "resolved": False}

    def test_an_unresolved_answer_is_not_cached(self, tmp_path):
        # Answering "one process" before locust has a runner must not stop
        # the real answer from being picked up.
        ns = self._ns(tmp_path)
        ns["_shard"]()
        runner = types.SimpleNamespace(worker_index=2)
        ns["_fake_locust"].events.init.fire(
            environment=types.SimpleNamespace(
                runner=runner,
                parsed_options=types.SimpleNamespace(expect_workers=5),
            ),
        )
        assert ns["_shard"]()["id"] == 2
        assert ns["_shard"]()["count"] == 5

    def test_an_old_master_declines_to_shard(self, tmp_path):
        # worker_index is -1 on masters <= 2.10.2. Guessing an index would
        # hand the same rows to several workers — the exact bug this exists
        # to prevent.
        scenario = {"requests": [{"name": "H", "method": "GET", "path": "/h"}]}
        ns = _exec_generated(tmp_path, scenario)
        runner = types.SimpleNamespace(worker_index=-1)
        ns["_fake_locust"].events.init.fire(
            environment=types.SimpleNamespace(
                runner=runner,
                parsed_options=types.SimpleNamespace(expect_workers=4),
            ),
        )
        assert ns["_shard"]() == {"id": 0, "count": 1, "resolved": True}

    def test_a_missing_worker_count_means_one_process(self, tmp_path):
        scenario = {"requests": [{"name": "H", "method": "GET", "path": "/h"}]}
        ns = _exec_generated(tmp_path, scenario)
        ns["_fake_locust"].events.init.fire(
            environment=types.SimpleNamespace(
                runner=types.SimpleNamespace(worker_index=0),
                parsed_options=None,
            ),
        )
        assert ns["_shard"]()["count"] == 1

    def test_an_index_beyond_the_count_wraps(self, tmp_path):
        scenario = {"requests": [{"name": "H", "method": "GET", "path": "/h"}]}
        ns = _exec_generated(tmp_path, scenario)
        ns["_fake_locust"].events.init.fire(
            environment=types.SimpleNamespace(
                runner=types.SimpleNamespace(worker_index=5),
                parsed_options=types.SimpleNamespace(expect_workers=4),
            ),
        )
        assert ns["_shard"]()["id"] == 1


class TestShardDataDisabled:
    """load.shard_data: false — every process keeps the whole pool."""

    def _rows(self, tmp_path, source, index, count, mode="unique_per_user"):
        ns = _shard_namespace(
            tmp_path, _pool_scenario(source, mode), index, count,
            target={"shard_data": False},
        )
        return ns["_load_pool"]("acc")

    def test_the_flag_reaches_the_generated_file(self, tmp_path):
        scenario = {"requests": [{"name": "H", "method": "GET", "path": "/h"}]}
        content = _generate(tmp_path, scenario, {"shard_data": False})
        assert "_SHARD_DATA = False" in content

    def test_it_defaults_to_on(self, tmp_path):
        scenario = {"requests": [{"name": "H", "method": "GET", "path": "/h"}]}
        assert "_SHARD_DATA = True" in _generate(tmp_path, scenario)

    def test_every_worker_keeps_the_whole_pool(self, tmp_path):
        source = _accounts_csv(tmp_path, 8)
        for index in range(4):
            rows = self._rows(tmp_path, source, index, 4)
            assert [r["login"] for r in rows] == [f"user{i}" for i in range(8)]

    def test_workers_then_collide_on_purpose(self, tmp_path):
        # The stated intent of the flag: two workers hand the same row to
        # different users. Sharding exists to prevent exactly this, which is
        # why it is the default.
        source = _accounts_csv(tmp_path, 4)
        logins = []
        for index in range(2):
            ns = _shard_namespace(
                tmp_path, _pool_scenario(source), index, 2,
                target={"shard_data": False},
            )
            user = _make_user(ns)
            user.on_start()
            logins.append(user._data_rows["acc"]["login"])
        assert logins[0] == logins[1]

    def test_iteration_is_still_seeded_per_worker(self, tmp_path):
        # shard_data only turns off pool slicing. The shard itself is still
        # resolved, so ${iteration} keeps producing ids that do not collide.
        scenario = {"requests": [{"name": "H", "method": "GET", "path": "/h"}]}
        ns = _shard_namespace(
            tmp_path, scenario, 1, 4, target={"shard_data": False},
        )
        assert ns["_iteration"]() == 1000000001


class TestIterationAcrossWorkers:
    def _iterations(self, tmp_path, index, count, n=3):
        scenario = {"requests": [{"name": "H", "method": "GET", "path": "/h"}]}
        ns = _shard_namespace(tmp_path, scenario, index, count)
        return [ns["_iteration"]() for _ in range(n)]

    def test_one_process_starts_at_one(self, tmp_path):
        assert self._iterations(tmp_path, 0, 1) == [1, 2, 3]

    def test_workers_do_not_overlap(self, tmp_path):
        # Four workers each emitting 1, 2, 3 turns ${iteration} into four
        # copies of the same order numbers.
        first = self._iterations(tmp_path, 0, 4)
        second = self._iterations(tmp_path, 1, 4)
        assert set(first).isdisjoint(second)
        assert second[0] == 1000000001

    def test_the_seed_is_taken_once(self, tmp_path):
        assert self._iterations(tmp_path, 2, 4, n=3) == [
            2000000001, 2000000002, 2000000003,
        ]
