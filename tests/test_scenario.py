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

    fake = types.ModuleType("locust")
    fake.HttpUser = type("HttpUser", (), {})
    fake.SequentialTaskSet = FakeSequentialTaskSet
    fake.task = fake_task
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

    fake = _t.ModuleType("locust")
    fake.HttpUser = type("HttpUser", (), {})
    fake.SequentialTaskSet = FakeSequentialTaskSet
    fake.task = fake_task
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
