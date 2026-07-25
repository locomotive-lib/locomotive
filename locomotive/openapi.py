"""OpenAPI parsing and smart value synthesis for config generation.

Turns an OpenAPI spec into request scaffolding where request bodies and query
parameters are pre-filled with sensible Locomotive placeholders
(``${fake:...}``, ``${randint:...}``, ``${uuid}``, ``${choice:...}`` ...), so the
generated config is close to runnable instead of a bag of ``TODO`` stubs.

The normalized parsing here (``$ref`` resolution, request/response schema
extraction) is intentionally a standalone module so it can later be reused by
``loco diff`` (spec-vs-config drift) and operationId-referencing scenarios.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

_MAX_DEPTH = 6

_PATH_PARAM_RE = re.compile(r"\{([^{}/]+)\}")

# JSON-schema ``format`` -> placeholder
_FORMAT_MAP = {
    "email": "${fake:email}",
    "uuid": "${uuid}",
    "date-time": "${now}",
    "date": "${now:%Y-%m-%d}",
    "password": "${fake:digits:12}",
    "uri": "${fake:domain}",
    "url": "${fake:domain}",
    "hostname": "${fake:domain}",
}

# Field-name substring -> placeholder. Checked in order, so more specific
# names must precede generic ones (``first_name`` before ``name``).
_NAME_RULES = [
    ("first_name", "${fake:first_name}"),
    ("firstname", "${fake:first_name}"),
    ("last_name", "${fake:last_name}"),
    ("lastname", "${fake:last_name}"),
    ("full_name", "${fake:name}"),
    ("username", "${fake:username}"),
    ("user_name", "${fake:username}"),
    ("nickname", "${fake:username}"),
    ("login", "${fake:username}"),
    ("email", "${fake:email}"),
    ("password", "${fake:digits:12}"),
    ("passwd", "${fake:digits:12}"),
    ("phone", "${fake:phone}"),
    ("mobile", "${fake:phone}"),
    ("city", "${fake:city}"),
    ("country", "${fake:country}"),
    ("address", "${fake:address}"),
    ("street", "${fake:address}"),
    ("title", "${fake:words:3}"),
    ("description", "${fake:sentence}"),
    ("comment", "${fake:sentence}"),
    ("message", "${fake:sentence}"),
    ("body", "${fake:sentence}"),
    ("url", "${fake:domain}"),
    ("domain", "${fake:domain}"),
    ("uuid", "${uuid}"),
    ("token", "${uuid}"),
    ("quantity", "${randint:1:10}"),
    ("count", "${randint:1:10}"),
    ("amount", "${randint:1:100}"),
    ("age", "${randint:18:80}"),
    ("price", "${randint:1:1000}"),
    ("name", "${fake:name}"),  # generic name — keep last
]

_HTTP_METHODS = ("get", "post", "put", "patch", "delete")


# ── spec loading ──────────────────────────────────────────────────────


def load_spec(path: Path) -> Dict[str, Any]:
    """Load an OpenAPI/Swagger spec from a JSON or YAML file."""
    content = Path(path).read_text(encoding="utf-8")
    if str(path).lower().endswith((".yml", ".yaml")):
        try:
            import yaml  # type: ignore
        except ImportError as exc:
            raise RuntimeError("PyYAML is required for YAML OpenAPI specs") from exc
        data = yaml.safe_load(content)
    else:
        data = json.loads(content)
    return data if isinstance(data, dict) else {}


# ── $ref / allOf resolution ───────────────────────────────────────────


def _deref(ref: str, spec: Dict[str, Any]) -> Dict[str, Any]:
    """Resolve a local ``#/...`` JSON pointer against the spec."""
    if not isinstance(ref, str) or not ref.startswith("#/"):
        return {}
    node: Any = spec
    for part in ref[2:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        if isinstance(node, dict):
            node = node.get(part)
        else:
            return {}
    return node if isinstance(node, dict) else {}


def resolve(schema: Any, spec: Dict[str, Any], depth: int = 0) -> Dict[str, Any]:
    """Resolve ``$ref``, merge ``allOf``, pick first ``oneOf``/``anyOf`` variant."""
    if not isinstance(schema, dict) or depth > _MAX_DEPTH:
        return {}
    if "$ref" in schema:
        return resolve(_deref(schema["$ref"], spec), spec, depth + 1)
    if "allOf" in schema and isinstance(schema["allOf"], list):
        props: Dict[str, Any] = {}
        required: List[str] = []
        merged: Dict[str, Any] = {}
        for sub in schema["allOf"]:
            r = resolve(sub, spec, depth + 1)
            props.update(r.get("properties") or {})
            required.extend(r.get("required") or [])
            for key, value in r.items():
                if key not in ("properties", "required", "allOf"):
                    merged.setdefault(key, value)
        for key, value in schema.items():
            if key != "allOf":
                merged.setdefault(key, value)
        merged["type"] = "object"
        if props:
            merged["properties"] = props
        if required:
            merged["required"] = sorted(set(required))
        return merged
    for combiner in ("oneOf", "anyOf"):
        variants = schema.get(combiner)
        if isinstance(variants, list) and variants:
            return resolve(variants[0], spec, depth + 1)
    return schema


# ── value synthesis ───────────────────────────────────────────────────


def _int_range(schema: Dict[str, Any]) -> str:
    lo = schema.get("minimum")
    hi = schema.get("maximum")
    lo = int(lo) if isinstance(lo, (int, float)) else 1
    hi = int(hi) if isinstance(hi, (int, float)) else 1000
    if lo > hi:
        lo, hi = 1, 1000
    return f"${{randint:{lo}:{hi}}}"


def _scalar_placeholder(name: str, schema: Dict[str, Any]) -> Any:
    """Pick a placeholder for a scalar field from its name/format/type."""
    fmt = schema.get("format")
    typ = schema.get("type")
    lname = (name or "").lower()

    if fmt in _FORMAT_MAP:
        return _FORMAT_MAP[fmt]
    if lname == "id" or lname.endswith("_id"):
        if typ == "string":
            return "${uuid}" if fmt == "uuid" else "${randint:1:1000}"
        return "${randint:1:1000}"
    # Explicit numeric bounds in the schema beat a name-based guess.
    if typ in ("integer", "number") and ("minimum" in schema or "maximum" in schema):
        return _int_range(schema)
    for sub, placeholder in _NAME_RULES:
        if sub in lname:
            return placeholder
    if typ in ("integer", "number"):
        return _int_range(schema)
    if typ == "boolean":
        return "${fake:bool}"
    return "${fake:word}"


def synthesize(
    schema: Any,
    spec: Dict[str, Any],
    name: str = "",
    depth: int = 0,
    required_only: bool = False,
) -> Any:
    """Build an example value for a JSON schema.

    Priority: explicit ``example``/``default``/``examples`` from the spec →
    ``enum`` (``${choice:...}``) → object/array recursion → scalar placeholder
    by ``format`` → field-name heuristic → ``type``.
    """
    schema = resolve(schema, spec, depth)
    if not schema or depth > _MAX_DEPTH:
        return "${fake:word}"

    if "example" in schema:
        return schema["example"]
    if "default" in schema:
        return schema["default"]
    examples = schema.get("examples")
    if isinstance(examples, list) and examples:
        return examples[0]
    if isinstance(examples, dict) and examples:
        first = next(iter(examples.values()))
        if isinstance(first, dict) and "value" in first:
            return first["value"]

    enum = schema.get("enum")
    if isinstance(enum, list) and enum:
        return "${choice:" + ",".join(str(v) for v in enum) + "}"

    typ = schema.get("type")
    if typ == "object" or "properties" in schema:
        props = schema.get("properties") or {}
        required = set(schema.get("required") or [])
        obj: Dict[str, Any] = {}
        for pname, pschema in props.items():
            if required_only and pname not in required:
                continue
            obj[pname] = synthesize(pschema, spec, pname, depth + 1, required_only)
        return obj
    if typ == "array":
        item = schema.get("items") or {}
        return [synthesize(item, spec, name, depth + 1, required_only)]

    return _scalar_placeholder(name, schema)


# ── path params ───────────────────────────────────────────────────────


def convert_path_params(path: str) -> str:
    """Convert OpenAPI path params to Locomotive placeholders.

    ``/users/{id}`` -> ``/users/${PATH_ID:-1}`` — resolves from the PATH_ID
    environment variable, with ``1`` as a scaffold default so the generated
    config works out of the box. Users replace it with a real value, an env
    var, or a captured variable (``${var:...}``).
    """
    def repl(match: "re.Match[str]") -> str:
        name = re.sub(r"[^A-Za-z0-9]+", "_", match.group(1)).strip("_").upper()
        return "${PATH_" + (name or "PARAM") + ":-1}"

    return _PATH_PARAM_RE.sub(repl, path)


# Backwards-compatible alias (template.py re-exports this name).
_convert_path_params = convert_path_params


# ── request extraction ────────────────────────────────────────────────


def _collect_params(
    operation: Dict[str, Any],
    common_params: List[Any],
    spec: Dict[str, Any],
) -> List[Dict[str, Any]]:
    raw = list(operation.get("parameters") or []) + list(common_params or [])
    out: List[Dict[str, Any]] = []
    for param in raw:
        if isinstance(param, dict) and "$ref" in param:
            param = _deref(param["$ref"], spec)
        if isinstance(param, dict):
            out.append(param)
    return out


def _request_body_schema(operation: Dict[str, Any], spec: Dict[str, Any]) -> Optional[Any]:
    request_body = operation.get("requestBody")
    if isinstance(request_body, dict):
        if "$ref" in request_body:
            request_body = _deref(request_body["$ref"], spec)
        content = request_body.get("content") or {}
        for content_type, media in content.items():
            if "json" in content_type.lower() and isinstance(media, dict):
                return media.get("schema")
    # Swagger 2.0: a parameter with in: body
    for param in operation.get("parameters") or []:
        if isinstance(param, dict) and param.get("in") == "body":
            return param.get("schema")
    return None


def _build_request(
    path: str,
    method: str,
    operation: Dict[str, Any],
    common_params: List[Any],
    spec: Dict[str, Any],
    required_only: bool = False,
) -> Dict[str, Any]:
    summary = operation.get("summary") or ""
    operation_id = operation.get("operationId") or ""

    req: Dict[str, Any] = {
        "name": summary or operation_id or f"{method.upper()} {path}",
        "method": method.upper(),
        "path": convert_path_params(path),
        "weight": 1,
    }
    if operation_id:
        req["_operation"] = operation_id
    if "{" in path:
        req["_comment_path"] = (
            "Path params converted to ${PATH_*:-1} placeholders - set the env "
            "vars, use a real value, or a ${var:...} captured in an earlier step"
        )

    tags = operation.get("tags")
    if isinstance(tags, list) and tags:
        req["tags"] = tags

    params = _collect_params(operation, common_params, spec)
    query: Dict[str, Any] = {}
    for param in params:
        if param.get("in") != "query" or not param.get("required"):
            continue
        pname = param.get("name")
        if pname:
            query[pname] = synthesize(param.get("schema") or param, spec, pname)
    if query:
        req["query"] = query

    body_schema = _request_body_schema(operation, spec)
    if body_schema is not None:
        body = synthesize(body_schema, spec, required_only=required_only)
        if isinstance(body, dict) and not body:
            req["json"] = {"_comment": "TODO: request body schema had no properties"}
        else:
            req["json"] = body

    if operation.get("security"):
        req["_requires_auth"] = True

    return req


def extract_requests(spec: Dict[str, Any], required_only: bool = False) -> List[Dict[str, Any]]:
    """Extract smart request definitions from an OpenAPI spec.

    Each request gets a synthesized body/query and carries ``_operation``
    (the operationId) for later spec-vs-config reconciliation.
    """
    requests: List[Dict[str, Any]] = []
    paths = spec.get("paths") or {}
    for path, path_item in paths.items():
        if not isinstance(path_item, dict):
            continue
        common_params = path_item.get("parameters") or []
        for method in _HTTP_METHODS:
            operation = path_item.get(method)
            if not isinstance(operation, dict):
                continue
            requests.append(
                _build_request(path, method, operation, common_params, spec, required_only)
            )
    return requests


# ── auth detection (securitySchemes) ──────────────────────────────────

# Path substrings that hint an operation is a login/token endpoint, with a
# score so a clearer match (``/login``) beats a vaguer one (``/session``).
_LOGIN_HINTS = [
    (3, "login"),
    (3, "signin"),
    (3, "sign-in"),
    (2, "authenticate"),
    (2, "/token"),
    (2, "/auth"),
    (1, "session"),
]

# Response fields that look like an auth token, most specific first.
_TOKEN_KEYS = [
    "access_token", "accessToken", "auth_token", "authToken",
    "id_token", "idToken", "jwt", "token",
]


def _security_schemes(spec: Dict[str, Any]) -> Dict[str, Any]:
    """Return securitySchemes (OpenAPI 3) or securityDefinitions (Swagger 2)."""
    components = spec.get("components")
    if isinstance(components, dict):
        schemes = components.get("securitySchemes")
        if isinstance(schemes, dict) and schemes:
            return schemes
    legacy = spec.get("securityDefinitions")
    if isinstance(legacy, dict) and legacy:
        return legacy
    return {}


def _primary_security_scheme(spec: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Pick the most relevant security scheme from the spec."""
    schemes = _security_schemes(spec)
    if not schemes:
        return None
    # A globally-applied scheme wins.
    global_security = spec.get("security")
    if isinstance(global_security, list):
        for requirement in global_security:
            if isinstance(requirement, dict):
                for name in requirement:
                    if name in schemes and isinstance(schemes[name], dict):
                        return schemes[name]
    # Otherwise prefer a token-style scheme, else take the first.
    for scheme in schemes.values():
        if isinstance(scheme, dict) and scheme.get("type") in ("http", "oauth2", "openIdConnect"):
            if scheme.get("type") != "http" or str(scheme.get("scheme", "")).lower() != "basic":
                return scheme
    for scheme in schemes.values():
        if isinstance(scheme, dict):
            return scheme
    return None


def _find_login(spec: Dict[str, Any]) -> Optional[tuple]:
    """Find the best POST operation that looks like a login/token endpoint."""
    best_score = 0
    best: Optional[tuple] = None
    for path, item in (spec.get("paths") or {}).items():
        if not isinstance(item, dict):
            continue
        operation = item.get("post")
        if not isinstance(operation, dict):
            continue
        lpath = path.lower()
        score = max((s for s, hint in _LOGIN_HINTS if hint in lpath), default=0)
        if score > best_score:
            best_score, best = score, (path, operation)
    return best


def _success_response_schema(operation: Dict[str, Any], spec: Dict[str, Any]) -> Optional[Any]:
    responses = operation.get("responses") or {}
    for code in ("200", "201", 200, 201):
        resp = responses.get(code)
        if not isinstance(resp, dict):
            continue
        if "$ref" in resp:
            resp = _deref(resp["$ref"], spec)
        content = resp.get("content") or {}
        for content_type, media in content.items():
            if "json" in content_type.lower() and isinstance(media, dict):
                return media.get("schema")
        if "schema" in resp:  # Swagger 2.0
            return resp["schema"]
    return None


def _find_token_path(schema: Any, spec: Dict[str, Any], depth: int = 0, prefix: str = "") -> Optional[str]:
    """Dot-path to a token-like field in a response schema (e.g. 'data.token')."""
    schema = resolve(schema, spec, depth)
    props = schema.get("properties") or {}
    lowered = {k.lower(): k for k in props}
    for key in _TOKEN_KEYS:
        if key.lower() in lowered:
            return prefix + lowered[key.lower()]
    if depth < 2:
        for pname, pschema in props.items():
            found = _find_token_path(pschema, spec, depth + 1, prefix + pname + ".")
            if found:
                return found
    return None


def _login_body(schema: Optional[Any], spec: Dict[str, Any]) -> Dict[str, Any]:
    """Synthesize a login body, but wire credential fields to env vars.

    Real login needs real credentials, so username/email/password map to
    ${TEST_USER} / ${TEST_PASSWORD} (set them as CI secrets) rather than
    random synthetic values that would never authenticate.
    """
    if schema is None:
        return {"username": "${TEST_USER}", "password": "${TEST_PASSWORD}"}
    body = synthesize(schema, spec)
    if isinstance(body, dict):
        for key in list(body):
            lkey = key.lower()
            if "pass" in lkey:
                body[key] = "${TEST_PASSWORD}"
            elif "user" in lkey or "email" in lkey or "login" in lkey:
                body[key] = "${TEST_USER}"
    return body if isinstance(body, dict) else {"username": "${TEST_USER}", "password": "${TEST_PASSWORD}"}


def _build_login_step(path: str, operation: Dict[str, Any], spec: Dict[str, Any]) -> tuple:
    """Build an on_start login request that captures the token. Returns (step, var)."""
    var = "token"
    token_path = _find_token_path(_success_response_schema(operation, spec), spec) or "token"
    step: Dict[str, Any] = {
        "name": operation.get("summary") or "Login",
        "method": "POST",
        "path": convert_path_params(path),
        "json": _login_body(_request_body_schema(operation, spec), spec),
        "capture": {var: token_path},
    }
    if operation.get("operationId"):
        step["_operation"] = operation["operationId"]
    if token_path == "token":
        step["_comment_capture"] = (
            "Could not infer the token field from the login response schema; "
            "adjust 'capture' to the real path (e.g. data.access_token)"
        )
    return step, var


def detect_auth(spec: Dict[str, Any]) -> tuple:
    """Detect auth from securitySchemes.

    Returns (auth_block, login_step). auth_block goes into scenario.auth;
    login_step (or None) is an on_start request that captures a token.
    """
    scheme = _primary_security_scheme(spec)
    if not isinstance(scheme, dict):
        return None, None
    typ = scheme.get("type")

    if typ == "http" and str(scheme.get("scheme", "")).lower() == "basic":
        return {"type": "basic", "username": "${API_USER}", "password": "${API_PASSWORD}"}, None
    if typ == "apiKey":
        if scheme.get("in") == "header":
            return {
                "type": "api_key",
                "header": scheme.get("name") or "X-API-Key",
                "key": "${API_KEY}",
            }, None
        return None, None  # query/cookie api keys aren't expressible in scenario.auth

    # bearer / oauth2 / openIdConnect -> token; try to wire a login flow
    login = _find_login(spec)
    if login:
        step, var = _build_login_step(login[0], login[1], spec)
        return {"type": "bearer", "token": "${var:%s}" % var}, step
    return {"type": "bearer", "token": "${API_TOKEN}"}, None


# ── flow inference (CRUD sequences) ───────────────────────────────────

_FIRST_PARAM_RE = re.compile(r"^(.*?)/\{([^{}/]+)\}")

# Order of item/sub-resource steps within an inferred flow.
_STEP_ORDER = {"GET": 1, "PUT": 2, "PATCH": 3, "POST": 4, "DELETE": 5}


def _first_param(path: str):
    """Return (collection_base, first_param_name) for an item path, else None."""
    match = _FIRST_PARAM_RE.search(path)
    if match:
        return match.group(1), match.group(2)
    return None


def _op_key(path: str, method: str, operation: Dict[str, Any]) -> tuple:
    op_id = operation.get("operationId")
    return ("op", op_id) if op_id else ("mp", method.upper(), convert_path_params(path))


def _response_id_field(operation: Dict[str, Any], spec: Dict[str, Any]) -> str:
    """Field in the create response that holds the new resource id."""
    schema = _success_response_schema(operation, spec)
    schema = resolve(schema, spec) if schema else {}
    props = schema.get("properties") or {}
    if "id" in props:
        return "id"
    for key in props:
        if key.lower().endswith("_id"):
            return key
    if "uuid" in props:
        return "uuid"
    return "id"


def _resource_name(base: str) -> str:
    segment = [s for s in base.split("/") if s and "{" not in s]
    word = segment[-1] if segment else "resource"
    word = re.sub(r"[^A-Za-z0-9]+", " ", word).strip()
    if word.lower().endswith("s") and len(word) > 1:
        word = word[:-1]
    return word[:1].upper() + word[1:] if word else "Resource"


def _wire_path_var(converted_path: str, param: str) -> str:
    token = re.sub(r"[^A-Za-z0-9]+", "_", param).strip("_").upper()
    placeholder = "${PATH_" + (token or "PARAM") + ":-1}"
    return converted_path.replace(placeholder, "${var:%s}" % param)


def _collect_ops(spec: Dict[str, Any]) -> List[tuple]:
    ops: List[tuple] = []
    for path, item in (spec.get("paths") or {}).items():
        if not isinstance(item, dict):
            continue
        common = item.get("parameters") or []
        for method in _HTTP_METHODS:
            operation = item.get(method)
            if isinstance(operation, dict):
                ops.append((path, method, operation, common))
    return ops


def _infer_flows(spec: Dict[str, Any], required_only: bool = False) -> tuple:
    """Infer CRUD flows: POST /res (capture id) -> {GET,PUT,PATCH,DELETE} /res/{id}.

    Returns (flows, consumed_keys) where consumed_keys marks the operations
    folded into flows so they can be dropped from the flat request list.
    """
    groups: Dict[str, Dict[str, Any]] = {}
    for path, method, operation, common in _collect_ops(spec):
        fp = _first_param(path)
        if fp is None:
            if method == "post":
                g = groups.setdefault(path, {"param": None, "create": None, "items": []})
                if g["create"] is None:
                    g["create"] = (path, method, operation, common)
        else:
            base, param = fp
            g = groups.setdefault(base, {"param": None, "create": None, "items": []})
            if g["param"] is None:
                g["param"] = param
            g["items"].append((path, method, operation, common))

    flows: List[Dict[str, Any]] = []
    consumed: set = set()
    for base, g in groups.items():
        if not g["create"] or not g["items"]:
            continue
        param = g["param"] or "id"

        c_path, c_method, c_op, c_common = g["create"]
        create_step = _build_request(c_path, c_method, c_op, c_common, spec, required_only)
        create_step["capture"] = {param: _response_id_field(c_op, spec)}
        steps = [create_step]
        consumed.add(_op_key(c_path, c_method, c_op))

        for i_path, i_method, i_op, i_common in sorted(
            g["items"], key=lambda t: (_STEP_ORDER.get(t[1].upper(), 9), t[0])
        ):
            step = _build_request(i_path, i_method, i_op, i_common, spec, required_only)
            step["path"] = _wire_path_var(step["path"], param)
            if "${PATH_" not in step["path"]:
                step.pop("_comment_path", None)
            steps.append(step)
            consumed.add(_op_key(i_path, i_method, i_op))

        flows.append({
            "name": _resource_name(base),
            "weight": 2,
            "_comment": "Inferred from CRUD paths - review the step order and captured id",
            "steps": steps,
        })

    return flows, consumed


def scaffold_scenario(spec: Dict[str, Any], required_only: bool = False) -> Dict[str, Any]:
    """Build the scenario pieces inferred from a spec: requests, auth, on_start, flows.

    Operations folded into a login on_start or a CRUD flow are removed from the
    flat requests so they aren't also run as standalone weighted tasks.
    """
    requests = extract_requests(spec, required_only)
    result: Dict[str, Any] = {}
    consumed: set = set()

    auth, login_step = detect_auth(spec)
    if auth:
        result["auth"] = auth
    if login_step:
        result["on_start"] = [login_step]
        consumed.add(("mp", "POST", login_step["path"]))

    flows, flow_consumed = _infer_flows(spec, required_only)
    if flows:
        result["flows"] = flows
        consumed |= flow_consumed

    def _is_consumed(req: Dict[str, Any]) -> bool:
        op_id = req.get("_operation")
        if op_id and ("op", op_id) in consumed:
            return True
        return ("mp", req.get("method"), req.get("path")) in consumed

    result["requests"] = [r for r in requests if not _is_consumed(r)]
    return result


# ── normalized operations (for loco diff) ─────────────────────────────


def _canonical_path(path: str) -> str:
    """Collapse OpenAPI path params to '*' so paths can be compared."""
    return re.sub(r"\{[^{}]+\}", "*", path)


def spec_operations(spec: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Normalized operations from a spec, for spec-vs-config diffing.

    Each entry: operation_id, method, path, canonical (path with params ->
    '*'), body_fields, required_body, required_query.
    """
    operations: List[Dict[str, Any]] = []
    for path, item in (spec.get("paths") or {}).items():
        if not isinstance(item, dict):
            continue
        common = item.get("parameters") or []
        for method in _HTTP_METHODS:
            operation = item.get(method)
            if not isinstance(operation, dict):
                continue
            body_schema = _request_body_schema(operation, spec)
            body_schema = resolve(body_schema, spec) if body_schema else {}
            props = body_schema.get("properties") or {}
            params = _collect_params(operation, common, spec)
            required_query = {
                p.get("name") for p in params
                if p.get("in") == "query" and p.get("required") and p.get("name")
            }
            operations.append({
                "operation_id": operation.get("operationId") or "",
                "method": method.upper(),
                "path": path,
                "canonical": _canonical_path(path),
                "body_fields": set(props.keys()),
                "required_body": set(body_schema.get("required") or []),
                "required_query": required_query,
            })
    return operations
