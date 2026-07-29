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


class TestExpectValidation:
    def _cfg(self, expect):
        return {"scenario": {"requests": [
            {"name": "C", "method": "GET", "path": "/c", "expect": expect}]}}

    def test_valid_expect_ok(self):
        cfg = self._cfg({"status": [200, 201], "contains": "ok",
                         "json": {"data.id": "1"}, "max_ms": 500})
        assert not any(i.level == ERROR for i in validate_config(cfg))

    def test_expect_not_object(self):
        assert _has(validate_config(self._cfg("nope")), ERROR, "'expect' must be an object")

    def test_bad_status(self):
        assert _has(validate_config(self._cfg({"status": "ok"})), ERROR, "'expect.status'")

    def test_bad_contains(self):
        assert _has(validate_config(self._cfg({"contains": 123})), ERROR, "'expect.contains'")

    def test_bad_json(self):
        assert _has(validate_config(self._cfg({"json": ["a"]})), ERROR, "'expect.json'")

    def test_bad_max_ms(self):
        assert _has(validate_config(self._cfg({"max_ms": "soon"})), ERROR, "'expect.max_ms'")

    def test_unknown_key_warns(self):
        assert _has(validate_config(self._cfg({"status": 200, "bogus": 1})), WARNING, "unknown 'expect' key")


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


def _pool_config(pool):
    return {
        "load": {"host": "http://x", "users": 1, "spawn_rate": 1, "run_time": "1m"},
        "scenario": {
            "data": {"acc": pool},
            "requests": [{"method": "POST", "path": "/login",
                          "json": {"u": "${data:acc.user}"}}],
        },
    }


class TestPoolSourceIsChecked:
    """A pool that cannot yield rows is an error before the run, not empty
    strings during it."""

    def test_missing_file_is_an_error(self, tmp_path):
        cfg = _pool_config({"source": str(tmp_path / "nope.csv")})
        assert _has(validate_config(cfg), ERROR, "data file not found")

    def test_existing_file_is_fine(self, tmp_path):
        path = tmp_path / "acc.csv"
        path.write_text("user,pass\nu1,p1\n", encoding="utf-8")
        cfg = _pool_config({"source": str(path)})
        assert not any(i.level == ERROR for i in validate_config(cfg))

    def test_header_only_csv_is_an_error(self, tmp_path):
        path = tmp_path / "acc.csv"
        path.write_text("user,pass\n", encoding="utf-8")
        assert _has(validate_config(_pool_config({"source": str(path)})), ERROR, "no rows")

    def test_empty_json_list_is_an_error(self, tmp_path):
        path = tmp_path / "acc.json"
        path.write_text("[]", encoding="utf-8")
        assert _has(validate_config(_pool_config({"source": str(path)})), ERROR, "no usable rows")

    def test_json_object_instead_of_list_is_an_error(self, tmp_path):
        path = tmp_path / "acc.json"
        path.write_text('{"user": "u1"}', encoding="utf-8")
        assert _has(validate_config(_pool_config({"source": str(path)})), ERROR, "list of objects")

    def test_broken_json_is_an_error(self, tmp_path):
        path = tmp_path / "acc.json"
        path.write_text("{not json", encoding="utf-8")
        assert _has(validate_config(_pool_config({"source": str(path)})), ERROR, "cannot parse")

    def test_directory_source_is_an_error(self, tmp_path):
        assert _has(validate_config(_pool_config({"source": str(tmp_path)})), ERROR, "directory")

    def test_unresolved_placeholder_source_is_left_alone(self):
        cfg = _pool_config({"source": "${env:POOL_PATH}"})
        assert not _has(validate_config(cfg), ERROR, "data file not found")

    def test_empty_inline_is_an_error(self):
        assert _has(validate_config(_pool_config({"inline": []})), ERROR, "no rows")

    def test_zero_generate_count_is_an_error(self):
        pool = {"generate": {"count": 0, "fields": {"user": "${fake:username}"}}}
        assert _has(validate_config(_pool_config(pool)), ERROR, "at least 1")

    def test_generate_count_stays_optional(self):
        pool = {"generate": {"fields": {"user": "${fake:username}"}}}
        assert not any(i.level == ERROR for i in validate_config(_pool_config(pool)))


class TestPoolFieldIsChecked:
    """Typos in ${data:pool.field} are worth a warning, never an error."""

    def test_unknown_csv_field_warns(self, tmp_path):
        path = tmp_path / "acc.csv"
        path.write_text("username,pass\nu1,p1\n", encoding="utf-8")
        issues = validate_config(_pool_config({"source": str(path)}))
        assert _has(issues, WARNING, "has no field 'user'")
        assert not any(i.level == ERROR for i in issues)

    def test_known_field_is_silent(self, tmp_path):
        path = tmp_path / "acc.csv"
        path.write_text("user,pass\nu1,p1\n", encoding="utf-8")
        assert not _has(validate_config(_pool_config({"source": str(path)})), WARNING, "has no field")

    def test_inline_and_generate_fields_are_checked(self):
        assert _has(validate_config(_pool_config({"inline": [{"login": "u1"}]})),
                    WARNING, "has no field 'user'")
        pool = {"generate": {"fields": {"login": "${fake:username}"}}}
        assert _has(validate_config(_pool_config(pool)), WARNING, "has no field 'user'")

    def test_dotted_path_is_not_second_guessed(self, tmp_path):
        # A JSON row can nest; the first-row key set says nothing about it.
        path = tmp_path / "acc.json"
        path.write_text(json.dumps([{"creds": {"user": "u1"}}]), encoding="utf-8")
        cfg = _pool_config({"source": str(path)})
        cfg["scenario"]["requests"][0]["json"] = {"u": "${data:acc.creds.user}"}
        assert not _has(validate_config(cfg), WARNING, "has no field")

    def test_unreadable_pool_does_not_warn_about_fields(self, tmp_path):
        cfg = _pool_config({"source": str(tmp_path / "nope.csv")})
        assert not _has(validate_config(cfg), WARNING, "has no field")


class TestEnvRefsMustResolve:
    """${env:} inside scenario/users survives loading, so validate is the
    only thing standing between an unset secret and a wall of 401s."""

    def test_unset_without_default_is_an_error(self, monkeypatch):
        monkeypatch.delenv("LOCO_TEST_SECRET", raising=False)
        cfg = {"scenario": {"auth": {"type": "bearer", "token": "${env:LOCO_TEST_SECRET}"},
                            "requests": [{"method": "GET", "path": "/a"}]}}
        issues = validate_config(cfg)
        assert _has(issues, ERROR, "LOCO_TEST_SECRET")
        assert any("scenario.auth.token" == i.location for i in issues)

    def test_set_variable_passes(self, monkeypatch):
        monkeypatch.setenv("LOCO_TEST_SECRET", "s3cret")
        cfg = {"scenario": {"auth": {"type": "bearer", "token": "${env:LOCO_TEST_SECRET}"},
                            "requests": [{"method": "GET", "path": "/a"}]}}
        assert not _has(validate_config(cfg), ERROR, "LOCO_TEST_SECRET")

    def test_default_passes(self, monkeypatch):
        monkeypatch.delenv("LOCO_TEST_SECRET", raising=False)
        cfg = {"scenario": {"requests": [
            {"method": "GET", "path": "/a", "headers": {"X": "${env:LOCO_TEST_SECRET:-anon}"}}]}}
        assert not _has(validate_config(cfg), ERROR, "LOCO_TEST_SECRET")

    def test_explicit_empty_default_passes(self, monkeypatch):
        monkeypatch.delenv("LOCO_TEST_SECRET", raising=False)
        cfg = {"scenario": {"requests": [
            {"method": "GET", "path": "/a", "headers": {"X": "${env:LOCO_TEST_SECRET:-}"}}]}}
        assert not _has(validate_config(cfg), ERROR, "LOCO_TEST_SECRET")

    def test_persona_env_is_checked(self, monkeypatch):
        monkeypatch.delenv("LOCO_TEST_SECRET", raising=False)
        cfg = {"users": [{"name": "r", "weight": 1, "scenario": {
            "requests": [{"method": "GET", "path": "/a",
                          "headers": {"X": "${env:LOCO_TEST_SECRET}"}}]}}]}
        assert _has(validate_config(cfg), ERROR, "LOCO_TEST_SECRET")

    def test_bare_dollar_form_warns_when_the_variable_exists(self, monkeypatch):
        monkeypatch.setenv("LOCO_TEST_HOST", "http://x")
        cfg = {"load": {"host": "$LOCO_TEST_HOST"},
               "scenario": {"requests": [{"method": "GET", "path": "/a"}]}}
        issues = validate_config(cfg)
        assert _has(issues, WARNING, "sent literally")
        assert not any(i.level == ERROR for i in issues)

    def test_bare_dollar_form_is_quiet_otherwise(self, monkeypatch):
        monkeypatch.delenv("LOCO_TEST_HOST", raising=False)
        cfg = {"scenario": {"requests": [
            {"method": "POST", "path": "/a", "json": {"$ref": "#/x"},
             "headers": {"H": "$LOCO_TEST_HOST"}}]}}
        assert not _has(validate_config(cfg), WARNING, "sent literally")

    def test_other_placeholders_are_untouched(self, monkeypatch):
        cfg = {"scenario": {"requests": [
            {"method": "GET", "path": "/a/${uuid}", "headers": {"X": "${var:t}"}}]}}
        assert not any(i.level == ERROR for i in validate_config(cfg))


def _scenario_config(**scenario):
    scenario.setdefault("requests", [{"method": "GET", "path": "/a"}])
    return {"load": {"host": "http://x", "users": 1, "spawn_rate": 1, "run_time": "1m"},
            "scenario": scenario}


class TestRequestBodyAndTimeoutRules:
    def test_json_and_data_together_is_an_error(self):
        cfg = _scenario_config(requests=[
            {"method": "POST", "path": "/a", "json": {"a": 1}, "data": {"b": 2}}])
        assert _has(validate_config(cfg), ERROR, "both set a request body")

    def test_either_one_alone_is_fine(self):
        for body in ({"json": {"a": 1}}, {"data": {"b": 2}}):
            cfg = _scenario_config(requests=[{"method": "POST", "path": "/a", **body}])
            assert not _has(validate_config(cfg), ERROR, "both set a request body")

    def test_string_tags_are_allowed(self):
        cfg = _scenario_config(requests=[{"method": "GET", "path": "/a", "tags": "buy"}])
        assert not _has(validate_config(cfg), ERROR, "'tags'")

    def test_non_list_non_string_tags_is_an_error(self):
        cfg = _scenario_config(requests=[{"method": "GET", "path": "/a", "tags": 7}])
        assert _has(validate_config(cfg), ERROR, "'tags'")

    def test_string_timeout_warns(self):
        cfg = _scenario_config(requests=[{"method": "GET", "path": "/a", "timeout": "5"}])
        issues = validate_config(cfg)
        assert _has(issues, WARNING, "write it as a number")
        assert not any(i.level == ERROR for i in issues)

    def test_unparseable_timeout_is_an_error(self):
        cfg = _scenario_config(requests=[{"method": "GET", "path": "/a", "timeout": "soon"}])
        assert _has(validate_config(cfg), ERROR, "'timeout' must be a number")

    def test_zero_timeout_is_an_error(self):
        cfg = _scenario_config(requests=[{"method": "GET", "path": "/a", "timeout": 0}])
        assert _has(validate_config(cfg), ERROR, "greater than 0")

    def test_numeric_timeout_is_silent(self):
        cfg = _scenario_config(requests=[{"method": "GET", "path": "/a", "timeout": 5}])
        assert not _has(validate_config(cfg), WARNING, "timeout")


class TestThinkTimeRules:
    def test_swapped_bounds_warn(self):
        issues = validate_config(_scenario_config(think_time={"min": 2, "max": 0.5}))
        assert _has(issues, WARNING, "is greater than max")
        assert not any(i.level == ERROR for i in issues)

    def test_ordered_bounds_are_silent(self):
        assert not _has(validate_config(_scenario_config(think_time={"min": 0.5, "max": 2})),
                        WARNING, "is greater than max")

    def test_negative_is_an_error(self):
        assert _has(validate_config(_scenario_config(think_time=-1)), ERROR, "negative")

    def test_non_numeric_is_an_error(self):
        assert _has(validate_config(_scenario_config(think_time="slow")),
                    ERROR, "must be a number of seconds")

    def test_plain_number_is_fine(self):
        assert not any(i.level == ERROR for i in validate_config(_scenario_config(think_time=1.5)))

    def test_flow_think_time_is_checked(self):
        cfg = _scenario_config(flows=[{"name": "F", "think_time": {"min": 3, "max": 1},
                                       "steps": [{"method": "GET", "path": "/s"}]}])
        issues = validate_config(cfg)
        assert any(i.location.endswith("flows[1].think_time") for i in issues)

    def test_request_think_time_is_checked(self):
        cfg = _scenario_config(requests=[{"method": "GET", "path": "/a", "think_time": -2}])
        assert _has(validate_config(cfg), ERROR, "negative")


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


# ── validate must reject everything run rejects ───────────────────────
#
# The promise of `loco validate` is "every problem at once, before locust
# starts". Each config below used to pass validation with zero errors and
# then die inside generate_locustfile — the CLI runs validation as a
# preflight, so the user got a clean bill of health followed immediately by
# a traceback.

CRASHING_CONFIGS = {
    "requests_is_a_mapping": {"requests": {"a": {"method": "GET", "path": "/a"}}},
    "requests_of_strings": {"requests": ["GET /a"]},
    "flows_is_a_mapping": {"flows": {"f": {"steps": []}}},
    "flow_without_steps": {"flows": [{"name": "F"}]},
    "flow_steps_is_a_string": {"flows": [{"name": "F", "steps": "GET /a"}]},
    "flow_steps_of_strings": {"flows": [{"name": "F", "steps": ["/a"]}]},
    "flow_is_a_string": {"flows": ["F"]},
    "flow_steps_empty": {"flows": [{"name": "F", "steps": []}]},
    "on_start_of_strings": {
        "on_start": ["/login"],
        "requests": [{"name": "R", "method": "GET", "path": "/r"}],
    },
    "pool_name_with_a_dot": {
        "data": {"a.b": {"inline": [{"x": "1"}]}},
        "requests": [{"name": "R", "method": "GET", "path": "/r"}],
    },
}


class TestValidateCatchesWhatRunRejects:
    @pytest.mark.parametrize("name", sorted(CRASHING_CONFIGS))
    def test_generator_rejects_it(self, name, tmp_path):
        # Guard the guard: if the generator ever starts accepting one of
        # these, the matching validate error becomes a false alarm.
        from locomotive.scenario import generate_locustfile

        with pytest.raises(ValueError):
            generate_locustfile(CRASHING_CONFIGS[name], {}, tmp_path)

    @pytest.mark.parametrize("name", sorted(CRASHING_CONFIGS))
    def test_validate_rejects_it_too(self, name):
        cfg = {"load": {"host": "http://x", "users": 1, "spawn_rate": 1, "run_time": "1m"},
               "scenario": CRASHING_CONFIGS[name]}
        issues = validate_config(cfg)
        assert [i for i in issues if i.level == ERROR], _levels(issues)


class TestScenarioStructure:
    def test_requests_as_a_mapping_says_so(self):
        issues = validate_config(_scenario_config(requests={"a": {"path": "/a"}}))
        assert _has(issues, ERROR, "must be a list of requests, got dict")

    def test_a_mapping_does_not_also_report_no_requests(self):
        # Two errors for one mistake sends the reader hunting for a second,
        # imaginary problem.
        issues = validate_config(_scenario_config(requests={"a": {"path": "/a"}}))
        assert not _has(issues, ERROR, "has no 'requests' or 'flows'")

    def test_request_entry_that_is_a_string(self):
        issues = validate_config(_scenario_config(requests=["GET /a"]))
        assert _has(issues, ERROR, "must be an object, got str")
        assert any(i.location == "scenario.requests[1]" for i in issues)

    def test_on_start_as_an_object_is_an_error(self):
        # Valid YAML, silently ignored: the login never ran and the whole
        # run came back 401.
        issues = validate_config(_scenario_config(
            on_start={"name": "Login", "method": "POST", "path": "/login"}))
        assert _has(issues, ERROR, "must be a list of requests, got dict")

    def test_on_stop_as_an_object_is_an_error(self):
        issues = validate_config(_scenario_config(
            on_stop={"name": "Bye", "method": "POST", "path": "/logout"}))
        assert any(i.location == "scenario.on_stop" and i.level == ERROR for i in issues)

    def test_flows_as_a_mapping(self):
        issues = validate_config(_scenario_config(flows={"f": {"steps": []}}))
        assert _has(issues, ERROR, "must be a list of flows, got dict")

    def test_flow_without_steps_names_the_flow(self):
        issues = validate_config(_scenario_config(flows=[{"name": "Checkout"}]))
        assert _has(issues, ERROR, "('Checkout') must define a non-empty 'steps' list")

    def test_flow_with_empty_steps(self):
        issues = validate_config(_scenario_config(flows=[{"name": "F", "steps": []}]))
        assert _has(issues, ERROR, "must define a non-empty 'steps' list")

    def test_unnamed_flow_is_labelled_by_position(self):
        issues = validate_config(_scenario_config(flows=[{"steps": []}]))
        assert _has(issues, ERROR, "('flow_1')")

    def test_flow_steps_of_strings(self):
        issues = validate_config(_scenario_config(flows=[{"name": "F", "steps": ["/a"]}]))
        assert any(i.location == "scenario.flows[1].steps[1]" and i.level == ERROR
                   for i in issues)

    def test_flow_weight_below_one_warns(self):
        issues = validate_config(_scenario_config(
            flows=[{"name": "F", "weight": 0,
                    "steps": [{"name": "S", "method": "GET", "path": "/a"}]}]))
        assert _has(issues, WARNING, "weights below 1 run as 1")

    def test_scenario_headers_must_be_an_object(self):
        issues = validate_config(_scenario_config(headers=["X-Api-Key: k"]))
        assert _has(issues, ERROR, "must be an object of header names to values")

    def test_valid_config_is_still_clean(self):
        assert validate_config(VALID) == []


class TestPoolNameSyntax:
    def test_a_dotted_pool_name_is_an_error(self):
        # ${data:a.b} would read field 'b' of pool 'a', so the pool named
        # "a.b" can never be referenced at all.
        issues = validate_config(_scenario_config(data={"a.b": {"inline": [{"x": "1"}]}}))
        assert _has(issues, ERROR, "pool name 'a.b' must match [A-Za-z0-9_]+")

    @pytest.mark.parametrize("name", ["my pool", "pool-1", "pöol", ""])
    def test_other_illegal_names(self, name):
        issues = validate_config(_scenario_config(data={name: {"inline": [{"x": "1"}]}}))
        assert _has(issues, ERROR, "must match [A-Za-z0-9_]+")

    @pytest.mark.parametrize("name", ["accounts", "pool_1", "A1"])
    def test_legal_names_are_left_alone(self, name):
        issues = validate_config(_scenario_config(data={name: {"inline": [{"x": "1"}]}}))
        assert not _has(issues, ERROR, "must match [A-Za-z0-9_]+")


class TestTopLevelSections:
    def test_users_not_a_list(self):
        issues = validate_config({"users": {"a": {}}})
        assert _has(issues, ERROR, "must be a list of personas, got dict")

    def test_users_entry_not_an_object(self):
        issues = validate_config({"users": ["reader"]})
        assert any(i.location == "users[1]" and i.level == ERROR for i in issues)

    def test_empty_users_warns(self):
        issues = validate_config({"users": [], "scenario": {
            "requests": [{"method": "GET", "path": "/a"}]}})
        assert _has(issues, WARNING, "no personas will be generated")

    def test_scenario_not_an_object(self):
        issues = validate_config({"scenario": "requests"})
        assert any(i.location == "scenario" and i.level == ERROR for i in issues)

    @pytest.mark.parametrize("section", ["load", "target", "analysis", "report", "artifacts"])
    def test_section_not_an_object(self, section):
        cfg = _scenario_config()
        cfg[section] = ["oops"]
        issues = validate_config(cfg)
        assert any(i.location == section and i.level == ERROR for i in issues)


class TestRunTimeSectionsThatCrashCI:
    def test_history_must_be_an_integer(self):
        # ci does int(artifacts["history"]) with no guard — the failure lands
        # after locust has already spent its minutes.
        cfg = _scenario_config()
        cfg["artifacts"] = {"history": "many"}
        assert _has(validate_config(cfg), ERROR, "must be an integer number of runs")

    def test_negative_history(self):
        cfg = _scenario_config()
        cfg["artifacts"] = {"history": -1}
        assert _has(validate_config(cfg), ERROR, "cannot be negative")

    def test_history_integer_is_fine(self):
        cfg = _scenario_config()
        cfg["artifacts"] = {"history": 30}
        assert not _has(validate_config(cfg), ERROR, "history")

    def test_unknown_fail_on_warns(self):
        cfg = _scenario_config()
        cfg["analysis"] = {"fail_on": "ERROR"}
        assert _has(validate_config(cfg), WARNING, "expected one of WARNING, DEGRADATION")

    @pytest.mark.parametrize("value", ["warning", "DEGRADATION", " degradation "])
    def test_fail_on_is_case_and_space_insensitive(self, value):
        cfg = _scenario_config()
        cfg["analysis"] = {"fail_on": value}
        assert not _has(validate_config(cfg), WARNING, "expected one of")

    def test_rule_without_thresholds(self):
        # analyzer.load_rules does float(item.get("warn")) unguarded.
        cfg = _scenario_config()
        cfg["analysis"] = {"rules": [
            {"metric": "p95_ms", "mode": "relative", "direction": "increase"}]}
        issues = validate_config(cfg)
        assert _has(issues, ERROR, "missing 'warn' threshold")
        assert _has(issues, ERROR, "missing 'fail' threshold")

    def test_non_numeric_threshold(self):
        cfg = _scenario_config()
        cfg["analysis"] = {"rules": [
            {"metric": "p95_ms", "mode": "relative", "direction": "increase",
             "warn": "a lot", "fail": 20}]}
        assert _has(validate_config(cfg), ERROR, "'warn' must be a number")

    def test_rules_not_a_list(self):
        cfg = _scenario_config()
        cfg["analysis"] = {"rules": {"p95_ms": 10}}
        assert _has(validate_config(cfg), ERROR, "must be a list of rules")


class TestRequestFieldsThatWereIgnored:
    def test_method_must_be_a_string(self):
        issues = validate_config(_scenario_config(
            requests=[{"name": "R", "method": 200, "path": "/r"}]))
        assert _has(issues, ERROR, "'method' must be a string, got int")

    @pytest.mark.parametrize("weight", [0, -3])
    def test_weight_below_one_warns(self, weight):
        issues = validate_config(_scenario_config(
            requests=[{"name": "R", "method": "GET", "path": "/r", "weight": weight}]))
        assert _has(issues, WARNING, "weights below 1 run as 1")

    def test_weight_one_is_silent(self):
        issues = validate_config(_scenario_config(
            requests=[{"name": "R", "method": "GET", "path": "/r", "weight": 1}]))
        assert not _has(issues, WARNING, "weights below 1")

    def test_duplicate_request_names_warn(self):
        issues = validate_config(_scenario_config(requests=[
            {"name": "Search", "method": "GET", "path": "/a"},
            {"name": "Search", "method": "GET", "path": "/b"}]))
        assert _has(issues, WARNING, "already used at scenario.requests[1]")

    def test_duplicates_across_flows_and_requests(self):
        issues = validate_config(_scenario_config(
            requests=[{"name": "Search", "method": "GET", "path": "/a"}],
            flows=[{"name": "F", "steps": [
                {"name": "Search", "method": "GET", "path": "/b"}]}]))
        assert _has(issues, WARNING, "Locust merges both")

    def test_distinct_names_are_silent(self):
        issues = validate_config(_scenario_config(requests=[
            {"name": "A", "method": "GET", "path": "/a"},
            {"name": "B", "method": "GET", "path": "/b"}]))
        assert not _has(issues, WARNING, "already used at")

    def test_personas_do_not_collide_with_each_other(self):
        # Two personas naming their own "Browse" is normal; the warning is
        # about one scope, where the merge is a surprise.
        cfg = {"load": {"host": "http://x", "users": 1, "spawn_rate": 1, "run_time": "1m"},
               "users": [
                   {"name": "a", "scenario": {"requests": [
                       {"name": "Browse", "method": "GET", "path": "/a"}]}},
                   {"name": "b", "scenario": {"requests": [
                       {"name": "Browse", "method": "GET", "path": "/b"}]}}]}
        assert not _has(validate_config(cfg), WARNING, "already used at")


# ── report colours ────────────────────────────────────────────────────
#
# The renderer refuses to write a value it can't recognise as a colour into
# its stylesheet, because a stylesheet is one of the two places `html.escape`
# does nothing. Dropping it is the right call — saying nothing about it is
# not, and this is where the user still has the config open.


def _report_config(report):
    cfg = json.loads(json.dumps(VALID))
    cfg["report"] = report
    return cfg


class TestReportColors:
    def test_hex_and_rgba_are_accepted(self):
        issues = validate_config(_report_config({
            "theme": {"colors": {"primary": "#ff8800", "pass": "rgba(0,128,0,.5)"}},
            "branding": {"name": "Acme", "color": "var(--primary)"},
        }))
        assert not any("colour" in i.message for i in issues)

    def test_a_value_that_closes_the_declaration_is_reported(self):
        issues = validate_config(_report_config({
            "theme": {"colors": {"primary": "red; } body { display: none"}},
        }))
        assert _has(issues, WARNING, "not a colour the report can write")

    def test_an_unusable_variable_name_is_reported(self):
        issues = validate_config(_report_config({
            "theme": {"colors": {"primary color": "#fff"}},
        }))
        assert _has(issues, WARNING, "not usable as a CSS variable")

    def test_a_brand_colour_is_checked_too(self):
        issues = validate_config(_report_config({
            "branding": {"name": "Acme", "color": "url(http://x/y.png)"},
        }))
        assert _has(issues, WARNING, "report.branding.color") or _has(
            issues, WARNING, "not a colour the report can write")

    def test_colors_as_a_list_is_an_error(self):
        issues = validate_config(_report_config({"theme": {"colors": ["#fff"]}}))
        assert _has(issues, ERROR, "must be an object of names to colours")

    def test_underscore_keys_are_left_alone(self):
        # `_comment` keys are stripped by the resolver, so they are not typos.
        issues = validate_config(_report_config({
            "theme": {"colors": {"_comment": "anything at all; really"}},
        }))
        assert not any("CSS variable" in i.message for i in issues)

    def test_a_colour_problem_never_blocks_a_run(self):
        issues = validate_config(_report_config({
            "theme": {"colors": {"primary": "red; }"}},
        }))
        assert not any(i.level == ERROR for i in issues)


def _load_config(**load):
    cfg = json.loads(json.dumps(VALID))
    cfg["load"].update(load)
    return cfg


class TestRunBudget:
    def test_absent_is_fine(self):
        assert validate_config(VALID) == []

    def test_null_means_wait_forever(self):
        assert validate_config(_load_config(timeout=None)) == []

    @pytest.mark.parametrize("value", [300, 300.5, "5m", "1h30m", "90s"])
    def test_durations_accepted(self, value):
        issues = validate_config(_load_config(run_time="1m", timeout=value))
        assert not _has(issues, ERROR, "load.timeout")

    @pytest.mark.parametrize("value", ["soon", "", 0, -30])
    def test_nonsense_is_an_error(self, value):
        assert _has(validate_config(_load_config(timeout=value)), ERROR, "is not a duration")

    def test_budget_shorter_than_run_time_is_warned(self):
        # 30s of budget for a 5m run kills every single run mid-flight, and
        # the artifacts that come back look like a flaky service.
        issues = validate_config(_load_config(run_time="5m", timeout="30s"))
        assert _has(issues, WARNING, "will be stopped before it finishes")

    def test_equal_budget_is_still_too_short(self):
        # Ramp-up and shutdown happen outside run_time, so exactly run_time
        # is never enough.
        issues = validate_config(_load_config(run_time="1m", timeout=60))
        assert _has(issues, WARNING, "not longer than run_time")

    def test_generous_budget_is_quiet(self):
        issues = validate_config(_load_config(run_time="1m", timeout="10m"))
        assert not any(i.location == "load.timeout" for i in issues)

    def test_budget_without_run_time_is_not_compared(self):
        cfg = json.loads(json.dumps(VALID))
        cfg["load"].pop("run_time")
        cfg["load"]["timeout"] = 5
        assert not _has(validate_config(cfg), WARNING, "not longer than run_time")


# ── distributed runs ──────────────────────────────────────────────────


def _shard_pool_config(rows, mode="unique_per_user", **load):
    """A config whose one data pool has `rows` inline rows."""
    cfg = json.loads(json.dumps(VALID))
    cfg["load"].update(load)
    cfg["scenario"]["data"] = {
        "acc": {"inline": [{"u": f"u{i}"} for i in range(rows)], "mode": mode}
    }
    return cfg


class TestDistributionShape:
    def test_a_plain_config_says_nothing_about_distribution(self):
        assert validate_config(VALID) == []

    @pytest.mark.parametrize("field", ["processes", "expect_workers", "master_port"])
    def test_counts_must_be_integers(self, field):
        issues = validate_config(_load_config(**{field: "four"}))
        assert _has(issues, ERROR, "must be an integer")

    @pytest.mark.parametrize("field", ["master", "worker", "shard_data"])
    def test_switches_must_be_booleans(self, field):
        issues = validate_config(_load_config(**{field: "yes"}))
        assert _has(issues, ERROR, "must be true or false")

    def test_zero_processes_is_an_error(self):
        issues = validate_config(_load_config(processes=0))
        assert _has(issues, ERROR, "at least 1")

    def test_one_process_is_the_default_spelled_out(self):
        assert validate_config(_load_config(processes=1)) == []

    def test_zero_expected_workers_is_an_error(self):
        issues = validate_config(_load_config(master=True, expect_workers=0))
        assert _has(issues, ERROR, "at least 1")


class TestDistributionRoles:
    def test_both_roles_at_once_is_an_error(self):
        issues = validate_config(_load_config(master=True, worker=True))
        assert _has(issues, ERROR, "one process is one role")

    def test_a_worker_cannot_fan_out_again(self):
        issues = validate_config(_load_config(worker=True, processes=4))
        assert _has(issues, ERROR, "cannot fan out again")

    def test_processes_already_means_master(self):
        issues = validate_config(_load_config(master=True, processes=4))
        assert _has(issues, ERROR, "already makes")

    def test_a_contradicted_worker_count_is_an_error(self):
        issues = validate_config(_load_config(processes=4, expect_workers=2))
        assert _has(issues, ERROR, "contradicts")

    def test_a_matching_worker_count_is_still_redundant_but_allowed(self):
        # Saying the same number twice is not a mistake worth blocking a run
        # over — it is only worth blocking when the two disagree.
        assert validate_config(_load_config(processes=4, expect_workers=4)) == []

    def test_a_master_must_say_how_many_workers(self):
        issues = validate_config(_load_config(master=True))
        assert _has(issues, ERROR, "how many workers")

    def test_a_master_with_a_count_is_fine(self):
        assert validate_config(_load_config(master=True, expect_workers=3)) == []

    def test_a_worker_without_an_address_is_warned(self):
        issues = validate_config(_load_config(worker=True))
        assert _has(issues, WARNING, "127.0.0.1")

    def test_a_worker_with_an_address_is_quiet(self):
        assert validate_config(_load_config(worker=True, master_host="10.0.0.5")) == []

    def test_a_worker_count_with_no_cluster_has_no_effect(self):
        issues = validate_config(_load_config(expect_workers=4))
        assert _has(issues, WARNING, "no effect")

    def test_windows_cannot_fork_workers(self, monkeypatch):
        import locomotive.validate as validate_module

        monkeypatch.setattr(validate_module.os, "name", "nt")
        issues = validate_config(_load_config(processes=4))
        assert _has(issues, ERROR, "Windows cannot do")


class TestPoolCapacityUnderSharding:
    def test_enough_rows_is_quiet(self):
        assert validate_config(_shard_pool_config(8, processes=4)) == []

    def test_fewer_rows_than_workers_is_warned(self):
        issues = validate_config(_shard_pool_config(2, processes=4))
        assert _has(issues, WARNING, "4 load generators")

    def test_the_warning_names_the_pool(self):
        issues = validate_config(_shard_pool_config(2, processes=4))
        assert any(i.location.endswith("data.acc") for i in issues)

    def test_a_single_process_never_warns(self):
        assert validate_config(_shard_pool_config(2)) == []

    @pytest.mark.parametrize("mode", ["random", "once"])
    def test_unsharded_modes_never_warn(self, mode):
        # 'random' is already correct across workers and 'once' must not be
        # split at all, so neither is ever divided and neither can run short.
        assert validate_config(_shard_pool_config(2, mode=mode, processes=4)) == []

    def test_shard_data_false_makes_it_intended(self):
        assert validate_config(_shard_pool_config(2, processes=4, shard_data=False)) == []

    def test_a_generated_pool_never_warns(self):
        # Rows are synthesised per process, so there is no shared sequence
        # to divide and nothing to run short.
        cfg = _load_config(processes=4)
        cfg["scenario"]["data"] = {
            "acc": {"generate": {"count": 2, "fields": {"u": "${fake:username}"}}}
        }
        assert validate_config(cfg) == []

    def test_a_csv_pool_is_counted(self, tmp_path):
        path = tmp_path / "accounts.csv"
        path.write_text("login\nalice\nbob\n", encoding="utf-8")
        cfg = _load_config(processes=4)
        cfg["scenario"]["data"] = {"acc": {"source": str(path)}}
        issues = validate_config(cfg)
        assert _has(issues, WARNING, "2 row(s) for 4 load generators")

    def test_a_contradictory_config_does_not_also_warn_about_pools(self):
        # processes+master is rejected; the shard count is unknowable after
        # that, and guessing one would bury the real error in noise.
        issues = validate_config(_shard_pool_config(2, processes=4, master=True))
        assert not _has(issues, WARNING, "load generators")
