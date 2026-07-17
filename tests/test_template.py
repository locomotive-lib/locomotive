import pytest

from locomotive.template import _convert_path_params, _extract_endpoints


# ── _convert_path_params ──────────────────────────────────────────────


class TestConvertPathParams:
    @pytest.mark.parametrize("path,expected", [
        ("/users/{id}", "/users/${PATH_ID:-1}"),
        ("/users/{userId}/orders/{orderId}",
         "/users/${PATH_USERID:-1}/orders/${PATH_ORDERID:-1}"),
        ("/health", "/health"),
        ("/items/{item-id}", "/items/${PATH_ITEM_ID:-1}"),
    ])
    def test_conversion(self, path, expected):
        assert _convert_path_params(path) == expected


# ── _extract_endpoints ────────────────────────────────────────────────


class TestExtractEndpoints:
    def test_path_params_converted(self):
        spec = {
            "paths": {
                "/users/{id}": {
                    "get": {"summary": "Get user"},
                }
            }
        }
        endpoints = _extract_endpoints(spec)
        assert len(endpoints) == 1
        req = endpoints[0]
        assert req["path"] == "/users/${PATH_ID:-1}"
        assert "{" not in req["path"].replace("${", "")
        assert "_comment_path" in req

    def test_name_keeps_template_path(self):
        spec = {"paths": {"/users/{id}": {"delete": {}}}}
        endpoints = _extract_endpoints(spec)
        # No summary/operationId -> default name uses the original path
        assert endpoints[0]["name"] == "DELETE /users/{id}"

    def test_static_path_untouched(self):
        spec = {"paths": {"/health": {"get": {"summary": "Health"}}}}
        endpoints = _extract_endpoints(spec)
        assert endpoints[0]["path"] == "/health"
        assert "_comment_path" not in endpoints[0]
