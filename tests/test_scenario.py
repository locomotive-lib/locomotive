import pytest

from locomotive.scenario import ScenarioGenerator, _slugify, _safe_int, _safe_float


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
        gen = ScenarioGenerator(scenario, {})
        path = gen.generate(tmp_path)
        content = path.read_text()
        assert "Bearer" in content
        assert "test-token" in content

    def test_api_key_auth(self, tmp_path):
        scenario = {
            "auth": {"type": "api_key", "header": "X-API-Key", "key": "my-key"},
            "requests": [{"method": "GET", "path": "/api"}],
        }
        gen = ScenarioGenerator(scenario, {})
        path = gen.generate(tmp_path)
        content = path.read_text()
        assert "X-API-Key" in content

    def test_think_time_dict(self, tmp_path):
        scenario = {
            "think_time": {"min": 1.0, "max": 3.0},
            "requests": [{"method": "GET", "path": "/api"}],
        }
        gen = ScenarioGenerator(scenario, {})
        path = gen.generate(tmp_path)
        content = path.read_text()
        assert "between(1.0, 3.0)" in content

    def test_task_weight(self, tmp_path):
        scenario = {
            "requests": [{"method": "GET", "path": "/api", "weight": 5}],
        }
        gen = ScenarioGenerator(scenario, {})
        path = gen.generate(tmp_path)
        content = path.read_text()
        assert "@task(5)" in content

    def test_tag_decorator(self, tmp_path):
        scenario = {
            "requests": [{"method": "GET", "path": "/api", "tags": ["smoke"]}],
        }
        gen = ScenarioGenerator(scenario, {})
        path = gen.generate(tmp_path)
        content = path.read_text()
        assert "@tag('smoke')" in content
