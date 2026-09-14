"""What `loco ci` hands to a CI system besides the HTML report.

The warning exit code, the markdown summary and JUnit file, pruning of stored
runs, the CI metadata in run.json, and how the report shows where a run came
from. Wrappers for GitHub Actions, GitLab CI and Jenkins are thin because all
of this lives here.
"""

import argparse
import json
import os
import xml.etree.ElementTree as ET

import pytest

from locomotive import cli
from locomotive.reporter import render_report
from locomotive.report_config import DEFAULT_CHART_JS_URL, resolve_report_config
from locomotive.storage import Storage
from locomotive.validate import ERROR, WARNING, validate_config


class FakeLauncher:
    def __init__(self, metrics, returncode=0):
        self.metrics = metrics
        self.returncode = returncode
        self.calls = 0

    def __call__(self, storage, run_id, locust_config):
        self.calls += 1
        storage.ensure_run(run_id)
        if self.metrics is not None:
            storage.save_json(storage.metrics_path(run_id), self.metrics)
        return {"returncode": self.returncode, "run_id": run_id}


def make_args(**overrides):
    defaults = dict(
        storage=None, run_id=None, baseline=None, rules=None, fail_on=None,
        set_baseline=False, no_validate=True, title=None, output=None,
        locustfile="dummy.py", host=None, users=None, spawn_rate=None,
        run_time=None, tags=None, exclude_tags=None, stop_timeout=None,
        extra_arg=None, locust_cmd=None, warning_exit_code=None,
        summary=None, junit=None, prune=False,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def metrics(p95):
    return {"requests": 5000, "failures": 0, "error_rate": 0.0, "p95_ms": float(p95), "rps": 50.0}


P95_RULE = {"metric": "p95_ms", "mode": "relative", "direction": "increase", "warn": 10, "fail": 25}


@pytest.fixture
def config(tmp_path):
    return {
        "artifacts": {"storage": str(tmp_path / "artifacts")},
        "load": {"locustfile": "dummy.py"},
        "analysis": {"rules": [P95_RULE]},
    }


@pytest.fixture
def storage(config):
    return Storage.from_root(config["artifacts"]["storage"])


def run_ci(monkeypatch, config, p95, returncode=0, **args):
    launcher = FakeLauncher(None if p95 is None else metrics(p95), returncode)
    monkeypatch.setattr(cli, "_run", launcher)
    return cli.cmd_ci(make_args(**args), config)


@pytest.fixture
def with_baseline(monkeypatch, config):
    assert run_ci(monkeypatch, config, 100, run_id="base", set_baseline=True) == 0


# ── warning exit code ─────────────────────────────────────────────────


@pytest.mark.usefixtures("with_baseline")
class TestWarningExitCode:
    def test_warning_passes_by_default(self, monkeypatch, config):
        assert run_ci(monkeypatch, config, 115, run_id="r") == 0

    def test_flag(self, monkeypatch, config):
        assert run_ci(monkeypatch, config, 115, run_id="r", warning_exit_code=2) == 2

    def test_config(self, monkeypatch, config):
        config["analysis"]["warning_exit_code"] = 3
        assert run_ci(monkeypatch, config, 115, run_id="r") == 3

    def test_flag_beats_config(self, monkeypatch, config):
        config["analysis"]["warning_exit_code"] = 3
        assert run_ci(monkeypatch, config, 115, run_id="r", warning_exit_code=2) == 2

    def test_fail_on_warning_still_fails(self, monkeypatch, config):
        assert run_ci(monkeypatch, config, 115, run_id="r", fail_on="WARNING", warning_exit_code=2) == 1

    def test_degradation_still_fails(self, monkeypatch, config):
        assert run_ci(monkeypatch, config, 200, run_id="r", warning_exit_code=2) == 1

    def test_a_pass_is_still_zero(self, monkeypatch, config):
        assert run_ci(monkeypatch, config, 101, run_id="r", warning_exit_code=2) == 0

    def test_locust_failure_outranks_a_warning(self, monkeypatch, config):
        assert run_ci(monkeypatch, config, 115, returncode=3, run_id="r", warning_exit_code=2) == 3

    def test_analyze_agrees(self, monkeypatch, config):
        run_ci(monkeypatch, config, 115, run_id="r")
        assert cli.cmd_analyze(make_args(run_id="r", warning_exit_code=2), config) == 2

    def test_a_bad_value_fails_before_the_run(self, monkeypatch, config):
        config["analysis"]["warning_exit_code"] = "two"
        launcher = FakeLauncher(metrics(115))
        monkeypatch.setattr(cli, "_run", launcher)
        with pytest.raises(ValueError, match="warning_exit_code"):
            cli.cmd_ci(make_args(run_id="r"), config)
        assert launcher.calls == 0


class TestValidateWarningExitCode:
    def issues(self, value):
        cfg = {"analysis": {"warning_exit_code": value}}
        return [i for i in validate_config(cfg) if i.location == "analysis.warning_exit_code"]

    @pytest.mark.parametrize("value", ["two", True, -1, 256, 2.5])
    def test_rejected(self, value):
        found = self.issues(value)
        assert found and found[0].level == ERROR

    @pytest.mark.parametrize("value", [0, 2, 255])
    def test_accepted(self, value):
        assert self.issues(value) == []


# ── summary and JUnit ─────────────────────────────────────────────────


@pytest.mark.usefixtures("with_baseline")
class TestExports:
    def test_ci_writes_both(self, monkeypatch, config, tmp_path):
        summary, junit = tmp_path / "out" / "summary.md", tmp_path / "out" / "junit.xml"
        run_ci(monkeypatch, config, 115, run_id="r", summary=str(summary), junit=str(junit))

        text = summary.read_text(encoding="utf-8")
        assert "WARNING" in text.splitlines()[0]
        assert "vs baseline `base`" in text
        suite = ET.parse(junit).getroot().find("testsuite")
        assert suite.get("failures") == "0"

    def test_junit_follows_fail_on(self, monkeypatch, config, tmp_path):
        junit = tmp_path / "junit.xml"
        run_ci(monkeypatch, config, 115, run_id="r", junit=str(junit), fail_on="WARNING")
        assert ET.parse(junit).getroot().get("failures") == "1"

    def test_report_writes_them_for_an_existing_run(self, monkeypatch, config, tmp_path):
        run_ci(monkeypatch, config, 115, run_id="r")
        summary = tmp_path / "summary.md"
        cli.cmd_report(make_args(run_id="r", summary=str(summary)), config)
        assert "vs baseline `base`" in summary.read_text(encoding="utf-8")

    def test_a_run_without_metrics_still_explains_itself(self, monkeypatch, config, tmp_path):
        summary, junit = tmp_path / "summary.md", tmp_path / "junit.xml"
        code = run_ci(monkeypatch, config, None, returncode=1, run_id="r",
                      summary=str(summary), junit=str(junit))
        assert code == 1
        assert "produced no metrics" in summary.read_text(encoding="utf-8")
        assert ET.parse(junit).getroot().get("failures") == "1"


def test_first_run_summary_says_there_is_no_baseline(monkeypatch, config, tmp_path):
    summary = tmp_path / "summary.md"
    run_ci(monkeypatch, config, 100, run_id="first", summary=str(summary))
    text = summary.read_text(encoding="utf-8")
    assert "nothing to compare against" in text.splitlines()[0]
    assert "**No baseline was used**" in text


def test_gate_only_config_does_not_ask_for_a_baseline(monkeypatch, tmp_path):
    config = {
        "artifacts": {"storage": str(tmp_path / "artifacts")},
        "load": {"locustfile": "dummy.py"},
        "analysis": {"gate": {"thresholds": {"p95_ms": {"fail": 500}}}},
    }
    summary = tmp_path / "summary.md"
    run_ci(monkeypatch, config, 100, run_id="first", summary=str(summary))
    assert "No baseline was used" not in summary.read_text(encoding="utf-8")


def test_summary_includes_endpoints_from_the_stats_csv(monkeypatch, config, storage, tmp_path):
    run_ci(monkeypatch, config, 100, run_id="r")
    (storage.raw_dir("r") / "locust_stats.csv").write_text(
        "Type,Name,Request Count,Failure Count,Average Response Time,Requests/s,95%,99%\n"
        "GET,/ping,20,0,3.5,5.0,6,9\n,Aggregated,20,0,3.5,5.0,6,9\n",
        encoding="utf-8",
    )
    summary = tmp_path / "summary.md"
    cli.cmd_report(make_args(run_id="r", summary=str(summary)), config)
    assert "| GET /ping | 20 | 0 | 3.50 | 6 | 9 | 5 |" in summary.read_text(encoding="utf-8")


# ── pruning ───────────────────────────────────────────────────────────


class TestPrune:
    def test_keeps_the_baseline_and_this_run(self, monkeypatch, config, storage, with_baseline):
        run_ci(monkeypatch, config, 200, run_id="second")
        run_ci(monkeypatch, config, 200, run_id="third", prune=True)
        assert sorted(p.name for p in storage.runs_dir().iterdir()) == ["base", "third"]

    def test_a_run_that_becomes_the_baseline_keeps_only_itself(self, monkeypatch, config, storage, with_baseline):
        run_ci(monkeypatch, config, 100, run_id="second", set_baseline=True, prune=True)
        assert storage.get_baseline() == "second"
        assert [p.name for p in storage.runs_dir().iterdir()] == ["second"]

    def test_off_by_default(self, monkeypatch, config, storage, with_baseline):
        run_ci(monkeypatch, config, 200, run_id="second")
        run_ci(monkeypatch, config, 200, run_id="third")
        assert len(list(storage.runs_dir().iterdir())) == 3

    def test_storage_prune_runs(self, tmp_path):
        store = Storage.from_root(tmp_path)
        for run_id in ("a", "b", "c"):
            store.ensure_run(run_id)
        assert store.prune_runs({"b", None}) == ["a", "c"]
        assert store.prune_runs({"b"}) == []

    def test_prune_without_runs(self, tmp_path):
        assert Storage.from_root(tmp_path / "nothing").prune_runs({"a"}) == []


# ── CI metadata ───────────────────────────────────────────────────────


def test_run_meta_records_the_ci_context(monkeypatch, config):
    for name in list(os.environ):
        if name.startswith(("GITHUB_", "GITLAB_", "CI_", "JENKINS_", "BUILD_", "GIT_", "CHANGE_", "BRANCH_")):
            monkeypatch.delenv(name)
    monkeypatch.setenv("JENKINS_URL", "https://ci.example/")
    monkeypatch.setenv("GIT_COMMIT", "c" * 40)
    monkeypatch.setenv("BRANCH_NAME", "main")
    monkeypatch.setenv("BUILD_NUMBER", "7")

    ci = cli._build_locust_config(make_args(), config)["meta"]["ci"]

    assert ci == {"provider": "jenkins", "commit": "c" * 40, "branch": "main", "build_id": "7"}


# ── report header and charts ──────────────────────────────────────────


GITHUB_PR = {
    "provider": "github", "branch": "feature/x", "target_branch": "main",
    "commit": "a" * 40, "change_id": "42",
    "change_url": "https://github.com/org/app/pull/42",
    "build_url": "https://github.com/org/app/actions/runs/1",
}


def render(ci=None, report=None, **kwargs):
    run_meta = {"run_id": "r1"}
    if ci is not None:
        run_meta["meta"] = {"ci": ci}
    return render_report(
        run_meta, {"requests": 100, "p95_ms": 12.0}, None, None, "T",
        report_config=resolve_report_config(report or {}), **kwargs,
    )


class TestReportHeader:
    def test_shows_where_the_run_came_from(self):
        page = render(GITHUB_PR)
        assert "feature/x → main" in page
        assert "aaaaaaaaaaaa" in page
        assert '<a href="https://github.com/org/app/pull/42">#42</a>' in page
        assert '<a href="https://github.com/org/app/actions/runs/1">Build</a>' in page

    def test_gitlab_merge_requests_use_their_own_prefix(self):
        page = render({"provider": "gitlab", "change_id": "12",
                       "change_url": "https://gitlab.example/g/app/-/merge_requests/12"})
        assert ">!12</a>" in page

    def test_a_non_http_url_is_not_a_link(self):
        page = render({**GITHUB_PR, "build_url": "javascript:alert(1)"})
        assert "javascript:" not in page
        assert "Build" in page

    def test_branch_names_are_escaped(self):
        page = render({"provider": "jenkins", "branch": "<script>x</script>"})
        assert "<script>x</script>" not in page

    def test_run_ids_that_share_a_commit_stay_apart(self):
        # Cut at 12 characters, a rebuild and its baseline both read "042d14c81983".
        page = render_report({"run_id": "042d14c81983-3", "baseline_id": "042d14c81983-2"},
                             {"requests": 1}, None, None, "T")
        assert "Run: 042d14c81983-3 | Baseline: 042d14c81983-2" in page

    def test_a_full_sha_is_still_shortened(self):
        page = render_report({"run_id": "a" * 40}, {"requests": 1}, None, None, "T")
        assert "Run: aaaaaaaaaaaa |" in page

    def test_nothing_for_a_local_run(self):
        assert "Build" not in render({"provider": "local"})
        assert "Build" not in render()


HISTORY = [
    {"run_id": "a", "started_at": "", "p95_ms": 10.0, "rps": 5.0, "error_rate": 0.0},
    {"run_id": "b", "started_at": "", "p95_ms": 11.0, "rps": 5.0, "error_rate": 0.0},
]


class TestChartScript:
    def test_chart_js_is_pinned(self):
        assert f'<script src="{DEFAULT_CHART_JS_URL}"></script>' in render()
        assert "@4." in DEFAULT_CHART_JS_URL

    def test_a_mirror_can_be_configured(self):
        page = render(report={"chart_js_url": "https://mirror.example/chart.umd.min.js"})
        assert '<script src="https://mirror.example/chart.umd.min.js"></script>' in page

    def test_an_empty_url_loads_nothing(self):
        assert "chart.umd" not in render(report={"chart_js_url": ""})

    def test_charts_explain_themselves_when_scripts_do_not_run(self):
        page = render(report={"sections": ["trends"]}, history_runs=HISTORY)
        assert page.count('class="chart-fallback"') == 3
        script = page[page.rindex("<script>"):]
        assert "typeof Chart !== 'undefined'" in script
        assert script.index("chart-fallback") < script.index("new Chart")


def test_validate_warns_about_a_non_string_chart_url():
    issues = [i for i in validate_config({"report": {"chart_js_url": 5}})
              if i.location == "report.chart_js_url"]
    assert issues and issues[0].level == WARNING
