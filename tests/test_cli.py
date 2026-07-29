import pytest

from locomotive.cli import (
    _exit_code_for_status,
    _gate_status,
    _normalize_mode,
    _parse_int,
    _parse_list,
    _resolve_gate_config,
)


# ── _normalize_mode ───────────────────────────────────────────────────


class TestNormalizeMode:
    @pytest.mark.parametrize("value,expected", [
        ("resilience", "resilience"),
        ("acceptance", "resilience"),
        ("RESILIENCE", "resilience"),
        ("  acceptance  ", "resilience"),
        (None, ""),
        ("invalid", ""),
        ("", ""),
    ])
    def test_modes(self, value, expected):
        assert _normalize_mode(value) == expected


# ── _parse_list ───────────────────────────────────────────────────────


class TestParseList:
    def test_none(self):
        assert _parse_list(None) == []

    def test_list_input(self):
        assert _parse_list(["a", "b", ""]) == ["a", "b"]

    def test_comma_string(self):
        assert _parse_list("api, smoke, ") == ["api", "smoke"]

    def test_single_value(self):
        assert _parse_list(42) == ["42"]


# ── _parse_int ────────────────────────────────────────────────────────


class TestParseInt:
    def test_valid(self):
        assert _parse_int("10", "test") == 10

    def test_none(self):
        assert _parse_int(None, "test") is None

    def test_empty_string(self):
        assert _parse_int("", "test") is None

    def test_invalid(self):
        with pytest.raises(ValueError):
            _parse_int("abc", "test")


# ── _resolve_gate_config ─────────────────────────────────────────────


class TestResolveGateConfig:
    def test_mode_from_analysis(self):
        cfg = {"mode": "resilience", "gate": {"thresholds": {"error_rate": {"fail": 5}}}}
        mode, gate = _resolve_gate_config(cfg)
        assert mode == "resilience"

    def test_mode_from_gate(self):
        cfg = {"gate": {"mode": "acceptance", "thresholds": {"error_rate": {"fail": 0}}}}
        mode, gate = _resolve_gate_config(cfg)
        assert mode == "resilience"  # acceptance maps to resilience

    def test_auto_resilience_from_thresholds(self):
        cfg = {"gate": {"thresholds": {"error_rate": {"fail": 5}}}}
        mode, gate = _resolve_gate_config(cfg)
        assert mode == "resilience"

    def test_resilience_without_thresholds_cleared(self):
        cfg = {"mode": "resilience", "gate": {}}
        mode, gate = _resolve_gate_config(cfg)
        assert mode == ""

    def test_no_mode_no_thresholds(self):
        mode, gate = _resolve_gate_config({})
        assert mode == ""


# ── _gate_status ──────────────────────────────────────────────────────


class TestGateStatus:
    def test_all_pass(self):
        gate_eval = {"results": [{"status": "PASS"}, {"status": "PASS"}]}
        assert _gate_status(gate_eval) == "PASS"

    def test_contains_degradation(self):
        gate_eval = {"results": [{"status": "PASS"}, {"status": "DEGRADATION"}]}
        assert _gate_status(gate_eval) == "DEGRADATION"

    def test_contains_warning(self):
        gate_eval = {"results": [{"status": "PASS"}, {"status": "WARNING"}]}
        assert _gate_status(gate_eval) == "WARNING"

    def test_nothing_evaluated_is_no_data(self):
        # Thresholds were configured but not one of them was actually checked.
        gate_eval = {"results": [{"status": "SKIP"}, {"status": "SKIP"}]}
        assert _gate_status(gate_eval) == "NO_DATA"

    def test_empty_results(self):
        assert _gate_status({"results": []}) == "NO_DATA"
        assert _gate_status({}) == "NO_DATA"

    def test_no_data_beats_warning(self):
        gate_eval = {"results": [{"status": "WARNING"}, {"status": "NO_DATA"}]}
        assert _gate_status(gate_eval) == "NO_DATA"

    def test_degradation_beats_no_data(self):
        gate_eval = {"results": [{"status": "NO_DATA"}, {"status": "DEGRADATION"}]}
        assert _gate_status(gate_eval) == "DEGRADATION"


# ── _exit_code_for_status ─────────────────────────────────────────────


class TestExitCodeForStatus:
    @pytest.mark.parametrize("status,fail_on,expected", [
        ("DEGRADATION", "DEGRADATION", 1),
        ("WARNING", "DEGRADATION", 0),
        ("PASS", "DEGRADATION", 0),
        ("DEGRADATION", "WARNING", 1),
        ("WARNING", "WARNING", 1),
        ("PASS", "WARNING", 0),
    ])
    def test_exit_codes(self, status, fail_on, expected):
        assert _exit_code_for_status(status, fail_on) == expected


# ── init -> diff round trip ───────────────────────────────────────────


class TestInitDiffRoundTrip:
    """A config generated from a spec must not drift against that same spec.

    This was the demo-breaking case: ``loco init --openapi spec.json`` then
    ``loco diff --openapi spec.json`` reported breaking drift and exited 1,
    because ``/users/${PATH_ID:-1}`` was substituted to ``/users/1`` before
    the comparison and matched no spec operation.
    """

    SPEC = {
        "openapi": "3.0.0",
        "info": {"title": "Shop", "version": "1"},
        "servers": [{"url": "https://api.example.com/v1"}],
        "paths": {
            "/orders": {
                "get": {"operationId": "listOrders",
                        "responses": {"200": {"description": "ok"}}},
                "post": {
                    "operationId": "createOrder",
                    "requestBody": {"content": {"application/json": {"schema": {
                        "type": "object", "required": ["product_id"],
                        "properties": {"product_id": {"type": "integer"}}}}}},
                    "responses": {"201": {"description": "ok", "content": {
                        "application/json": {"schema": {"type": "object", "properties": {
                            "id": {"type": "integer"}}}}}}},
                },
            },
            "/products/{sku}": {"get": {"operationId": "getProduct",
                                        "responses": {"200": {"description": "ok"}}}},
        },
    }

    def _run(self, tmp_path, capsys):
        import json

        from locomotive.cli import main

        spec_path = tmp_path / "spec.json"
        spec_path.write_text(json.dumps(self.SPEC), encoding="utf-8")
        cfg_path = tmp_path / "loconfig.json"
        assert main(["init", "--openapi", str(spec_path), "-o", str(cfg_path)]) == 0
        code = main(["--config", str(cfg_path), "diff", "--openapi", str(spec_path)])
        return code, capsys.readouterr().out

    def test_no_breaking_drift(self, tmp_path, capsys):
        code, out = self._run(tmp_path, capsys)
        assert code == 0, out
        assert "0 breaking" in out or "matches the spec" in out

    def test_path_params_are_not_reported_removed(self, tmp_path, capsys):
        _, out = self._run(tmp_path, capsys)
        assert "REMOVED" not in out

    def test_env_placeholder_in_a_path_survives_the_comparison(self, tmp_path, capsys):
        import json

        from locomotive.cli import main

        spec_path = tmp_path / "spec.json"
        spec_path.write_text(json.dumps(self.SPEC), encoding="utf-8")
        cfg_path = tmp_path / "loconfig.json"
        cfg_path.write_text(json.dumps({
            "load": {"host": "http://x"},
            "scenario": {"requests": [
                {"name": "P", "method": "GET", "path": "/v1/products/${SKU:-abc}"},
            ]},
        }), encoding="utf-8")
        code = main(["--config", str(cfg_path), "diff", "--openapi", str(spec_path)])
        out = capsys.readouterr().out
        assert "REMOVED" not in out
        assert code == 0


# ── the run budget reaches the launcher ───────────────────────────────


class _NoOverrides:
    """An args namespace where every override is absent."""

    def __getattr__(self, name):
        return None


class TestRunBudgetPassthrough:
    def _build(self, load):
        from locomotive.cli import _build_locust_config

        return _build_locust_config(_NoOverrides(), {"load": load})

    def test_absent_stays_absent(self):
        # The launcher tells "no key" (derive a budget from run_time) apart
        # from "null" (wait forever). Writing None here would silently turn
        # every default run into an unbounded one.
        assert "timeout" not in self._build({"host": "http://x", "run_time": "1m"})

    def test_configured_budget_is_passed_through(self):
        merged = self._build({"host": "http://x", "run_time": "1m", "timeout": "10m"})
        assert merged["timeout"] == "10m"

    def test_null_is_passed_through(self):
        merged = self._build({"host": "http://x", "run_time": "1m", "timeout": None})
        assert "timeout" in merged and merged["timeout"] is None


class TestDistributionPassthrough:
    def _build(self, load=None, **overrides):
        import types

        from locomotive.cli import _build_locust_config

        args = _NoOverrides()
        for key, value in overrides.items():
            setattr(args, key, value)
        base = {"host": "http://x", "run_time": "1m"}
        base.update(load or {})
        return _build_locust_config(args, {"load": base})

    def test_defaults_are_a_single_sharded_process(self):
        merged = self._build()
        assert merged["processes"] is None
        assert merged["master"] is False and merged["worker"] is False
        assert merged["shard_data"] is True

    def test_config_values_are_carried(self):
        merged = self._build({
            "processes": 8, "expect_workers": 8, "master_host": "10.0.0.5",
            "master_port": 5558,
        })
        assert merged["processes"] == 8
        assert merged["expect_workers"] == 8
        assert merged["master_host"] == "10.0.0.5"
        assert merged["master_port"] == 5558

    def test_a_flag_beats_the_config(self):
        merged = self._build({"processes": 2}, processes="8")
        assert merged["processes"] == 8

    def test_roles_come_from_either_side(self):
        assert self._build({"worker": True})["worker"] is True
        assert self._build(master=True)["master"] is True

    def test_shard_data_can_be_turned_off_in_the_config(self):
        assert self._build({"shard_data": False})["shard_data"] is False

    def test_shard_data_can_be_turned_off_by_flag(self):
        assert self._build(no_shard_data=True)["shard_data"] is False

    def test_the_flag_does_not_turn_sharding_back_on(self):
        # --no-shard-data is store_true: absent means "the config decides",
        # not "shard anyway".
        assert self._build({"shard_data": False}, no_shard_data=False)["shard_data"] is False


class TestWorkerShortCircuit:
    """A worker writes no CSVs, so nothing downstream of the run applies."""

    def _config(self, tmp_path, **load):
        base = {
            "host": "http://x", "users": 1, "spawn_rate": 1, "run_time": "1s",
            "locustfile": str(tmp_path / "dummy.py"),
        }
        base.update(load)
        return {
            "load": base,
            "artifacts": {"storage": str(tmp_path / "artifacts")},
            "scenario": {"requests": [{"name": "H", "method": "GET", "path": "/h"}]},
        }

    def _run_ci(self, tmp_path, monkeypatch, topology, returncode=0):
        import locomotive.cli as cli_module

        def fake_run(storage, run_id, locust_config):
            storage.ensure_run(run_id)
            return {"returncode": returncode, "metrics": {}, "topology": topology}

        monkeypatch.setattr(cli_module, "_run", fake_run)
        args = _NoOverrides()
        args.no_validate = True
        return cli_module.cmd_ci(args, self._config(tmp_path))

    def test_a_worker_that_finished_cleanly_exits_zero(self, tmp_path, monkeypatch):
        # Without the short-circuit this falls through to "no metrics were
        # written, so the run failed" and turns a healthy worker red.
        code = self._run_ci(tmp_path, monkeypatch, {"role": "worker", "load_generators": 1})
        assert code == 0

    def test_a_worker_that_crashed_keeps_its_exit_code(self, tmp_path, monkeypatch):
        code = self._run_ci(
            tmp_path, monkeypatch, {"role": "worker", "load_generators": 1}, returncode=3
        )
        assert code == 3

    def test_a_worker_writes_no_report(self, tmp_path, monkeypatch):
        self._run_ci(tmp_path, monkeypatch, {"role": "worker", "load_generators": 1})
        assert not list((tmp_path / "artifacts").rglob("*.html"))

    def test_a_master_with_no_metrics_still_fails(self, tmp_path, monkeypatch):
        # The short-circuit is for workers only: a master that produced no
        # stats really did fail, and that has to keep being an error.
        code = self._run_ci(tmp_path, monkeypatch, {"role": "master", "load_generators": 4})
        assert code == 1
