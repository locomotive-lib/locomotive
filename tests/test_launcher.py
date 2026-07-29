import pytest

from locomotive.launcher import (
    _apply_failure_rates,
    _extract_status_code,
    parse_locust_failures,
    parse_locust_stats,
    parse_locust_stats_history,
)


# ── _extract_status_code ──────────────────────────────────────────────


class TestExtractStatusCode:
    @pytest.mark.parametrize("text,expected", [
        ("HTTPError('503 Service Unavailable')", 503),
        ("HTTPError('404 Not Found')", 404),
        ("ConnectionError('Connection refused')", None),
        ("", None),
    ])
    def test_extract(self, text, expected):
        assert _extract_status_code(text) == expected


# ── parse_locust_stats ────────────────────────────────────────────────


class TestParseLocustStats:
    def test_standard_csv(self, tmp_path):
        csv_content = (
            "Type,Name,Request Count,Failure Count,Median Response Time,"
            "Average Response Time,Min Response Time,Max Response Time,"
            "Average Content Size,Requests/s,Failures/s,50%,66%,75%,80%,"
            "90%,95%,98%,99%,99.9%,99.99%,100%\n"
            'GET,/api/users,500,5,120,125.5,80,450,1024,50.0,0.5,'
            '120,130,140,150,170,200,250,300,400,440,450\n'
            ',"Aggregated",1000,10,115,120.0,75,500,2048,100.0,1.0,'
            '115,125,135,145,165,195,245,295,395,480,500\n'
        )
        csv_path = tmp_path / "locust_stats.csv"
        csv_path.write_text(csv_content)
        metrics = parse_locust_stats(csv_path)
        assert metrics["requests"] == 1000
        assert metrics["failures"] == 10
        assert metrics["avg_ms"] == pytest.approx(120.0)
        assert metrics["rps"] == pytest.approx(100.0)

    def test_fallback_to_first_row(self, tmp_path):
        csv_content = (
            "Type,Name,Request Count,Failure Count,Average Response Time,Requests/s,95%,99%\n"
            "GET,/health,200,0,50.0,20.0,80,100\n"
        )
        csv_path = tmp_path / "locust_stats.csv"
        csv_path.write_text(csv_content)
        metrics = parse_locust_stats(csv_path)
        assert metrics["requests"] == 200


# ── parse_locust_failures ─────────────────────────────────────────────


class TestParseLocustFailures:
    def test_status_code_counting(self, tmp_path):
        csv_content = (
            "Method,Name,Error,Occurrences\n"
            "GET,/api,\"HTTPError('503 Service Unavailable')\",10\n"
            "GET,/api,\"HTTPError('500 Internal Server Error')\",5\n"
            "GET,/api,\"HTTPError('404 Not Found')\",3\n"
            "POST,/api,\"ConnectionError('refused')\",2\n"
        )
        csv_path = tmp_path / "locust_failures.csv"
        csv_path.write_text(csv_content)
        result = parse_locust_failures(csv_path)
        assert result["failures_503"] == 10
        assert result["failures_5xx"] == 15  # 503 + 500
        assert result["failures_4xx"] == 3
        assert result["failures_other"] == 2

    def test_empty_csv(self, tmp_path):
        csv_path = tmp_path / "locust_failures.csv"
        csv_path.write_text("Method,Name,Error,Occurrences\n")
        result = parse_locust_failures(csv_path)
        assert result == {}


# ── parse_locust_stats_history ────────────────────────────────────────


class TestParseLocustStatsHistory:
    def test_basic_parsing(self, tmp_path):
        csv_content = (
            "Type,Name,User Count,Timestamp,Requests/s,Failures/s\n"
            ",Aggregated,10,1000,50.0,1.0\n"
            ",Aggregated,20,1001,100.0,2.0\n"
        )
        csv_path = tmp_path / "locust_stats_history.csv"
        csv_path.write_text(csv_content)
        history = parse_locust_stats_history(csv_path)
        assert len(history) == 2
        assert history[0]["rps"] == pytest.approx(50.0)
        assert history[1]["failures_s"] == pytest.approx(2.0)


# ── _apply_failure_rates ──────────────────────────────────────────────


class TestApplyFailureRates:
    def test_rates_calculated(self):
        metrics = {"requests": 1000, "failures": 20}
        breakdown = {"failures_4xx": 5, "failures_5xx": 15, "failures_503": 10}
        _apply_failure_rates(metrics, breakdown)
        assert metrics["error_rate_4xx"] == pytest.approx(0.5)
        assert metrics["error_rate_5xx"] == pytest.approx(1.5)
        assert metrics["error_rate_503"] == pytest.approx(1.0)
        assert metrics["failures_non_503"] == 10

    def test_zero_requests(self):
        metrics = {"requests": 0}
        breakdown = {"failures_4xx": 5}
        _apply_failure_rates(metrics, breakdown)
        assert "error_rate_4xx" not in metrics


class FakeProcess:
    """A locust that does whatever the test needs and never forks.

    ``wait`` replays ``waits``: an exception class is raised, anything else
    is returned as the exit code. The last entry repeats, so a test only has
    to describe the interesting part.
    """

    pid = 4242

    def __init__(self, *waits):
        self.waits = list(waits) or [0]
        self.signals = []
        self.killed = False

    def wait(self, timeout=None):
        step = self.waits.pop(0) if len(self.waits) > 1 else self.waits[0]
        if isinstance(step, type) and issubclass(step, BaseException):
            import subprocess as _sp
            if step is _sp.TimeoutExpired:
                raise _sp.TimeoutExpired(cmd="locust", timeout=timeout or 0)
            raise step()
        return step

    def send_signal(self, sig):
        self.signals.append(sig)

    def kill(self):
        self.killed = True


# ── B5: stale CSVs from a previous run into the same run_id ───────────


class TestClearPreviousCsvs:
    STATS_CSV = (
        "Type,Name,Request Count,Failure Count,Median Response Time,"
        "Average Response Time,Min Response Time,Max Response Time,"
        "Average Content Size,Requests/s,Failures/s,50%,66%,75%,80%,"
        "90%,95%,98%,99%,99.9%,99.99%,100%\n"
        ',"Aggregated",1000,10,115,120.0,75,500,2048,100.0,1.0,'
        "115,125,135,145,165,195,245,295,395,480,500\n"
    )

    def _launcher(self, tmp_path, monkeypatch, returncode=1):
        import subprocess

        from locomotive.launcher import LocustLauncher
        from locomotive.storage import Storage

        storage = Storage.from_root(tmp_path / "artifacts")
        launcher = LocustLauncher(storage, "run-1", {
            "locustfile": "dummy.py", "users": 1, "spawn_rate": 1,
            "run_time": "1s",
        })

        # locust never starts, so it writes no CSVs of its own
        monkeypatch.setattr(subprocess, "Popen",
                            lambda *a, **kw: FakeProcess(returncode))
        return storage, launcher

    def test_previous_csvs_are_not_reused(self, tmp_path, monkeypatch):
        storage, launcher = self._launcher(tmp_path, monkeypatch)
        raw_dir = storage.raw_dir("run-1")
        raw_dir.mkdir(parents=True, exist_ok=True)
        (raw_dir / "locust_stats.csv").write_text(self.STATS_CSV)

        result = launcher.run()

        assert not (raw_dir / "locust_stats.csv").exists()
        assert not storage.metrics_path("run-1").exists()
        assert result.get("returncode") == 1

    def test_unrelated_files_are_kept(self, tmp_path, monkeypatch):
        storage, launcher = self._launcher(tmp_path, monkeypatch)
        raw_dir = storage.raw_dir("run-1")
        raw_dir.mkdir(parents=True, exist_ok=True)
        keeper = raw_dir / "notes.txt"
        keeper.write_text("keep me")

        launcher.run()

        assert keeper.exists()


# ── the run has to survive Ctrl-C and a hung locust ───────────────────


def _launcher(tmp_path, extra=None):
    from locomotive.launcher import LocustLauncher
    from locomotive.storage import Storage

    cfg = {"locustfile": "dummy.py", "users": 1, "spawn_rate": 1, "run_time": "1m"}
    cfg.update(extra or {})
    storage = Storage.from_root(tmp_path / "artifacts")
    return storage, LocustLauncher(storage, "run-1", cfg)


@pytest.fixture
def spawned(monkeypatch):
    """Capture the FakeProcess a launcher starts, and neuter real signals."""
    import os
    import subprocess

    box = {}

    def fake_popen(cmd, **kwargs):
        box["cmd"] = cmd
        box["kwargs"] = kwargs
        return box["proc"]

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    # os.killpg against a made-up pid would signal whatever really owns it.
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: box["proc"].send_signal(sig))
    monkeypatch.setattr(os, "getpgid", lambda pid: pid)
    return box


class TestDurationParsing:
    from locomotive.launcher import parse_duration as _p

    @pytest.mark.parametrize("text,seconds", [
        ("30s", 30), ("5m", 300), ("1h", 3600), ("1h30m", 5400),
        ("90", 90), (45, 45), ("2d", 172800),
    ])
    def test_parses(self, text, seconds):
        from locomotive.launcher import parse_duration
        assert parse_duration(text) == seconds

    @pytest.mark.parametrize("text", [None, "", "forever"])
    def test_unparseable_is_none(self, text):
        from locomotive.launcher import parse_duration
        assert parse_duration(text) is None


class TestTimeoutBudget:
    def test_budget_exceeds_run_time(self):
        from locomotive.launcher import _default_timeout
        assert _default_timeout("5m", None) > 300

    def test_stop_timeout_is_added(self):
        from locomotive.launcher import _default_timeout
        assert _default_timeout("5m", 60) == _default_timeout("5m", None) + 60

    def test_a_long_run_gets_a_proportional_margin(self):
        from locomotive.launcher import _default_timeout
        # A fixed margin would be a rounding error on a two-hour soak test.
        assert _default_timeout("2h", None) == 7200 + 3600

    def test_no_run_time_means_no_deadline(self):
        from locomotive.launcher import _default_timeout
        assert _default_timeout(None, None) is None


class TestCtrlC:
    def test_run_json_is_written_anyway(self, tmp_path, spawned):
        # The whole point: locust has already flushed its CSVs by the time
        # the user's Ctrl-C lands, and losing run.json loses the run.
        storage, launcher = _launcher(tmp_path)
        spawned["proc"] = FakeProcess(KeyboardInterrupt, 1)

        result = launcher.run()

        meta = storage.load_json(storage.run_meta_path("run-1"))
        assert meta["interrupted"] is True
        assert result["interrupted"] is True

    def test_locust_is_asked_to_stop(self, tmp_path, spawned):
        import signal

        _storage, launcher = _launcher(tmp_path)
        spawned["proc"] = FakeProcess(KeyboardInterrupt, 0)
        launcher.run()
        assert signal.SIGINT in spawned["proc"].signals

    def test_no_exception_escapes(self, tmp_path, spawned):
        _storage, launcher = _launcher(tmp_path)
        spawned["proc"] = FakeProcess(KeyboardInterrupt, 0)
        launcher.run()  # would raise if the interrupt propagated

    def test_a_locust_that_ignores_sigint_is_killed(self, tmp_path, spawned):
        import subprocess

        _storage, launcher = _launcher(tmp_path)
        spawned["proc"] = FakeProcess(KeyboardInterrupt, subprocess.TimeoutExpired, -9)
        result = launcher.run()
        assert spawned["proc"].signals[-1] in (9, 15)
        assert result["returncode"] == -9

    def test_metrics_are_still_parsed(self, tmp_path, spawned):
        storage, launcher = _launcher(tmp_path)
        raw = storage.raw_dir("run-1")
        raw.mkdir(parents=True, exist_ok=True)
        spawned["proc"] = FakeProcess(KeyboardInterrupt, 1)

        # Locust wrote its CSV before it was asked to stop; _clear_previous_csvs
        # runs first, so the file has to appear when Popen is called.
        real_proc = spawned["proc"]

        class Writer(FakeProcess):
            def wait(self, timeout=None):
                (raw / "locust_stats.csv").write_text(
                    TestClearPreviousCsvs.STATS_CSV)
                return real_proc.wait(timeout)

        spawned["proc"] = Writer()
        launcher.run()
        assert storage.load_json(storage.metrics_path("run-1"))["requests"] == 1000


class TestHungLocust:
    def test_a_run_that_overruns_is_stopped(self, tmp_path, spawned, capsys):
        import subprocess

        storage, launcher = _launcher(tmp_path)
        spawned["proc"] = FakeProcess(subprocess.TimeoutExpired, -15)

        result = launcher.run()

        assert result["timed_out"] is True
        assert storage.load_json(storage.run_meta_path("run-1"))["timed_out"] is True
        assert "budget" in capsys.readouterr().err

    def test_timeout_can_be_set_explicitly(self, tmp_path, spawned):
        seen = {}

        class Recorder(FakeProcess):
            def wait(self, timeout=None):
                seen["timeout"] = timeout
                return 0

        _storage, launcher = _launcher(tmp_path, {"timeout": "10m"})
        spawned["proc"] = Recorder()
        launcher.run()
        assert seen["timeout"] == 600

    def test_timeout_none_waits_forever(self, tmp_path, spawned):
        seen = {}

        class Recorder(FakeProcess):
            def wait(self, timeout=None):
                seen["timeout"] = timeout
                return 0

        _storage, launcher = _launcher(tmp_path, {"timeout": None})
        spawned["proc"] = Recorder()
        launcher.run()
        assert seen["timeout"] is None

    def test_default_timeout_comes_from_run_time(self, tmp_path, spawned):
        from locomotive.launcher import _default_timeout

        seen = {}

        class Recorder(FakeProcess):
            def wait(self, timeout=None):
                seen["timeout"] = timeout
                return 0

        _storage, launcher = _launcher(tmp_path)
        spawned["proc"] = Recorder()
        launcher.run()
        assert seen["timeout"] == _default_timeout("1m", None)


class TestNormalRun:
    def test_clean_exit_marks_nothing(self, tmp_path, spawned):
        storage, launcher = _launcher(tmp_path)
        spawned["proc"] = FakeProcess(0)
        result = launcher.run()
        meta = storage.load_json(storage.run_meta_path("run-1"))
        assert "interrupted" not in result and "interrupted" not in meta
        assert "timed_out" not in result and "timed_out" not in meta
        assert result["returncode"] == 0

    def test_locust_gets_its_own_session(self, tmp_path, spawned):
        import os

        _storage, launcher = _launcher(tmp_path)
        spawned["proc"] = FakeProcess(0)
        launcher.run()
        if os.name == "posix":
            assert spawned["kwargs"].get("start_new_session") is True


# ── distributed runs: roles, flags, and what lands in run.json ────────


def _flag_value(cmd, flag):
    """The argument following `flag`, or None if the flag is absent."""
    return cmd[cmd.index(flag) + 1] if flag in cmd else None


class TestResolveTopology:
    def _t(self, **cfg):
        from locomotive.launcher import resolve_topology

        return resolve_topology(cfg)

    def test_nothing_set_is_one_standalone_process(self):
        assert self._t() == {"role": "standalone", "load_generators": 1}

    def test_processes_one_is_still_standalone(self):
        # locust does not fork for --processes 1, so calling it a master
        # would put a badge on a report that means nothing.
        assert self._t(processes=1)["role"] == "standalone"

    def test_processes_makes_a_master_of_that_many(self):
        topology = self._t(processes=8)
        assert topology["role"] == "master"
        assert topology["load_generators"] == 8

    def test_a_master_counts_the_workers_it_expects(self):
        topology = self._t(master=True, expect_workers=4)
        assert topology["role"] == "master"
        assert topology["load_generators"] == 4

    def test_a_master_with_no_count_falls_back_to_one(self):
        # validate() rejects this config; the launcher still has to answer.
        assert self._t(master=True)["load_generators"] == 1

    def test_a_worker_does_not_know_the_cluster_size(self):
        # The master owns that number and sends it at connect time.
        topology = self._t(worker=True, expect_workers=9)
        assert topology["role"] == "worker"
        assert topology["load_generators"] == 1

    def test_a_worker_defaults_to_a_local_master(self):
        assert self._t(worker=True)["master_host"] == "127.0.0.1"

    def test_a_worker_keeps_the_master_it_was_given(self):
        assert self._t(worker=True, master_host="10.0.0.5")["master_host"] == "10.0.0.5"


class TestTopologyCommand:
    def test_a_standalone_run_says_nothing_about_distribution(self, tmp_path, spawned):
        _storage, launcher = _launcher(tmp_path)
        spawned["proc"] = FakeProcess(0)
        launcher.run()
        cmd = spawned["cmd"]
        for flag in ("--processes", "--master", "--worker", "--expect-workers"):
            assert flag not in cmd

    def test_processes_is_passed_through(self, tmp_path, spawned):
        _storage, launcher = _launcher(tmp_path, {"processes": 4})
        spawned["proc"] = FakeProcess(0)
        launcher.run()
        assert _flag_value(spawned["cmd"], "--processes") == "4"

    def test_processes_does_not_also_say_master(self, tmp_path, spawned):
        # --processes already appoints the parent master and sets
        # expect_workers from the same number; --master on top is locust
        # arguing with itself.
        _storage, launcher = _launcher(tmp_path, {"processes": 4})
        spawned["proc"] = FakeProcess(0)
        launcher.run()
        assert "--master" not in spawned["cmd"]
        assert "--expect-workers" not in spawned["cmd"]

    def test_an_explicit_master_announces_its_worker_count(self, tmp_path, spawned):
        _storage, launcher = _launcher(tmp_path, {"master": True, "expect_workers": 3})
        spawned["proc"] = FakeProcess(0)
        launcher.run()
        cmd = spawned["cmd"]
        assert "--master" in cmd
        assert _flag_value(cmd, "--expect-workers") == "3"

    def test_a_worker_is_pointed_at_its_master(self, tmp_path, spawned):
        _storage, launcher = _launcher(
            tmp_path, {"worker": True, "master_host": "10.0.0.5", "master_port": 5558}
        )
        spawned["proc"] = FakeProcess(0)
        launcher.run()
        cmd = spawned["cmd"]
        assert "--worker" in cmd
        assert _flag_value(cmd, "--master-host") == "10.0.0.5"
        assert _flag_value(cmd, "--master-port") == "5558"

    def test_a_worker_is_not_given_the_masters_job(self, tmp_path, spawned):
        # -u/-r/--run-time are cluster totals the master owns, and only the
        # master has aggregated stats to write, so --csv on a worker would
        # produce empty files that read as a run measuring nothing.
        _storage, launcher = _launcher(tmp_path, {"worker": True})
        spawned["proc"] = FakeProcess(0)
        launcher.run()
        cmd = spawned["cmd"]
        for flag in ("-u", "-r", "--run-time", "--csv"):
            assert flag not in cmd

    def test_a_worker_does_not_need_users_or_run_time(self, tmp_path, spawned):
        from locomotive.launcher import LocustLauncher
        from locomotive.storage import Storage

        storage = Storage.from_root(tmp_path / "artifacts")
        launcher = LocustLauncher(
            storage, "run-1", {"locustfile": "dummy.py", "worker": True}
        )
        spawned["proc"] = FakeProcess(0)
        assert launcher.run()["returncode"] == 0

    def test_a_standalone_run_still_needs_them(self, tmp_path, spawned):
        from locomotive.launcher import LocustLauncher
        from locomotive.storage import Storage

        storage = Storage.from_root(tmp_path / "artifacts")
        launcher = LocustLauncher(storage, "run-1", {"locustfile": "dummy.py"})
        spawned["proc"] = FakeProcess(0)
        with pytest.raises(ValueError, match="users is required"):
            launcher.run()


class TestTopologyIsRecorded:
    def test_run_json_carries_the_topology(self, tmp_path, spawned):
        # "p95 got worse" is a different sentence when the previous run had
        # one load generator and this one has eight, and nothing else in
        # run.json says so.
        storage, launcher = _launcher(tmp_path, {"processes": 8})
        spawned["proc"] = FakeProcess(0)
        result = launcher.run()
        meta = storage.load_json(storage.run_meta_path("run-1"))
        assert meta["topology"]["load_generators"] == 8
        assert result["topology"]["role"] == "master"

    def test_a_single_process_run_records_itself_as_one(self, tmp_path, spawned):
        storage, launcher = _launcher(tmp_path)
        spawned["proc"] = FakeProcess(0)
        launcher.run()
        meta = storage.load_json(storage.run_meta_path("run-1"))
        assert meta["topology"] == {"role": "standalone", "load_generators": 1}
