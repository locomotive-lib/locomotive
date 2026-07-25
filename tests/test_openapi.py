import pytest

from locomotive.openapi import (
    convert_path_params,
    extract_requests,
    resolve,
    synthesize,
)


# ── resolve: $ref / allOf / oneOf ─────────────────────────────────────


class TestResolve:
    def test_deref(self):
        spec = {"components": {"schemas": {"User": {"type": "object", "properties": {"a": {"type": "string"}}}}}}
        r = resolve({"$ref": "#/components/schemas/User"}, spec)
        assert r["type"] == "object" and "a" in r["properties"]

    def test_allof_merges_properties(self):
        spec = {"components": {"schemas": {
            "Base": {"type": "object", "properties": {"id": {"type": "integer"}}, "required": ["id"]},
        }}}
        schema = {"allOf": [
            {"$ref": "#/components/schemas/Base"},
            {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
        ]}
        r = resolve(schema, spec)
        assert set(r["properties"]) == {"id", "name"}
        assert set(r["required"]) == {"id", "name"}

    def test_oneof_picks_first(self):
        schema = {"oneOf": [{"type": "string"}, {"type": "integer"}]}
        assert resolve(schema, {})["type"] == "string"

    def test_missing_ref_safe(self):
        assert resolve({"$ref": "#/nope/missing"}, {}) == {}


# ── synthesize: scalar mapping ────────────────────────────────────────


class TestSynthesizeScalars:
    def test_example_wins(self):
        assert synthesize({"type": "string", "example": "hello"}, {}) == "hello"

    def test_default_used(self):
        assert synthesize({"type": "integer", "default": 42}, {}) == 42

    def test_enum_to_choice(self):
        assert synthesize({"type": "string", "enum": ["a", "b", "c"]}, {}) == "${choice:a,b,c}"

    @pytest.mark.parametrize("fmt,expected", [
        ("email", "${fake:email}"),
        ("uuid", "${uuid}"),
        ("date-time", "${now}"),
        ("date", "${now:%Y-%m-%d}"),
        ("password", "${fake:digits:12}"),
    ])
    def test_format_mapping(self, fmt, expected):
        assert synthesize({"type": "string", "format": fmt}, {}, name="whatever") == expected

    @pytest.mark.parametrize("name,expected", [
        ("email", "${fake:email}"),
        ("customer_email", "${fake:email}"),
        ("first_name", "${fake:first_name}"),
        ("last_name", "${fake:last_name}"),
        ("full_name", "${fake:name}"),
        ("username", "${fake:username}"),
        ("password", "${fake:digits:12}"),
        ("phone", "${fake:phone}"),
        ("city", "${fake:city}"),
        ("quantity", "${randint:1:10}"),
    ])
    def test_name_heuristics(self, name, expected):
        assert synthesize({"type": "string"}, {}, name=name) == expected

    def test_id_integer(self):
        assert synthesize({"type": "integer"}, {}, name="product_id") == "${randint:1:1000}"

    def test_id_bare(self):
        assert synthesize({"type": "integer"}, {}, name="id") == "${randint:1:1000}"

    def test_integer_range_respects_bounds(self):
        assert synthesize({"type": "integer", "minimum": 5, "maximum": 9}, {}, name="qty_x") == "${randint:5:9}"

    def test_boolean(self):
        assert synthesize({"type": "boolean"}, {}, name="active") == "${fake:bool}"

    def test_string_fallback(self):
        assert synthesize({"type": "string"}, {}, name="foobar") == "${fake:word}"


# ── synthesize: objects / arrays ──────────────────────────────────────


class TestSynthesizeComposite:
    def test_object_recursion(self):
        schema = {"type": "object", "properties": {
            "email": {"type": "string", "format": "email"},
            "age": {"type": "integer", "minimum": 18, "maximum": 65},
        }}
        result = synthesize(schema, {})
        assert result == {"email": "${fake:email}", "age": "${randint:18:65}"}

    def test_required_only(self):
        schema = {"type": "object",
                  "properties": {"a": {"type": "string"}, "b": {"type": "string"}},
                  "required": ["a"]}
        assert synthesize(schema, {}, required_only=True) == {"a": "${fake:word}"}

    def test_array_one_item(self):
        schema = {"type": "array", "items": {"type": "string", "format": "email"}}
        assert synthesize(schema, {}) == ["${fake:email}"]

    def test_ref_body(self):
        spec = {"components": {"schemas": {"Order": {
            "type": "object", "properties": {"customer_email": {"type": "string", "format": "email"}}}}}}
        assert synthesize({"$ref": "#/components/schemas/Order"}, spec) == {"customer_email": "${fake:email}"}

    def test_nested_object(self):
        schema = {"type": "object", "properties": {
            "user": {"type": "object", "properties": {"name": {"type": "string"}}}}}
        assert synthesize(schema, {}) == {"user": {"name": "${fake:name}"}}


# ── extract_requests: full request scaffolding ────────────────────────


SPEC = {
    "components": {
        "securitySchemes": {"bearerAuth": {"type": "http", "scheme": "bearer"}},
        "schemas": {
            "OrderIn": {
                "type": "object",
                "required": ["product_id", "quantity"],
                "properties": {
                    "product_id": {"type": "integer"},
                    "quantity": {"type": "integer", "minimum": 1, "maximum": 5},
                    "customer_email": {"type": "string", "format": "email"},
                },
            }
        },
    },
    "paths": {
        "/orders": {
            "post": {
                "operationId": "createOrder",
                "summary": "Create order",
                "tags": ["orders"],
                "security": [{"bearerAuth": []}],
                "requestBody": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/OrderIn"}}}},
            }
        },
        "/orders/{id}": {
            "get": {
                "operationId": "getOrder",
                "parameters": [{"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}],
            }
        },
        "/products": {
            "get": {
                "operationId": "listProducts",
                "parameters": [
                    {"name": "category", "in": "query", "required": True, "schema": {"type": "string"}},
                    {"name": "page", "in": "query", "required": False, "schema": {"type": "integer"}},
                ],
            }
        },
    },
}


class TestExtractRequests:
    def _by_op(self, requests, op):
        return next(r for r in requests if r.get("_operation") == op)

    def test_body_synthesized_not_stub(self):
        reqs = extract_requests(SPEC)
        create = self._by_op(reqs, "createOrder")
        assert create["json"] == {
            "product_id": "${randint:1:1000}",
            "quantity": "${randint:1:5}",
            "customer_email": "${fake:email}",
        }
        assert "TODO" not in str(create["json"])

    def test_operation_id_recorded(self):
        reqs = extract_requests(SPEC)
        assert {r.get("_operation") for r in reqs} == {"createOrder", "getOrder", "listProducts"}

    def test_security_flag(self):
        create = self._by_op(extract_requests(SPEC), "createOrder")
        assert create.get("_requires_auth") is True

    def test_path_param_converted(self):
        get = self._by_op(extract_requests(SPEC), "getOrder")
        assert get["path"] == "/orders/${PATH_ID:-1}"
        assert "_comment_path" in get

    def test_required_query_only(self):
        lst = self._by_op(extract_requests(SPEC), "listProducts")
        assert lst["query"] == {"category": "${fake:word}"}  # required kept, optional 'page' dropped

    def test_name_and_tags(self):
        create = self._by_op(extract_requests(SPEC), "createOrder")
        assert create["name"] == "Create order"
        assert create["tags"] == ["orders"]

    def test_name_fallback_to_method_path(self):
        spec = {"paths": {"/health": {"get": {}}}}
        assert extract_requests(spec)[0]["name"] == "GET /health"

    def test_required_only_flag(self):
        reqs = extract_requests(SPEC, required_only=True)
        create = self._by_op(reqs, "createOrder")
        assert set(create["json"]) == {"product_id", "quantity"}  # customer_email optional -> dropped


# ── convert_path_params (unchanged behaviour) ─────────────────────────


class TestConvertPathParams:
    @pytest.mark.parametrize("path,expected", [
        ("/users/{id}", "/users/${PATH_ID:-1}"),
        ("/a/{x}/b/{y}", "/a/${PATH_X:-1}/b/${PATH_Y:-1}"),
        ("/health", "/health"),
    ])
    def test_conversion(self, path, expected):
        assert convert_path_params(path) == expected


# ── auth detection ────────────────────────────────────────────────────

from locomotive.openapi import detect_auth, scaffold_scenario


def _spec(schemes=None, paths=None, global_security=None):
    spec = {"openapi": "3.0.0", "paths": paths or {}}
    if schemes is not None:
        spec["components"] = {"securitySchemes": schemes}
    if global_security is not None:
        spec["security"] = global_security
    return spec


class TestDetectAuth:
    def test_bearer_with_login(self):
        spec = _spec(
            schemes={"bearerAuth": {"type": "http", "scheme": "bearer"}},
            paths={"/auth/login": {"post": {
                "operationId": "login",
                "requestBody": {"content": {"application/json": {"schema": {"type": "object",
                    "properties": {"username": {"type": "string"}, "password": {"type": "string"}}}}}},
                "responses": {"200": {"content": {"application/json": {"schema": {"type": "object",
                    "properties": {"access_token": {"type": "string"}}}}}}},
            }}},
        )
        auth, login = detect_auth(spec)
        assert auth == {"type": "bearer", "token": "${var:token}"}
        assert login["method"] == "POST" and login["path"] == "/auth/login"
        assert login["capture"] == {"token": "access_token"}
        assert login["json"] == {"username": "${TEST_USER}", "password": "${TEST_PASSWORD}"}

    def test_bearer_without_login(self):
        auth, login = detect_auth(_spec(schemes={"b": {"type": "http", "scheme": "bearer"}}))
        assert auth == {"type": "bearer", "token": "${API_TOKEN}"}
        assert login is None

    def test_basic(self):
        auth, login = detect_auth(_spec(schemes={"b": {"type": "http", "scheme": "basic"}}))
        assert auth == {"type": "basic", "username": "${API_USER}", "password": "${API_PASSWORD}"}
        assert login is None

    def test_api_key_header(self):
        auth, _ = detect_auth(_spec(schemes={"k": {"type": "apiKey", "in": "header", "name": "X-Key"}}))
        assert auth == {"type": "api_key", "header": "X-Key", "key": "${API_KEY}"}

    def test_api_key_query_unsupported(self):
        auth, login = detect_auth(_spec(schemes={"k": {"type": "apiKey", "in": "query", "name": "api_key"}}))
        assert auth is None and login is None

    def test_no_schemes(self):
        assert detect_auth(_spec()) == (None, None)

    def test_global_security_selects_scheme(self):
        spec = _spec(
            schemes={"basicAuth": {"type": "http", "scheme": "basic"},
                     "bearerAuth": {"type": "http", "scheme": "bearer"}},
            global_security=[{"basicAuth": []}],
        )
        auth, _ = detect_auth(spec)
        assert auth["type"] == "basic"


class TestTokenPathInference:
    def _login_capture(self, response_schema):
        spec = _spec(
            schemes={"b": {"type": "http", "scheme": "bearer"}},
            paths={"/login": {"post": {"operationId": "login",
                "responses": {"200": {"content": {"application/json": {"schema": response_schema}}}}}}},
        )
        _, login = detect_auth(spec)
        return login["capture"]["token"]

    def test_plain_token(self):
        assert self._login_capture({"type": "object", "properties": {"token": {"type": "string"}}}) == "token"

    def test_access_token(self):
        assert self._login_capture({"type": "object", "properties": {"access_token": {"type": "string"}}}) == "access_token"

    def test_nested_token(self):
        schema = {"type": "object", "properties": {"data": {"type": "object",
            "properties": {"token": {"type": "string"}}}}}
        assert self._login_capture(schema) == "data.token"

    def test_no_token_defaults_with_comment(self):
        spec = _spec(
            schemes={"b": {"type": "http", "scheme": "bearer"}},
            paths={"/login": {"post": {"operationId": "login",
                "responses": {"200": {"content": {"application/json": {"schema": {"type": "object",
                    "properties": {"status": {"type": "string"}}}}}}}}}},
        )
        _, login = detect_auth(spec)
        assert login["capture"]["token"] == "token"
        assert "_comment_capture" in login


class TestScaffoldScenario:
    def test_login_removed_from_requests(self):
        spec = _spec(
            schemes={"b": {"type": "http", "scheme": "bearer"}},
            paths={
                "/auth/login": {"post": {"operationId": "login",
                    "responses": {"200": {"content": {"application/json": {"schema": {"type": "object",
                        "properties": {"token": {"type": "string"}}}}}}}}},
                "/orders": {"get": {"operationId": "listOrders"}},
            },
        )
        result = scaffold_scenario(spec)
        assert result["auth"] == {"type": "bearer", "token": "${var:token}"}
        assert len(result["on_start"]) == 1
        ops = {r.get("_operation") for r in result["requests"]}
        assert ops == {"listOrders"}  # login op moved to on_start, not a flat request

    def test_no_auth_spec(self):
        result = scaffold_scenario(_spec(paths={"/health": {"get": {"operationId": "health"}}}))
        assert "auth" not in result and "on_start" not in result
        assert result["requests"][0]["_operation"] == "health"


# ── flow inference (CRUD) ─────────────────────────────────────────────


def _crud_spec(extra_paths=None, create_response=None):
    paths = {
        "/orders": {
            "post": {"operationId": "createOrder", "summary": "Create order",
                "requestBody": {"content": {"application/json": {"schema": {"type": "object",
                    "properties": {"product_id": {"type": "integer"}}}}}},
                "responses": {"201": {"content": {"application/json": {"schema":
                    create_response or {"type": "object", "properties": {"id": {"type": "string"}}}}}}}},
            "get": {"operationId": "listOrders", "summary": "List orders"},
        },
        "/orders/{id}": {
            "get": {"operationId": "getOrder", "summary": "Get order",
                "parameters": [{"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}]},
        },
    }
    if extra_paths:
        for p, item in extra_paths.items():
            paths.setdefault(p, {}).update(item)
    return {"openapi": "3.0.0", "paths": paths}


class TestFlowInference:
    def _flows(self, spec):
        return scaffold_scenario(spec).get("flows") or []

    def test_basic_crud_flow(self):
        spec = _crud_spec()
        result = scaffold_scenario(spec)
        flows = result["flows"]
        assert len(flows) == 1
        flow = flows[0]
        assert flow["name"] == "Order"
        names = [s["name"] for s in flow["steps"]]
        assert names == ["Create order", "Get order"]
        # create captures id, item step uses it in the path
        assert flow["steps"][0]["capture"] == {"id": "id"}
        assert flow["steps"][1]["path"] == "/orders/${var:id}"
        assert "_comment_path" not in flow["steps"][1]

    def test_create_step_keeps_body(self):
        flow = self._flows(_crud_spec())[0]
        assert flow["steps"][0]["json"] == {"product_id": "${randint:1:1000}"}

    def test_flow_ops_removed_from_flat_requests(self):
        result = scaffold_scenario(_crud_spec())
        ops = {r.get("_operation") for r in result["requests"]}
        # create + item folded into flow; only the collection GET (list) stays flat
        assert ops == {"listOrders"}

    def test_id_field_inferred_from_response(self):
        spec = _crud_spec(create_response={"type": "object", "properties": {"order_id": {"type": "string"}}})
        flow = self._flows(spec)[0]
        assert flow["steps"][0]["capture"] == {"id": "order_id"}

    def test_subresource_shares_captured_id(self):
        spec = _crud_spec(extra_paths={"/orders/{id}/pay": {
            "post": {"operationId": "payOrder", "summary": "Pay order",
                "parameters": [{"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}]}}})
        flow = self._flows(spec)[0]
        names = [s["name"] for s in flow["steps"]]
        # order: create, GET (order 1), POST subresource (order 4)
        assert names == ["Create order", "Get order", "Pay order"]
        assert flow["steps"][2]["path"] == "/orders/${var:id}/pay"

    def test_delete_ordered_last(self):
        spec = _crud_spec(extra_paths={"/orders/{id}": {
            "delete": {"operationId": "deleteOrder", "summary": "Delete order",
                "parameters": [{"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}]}}})
        flow = self._flows(spec)[0]
        assert flow["steps"][-1]["name"] == "Delete order"

    def test_no_create_no_flow(self):
        spec = {"paths": {"/orders/{id}": {"get": {"operationId": "getOrder",
            "parameters": [{"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}]}}}}
        result = scaffold_scenario(spec)
        assert "flows" not in result
        # item stays a flat request with the PATH placeholder
        assert result["requests"][0]["path"] == "/orders/${PATH_ID:-1}"

    def test_create_without_items_stays_flat(self):
        spec = {"paths": {"/orders": {"post": {"operationId": "createOrder"}}}}
        result = scaffold_scenario(spec)
        assert "flows" not in result
        assert result["requests"][0]["_operation"] == "createOrder"

    def test_resource_name_from_nested_path(self):
        spec = {"paths": {
            "/api/v1/orders": {"post": {"operationId": "c",
                "responses": {"201": {"content": {"application/json": {"schema": {"type": "object",
                    "properties": {"id": {"type": "string"}}}}}}}}},
            "/api/v1/orders/{id}": {"get": {"operationId": "g",
                "parameters": [{"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}]}},
        }}
        assert self._flows(spec)[0]["name"] == "Order"
