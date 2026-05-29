"""``gh`` CLI wrappers for PR open / list."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from ._run import GitOpError, run_gh


class PullRequestError(RuntimeError):
    """Raised when the gh invocation fails or returns unexpected payload."""


@dataclass(frozen=True)
class PullRequestSummary:
    number: int
    title: str
    state: str
    url: str
    head_ref: str
    base_ref: str


def list_pull_requests(
    repo: Path | str, *, state: str = "open"
) -> list[PullRequestSummary]:
    """List PRs for the repo at *repo* (defaults to the current cwd)."""
    fields = "number,title,state,url,headRefName,baseRefName"
    try:
        result = run_gh(
            "pr",
            "list",
            "--state",
            state,
            "--json",
            fields,
            cwd=repo,
        )
    except GitOpError as exc:
        raise PullRequestError(str(exc)) from exc
    try:
        payload = json.loads(result.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise PullRequestError(f"gh returned non-JSON: {exc}") from exc
    return [
        PullRequestSummary(
            number=int(entry["number"]),
            title=str(entry["title"]),
            state=str(entry["state"]),
            url=str(entry["url"]),
            head_ref=str(entry["headRefName"]),
            base_ref=str(entry["baseRefName"]),
        )
        for entry in payload
    ]


def create_pull_request(
    repo: Path | str,
    *,
    title: str,
    body: str = "",
    base: str | None = None,
    head: str | None = None,
    draft: bool = False,
) -> str:
    """Create a PR via ``gh pr create``. Returns the new PR URL."""
    args: list[str] = ["pr", "create", "--title", title, "--body", body]
    if base:
        args.extend(["--base", base])
    if head:
        args.extend(["--head", head])
    if draft:
        args.append("--draft")
    try:
        result = run_gh(*args, cwd=repo, timeout=60)
    except GitOpError as exc:
        raise PullRequestError(str(exc)) from exc
    # gh prints the URL on the last line of stdout.
    url = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""
    if not url.startswith("http"):
        raise PullRequestError(f"unexpected gh stdout: {result.stdout!r}")
    return url
