import pytest

from locomotive.diff import (
    ADDED,
    BREAKING,
    CHANGED,
    INFO,
    REMOVED,
    diff_config_spec,
    format_findings,
    has_breaking,
)


SPEC = {
    "openapi": "3.0.0",
    "paths": {
        "/orders": {
            "post": {
                "operationId": "createOrder",
                "requestBody": {"content": {"application/json": {"schema": {
                    "type": "object", "required": ["product_id"],
                    "properties": {"product_id": {"type": "integer"}, "note": {"type": "string"}}}}}},
            }
        },
        "/orders/{id}": {"get": {"operationId": "getOrder"}},
        "/users": {"post": {"operationId": "createUser"}},
    },
}


def _cfg(requests):
    return {"scenario": {"requests": requests}}


def _kinds(findings):
    return {(f.kind, f.severity) for f in findings}


class TestNoDrift:
    def test_fully_covered(self):
        cfg = _cfg([
            {"method": "POST", "path": "/orders", "_operation": "createOrder",
             "json": {"product_id": "${randint:1:9}", "note": "x"}},
            {"method": "GET", "path": "/orders/${var:id}", "_operation": "getOrder"},
            {"method": "POST", "path": "/users", "_operation": "createUser"},
        ])
        assert diff_config_spec(cfg, SPEC) == []
        assert format_findings([]) == "✓ Config matches the spec."


class TestRemoved:
    def test_config_op_not_in_spec(self):
        cfg = _cfg([{"method": "GET", "path": "/legacy", "_operation": "oldThing"}])
        findings = diff_config_spec(cfg, SPEC)
        removed = [f for f in findings if f.kind == REMOVED]
        assert removed and removed[0].severity == BREAKING
        assert "removed or renamed" in removed[0].message


class TestAdded:
    def test_spec_op_not_in_config(self):
        cfg = _cfg([{"method": "POST", "path": "/orders", "_operation": "createOrder",
                     "json": {"product_id": 1}}])
        findings = diff_config_spec(cfg, SPEC)
        added = {f.message.split()[0] for f in findings if f.kind == ADDED}
        assert "getOrder" in added and "createUser" in added
        assert all(f.severity == INFO for f in findings if f.kind == ADDED)


class TestChanged:
    def test_missing_required_body_field(self):
        cfg = _cfg([{"method": "POST", "path": "/orders", "_operation": "createOrder",
                     "json": {"note": "x"}}])  # missing product_id
        findings = diff_config_spec(cfg, SPEC)
        changed = [f for f in findings if f.kind == CHANGED and f.severity == BREAKING]
        assert changed and "product_id" in changed[0].message

    def test_extra_body_field_is_info(self):
        cfg = _cfg([{"method": "POST", "path": "/orders", "_operation": "createOrder",
                     "json": {"product_id": 1, "ghost": "x"}}])
        findings = diff_config_spec(cfg, SPEC)
        changed = [f for f in findings if f.kind == CHANGED]
        assert changed and changed[0].severity == INFO and "ghost" in changed[0].message

    def test_missing_required_query(self):
        spec = {"paths": {"/search": {"get": {"operationId": "search", "parameters": [
            {"name": "q", "in": "query", "required": True, "schema": {"type": "string"}}]}}}}
        cfg = _cfg([{"method": "GET", "path": "/search", "_operation": "search"}])
        findings = diff_config_spec(cfg, spec)
        assert any(f.kind == CHANGED and f.severity == BREAKING and "q" in f.message for f in findings)


class TestMatching:
    def test_match_by_operation_id_over_path(self):
        # path differs, but operationId matches -> matched (no 'removed')
        cfg = _cfg([{"method": "POST", "path": "/v2/orders", "_operation": "createOrder",
                     "json": {"product_id": 1}}])
        findings = diff_config_spec(cfg, SPEC)
        assert not any(f.kind == REMOVED for f in findings)

    def test_match_by_canonical_path_without_opid(self):
        # no _operation; ${var:id} canonicalizes to '*' like spec's {id}
        cfg = _cfg([{"method": "GET", "path": "/orders/${var:id}"}])
        findings = diff_config_spec(cfg, SPEC)
        assert not any(f.kind == REMOVED for f in findings)

    def test_flows_and_on_start_are_walked(self):
        cfg = {"scenario": {
            "on_start": [{"method": "POST", "path": "/users", "_operation": "createUser"}],
            "flows": [{"name": "F", "steps": [
                {"method": "POST", "path": "/orders", "_operation": "createOrder", "json": {"product_id": 1}},
                {"method": "GET", "path": "/orders/${var:id}", "_operation": "getOrder"}]}],
        }}
        assert diff_config_spec(cfg, SPEC) == []  # everything covered across sections


class TestSeverityHelpers:
    def test_has_breaking(self):
        cfg = _cfg([{"method": "GET", "path": "/gone", "_operation": "gone"}])
        assert has_breaking(diff_config_spec(cfg, SPEC)) is True

    def test_no_breaking_when_only_added(self):
        cfg = _cfg([{"method": "POST", "path": "/orders", "_operation": "createOrder",
                     "json": {"product_id": 1}}])
        findings = diff_config_spec(cfg, SPEC)
        assert not has_breaking(findings)  # only 'added' info findings

    def test_format_orders_removed_first(self):
        cfg = _cfg([{"method": "GET", "path": "/gone", "_operation": "gone"}])
        out = format_findings(diff_config_spec(cfg, SPEC))
        assert "REMOVED" in out and "breaking" in out


class TestStaleOperationId:
    """A renamed operation still resolves by path — but not silently."""

    CFG = _cfg([{"method": "POST", "path": "/orders", "_operation": "makeOrder",
                 "json": {"product_id": 1}}])

    def test_stale_id_is_not_a_removal(self):
        findings = diff_config_spec(self.CFG, SPEC)
        assert not any(f.kind == REMOVED for f in findings)

    def test_stale_id_is_reported(self):
        findings = diff_config_spec(self.CFG, SPEC)
        stale = [f for f in findings if "makeOrder" in f.message]
        assert len(stale) == 1
        assert stale[0].kind == CHANGED and stale[0].severity == INFO
        assert "createOrder" in stale[0].message

    def test_current_id_is_silent(self):
        cfg = _cfg([{"method": "POST", "path": "/orders", "_operation": "createOrder",
                     "json": {"product_id": 1}}])
        assert not any("not in the spec" in f.message
                       for f in diff_config_spec(cfg, SPEC) if f.kind == CHANGED)

    def test_no_id_no_noise(self):
        cfg = _cfg([{"method": "POST", "path": "/orders", "json": {"product_id": 1}}])
        assert not any(f.kind == CHANGED for f in diff_config_spec(cfg, SPEC))


class TestFormBodiesAreBodies:
    """A form-encoded request keeps its fields in 'data', not 'json'."""

    SPEC = {
        "openapi": "3.0.0",
        "paths": {"/token": {"post": {
            "operationId": "getToken",
            "requestBody": {"content": {"application/x-www-form-urlencoded": {"schema": {
                "type": "object", "required": ["username", "password"],
                "properties": {"username": {"type": "string"},
                               "password": {"type": "string"}}}}}},
        }}},
    }

    def test_data_fields_satisfy_required_body(self):
        cfg = _cfg([{"method": "POST", "path": "/token", "_operation": "getToken",
                     "data": {"username": "u", "password": "p"}}])
        assert diff_config_spec(cfg, self.SPEC) == []

    def test_missing_data_field_is_still_breaking(self):
        cfg = _cfg([{"method": "POST", "path": "/token", "_operation": "getToken",
                     "data": {"username": "u"}}])
        findings = diff_config_spec(cfg, self.SPEC)
        assert any(f.kind == CHANGED and f.severity == BREAKING for f in findings)

    def test_json_wins_when_both_are_present(self):
        # 'json' is what the generator sends when both are set, so it is what
        # the diff compares.
        cfg = _cfg([{"method": "POST", "path": "/token", "_operation": "getToken",
                     "json": {"username": "u", "password": "p"}, "data": {"nope": 1}}])
        assert diff_config_spec(cfg, self.SPEC) == []


class TestUncommonMethods:
    """HEAD/OPTIONS in a config are real endpoints, not dead routes."""

    SPEC = {
        "openapi": "3.0.0",
        "paths": {"/health": {
            "head": {"operationId": "headHealth"},
            "options": {"operationId": "optionsHealth"},
        }},
    }

    @pytest.mark.parametrize("method", ["HEAD", "OPTIONS"])
    def test_method_is_matched(self, method):
        cfg = _cfg([{"method": method, "path": "/health"}])
        findings = diff_config_spec(cfg, self.SPEC)
        assert not any(f.kind == REMOVED for f in findings)

    def test_unmatched_method_is_still_removed(self):
        cfg = _cfg([{"method": "TRACE", "path": "/health"}])
        assert any(f.kind == REMOVED for f in diff_config_spec(cfg, self.SPEC))


class TestServerPrefixIsNotDrift:
    """The /v1 may live in the paths or in load.host; neither is drift."""

    SPEC = {
        "openapi": "3.0.0",
        "servers": [{"url": "https://api.example.com/v1"}],
        "paths": {"/orders": {"get": {"operationId": "listOrders"}}},
    }

    @pytest.mark.parametrize("path", ["/v1/orders", "/orders"])
    def test_both_forms_match(self, path):
        cfg = _cfg([{"method": "GET", "path": path}])
        assert diff_config_spec(cfg, self.SPEC) == []
