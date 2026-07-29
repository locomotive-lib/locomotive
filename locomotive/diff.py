"""Spec-vs-config drift detection for Locomotive (``loco diff``).

Compares a loconfig against an OpenAPI spec and reports where they've drifted:
endpoints the config calls that no longer exist, spec endpoints not covered by
the config, and request bodies/params whose required fields changed.

Config requests are matched to spec operations first by ``_operation``
(operationId stamped by smart generation), then by method + a canonicalized
path (params -> ``*``). Uses the shared traversals from openapi.py (spec side)
and validate.py (config side).
"""
from __future__ import annotations

import re
from typing import Any, Dict, List

from .openapi import spec_operations
from .validate import iter_requests, iter_scenarios

REMOVED = "removed"
ADDED = "added"
CHANGED = "changed"

BREAKING = "breaking"
INFO = "info"


class Finding:
    __slots__ = ("kind", "severity", "location", "message")

    def __init__(self, kind: str, severity: str, location: str, message: str) -> None:
        self.kind = kind
        self.severity = severity
        self.location = location
        self.message = message

    def __repr__(self) -> str:
        return f"Finding({self.kind!r}, {self.severity!r}, {self.location!r}, {self.message!r})"


def _canonical_config_path(path: str) -> str:
    """Collapse config path placeholders (${...}) to '*' for comparison."""
    return re.sub(r"\$\{[^}]+\}", "*", str(path))


def _config_operations(config: Dict[str, Any]) -> List[Dict[str, Any]]:
    ops: List[Dict[str, Any]] = []
    for scope, scenario in iter_scenarios(config):
        for loc, req in iter_requests(scenario, scope):
            # A form-encoded request carries its fields in ``data`` — the spec
            # calls both a requestBody, so both are body fields here. Reading
            # only ``json`` made every form endpoint (an OAuth2 /token, say)
            # look like it was missing every field the spec requires.
            body = req.get("json")
            if not isinstance(body, dict):
                body = req.get("data") if isinstance(req.get("data"), dict) else {}
            query = req.get("query") if isinstance(req.get("query"), dict) else {}
            ops.append({
                "location": loc,
                "operation_id": req.get("_operation") or "",
                "method": str(req.get("method", "GET")).upper(),
                "path": str(req.get("path", "")),
                "canonical": _canonical_config_path(req.get("path", "")),
                "body_fields": {str(k) for k in body if not str(k).startswith("_")},
                "query_fields": {str(k) for k in query},
            })
    return ops


def _op_label(op: Dict[str, Any]) -> str:
    return f"{op['method']} {op['path']}"


def diff_config_spec(config: Dict[str, Any], spec: Dict[str, Any]) -> List[Finding]:
    """Return drift findings between a config and an OpenAPI spec."""
    spec_ops = spec_operations(spec)
    cfg_ops = _config_operations(config)

    spec_by_id = {o["operation_id"]: o for o in spec_ops if o["operation_id"]}
    spec_by_mp: Dict[tuple, Dict[str, Any]] = {}
    for o in spec_ops:
        # Both forms are registered: the spec's server prefix (``/v1``) may
        # live in the config's request paths or in ``load.host``, and neither
        # choice is drift.
        spec_by_mp.setdefault((o["method"], o["canonical"]), o)
        spec_by_mp.setdefault((o["method"], o.get("canonical_bare") or o["canonical"]), o)

    matched: set = set()
    findings: List[Finding] = []

    for c in cfg_ops:
        match = None
        stale_id = ""
        if c["operation_id"] and c["operation_id"] in spec_by_id:
            match = spec_by_id[c["operation_id"]]
        else:
            match = spec_by_mp.get((c["method"], c["canonical"]))
            if c["operation_id"] and match is not None:
                # The path still resolves, so this is not a dead route — but
                # the operationId the config was generated against is gone.
                # Falling through silently is how a renamed operation stays
                # invisible until the next regeneration overwrites the edits.
                stale_id = c["operation_id"]

        if match is None:
            findings.append(Finding(
                REMOVED, BREAKING, c["location"],
                f"{_op_label(c)} is not in the spec (endpoint removed or renamed)",
            ))
            continue

        matched.add(id(match))

        if stale_id:
            new_id = match["operation_id"]
            became = f"renamed to '{new_id}'" if new_id else "no longer has an operationId"
            findings.append(Finding(
                CHANGED, INFO, c["location"],
                f"{_op_label(match)}: _operation '{stale_id}' is not in the spec "
                f"(matched by path; the operation {became})",
            ))

        missing = match["required_body"] - c["body_fields"]
        if missing:
            findings.append(Finding(
                CHANGED, BREAKING, c["location"],
                f"{_op_label(match)}: spec requires body field(s) "
                f"{sorted(missing)} missing from the request",
            ))
        missing_q = match["required_query"] - c["query_fields"]
        if missing_q:
            findings.append(Finding(
                CHANGED, BREAKING, c["location"],
                f"{_op_label(match)}: spec requires query param(s) "
                f"{sorted(missing_q)} missing from the request",
            ))
        extra = c["body_fields"] - match["body_fields"]
        if extra and match["body_fields"]:
            findings.append(Finding(
                CHANGED, INFO, c["location"],
                f"{_op_label(match)}: request sends field(s) {sorted(extra)} "
                "not in the spec (removed field?)",
            ))

    for o in spec_ops:
        if id(o) in matched:
            continue
        label = o["operation_id"] or _op_label(o)
        findings.append(Finding(
            ADDED, INFO, _op_label(o),
            f"{label} is in the spec but not covered by the config",
        ))

    return findings


def has_breaking(findings: List[Finding]) -> bool:
    return any(f.severity == BREAKING for f in findings)


def format_findings(findings: List[Finding]) -> str:
    """Human-readable diff report."""
    if not findings:
        return "✓ Config matches the spec."
    order = {REMOVED: 0, CHANGED: 1, ADDED: 2}
    findings = sorted(findings, key=lambda f: (order.get(f.kind, 9), f.severity != BREAKING))
    lines: List[str] = []
    label = {REMOVED: "REMOVED", CHANGED: "CHANGED", ADDED: "ADDED  "}
    for f in findings:
        mark = "!" if f.severity == BREAKING else " "
        lines.append(f" {mark} {label.get(f.kind, f.kind.upper())} {f.location}: {f.message}")
    breaking = sum(1 for f in findings if f.severity == BREAKING)
    info = len(findings) - breaking
    lines.append(f"{breaking} breaking, {info} informational")
    return "\n".join(lines)
