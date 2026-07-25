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
