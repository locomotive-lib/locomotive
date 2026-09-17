"""Which CI system is running this process, and what it knows about the build.

Every CI exposes the same handful of facts — the commit, the branch, whether
this is a pull/merge request and into which branch, a link to the build — but
each under its own variable names. Everything downstream wants the facts, not
the names: the run id, the run metadata, and later the report header and the
PR/MR comment. This module is the one place that knows the names.

Detection only reads the environment; nothing here talks to a network or
fails a run. An unknown CI simply looks like a local run.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

LOCAL = "local"
GITHUB = "github"
GITLAB = "gitlab"
JENKINS = "jenkins"


@dataclass(frozen=True)
class CIContext:
    provider: str = LOCAL
    commit: Optional[str] = None
    # The branch being built. For a pull/merge request this is its source
    # branch, not the synthetic ref some systems check out (``12/merge``,
    # ``PR-12``).
    branch: Optional[str] = None
    # Set only for a pull/merge request: the branch it would merge into.
    target_branch: Optional[str] = None
    change_id: Optional[str] = None
    change_url: Optional[str] = None
    # Unique per execution, re-runs included — unlike the commit, which a
    # re-run shares with the run it repeats.
    build_id: Optional[str] = None
    build_url: Optional[str] = None
    repository: Optional[str] = None
    default_branch: Optional[str] = None

    @property
    def is_ci(self) -> bool:
        return self.provider != LOCAL

    def to_meta(self) -> Dict[str, Any]:
        """The known fields, for run.json. Unknown ones are left out."""
        return {key: value for key, value in asdict(self).items() if value}


def _get(env: Mapping[str, str], *names: str) -> Optional[str]:
    """The first of *names* that is set to something other than whitespace."""
    for name in names:
        value = (env.get(name) or "").strip()
        if value:
            return value
    return None


def _github_event(env: Mapping[str, str]) -> Dict[str, Any]:
    path = _get(env, "GITHUB_EVENT_PATH")
    if not path:
        return {}
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _github(env: Mapping[str, str]) -> CIContext:
    server = (_get(env, "GITHUB_SERVER_URL") or "https://github.com").rstrip("/")
    repository = _get(env, "GITHUB_REPOSITORY")
    event = _github_event(env)
    pull = event.get("pull_request") if isinstance(event.get("pull_request"), dict) else {}

    change_id = str(pull["number"]) if pull.get("number") else None
    if not change_id:
        # pull_request events check out refs/pull/<n>/merge; the payload is
        # the better source, but it is not always readable.
        match = re.match(r"refs/pull/(\d+)/", _get(env, "GITHUB_REF") or "")
        change_id = match.group(1) if match else None
    change_url = pull.get("html_url") or (
        f"{server}/{repository}/pull/{change_id}" if change_id and repository else None
    )

    run_id = _get(env, "GITHUB_RUN_ID")
    attempt = _get(env, "GITHUB_RUN_ATTEMPT")
    build_id = f"{run_id}-{attempt}" if run_id and attempt else run_id
    repo_info = event.get("repository") if isinstance(event.get("repository"), dict) else {}

    return CIContext(
        provider=GITHUB,
        commit=_get(env, "GITHUB_SHA"),
        # GITHUB_REF_NAME on a pull request is "12/merge"; the head ref is the
        # branch a person would recognise.
        branch=_get(env, "GITHUB_HEAD_REF", "GITHUB_REF_NAME"),
        target_branch=_get(env, "GITHUB_BASE_REF"),
        change_id=change_id,
        change_url=change_url,
        build_id=build_id,
        build_url=f"{server}/{repository}/actions/runs/{run_id}" if repository and run_id else None,
        repository=repository,
        default_branch=repo_info.get("default_branch") or None,
    )


def _gitlab(env: Mapping[str, str]) -> CIContext:
    # External pull request pipelines (a GitHub repository built by GitLab CI)
    # carry the same facts under CI_EXTERNAL_PULL_REQUEST_*.
    mr_iid = _get(env, "CI_MERGE_REQUEST_IID")
    project_url = _get(env, "CI_MERGE_REQUEST_PROJECT_URL", "CI_PROJECT_URL")
    return CIContext(
        provider=GITLAB,
        commit=_get(env, "CI_COMMIT_SHA"),
        branch=_get(
            env,
            "CI_MERGE_REQUEST_SOURCE_BRANCH_NAME",
            "CI_EXTERNAL_PULL_REQUEST_SOURCE_BRANCH_NAME",
            "CI_COMMIT_BRANCH",
            "CI_COMMIT_REF_NAME",
        ),
        target_branch=_get(
            env,
            "CI_MERGE_REQUEST_TARGET_BRANCH_NAME",
            "CI_EXTERNAL_PULL_REQUEST_TARGET_BRANCH_NAME",
        ),
        change_id=mr_iid or _get(env, "CI_EXTERNAL_PULL_REQUEST_IID"),
        change_url=f"{project_url}/-/merge_requests/{mr_iid}" if mr_iid and project_url else None,
        # CI_JOB_ID is unique across the instance and changes on retry;
        # CI_PIPELINE_ID is shared by every job of the pipeline.
        build_id=_get(env, "CI_JOB_ID"),
        build_url=_get(env, "CI_JOB_URL"),
        repository=_get(env, "CI_PROJECT_PATH"),
        default_branch=_get(env, "CI_DEFAULT_BRANCH"),
    )


def _strip_remote(branch: Optional[str]) -> Optional[str]:
    """``origin/main`` -> ``main``: the git plugin reports the remote ref."""
    if not branch:
        return None
    return re.sub(r"^(refs/heads/|refs/remotes/[^/]+/|origin/)", "", branch) or None


def _jenkins(env: Mapping[str, str]) -> CIContext:
    return CIContext(
        provider=JENKINS,
        commit=_get(env, "GIT_COMMIT"),
        # In a multibranch pull request build BRANCH_NAME is "PR-12" and the
        # real head branch is CHANGE_BRANCH. Outside multibranch there is no
        # BRANCH_NAME at all, only the git plugin's GIT_BRANCH.
        branch=_get(env, "CHANGE_BRANCH", "BRANCH_NAME", "GIT_LOCAL_BRANCH")
        or _strip_remote(_get(env, "GIT_BRANCH")),
        target_branch=_get(env, "CHANGE_TARGET"),
        change_id=_get(env, "CHANGE_ID"),
        change_url=_get(env, "CHANGE_URL"),
        # Unique within one job only — every branch job of a multibranch
        # project counts from 1. Paired with the commit that is enough; the
        # run-id collision guard in the CLI covers what is left.
        build_id=_get(env, "BUILD_NUMBER"),
        build_url=_get(env, "BUILD_URL"),
    )


def detect_ci(env: Optional[Mapping[str, str]] = None) -> CIContext:
    """Describe the CI build this process runs in, or a local run."""
    if env is None:
        import os

        env = os.environ
    if (env.get("GITHUB_ACTIONS") or "").lower() == "true":
        return _github(env)
    if (env.get("GITLAB_CI") or "").lower() == "true":
        return _gitlab(env)
    # JENKINS_URL is only set once the root URL is configured; BUILD_TAG is
    # always set and always starts with "jenkins-".
    if _get(env, "JENKINS_URL") or (env.get("BUILD_TAG") or "").startswith("jenkins-"):
        return _jenkins(env)
    return CIContext()


_UNSAFE_ID_CHARS = re.compile(r"[^A-Za-z0-9._-]+")


def default_run_id(ctx: CIContext, now: Optional[float] = None) -> str:
    """A run id that differs between any two executions.

    The commit alone is not enough: a re-run shares it with the run it
    repeats, and when that run set the baseline the re-run is written into
    the baseline's own directory and compared against itself. So the id is
    the short commit plus the build id, which changes on every execution.
    """
    commit = ctx.commit[:12] if ctx.commit else None
    if commit and ctx.build_id:
        run_id = f"{commit}-{ctx.build_id}"
    elif commit:
        run_id = commit
    elif ctx.build_id:
        run_id = f"build-{ctx.build_id}"
    else:
        run_id = f"run-{int(now if now is not None else time.time())}"
    # The id becomes a directory name.
    return _UNSAFE_ID_CHARS.sub("-", run_id).strip("-") or "run"
