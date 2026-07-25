import json

import pytest

from locomotive.validate import (
    ERROR,
    WARNING,
    Issue,
    format_issues,
    iter_requests,
    validate_config,
)


def _levels(issues):
    return [(i.level, i.message) for i in issues]


def _has(issues, level, needle):
    return any(i.level == level and needle in i.message for i in issues)


VALID = {
    "load": {"host": "http://x", "users": 10, "spawn_rate": 2, "run_time": "1m"},
    "scenario": {
        "auth": {"type": "bearer", "token": "${var:token}"},
        "on_start": [{"name": "Login", "method": "POST", "path": "/login", "capture": {"token": "token"}}],
        "data": {"acc": {"generate": {"count": 5, "fields": {"u": "${fake:username}"}}, "mode": "random"}},
        "flows": [{"name": "F", "steps": [
            {"name": "Create", "method": "POST", "path": "/orders", "capture": {"oid": "id"}},
            {"name": "Pay", "method": "POST", "path": "/orders/${var:oid}/pay",
             "json": {"login": "${data:acc.u}"}},
        ]}],
        "requests": [{"name": "Health", "method": "GET", "path": "/health"}],
    },
}


class TestValidConfig:
    def test_clean(self):
        assert validate_config(VALID) == []

    def test_format_valid(self):
        assert format_issues([]) == "✓ Config is valid."


class TestStructural:
    def test_not_dict(self):
        issues = validate_config([])  # not an object
        assert issues and issues[0].level == ERROR

    def test_missing_path(self):
        cfg = {"load": {"host": "x", "users": 1, "spawn_rate": 1, "run_time": "1m"},
               "scenario": {"requests": [{"name": "X", "method": "GET"}]}}
        assert _has(validate_config(cfg), ERROR, "missing required 'path'")

    def test_no_scenario_source(self):
        assert _has(validate_config({"load": {"host": "x"}}), ERROR, "no scenario source")

    def test_weight_not_int(self):
        cfg = {"scenario": {"requests": [{"name": "X", "method": "GET", "path": "/x", "weight": "lots"}]}}
        assert _has(validate_config(cfg), ERROR, "'weight' must be an integer")

    def test_load_users_not_int(self):
        cfg = {"load": {"host": "x", "users": "ten", "spawn_rate": 1, "run_time": "1m"},
               "scenario": {"requests": [{"method": "GET", "path": "/x"}]}}
        assert _has(validate_config(cfg), ERROR, "'users' must be an integer")

    def test_missing_load_fields_warn(self):
        cfg = {"load": {}, "scenario": {"requests": [{"method": "GET", "path": "/x"}]}}
        assert _has(validate_config(cfg), WARNING, "'host' missing")


class TestSemantic:
    def test_uncaptured_var_warns(self):
        cfg = {"scenario": {"requests": [{"method": "GET", "path": "/x/${var:missing}"}]}}
        assert _has(validate_config(cfg), WARNING, "nothing captures 'missing'")

    def test_captured_var_ok(self):
        cfg = {"scenario": {
            "on_start": [{"method": "POST", "path": "/login", "capture": {"tok": "t"}}],
            "requests": [{"method": "GET", "path": "/x", "headers": {"A": "Bearer ${var:tok}"}}],
        }}
        assert not _has(validate_config(cfg), WARNING, "nothing captures")

    def test_undeclared_data_pool_warns(self):
        cfg = {"scenario": {"requests": [{"method": "GET", "path": "/x", "json": {"u": "${data:nope.field}"}}]}}
        assert _has(validate_config(cfg), WARNING, "data pool 'nope'")

    def test_declared_data_pool_ok(self):
        cfg = {"scenario": {
            "data": {"acc": {"inline": [{"u": "a"}], "mode": "once"}},
            "requests": [{"method": "GET", "path": "/x", "json": {"u": "${data:acc.u}"}}],
        }}
        assert not _has(validate_config(cfg), WARNING, "data pool")

    def test_requires_auth_without_auth_warns(self):
        cfg = {"scenario": {"requests": [{"method": "GET", "path": "/x", "_requires_auth": True}]}}
        assert _has(validate_config(cfg), WARNING, "no 'auth' block")

    def test_dedup_repeated_var(self):
        cfg = {"scenario": {"requests": [
            {"method": "GET", "path": "/x/${var:m}", "headers": {"H": "${var:m}"}, "json": {"a": "${var:m}"}},
        ]}}
        warns = [i for i in validate_config(cfg) if "nothing captures 'm'" in i.message]
        assert len(warns) == 1  # de-duplicated


class TestDataAndAuth:
    def test_bad_data_mode(self):
        cfg = {"scenario": {"data": {"p": {"inline": [{"a": 1}], "mode": "weird"}},
                            "requests": [{"method": "GET", "path": "/x"}]}}
        assert _has(validate_config(cfg), ERROR, "'mode' must be one of")

    def test_generate_without_fields(self):
        cfg = {"scenario": {"data": {"p": {"generate": {"count": 3}}},
                            "requests": [{"method": "GET", "path": "/x"}]}}
        assert _has(validate_config(cfg), ERROR, "generate.fields")

    def test_bad_auth_type(self):
        cfg = {"scenario": {"auth": {"type": "magic"},
                            "requests": [{"method": "GET", "path": "/x"}]}}
        assert _has(validate_config(cfg), ERROR, "'auth.type' must be one of")


class TestAnalysis:
    def test_bad_rule_mode(self):
        cfg = {"scenario": {"requests": [{"method": "GET", "path": "/x"}]},
               "analysis": {"rules": [{"metric": "p95_ms", "mode": "weird", "direction": "increase"}]}}
        assert _has(validate_config(cfg), ERROR, "'mode' must be 'relative' or 'absolute'")

    def test_unknown_metric_warns(self):
        cfg = {"scenario": {"requests": [{"method": "GET", "path": "/x"}]},
               "analysis": {"rules": [{"metric": "made_up", "mode": "relative", "direction": "increase"}]}}
        assert _has(validate_config(cfg), WARNING, "unknown metric")

    def test_gate_unknown_metric_warns(self):
        cfg = {"scenario": {"requests": [{"method": "GET", "path": "/x"}]},
               "analysis": {"gate": {"thresholds": {"nonsense": {"fail": 5}}}}}
        assert _has(validate_config(cfg), WARNING, "unknown metric")


class TestPersonas:
    def test_users_validated(self):
        cfg = {"users": [
            {"weight": 1, "scenario": {"requests": [{"method": "GET", "path": "/a"}]}},
            {"weight": 1, "scenario": {"requests": [{"method": "GET", "path": "/b/${var:x}"}]}},
        ]}
        # persona 2 references an uncaptured var
        assert _has(validate_config(cfg), WARNING, "nothing captures 'x'")

    def test_users_flat_form(self):
        cfg = {"users": [{"weight": 2, "requests": [{"method": "GET", "path": "/a"}]}]}
        assert not _has(validate_config(cfg), ERROR, "no 'requests'")


class TestIterRequests:
    def test_covers_all_sections(self):
        scenario = {
            "on_start": [{"method": "POST", "path": "/login"}],
            "on_stop": [{"method": "POST", "path": "/logout"}],
            "flows": [{"name": "F", "steps": [{"method": "GET", "path": "/s"}]}],
            "requests": [{"method": "GET", "path": "/r"}],
        }
        locs = [loc for loc, _ in iter_requests(scenario)]
        assert any("on_start" in l for l in locs)
        assert any("on_stop" in l for l in locs)
        assert any("flows[1].steps[1]" in l for l in locs)
        assert any("requests[1]" in l for l in locs)


class TestFormat:
    def test_errors_before_warnings(self):
        issues = [Issue(WARNING, "a", "w"), Issue(ERROR, "b", "e")]
        out = format_issues(issues)
        assert out.index("ERROR") < out.index("WARNING")
        assert "1 error(s), 1 warning(s)" in out
