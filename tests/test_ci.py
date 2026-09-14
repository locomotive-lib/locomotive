"""CI detection, run ids, and the baseline collision guard.

The bug behind this file: two runs sharing a run id — the generated
`${GITHUB_SHA:-local}` on GitLab or Jenkins, or a re-run of the commit that set
the baseline — wrote the second run over the baseline and compared it with
itself, so a fourfold p95 regression came out PASS with exit code 0.
"""

import argparse
import json

import pytest

from locomotive import cli
from locomotive.ci import CIContext, default_run_id, detect_ci
from locomotive.config import load_config
from locomotive.storage import Storage
from locomotive.template import generate_template


# ── detection ─────────────────────────────────────────────────────────


class TestDetectLocal:
    def test_empty_environment_is_local(self):
        ctx = detect_ci({})
        assert ctx.provider == "local"
        assert not ctx.is_ci
        assert ctx.to_meta() == {"provider": "local"}

    def test_generic_ci_flag_names_no_provider(self):
        assert detect_ci({"CI": "true"}).provider == "local"


class TestDetectGitHub:
    BASE = {
        "GITHUB_ACTIONS": "true",
        "GITHUB_SHA": "a" * 40,
        "GITHUB_REPOSITORY": "org/app",
        "GITHUB_SERVER_URL": "https://github.com",
        "GITHUB_RUN_ID": "9001",
        "GITHUB_RUN_ATTEMPT": "2",
    }

    def test_push(self):
        ctx = detect_ci({**self.BASE, "GITHUB_REF": "refs/heads/main", "GITHUB_REF_NAME": "main"})
        assert ctx.provider == "github"
        assert ctx.commit == "a" * 40
        assert ctx.branch == "main"
        assert ctx.target_branch is None
        assert ctx.change_id is None
        assert ctx.build_id == "9001-2"
        assert ctx.build_url == "https://github.com/org/app/actions/runs/9001"

    def test_pull_request_without_payload(self):
        ctx = detect_ci({
            **self.BASE,
            "GITHUB_REF": "refs/pull/42/merge",
            "GITHUB_REF_NAME": "42/merge",
            "GITHUB_HEAD_REF": "feature/x",
            "GITHUB_BASE_REF": "main",
        })
        # The branch is the head branch, not the synthetic "42/merge" ref.
        assert ctx.branch == "feature/x"
        assert ctx.target_branch == "main"
        assert ctx.change_id == "42"
        assert ctx.change_url == "https://github.com/org/app/pull/42"

    def test_payload_is_preferred(self, tmp_path):
        # pull_request_target runs on the base ref, so only the payload knows
        # which pull request this is.
        event = tmp_path / "event.json"
        event.write_text(json.dumps({
            "pull_request": {"number": 7, "html_url": "https://ghe.example/org/app/pull/7"},
            "repository": {"default_branch": "trunk"},
        }))
        ctx = detect_ci({**self.BASE, "GITHUB_EVENT_PATH": str(event), "GITHUB_REF": "refs/heads/main"})
        assert ctx.change_id == "7"
        assert ctx.change_url == "https://ghe.example/org/app/pull/7"
        assert ctx.default_branch == "trunk"

    def test_unreadable_payload_is_ignored(self, tmp_path):
        ctx = detect_ci({**self.BASE, "GITHUB_EVENT_PATH": str(tmp_path / "missing.json")})
        assert ctx.provider == "github"
        assert ctx.change_id is None


class TestDetectGitLab:
    BASE = {
        "GITLAB_CI": "true",
        "CI_COMMIT_SHA": "b" * 40,
        "CI_PIPELINE_ID": "77",
        "CI_JOB_ID": "555",
        "CI_JOB_URL": "https://gitlab.example/g/app/-/jobs/555",
        "CI_PROJECT_PATH": "g/app",
        "CI_PROJECT_URL": "https://gitlab.example/g/app",
        "CI_DEFAULT_BRANCH": "main",
    }

    def test_branch_pipeline(self):
        ctx = detect_ci({**self.BASE, "CI_COMMIT_BRANCH": "main", "CI_COMMIT_REF_NAME": "main"})
        assert ctx.provider == "gitlab"
        assert ctx.branch == "main"
        assert ctx.target_branch is None
        # The job id, not the pipeline id every job of the pipeline shares.
        assert ctx.build_id == "555"
        assert ctx.build_url == "https://gitlab.example/g/app/-/jobs/555"
        assert ctx.repository == "g/app"
        assert ctx.default_branch == "main"

    def test_merge_request_pipeline(self):
        ctx = detect_ci({
            **self.BASE,
            "CI_COMMIT_REF_NAME": "feature/x",
            "CI_MERGE_REQUEST_IID": "12",
            "CI_MERGE_REQUEST_SOURCE_BRANCH_NAME": "feature/x",
            "CI_MERGE_REQUEST_TARGET_BRANCH_NAME": "develop",
            "CI_MERGE_REQUEST_PROJECT_URL": "https://gitlab.example/g/app",
        })
        assert ctx.branch == "feature/x"
        assert ctx.target_branch == "develop"
        assert ctx.change_id == "12"
        assert ctx.change_url == "https://gitlab.example/g/app/-/merge_requests/12"


class TestDetectJenkins:
    BASE = {
        "JENKINS_URL": "https://ci.example/",
        "BUILD_NUMBER": "31",
        "BUILD_URL": "https://ci.example/job/app/job/main/31/",
        "GIT_COMMIT": "c" * 40,
    }

    def test_multibranch_branch_build(self):
        ctx = detect_ci({**self.BASE, "BRANCH_NAME": "main"})
        assert ctx.provider == "jenkins"
        assert ctx.branch == "main"
        assert ctx.target_branch is None
        assert ctx.build_id == "31"
        assert ctx.build_url == "https://ci.example/job/app/job/main/31/"

    def test_multibranch_pull_request(self):
        ctx = detect_ci({
            **self.BASE,
            "BRANCH_NAME": "PR-5",
            "CHANGE_ID": "5",
            "CHANGE_BRANCH": "feature/x",
            "CHANGE_TARGET": "main",
            "CHANGE_URL": "https://github.com/org/app/pull/5",
        })
        # "PR-5" is the job's name, not a branch anyone pushed.
        assert ctx.branch == "feature/x"
        assert ctx.target_branch == "main"
        assert ctx.change_id == "5"
        assert ctx.change_url == "https://github.com/org/app/pull/5"

    def test_plain_pipeline_strips_the_remote(self):
        ctx = detect_ci({**self.BASE, "GIT_BRANCH": "origin/release/1.2"})
        assert ctx.branch == "release/1.2"

    def test_detected_without_a_root_url(self):
        env = {k: v for k, v in self.BASE.items() if k != "JENKINS_URL"}
        env["BUILD_TAG"] = "jenkins-app-main-31"
        assert detect_ci(env).provider == "jenkins"


# ── run ids ───────────────────────────────────────────────────────────


class TestDefaultRunId:
    def test_commit_and_build(self):
        ctx = CIContext(provider="gitlab", commit="b" * 40, build_id="555")
        assert default_run_id(ctx) == "bbbbbbbbbbbb-555"

    def test_a_rerun_of_the_same_commit_gets_a_different_id(self):
        env = dict(TestDetectGitHub.BASE)
        first = default_run_id(detect_ci({**env, "GITHUB_RUN_ATTEMPT": "1"}))
        second = default_run_id(detect_ci({**env, "GITHUB_RUN_ATTEMPT": "2"}))
        assert first != second

    def test_commit_only(self):
        assert default_run_id(CIContext(provider="jenkins", commit="c" * 40)) == "c" * 12

    def test_build_only(self):
        assert default_run_id(CIContext(provider="jenkins", build_id="31")) == "build-31"

    def test_local_run_uses_the_clock(self):
        assert default_run_id(CIContext(), now=1700000000) == "run-1700000000"

    def test_id_is_safe_as_a_directory_name(self):
        assert default_run_id(CIContext(provider="x", build_id="a/b c")) == "build-a-b-c"


@pytest.fixture
def gitlab_env(monkeypatch):
    """A GitLab job, with any CI variables of the machine running the tests removed."""
    import os

    prefixes = ("GITHUB_", "GITLAB_", "CI_", "JENKINS_", "BUILD_", "GIT_", "CHANGE_", "BRANCH_")
    for name in list(os.environ):
        if name.startswith(prefixes):
            monkeypatch.delenv(name)
    monkeypatch.setenv("GITLAB_CI", "true")
    monkeypatch.setenv("CI_COMMIT_SHA", "abc123abc123abc123")
    monkeypatch.setenv("CI_JOB_ID", "555")


class TestGeneratedConfig:
    def test_leaves_the_run_id_to_detection(self, tmp_path):
        path = tmp_path / "loconfig.json"
        generate_template(path)
        assert "run_id" not in json.loads(path.read_text())["artifacts"]

    def test_gets_a_unique_id_on_gitlab(self, tmp_path, gitlab_env):
        path = tmp_path / "loconfig.json"
        generate_template(path)
        config = load_config(path)
        run_id = cli._build_run_id(argparse.Namespace(run_id=None), config)
        assert run_id == "abc123abc123-555"


# ── the collision guard ───────────────────────────────────────────────


class FakeLauncher:
    """Stands in for `cli._run`: writes metrics.json and returns a code."""

    def __init__(self, metrics, returncode=0):
        self.metrics = metrics
        self.returncode = returncode

    def __call__(self, storage, run_id, locust_config):
        storage.ensure_run(run_id)
        storage.save_json(storage.metrics_path(run_id), self.metrics)
        return {"returncode": self.returncode, "run_id": run_id}


def make_args(**overrides):
    defaults = dict(
        storage=None, run_id=None, baseline=None, rules=None, fail_on=None,
        set_baseline=True, no_validate=True, title=None, output=None,
        locustfile="dummy.py", host=None, users=None, spawn_rate=None,
        run_time=None, tags=None, exclude_tags=None, stop_timeout=None,
        extra_arg=None, locust_cmd=None,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def metrics(p95):
    return {"requests": 5000, "failures": 0, "error_rate": 0.0, "p95_ms": float(p95), "rps": 50.0}


P95_RULE = {"metric": "p95_ms", "mode": "relative", "direction": "increase", "warn": 10, "fail": 25}


class TestBaselineCollision:
    @pytest.fixture
    def config(self, tmp_path):
        return {
            "artifacts": {"storage": str(tmp_path / "artifacts"), "run_id": "local"},
            "load": {"locustfile": "dummy.py"},
            "analysis": {"rules": [P95_RULE]},
        }

    @pytest.fixture
    def storage(self, config):
        return Storage.from_root(config["artifacts"]["storage"])

    def ci(self, monkeypatch, config, p95, **args):
        monkeypatch.setattr(cli, "_run", FakeLauncher(metrics(p95)))
        return cli.cmd_ci(make_args(**args), config)

    def test_a_regression_under_a_constant_run_id_fails(self, monkeypatch, config, storage):
        assert self.ci(monkeypatch, config, 100) == 0
        assert self.ci(monkeypatch, config, 400) == 1

        analysis = json.loads(storage.analysis_path("local-2").read_text())
        assert analysis["baseline_id"] == "local"
        p95 = next(r for r in analysis["results"] if r["metric"] == "p95_ms")
        assert (p95["baseline"], p95["current"], p95["status"]) == (100.0, 400.0, "DEGRADATION")

    def test_the_baseline_run_is_left_intact(self, monkeypatch, config, storage):
        self.ci(monkeypatch, config, 100)
        self.ci(monkeypatch, config, 400)

        assert storage.get_baseline() == "local"
        assert storage.load_json(storage.metrics_path("local"))["p95_ms"] == 100.0

    def test_the_warning_names_the_setting(self, monkeypatch, config, capsys):
        self.ci(monkeypatch, config, 100)
        capsys.readouterr()
        self.ci(monkeypatch, config, 100)
        out = capsys.readouterr().out
        assert "'local' is the current baseline" in out
        assert "artifacts.run_id" in out

    def test_a_passing_run_becomes_the_new_baseline_under_its_new_id(self, monkeypatch, config, storage):
        self.ci(monkeypatch, config, 100)
        assert self.ci(monkeypatch, config, 105) == 0
        assert storage.get_baseline() == "local-2"

        # "local" is no longer the baseline, so the next run may reuse it —
        # and is compared against local-2.
        assert self.ci(monkeypatch, config, 400) == 1
        analysis = json.loads(storage.analysis_path("local").read_text())
        assert analysis["baseline_id"] == "local-2"

    def test_existing_run_directories_are_not_reused(self, monkeypatch, config, storage):
        self.ci(monkeypatch, config, 100)
        storage.ensure_run("local-2")
        self.ci(monkeypatch, config, 100)
        assert storage.metrics_path("local-3").exists()

    def test_an_explicit_run_id_is_guarded_too(self, monkeypatch, config, storage):
        self.ci(monkeypatch, config, 100, run_id="abc")
        assert self.ci(monkeypatch, config, 400, run_id="abc") == 1
        assert storage.load_json(storage.metrics_path("abc"))["p95_ms"] == 100.0

    def test_distinct_run_ids_are_untouched(self, monkeypatch, config, storage):
        self.ci(monkeypatch, config, 100, run_id="first")
        self.ci(monkeypatch, config, 100, run_id="second")
        assert storage.metrics_path("second").exists()
        assert not storage.run_dir("second-2").exists()

    def test_run_command_is_guarded(self, monkeypatch, config, storage):
        monkeypatch.setattr(cli, "_run", FakeLauncher(metrics(100)))
        cli.cmd_run(make_args(), config)
        monkeypatch.setattr(cli, "_run", FakeLauncher(metrics(400)))
        cli.cmd_run(make_args(set_baseline=False), config)

        assert storage.load_json(storage.metrics_path("local"))["p95_ms"] == 100.0
        assert storage.load_json(storage.metrics_path("local-2"))["p95_ms"] == 400.0


class TestAnalyzeTheBaselineItself:
    def test_gate_still_runs_without_a_self_comparison(self, tmp_path, capsys):
        storage = Storage.from_root(tmp_path / "artifacts")
        storage.ensure_run("base")
        storage.save_json(storage.metrics_path("base"), metrics(100))
        storage.set_baseline("base")
        config = {
            "artifacts": {"storage": str(tmp_path / "artifacts")},
            "analysis": {
                "rules": [P95_RULE],
                "gate": {"thresholds": {"p95_ms": {"fail": 500}}},
            },
        }

        code = cli.cmd_analyze(make_args(run_id="base"), config)

        assert code == 0
        assert "is the baseline itself" in capsys.readouterr().out
        analysis = json.loads(storage.analysis_path("base").read_text())
        assert "baseline_id" not in analysis
        assert all(r["metric"] != "p95_ms" for r in analysis["results"])

    def test_without_a_gate_there_is_nothing_to_analyze(self, tmp_path):
        storage = Storage.from_root(tmp_path / "artifacts")
        storage.ensure_run("base")
        storage.save_json(storage.metrics_path("base"), metrics(100))
        storage.set_baseline("base")
        config = {"artifacts": {"storage": str(tmp_path / "artifacts")}, "analysis": {"rules": [P95_RULE]}}

        with pytest.raises(ValueError, match="baseline run id is required"):
            cli.cmd_analyze(make_args(run_id="base"), config)
