"""Post a run's summary to its pull or merge request, as one comment kept current.

``loco ci --summary summary.md`` writes the text and ``loco comment --body
summary.md`` puts it on the change request the build belongs to. The next push
edits that same comment instead of adding another — it is found again by a
hidden marker — so a busy pull request does not collect one comment per push.

The API spoken is the code host's, not the CI's: a Jenkins build of a GitHub
pull request comments through GitHub. Only the standard library is used.
"""
from __future__ import annotations

import http.client
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from .ci import GITHUB, GITLAB, CIContext, _get, detect_ci

# GitHub refuses comments longer than 65536 characters; GitLab allows far more.
MAX_BODY = 65000
_PER_PAGE = 100
_MAX_PAGES = 50

Transport = Callable[[str, str, Dict[str, str], Optional[bytes]], Tuple[int, Any]]

_GITHUB_PR = re.compile(r"^(https?://[^/]+)/([^/]+/[^/]+)/pull/(\d+)/?$")
_GITLAB_MR = re.compile(r"^(https?://[^/]+)/(.+?)/-/merge_requests/(\d+)/?$")

_TOKEN_ENVS = {
    GITHUB: ("LOCO_GITHUB_TOKEN", "GITHUB_TOKEN", "GH_TOKEN"),
    GITLAB: ("LOCO_GITLAB_TOKEN", "GITLAB_TOKEN"),
}

_HINTS = {
    (GITHUB, 403): " — the token needs 'pull-requests: write'; GITHUB_TOKEN is read-only "
                   "on pull requests from forks",
    (GITHUB, 404): " — check the repository and pull request number, and that the token "
                   "can see the repository",
    (GITLAB, 403): " — the token needs the 'api' scope and at least the Reporter role; "
                   "CI_JOB_TOKEN cannot write comments",
    (GITLAB, 404): " — check the project and merge request IID, and that the token can "
                   "see the project",
    (None, 401): " — the token was rejected (wrong or expired)",
}


class CommentError(Exception):
    """Commenting failed in a way the user can act on."""


@dataclass(frozen=True)
class Target:
    host: str
    api_url: str
    # owner/repo on GitHub; a group/project path or numeric id on GitLab.
    project: str
    number: str

    def label(self) -> str:
        return f"{self.project}{'!' if self.host == GITLAB else '#'}{self.number}"


@dataclass(frozen=True)
class CommentResult:
    action: str  # "created", "updated" or "dry-run"
    target: Target
    comment_id: Any = None

    def describe(self) -> str:
        if self.action == "dry-run":
            return f"Would comment on {self.target.label()} through {self.target.api_url}"
        return f"{self.action.capitalize()} comment on {self.target.label()}"


# ── where the comment goes ────────────────────────────────────────────


def _github_api_for(base: str) -> str:
    if urllib.parse.urlparse(base).netloc.lower() == "github.com":
        return "https://api.github.com"
    return f"{base}/api/v3"  # GitHub Enterprise Server


def _from_context(ctx: CIContext, env: Mapping[str, str]) -> Optional[Target]:
    if not ctx.change_id:
        return None
    if ctx.provider == GITHUB and ctx.repository:
        api = _get(env, "GITHUB_API_URL") or "https://api.github.com"
        return Target(GITHUB, api, ctx.repository, ctx.change_id)
    if ctx.provider == GITLAB and _get(env, "CI_MERGE_REQUEST_IID"):
        project = _get(env, "CI_MERGE_REQUEST_PROJECT_ID", "CI_PROJECT_ID") or ctx.repository or ""
        return Target(GITLAB, _get(env, "CI_API_V4_URL") or "", project, ctx.change_id)
    # Everything else — Jenkins above all — only knows the change by its URL.
    url = ctx.change_url or ""
    match = _GITHUB_PR.match(url)
    if match:
        base, repo, number = match.groups()
        return Target(GITHUB, _github_api_for(base), repo, number)
    match = _GITLAB_MR.match(url)
    if match:
        base, path, number = match.groups()
        return Target(GITLAB, f"{base}/api/v4", path, number)
    return None


def resolve_target(
    ctx: CIContext,
    env: Mapping[str, str],
    *,
    host: Optional[str] = None,
    api_url: Optional[str] = None,
    project: Optional[str] = None,
    number: Optional[str] = None,
) -> Optional[Target]:
    """The pull/merge request to comment on; None for a build that has none.

    Explicit values win over what the CI environment says.
    """
    guessed = _from_context(ctx, env)
    if guessed is None and not number:
        if ctx.change_id:
            where = f" ({ctx.change_url})" if ctx.change_url else ""
            raise CommentError(
                f"this build is for change {ctx.change_id}{where}, but its code host could "
                "not be recognised; pass --host, --project and --number "
                "(and --api-url for GitLab or GitHub Enterprise)"
            )
        return None

    host = host or (guessed.host if guessed else None)
    if host not in (GITHUB, GITLAB):
        raise CommentError("pass --host github or --host gitlab")
    same_host = guessed is not None and guessed.host == host
    project = project or (guessed.project if same_host else None)
    number = str(number or (guessed.number if guessed else ""))
    api_url = api_url or (guessed.api_url if same_host else None)
    if not api_url:
        api_url = _get(env, "GITHUB_API_URL") or "https://api.github.com" if host == GITHUB \
            else _get(env, "CI_API_V4_URL")
    if not project or not number:
        raise CommentError("pass --project and --number: they could not be detected")
    if not api_url:
        raise CommentError("pass --api-url, e.g. https://gitlab.example.com/api/v4")
    return Target(host, api_url.rstrip("/"), project, number)


def _token(host: str, env: Mapping[str, str], token_env: Optional[str]) -> str:
    names = (token_env,) if token_env else _TOKEN_ENVS[host]
    token = _get(env, *names)
    if token:
        return token
    message = f"no token for {host}: set {' or '.join(names)}"
    if host == GITLAB:
        message += (
            " (a project access token with the 'api' scope; CI_JOB_TOKEN cannot "
            "write merge request comments)"
        )
    raise CommentError(message)


# ── talking to the host ───────────────────────────────────────────────


def _urllib_transport(
    method: str, url: str, headers: Dict[str, str], body: Optional[bytes]
) -> Tuple[int, Any]:
    request = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            status, raw = response.status, response.read()
    except urllib.error.HTTPError as exc:
        status, raw = exc.code, exc.read()
    except (urllib.error.URLError, http.client.HTTPException, OSError) as exc:
        # URLError covers DNS and refused connections; a proxy or server that
        # hangs up mid-request raises RemoteDisconnected, which is neither.
        reason = getattr(exc, "reason", None) or exc
        raise CommentError(f"could not reach {url}: {reason}") from exc
    try:
        payload: Any = json.loads(raw) if raw else None
    except ValueError:
        payload = raw.decode("utf-8", "replace")
    return status, payload


class _Client:
    def __init__(self, target: Target, token: str, transport: Transport) -> None:
        self.target = target
        self.token = token
        self.transport = transport

    def _call(self, method: str, url: str, payload: Optional[Dict[str, Any]], what: str) -> Any:
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        status, data = self.transport(method, url, self.headers(), body)
        if 200 <= status < 300:
            return data
        message = data.get("message") if isinstance(data, dict) else None
        detail = f": {message}" if isinstance(message, str) and message else ""
        hint = _HINTS.get((self.target.host, status)) or _HINTS.get((None, status), "")
        raise CommentError(f"{what} failed with HTTP {status}{detail}{hint}")

    def comments(self) -> List[Dict[str, Any]]:
        found: List[Dict[str, Any]] = []
        for page in range(1, _MAX_PAGES + 1):
            url = self.list_url(page)
            batch = self._call("GET", url, None, "listing comments") or []
            if not isinstance(batch, list):
                # An HTML login page from a proxy, or the wrong --api-url.
                raise CommentError(f"listing comments at {url} did not return a JSON list; check --api-url")
            found.extend(item for item in batch if isinstance(item, dict))
            if len(batch) < _PER_PAGE:
                break
        return found

    def headers(self) -> Dict[str, str]:
        raise NotImplementedError

    def list_url(self, page: int) -> str:
        raise NotImplementedError

    def create(self, body: str) -> Any:
        raise NotImplementedError

    def update(self, comment_id: Any, body: str) -> Any:
        raise NotImplementedError


class _GitHub(_Client):
    def _base(self) -> str:
        return f"{self.target.api_url}/repos/{self.target.project}/issues"

    def headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
            "User-Agent": "locomotive",
        }

    def list_url(self, page: int) -> str:
        return f"{self._base()}/{self.target.number}/comments?per_page={_PER_PAGE}&page={page}"

    def create(self, body: str) -> Any:
        url = f"{self._base()}/{self.target.number}/comments"
        return self._call("POST", url, {"body": body}, "creating the comment")

    def update(self, comment_id: Any, body: str) -> Any:
        url = f"{self._base()}/comments/{comment_id}"
        return self._call("PATCH", url, {"body": body}, "updating the comment")


class _GitLab(_Client):
    def _base(self) -> str:
        project = urllib.parse.quote(self.target.project, safe="")
        return f"{self.target.api_url}/projects/{project}/merge_requests/{self.target.number}/notes"

    def headers(self) -> Dict[str, str]:
        return {
            "PRIVATE-TOKEN": self.token,
            "Content-Type": "application/json",
            "User-Agent": "locomotive",
        }

    def list_url(self, page: int) -> str:
        return f"{self._base()}?per_page={_PER_PAGE}&page={page}&sort=asc&order_by=created_at"

    def create(self, body: str) -> Any:
        return self._call("POST", self._base(), {"body": body}, "creating the comment")

    def update(self, comment_id: Any, body: str) -> Any:
        return self._call("PUT", f"{self._base()}/{comment_id}", {"body": body}, "updating the comment")


def marker_for(key: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", key).strip("-") or "loadtest"
    return f"<!-- locomotive:comment:{safe} -->"


def _fit(body: str) -> str:
    if len(body) <= MAX_BODY:
        return body
    note = "\n\n_…truncated: the full summary is in the build artifacts._\n"
    return body[: MAX_BODY - len(note)] + note


def upsert_comment(
    target: Target,
    token: str,
    body: str,
    *,
    key: str = "loadtest",
    transport: Optional[Transport] = None,
) -> Tuple[str, Any]:
    """Edit Locomotive's earlier comment for *key*, or create one."""
    client_cls = _GitLab if target.host == GITLAB else _GitHub
    client = client_cls(target, token, transport or _urllib_transport)
    marker = marker_for(key)
    text = f"{marker}\n{_fit(body)}"
    for comment in client.comments():
        if marker in str(comment.get("body") or ""):
            client.update(comment["id"], text)
            return "updated", comment["id"]
    created = client.create(text)
    return "created", created.get("id") if isinstance(created, dict) else None


def post_comment(
    body: str,
    *,
    key: str = "loadtest",
    host: Optional[str] = None,
    api_url: Optional[str] = None,
    project: Optional[str] = None,
    number: Optional[str] = None,
    token_env: Optional[str] = None,
    dry_run: bool = False,
    env: Optional[Mapping[str, str]] = None,
    transport: Optional[Transport] = None,
) -> Optional[CommentResult]:
    """Comment on this build's pull/merge request; None if it has none."""
    env = os.environ if env is None else env
    target = resolve_target(
        detect_ci(env), env, host=host, api_url=api_url, project=project, number=number
    )
    if target is None:
        return None
    if dry_run:
        return CommentResult("dry-run", target)
    token = _token(target.host, env, token_env)
    action, comment_id = upsert_comment(target, token, body, key=key, transport=transport)
    return CommentResult(action, target, comment_id)
