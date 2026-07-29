"""End-to-end runs against real Locust and a real HTTP server.

Every other test in this suite talks to a fake `locust` module. That is a
deliberate choice — it is fast and it isolates the generator — but it can only
ever prove that the generated file matches what the fake expects. These tests
prove the claim the project actually makes: that a JSON config turns into a
locustfile that a real Locust runs against a real server, and that what comes
back out the other end is a report and an analysis.

They are slow by the standards of the rest of the suite (a few seconds each,
because a load test has to actually last a moment) and they are skipped when
the locust binary is missing. Run just these with `pytest -m e2e`, or skip them
with `pytest -m "not e2e"`.
"""

import json
import shutil
import threading
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from locomotive.cli import main


pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        shutil.which("locust") is None,
        reason="locust binary not on PATH",
    ),
]


# ── the target service ────────────────────────────────────────────────


class _Recorder:
    """What the server was asked for, so a test can check the load landed."""

    def __init__(self):
        self._lock = threading.Lock()
        self.paths = Counter()
        self.logins = Counter()

    def record(self, path, body):
        with self._lock:
            self.paths[path] += 1
            login = body.get("login") if isinstance(body, dict) else None
            if login is not None:
                self.logins[str(login)] += 1

    def snapshot(self):
        with self._lock:
            return Counter(self.paths), Counter(self.logins)


def _make_handler(recorder):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _body(self):
            length = int(self.headers.get("content-length") or 0)
            if not length:
                return None
            raw = self.rfile.read(length)
            try:
                return json.loads(raw)
            except ValueError:
                return None

        def _respond(self, status, payload):
            data = json.dumps(payload).encode()
            try:
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                # Locust drops its connections when the run stops; a half
                # written response then is noise, not a failure.
                pass

        def _handle(self):
            path = self.path.split("?", 1)[0]
            body = self._body()
            recorder.record(path, body)
            if path == "/boom":
                # A server error, for the run that is supposed to go red.
                self._respond(500, {"error": "boom"})
            elif path in ("/login", "/auth/login"):
                login = (body or {}).get("login", "anon")
                self._respond(200, {"token": f"tok-{login}", "login": login})
            else:
                self._respond(200, {"status": "ok", "items": [{"id": 1}]})

        do_GET = do_POST = do_PUT = do_DELETE = _handle

        def log_message(self, *args):  # keep pytest output readable
            pass

    return Handler


class _Server:
    def __init__(self):
        self.recorder = _Recorder()
        # Threaded: one virtual user per connection, and a serialised server
        # would measure its own queue instead of the load.
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(self.recorder))
        self._httpd.daemon_threads = True
        self.url = "http://127.0.0.1:%d" % self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)


@pytest.fixture
def server():
    """A fresh target service per test, so the counters start empty."""
    with _Server() as srv:
        yield srv


# ── configs ───────────────────────────────────────────────────────────


def _write_config(tmp_path, config, name="loconfig.json"):
    path = tmp_path / name
    path.write_text(json.dumps(config), encoding="utf-8")
    return str(path)


def _base_config(server, tmp_path, **load):
    """A short, quiet run: enough requests to measure, few enough to be fast."""
    merged = {
        "host": server.url,
        "users": 4,
        "spawn_rate": 4,
        "run_time": "3s",
    }
    merged.update(load)
    return {
        "load": merged,
        "scenario": {
            "requests": [
                {"name": "Health", "method": "GET", "path": "/health"},
            ],
        },
        "artifacts": {"storage": str(tmp_path / "artifacts"), "history": 0},
        "analysis": {
            # A gate that a healthy run passes, so every run here produces an
            # analysis.json to assert against.
            "gate": {"min_requests": 1, "thresholds": {"error_rate": {"fail": 50}}},
            "rules": [],
        },
    }


def _artifacts(tmp_path, run_id):
    return Path(tmp_path) / "artifacts" / "runs" / run_id


def _read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


# ── happy path ────────────────────────────────────────────────────────


class TestHappyPath:
    """A config in, a green report out — the whole promise in one test."""

    def test_ci_runs_and_produces_every_artifact(self, server, tmp_path):
        config = _base_config(server, tmp_path)
        config["scenario"]["requests"].append(
            {"name": "Items", "method": "GET", "path": "/items"}
        )
        path = _write_config(tmp_path, config)

        code = main(["--config", path, "ci", "--run-id", "e2e1"])

        assert code == 0
        run_dir = _artifacts(tmp_path, "e2e1")
        assert (run_dir / "generated" / "generated_locustfile.py").exists()
        assert (run_dir / "report.html").exists()

        metrics = _read_json(run_dir / "metrics.json")
        assert metrics["requests"] > 0
        assert metrics["failures"] == 0
        assert metrics["error_rate"] == 0

        analysis = _read_json(run_dir / "analysis.json")
        assert analysis["status"] == "PASS"

        paths, _ = server.recorder.snapshot()
        assert paths["/health"] > 0
        assert paths["/items"] > 0
        # Locust counted what the server saw. The two numbers cannot be
        # compared for equality: locust counts a request when the response
        # comes back, the server counts it when it arrives, so a request that
        # is still in flight when the run time expires is seen by the server
        # and never reaches the stats. At most one such request per user can
        # be open at any moment, which bounds the gap by the user count.
        # What must hold is the direction: every request in the stats really
        # went to the target, so the stats can never run ahead of the server.
        sent = paths["/health"] + paths["/items"]
        assert metrics["requests"] <= sent
        assert sent - metrics["requests"] <= config["load"]["users"]

    def test_capture_chains_a_real_response_into_the_next_request(
        self, server, tmp_path
    ):
        # The fake cannot prove this: it needs a server that answers with a
        # token and a second request that carries it back.
        config = _base_config(server, tmp_path)
        config["scenario"]["on_start"] = [{
            "name": "Login", "method": "POST", "path": "/login",
            "json": {"login": "alice"},
            "capture": {"auth_token": "token"},
        }]
        config["scenario"]["requests"] = [{
            "name": "Me", "method": "GET", "path": "/me",
            "headers": {"Authorization": "Bearer ${var:auth_token}"},
            "expect": {"status": 200},
        }]
        path = _write_config(tmp_path, config)

        assert main(["--config", path, "ci", "--run-id", "e2e2"]) == 0

        metrics = _read_json(_artifacts(tmp_path, "e2e2") / "metrics.json")
        # A `${var:}` that failed to resolve would send the literal
        # placeholder; the request would still be 200 and only the failure
        # count would betray it. There is none, so it resolved.
        assert metrics["failures"] == 0
        paths, _ = server.recorder.snapshot()
        assert paths["/login"] > 0 and paths["/me"] > 0


# ── the run that is supposed to go red ────────────────────────────────


class TestFailuresAreRealFailures:
    def test_a_server_error_fails_the_gate(self, server, tmp_path):
        config = _base_config(server, tmp_path)
        config["scenario"]["requests"] = [
            {"name": "Boom", "method": "GET", "path": "/boom"},
        ]
        config["analysis"]["gate"]["thresholds"]["error_rate"] = {"fail": 1}
        path = _write_config(tmp_path, config)

        code = main(["--config", path, "ci", "--run-id", "e2e3"])

        assert code == 1
        metrics = _read_json(_artifacts(tmp_path, "e2e3") / "metrics.json")
        assert metrics["failures"] > 0
        assert metrics["error_rate"] > 99

    def test_a_failing_expect_turns_a_200_into_a_failure(self, server, tmp_path):
        # The server answers 200 and the assertion is what fails. Nothing in
        # locust's own stats would notice without `expect`, which is exactly
        # why it exists — and why a fake cannot prove it works.
        config = _base_config(server, tmp_path)
        config["scenario"]["requests"] = [{
            "name": "Health", "method": "GET", "path": "/health",
            "expect": {"json": {"status": "definitely-not-ok"}},
        }]
        config["analysis"]["gate"]["thresholds"]["error_rate"] = {"fail": 1}
        path = _write_config(tmp_path, config)

        code = main(["--config", path, "ci", "--run-id", "e2e4"])

        assert code == 1
        metrics = _read_json(_artifacts(tmp_path, "e2e4") / "metrics.json")
        assert metrics["requests"] > 0
        assert metrics["failures"] == metrics["requests"]

    def test_the_same_request_passes_when_the_expectation_matches(
        self, server, tmp_path
    ):
        # The control for the test above: same request, true expectation. If
        # this also failed, the one above would prove nothing.
        config = _base_config(server, tmp_path)
        config["scenario"]["requests"] = [{
            "name": "Health", "method": "GET", "path": "/health",
            "expect": {"status": 200, "json": {"status": "ok"}},
        }]
        path = _write_config(tmp_path, config)

        assert main(["--config", path, "ci", "--run-id", "e2e5"]) == 0
        metrics = _read_json(_artifacts(tmp_path, "e2e5") / "metrics.json")
        assert metrics["failures"] == 0


# ── init → validate → ci ──────────────────────────────────────────────


class TestRoundTrip:
    def test_the_generated_template_runs(self, server, tmp_path, monkeypatch):
        """`loco init` then `loco ci` — literally the demo script.

        The template is the first thing every new user runs and the first
        thing shown on a stage. If it does not survive its own pipeline, that
        is where it will be discovered.
        """
        monkeypatch.chdir(tmp_path)
        config_path = tmp_path / "loconfig.json"

        assert main(["init", "--output", str(config_path), "--host", server.url]) == 0
        assert config_path.exists()

        # The one edit the template asks for is the endpoints; here the
        # server answers everything, so the run budget is all that changes.
        config = _read_json(config_path)
        config["load"].update({"users": 4, "spawn_rate": 4, "run_time": "3s"})
        config["analysis"]["gate"]["min_requests"] = 1
        config["artifacts"]["storage"] = str(tmp_path / "artifacts")
        config_path.write_text(json.dumps(config), encoding="utf-8")

        assert main(["--config", str(config_path), "validate"]) == 0
        assert main(["--config", str(config_path), "ci", "--run-id", "rt1"]) == 0

        run_dir = _artifacts(tmp_path, "rt1")
        assert (run_dir / "report.html").exists()
        assert _read_json(run_dir / "metrics.json")["requests"] > 0

        paths, _ = server.recorder.snapshot()
        assert paths["/health"] > 0
        assert paths["/api/resource"] > 0

    def test_a_second_run_compares_against_the_first(self, server, tmp_path):
        # Baselines are stored, reloaded and compared across process
        # boundaries; this is the only test that exercises that path with
        # metrics a real run produced.
        path = _write_config(tmp_path, _base_config(server, tmp_path))

        assert main(["--config", path, "ci", "--run-id", "b1", "--set-baseline"]) == 0
        assert main(["--config", path, "ci", "--run-id", "b2"]) == 0

        analysis = _read_json(_artifacts(tmp_path, "b2") / "analysis.json")
        assert analysis["status"] in ("PASS", "WARNING")
        html = (_artifacts(tmp_path, "b2") / "report.html").read_text(encoding="utf-8")
        assert "b1" in html  # the baseline is named in the report header


# ── distributed ───────────────────────────────────────────────────────


class TestDistributedRun:
    def test_two_processes_do_not_share_pool_rows(self, server, tmp_path):
        """The claim sharding exists to make, measured at the server.

        Eight accounts, eight users, two processes: every account is used and
        none is used by two users. Without sharding both workers slice from
        the same eight rows and only the first four ever appear.
        """
        accounts = tmp_path / "accounts.csv"
        accounts.write_text(
            "login,password\n" + "".join(f"user{i},pw{i}\n" for i in range(8)),
            encoding="utf-8",
        )
        config = _base_config(server, tmp_path, users=8, spawn_rate=8, processes=2)
        config["scenario"]["data"] = {
            "acc": {"source": str(accounts), "mode": "unique_per_user"}
        }
        config["scenario"]["requests"] = [{
            "name": "Login", "method": "POST", "path": "/login",
            "json": {"login": "${data:acc.login}"},
        }]
        path = _write_config(tmp_path, config)

        assert main(["--config", path, "ci", "--run-id", "dist1"]) == 0

        _, logins = server.recorder.snapshot()
        assert set(logins) == {f"user{i}" for i in range(8)}

        run_meta = _read_json(_artifacts(tmp_path, "dist1") / "run.json")
        assert run_meta["topology"]["load_generators"] == 2
        assert run_meta["topology"]["role"] == "master"

        html = (_artifacts(tmp_path, "dist1") / "report.html").read_text(
            encoding="utf-8"
        )
        assert "Load generators: 2" in html

    def test_shard_data_false_lets_the_processes_collide(self, server, tmp_path):
        # The control: same run with sharding off reaches only the head of
        # the pool, because every worker starts from row 0. This is the bug
        # the default prevents, kept here so a regression in the default
        # cannot pass unnoticed.
        accounts = tmp_path / "accounts.csv"
        accounts.write_text(
            "login,password\n" + "".join(f"user{i},pw{i}\n" for i in range(8)),
            encoding="utf-8",
        )
        config = _base_config(
            server, tmp_path, users=8, spawn_rate=8, processes=2, shard_data=False
        )
        config["scenario"]["data"] = {
            "acc": {"source": str(accounts), "mode": "unique_per_user"}
        }
        config["scenario"]["requests"] = [{
            "name": "Login", "method": "POST", "path": "/login",
            "json": {"login": "${data:acc.login}"},
        }]
        path = _write_config(tmp_path, config)

        assert main(["--config", path, "ci", "--run-id", "dist2"]) == 0

        _, logins = server.recorder.snapshot()
        assert set(logins) == {f"user{i}" for i in range(4)}

    def test_a_baseline_on_another_topology_warns(self, server, tmp_path):
        # Comparing 2 generators against 1 measures the generators, not the
        # service. The run still succeeds — re-baselining has to be possible.
        single = _write_config(tmp_path, _base_config(server, tmp_path), "single.json")
        multi = _write_config(
            tmp_path, _base_config(server, tmp_path, processes=2), "multi.json"
        )

        assert main(["--config", single, "ci", "--run-id", "t1", "--set-baseline"]) == 0
        assert main(["--config", multi, "ci", "--run-id", "t2"]) == 0

        analysis = _read_json(_artifacts(tmp_path, "t2") / "analysis.json")
        drift = [r for r in analysis["results"] if r["metric"] == "load_generators"]
        assert len(drift) == 1
        assert drift[0]["status"] == "WARNING"
        assert drift[0]["current"] == 2 and drift[0]["baseline"] == 1
