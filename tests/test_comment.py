"""`loco comment`: one pull/merge request comment, kept up to date."""

import http.client
import json
import os
import re
import urllib.error
from urllib.parse import urlparse

import pytest

from locomotive import cli
from locomotive.ci import detect_ci
from locomotive.comment import (
    MAX_BODY,
    CommentError,
    Target,
    _urllib_transport,
    marker_for,
    post_comment,
    resolve_target,
    upsert_comment,
)

ACTIONS_BOT = {"login": "github-actions[bot]", "type": "Bot"}
MARKED = marker_for("loadtest")


class FakeHost:
    """A code host API that records every request.

    *me* is what `GET /user` answers; None answers it the way GitHub answers a
    GITHUB_TOKEN, with a 403.
    """

    def __init__(self, comments=None, status=None, me=None):
        self.comments = comments or []
        self.status = status
        self.me = me
        self.requests = []

    def __call__(self, method, url, headers, body):
        self.requests.append((method, url, headers, json.loads(body) if body else None))
        if self.status:
            return self.status, {"message": "nope"}
        if method == "GET" and urlparse(url).path.endswith("/user"):
            if self.me is None:
                return 403, {"message": "Resource not accessible by integration"}
            return 200, self.me
        if method == "GET":
            page = int(re.search(r"[?&]page=(\d+)", url).group(1))
            return 200, self.comments[(page - 1) * 100: page * 100]
        if method == "POST":
            return 201, {"id": 999}
        return 200, {"id": int(url.rstrip("/").split("/")[-1])}

    def writes(self):
        return [(method, url) for method, url, *_ in self.requests if method != "GET"]

    def listings(self):
        return [url for method, url, *_ in self.requests if method == "GET" and "page=" in url]


GITHUB_ENV = {
    "GITHUB_ACTIONS": "true", "GITHUB_REPOSITORY": "org/app",
    "GITHUB_REF": "refs/pull/42/merge", "GITHUB_TOKEN": "ghs_x",
}
GITLAB_ENV = {
    "GITLAB_CI": "true", "CI_MERGE_REQUEST_IID": "12", "CI_MERGE_REQUEST_PROJECT_ID": "321",
    "CI_API_V4_URL": "https://gitlab.example/api/v4", "CI_PROJECT_PATH": "g/app",
    "LOCO_GITLAB_TOKEN": "glpat-x",
}


def jenkins(change_url, **extra):
    return {"JENKINS_URL": "https://ci.example/", "CHANGE_ID": "5", "CHANGE_URL": change_url, **extra}


def target(env, **overrides):
    return resolve_target(detect_ci(env), env, **overrides)


# ── resolving the target ──────────────────────────────────────────────


class TestResolveTarget:
    def test_github_actions(self):
        assert target(GITHUB_ENV) == Target("github", "https://api.github.com", "org/app", "42")

    def test_github_enterprise_api_from_the_runner(self):
        env = {**GITHUB_ENV, "GITHUB_API_URL": "https://ghe.example/api/v3"}
        assert target(env).api_url == "https://ghe.example/api/v3"

    def test_gitlab_merge_request_pipeline(self):
        assert target(GITLAB_ENV) == Target("gitlab", "https://gitlab.example/api/v4", "321", "12")

    def test_jenkins_github(self):
        env = jenkins("https://github.com/org/app/pull/5")
        assert target(env) == Target("github", "https://api.github.com", "org/app", "5")

    def test_jenkins_github_enterprise(self):
        env = jenkins("https://ghe.example/org/app/pull/5")
        assert target(env).api_url == "https://ghe.example/api/v3"

    def test_jenkins_gitlab_with_subgroups(self):
        env = jenkins("https://gitlab.example/g/sub/app/-/merge_requests/5")
        assert target(env) == Target("gitlab", "https://gitlab.example/api/v4", "g/sub/app", "5")

    def test_jenkins_unknown_host_says_what_to_pass(self):
        env = jenkins("https://bitbucket.org/ws/app/pull-requests/5")
        with pytest.raises(CommentError, match="--host"):
            target(env)

    @pytest.mark.parametrize("env", [
        {"GITHUB_ACTIONS": "true", "GITHUB_REPOSITORY": "org/app", "GITHUB_REF": "refs/heads/main"},
        {"GITLAB_CI": "true", "CI_COMMIT_BRANCH": "main"},
        {"JENKINS_URL": "https://ci.example/", "BRANCH_NAME": "main"},
        {},
    ])
    def test_a_branch_build_has_nothing_to_comment_on(self, env):
        assert target(env) is None

    def test_explicit_values_outside_ci(self):
        found = target({}, host="github", project="org/app", number="7")
        assert found == Target("github", "https://api.github.com", "org/app", "7")

    def test_explicit_values_override_detection(self):
        found = target(GITLAB_ENV, project="g/other", api_url="https://gl2.example/api/v4/")
        assert found == Target("gitlab", "https://gl2.example/api/v4", "g/other", "12")

    def test_gitlab_without_an_api_url(self):
        with pytest.raises(CommentError, match="--api-url"):
            target({}, host="gitlab", project="g/app", number="1")


# ── creating and updating ─────────────────────────────────────────────


GH = Target("github", "https://api.github.com", "org/app", "42")
GL = Target("gitlab", "https://gitlab.example/api/v4", "g/app", "12")


class TestUpsert:
    def test_creates_the_first_comment(self):
        host = FakeHost()
        action, comment_id = upsert_comment(GH, "t", "hello", transport=host)

        assert (action, comment_id) == ("created", 999)
        method, url, headers, body = host.requests[-1]
        assert method == "POST"
        assert url == "https://api.github.com/repos/org/app/issues/42/comments"
        assert body["body"] == f"{MARKED}\nhello"
        assert headers["Authorization"] == "Bearer t"

    def test_updates_its_own_comment_even_on_a_later_page(self):
        others = [{"id": n, "body": "someone else", "user": {"login": "dev", "type": "User"}} for n in range(100)]
        mine = {"id": 555, "body": f"{MARKED}\nold", "user": ACTIONS_BOT}
        host = FakeHost(comments=others + [mine])

        action, comment_id = upsert_comment(GH, "t", "new", transport=host)

        assert (action, comment_id) == ("updated", 555)
        assert len(host.listings()) == 2
        assert host.writes() == [("PATCH", "https://api.github.com/repos/org/app/issues/comments/555")]
        assert host.requests[-1][3]["body"].endswith("\nnew")

    def test_a_different_key_is_a_different_comment(self):
        host = FakeHost(comments=[{"id": 1, "body": f"{marker_for('smoke')}\nx", "user": ACTIONS_BOT}])
        action, _ = upsert_comment(GH, "t", "y", key="soak", transport=host)
        assert action == "created"

    def test_gitlab_notes(self):
        bot = {"username": "project_321_bot"}
        host = FakeHost(comments=[{"id": 77, "body": f"{MARKED}\nold", "author": bot}], me=bot)
        action, _ = upsert_comment(GL, "glpat", "new", transport=host)

        assert action == "updated"
        method, url, headers, _ = host.requests[-1]
        assert method == "PUT"
        assert url == "https://gitlab.example/api/v4/projects/g%2Fapp/merge_requests/12/notes/77"
        assert headers["PRIVATE-TOKEN"] == "glpat"

    def test_a_long_body_is_cut_to_what_github_accepts(self):
        host = FakeHost()
        upsert_comment(GH, "t", "x" * (MAX_BODY * 2), transport=host)
        body = host.requests[-1][3]["body"]
        assert len(body) <= MAX_BODY + len(MARKED) + 1
        assert "truncated" in body

    def test_permission_errors_carry_a_hint(self):
        with pytest.raises(CommentError, match="HTTP 403.*api' scope"):
            upsert_comment(GL, "t", "x", transport=FakeHost(status=403))

    @pytest.mark.parametrize("error", [
        urllib.error.URLError("connection refused"),
        # What a server or proxy that hangs up mid-request raises: not a URLError.
        http.client.RemoteDisconnected("Remote end closed connection without response"),
        TimeoutError("timed out"),
    ])
    def test_network_errors_become_comment_errors(self, monkeypatch, error):
        def fail(*args, **kwargs):
            raise error

        monkeypatch.setattr("urllib.request.urlopen", fail)
        with pytest.raises(CommentError, match="could not reach"):
            _urllib_transport("GET", "https://api.github.com/x", {}, None)

    def test_a_non_json_listing_is_an_error_not_a_loop(self):
        def html_page(method, url, headers, body):
            return 200, "<html>" + "x" * 500 + "</html>"

        with pytest.raises(CommentError, match="did not return a JSON list"):
            upsert_comment(GH, "t", "x", transport=html_page)


class TestOnlyItsOwnComment:
    """Anyone who can comment can paste the marker; that must not be followed."""

    def test_github_token_leaves_a_persons_marked_comment_alone(self):
        planted = {"id": 1, "body": f"{MARKED}\nlooks official", "user": {"login": "mallory", "type": "User"}}
        host = FakeHost(comments=[planted])

        action, _ = upsert_comment(GH, "ghs_x", "results", transport=host)

        assert action == "created"
        assert host.writes() == [("POST", "https://api.github.com/repos/org/app/issues/42/comments")]

    def test_a_personal_token_edits_only_the_comment_it_wrote(self):
        planted = {"id": 1, "body": f"{MARKED}\nfake", "user": {"login": "mallory", "type": "User"}}
        mine = {"id": 2, "body": f"{MARKED}\nold", "user": {"login": "perf-bot", "type": "User"}}
        host = FakeHost(comments=[planted, mine], me={"login": "perf-bot"})

        action, comment_id = upsert_comment(GH, "pat", "new", transport=host)

        assert (action, comment_id) == ("updated", 2)

    def test_gitlab_edits_only_the_token_users_note(self):
        planted = {"id": 1, "body": f"{MARKED}\nfake", "author": {"username": "mallory"}}
        mine = {"id": 2, "body": f"{MARKED}\nold", "author": {"username": "project_321_bot"}}
        host = FakeHost(comments=[planted, mine], me={"username": "project_321_bot"})

        action, comment_id = upsert_comment(GL, "glpat", "new", transport=host)

        assert (action, comment_id) == ("updated", 2)

    def test_gitlab_without_an_identity_never_edits(self):
        planted = {"id": 1, "body": f"{MARKED}\nfake", "author": {"username": "mallory"}}
        host = FakeHost(comments=[planted])

        action, _ = upsert_comment(GL, "glpat", "new", transport=host)

        assert action == "created"


class TestPostComment:
    def test_github_actions_end_to_end(self):
        host = FakeHost()
        result = post_comment("hi", env=GITHUB_ENV, transport=host)
        assert result.describe() == "Created comment on org/app#42"

    def test_gitlab_needs_a_real_token(self):
        env = {k: v for k, v in GITLAB_ENV.items() if k != "LOCO_GITLAB_TOKEN"}
        env["CI_JOB_TOKEN"] = "job"
        with pytest.raises(CommentError, match="LOCO_GITLAB_TOKEN.*CI_JOB_TOKEN cannot"):
            post_comment("hi", env=env, transport=FakeHost())

    def test_token_env_names_another_variable(self):
        env = {**jenkins("https://github.com/org/app/pull/5"), "PERF_BOT_TOKEN": "t"}
        host = FakeHost()
        post_comment("hi", env=env, token_env="PERF_BOT_TOKEN", transport=host)
        assert host.requests[-1][2]["Authorization"] == "Bearer t"

    def test_dry_run_needs_no_token_and_sends_nothing(self):
        env = jenkins("https://gitlab.example/g/app/-/merge_requests/5")
        host = FakeHost()
        result = post_comment("hi", env=env, dry_run=True, transport=host)
        assert result.describe() == "Would comment on g/app!5 through https://gitlab.example/api/v4"
        assert host.requests == []

    def test_branch_build(self):
        assert post_comment("hi", env={"JENKINS_URL": "https://ci/"}) is None


# ── the CLI ───────────────────────────────────────────────────────────


@pytest.fixture
def clean_env(monkeypatch):
    prefixes = ("GITHUB_", "GITLAB_", "CI_", "JENKINS_", "BUILD_", "GIT_", "CHANGE_", "BRANCH_", "LOCO_")
    for name in list(os.environ):
        if name.startswith(prefixes) or name in ("GH_TOKEN", "GITLAB_TOKEN"):
            monkeypatch.delenv(name)
    return monkeypatch


class TestCommand:
    def test_dry_run(self, clean_env, tmp_path, capsys):
        for name, value in jenkins("https://github.com/org/app/pull/5").items():
            clean_env.setenv(name, value)
        body = tmp_path / "summary.md"
        body.write_text("### Load test: PASS\n", encoding="utf-8")

        code = cli.main(["comment", "--body", str(body), "--dry-run"])

        out = capsys.readouterr().out
        assert code == 0
        assert "Would comment on org/app#5" in out
        assert "### Load test: PASS" in out

    def test_needs_no_config_file(self, clean_env, tmp_path, capsys):
        body = tmp_path / "summary.md"
        body.write_text("x", encoding="utf-8")
        code = cli.main(["--config", str(tmp_path / "missing.json"), "comment", "--body", str(body)])
        assert code == 0
        assert "nothing to comment on" in capsys.readouterr().out

    def test_missing_body(self, clean_env, tmp_path, capsys):
        code = cli.main(["comment", "--body", str(tmp_path / "nope.md")])
        assert code == 1
        assert "summary file not found" in capsys.readouterr().err

    def test_errors_are_one_line(self, clean_env, tmp_path, capsys):
        for name, value in jenkins("https://bitbucket.org/ws/app/pull-requests/5").items():
            clean_env.setenv(name, value)
        body = tmp_path / "summary.md"
        body.write_text("x", encoding="utf-8")
        code = cli.main(["comment", "--body", str(body)])
        err = capsys.readouterr().err
        assert code == 1
        assert err.startswith("Error: ")
        assert "Traceback" not in err
