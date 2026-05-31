"""Web PR composer — ``GET/POST /compose/{token}`` (Open-PR only, tightly scoped).

Security model: a GET *share* token is a read bearer and can NEVER reach this
surface. The composer is gated by a separate, short-lived ``compose`` token
minted only from a Telegram tap by the authenticated user, plus a single-use
server-side CSRF nonce embedded in the form. The only mutating action is
opening a PR for the window's already-pushed current branch — branch/commit/
push stay Telegram-only. The repo is resolved from the token's window_id and
never from the request body.
"""

from __future__ import annotations

import asyncio
import html
from typing import TYPE_CHECKING

import structlog

from ccgram.miniapp.server import _BOT_TOKEN_KEY  # type: ignore[attr-defined]

from ..share.csrf import consume_nonce, mint_nonce
from ..share.tokens import InvalidShareToken, verify_compose_token
from ._page_shell import error_page, render_page

if TYPE_CHECKING:
    from aiohttp import web

logger = structlog.get_logger()

_MAX_BODY_CHARS = 60_000


def _resolve_repo(window_id: str) -> str | None:
    # Lazy: state pulls in the query layer.
    from .. import state

    return state.resolve_repo(window_id)


def _default_base(repo: str, branches: list[str]) -> str:
    # Lazy: layer module deferred to the call path.
    from ..git_ops import GitOpError, current_branch

    # Lazy: layer module deferred to the call path.
    from ..git_ops._run import run_git

    try:
        result = run_git(repo, "rev-parse", "--abbrev-ref", "origin/HEAD", check=False)
        ref = result.stdout.strip()
        if result.returncode == 0 and ref.startswith("origin/"):
            return ref[len("origin/") :]
    except GitOpError:
        pass
    for candidate in ("main", "master", "develop"):
        if candidate in branches:
            return candidate
    try:
        return current_branch(repo)
    except GitOpError:
        return branches[0] if branches else "main"


def _render_form(
    token: str, *, head: str, base: str, branches: list[str], nonce: str
) -> str:
    options = (
        "\n".join(
            f'<option value="{html.escape(b)}"{" selected" if b == base else ""}>{html.escape(b)}</option>'
            for b in branches
            if b != head
        )
        or f'<option value="{html.escape(base)}" selected>{html.escape(base)}</option>'
    )
    body = (
        "<h1>🔀 Open pull request</h1>"
        f'<div><span class="chip">🌿 head <code>{html.escape(head)}</code></span>'
        f'<span class="chip">⎇ base <code>{html.escape(base)}</code></span></div>'
        f'<form method="POST" action="/compose/{html.escape(token)}/pr">'
        f'<input type="hidden" name="csrf" value="{html.escape(nonce)}">'
        "<label>Title</label>"
        '<input type="text" name="title" maxlength="256" required autofocus>'
        "<label>Body</label>"
        '<textarea name="body" maxlength="60000" placeholder="Optional"></textarea>'
        f'<label>Base branch</label><select name="base">{options}</select>'
        '<div class="row"><input type="checkbox" name="draft" id="draft">'
        '<label for="draft" style="margin:0">Open as draft</label></div>'
        '<button type="submit">Open PR</button>'
        "</form>"
    )
    return render_page(title="Open PR", body_html=body)


async def _handle_compose(request: "web.Request") -> "web.Response":
    # Lazy: aiohttp only needed inside the request handler.
    from aiohttp import web

    # Lazy: layer module deferred to the call path.
    from ..git_ops import (
        GitOpError,
        PRValidationError,
        current_branch,
        is_git_repo,
        list_branches,
        preflight_pull_request,
    )

    token = request.match_info.get("token", "")
    bot_token = request.app[_BOT_TOKEN_KEY]
    try:
        payload = verify_compose_token(token, bot_token=bot_token)
    except InvalidShareToken as exc:
        return web.Response(
            status=403,
            text=error_page(_token_reason(exc), title="Link expired"),
            content_type="text/html",
        )
    window_id = payload.share_id
    repo = _resolve_repo(window_id)
    if repo is None or not await asyncio.to_thread(is_git_repo, repo):
        return web.Response(
            status=404,
            text=error_page("No git repository for this session."),
            content_type="text/html",
        )
    try:
        head = await asyncio.to_thread(current_branch, repo)
        branches = [b.name for b in await asyncio.to_thread(list_branches, repo)]
        base = _default_base(repo, branches)
        await asyncio.to_thread(preflight_pull_request, repo, base=base, head=head)
    except PRValidationError as exc:
        return web.Response(
            status=409,
            text=error_page(str(exc), title="Not ready"),
            content_type="text/html",
        )
    except (GitOpError, OSError) as exc:
        return web.Response(
            status=409,
            text=error_page(str(exc), title="Not ready"),
            content_type="text/html",
        )
    nonce = mint_nonce(window_id)
    return web.Response(
        text=_render_form(token, head=head, base=base, branches=branches, nonce=nonce),
        content_type="text/html",
    )


async def _handle_compose_pr(request: "web.Request") -> "web.Response":
    # Lazy: aiohttp only needed inside the request handler.
    from aiohttp import web

    # Lazy: layer module deferred to the call path.
    from ..git_ops import (
        GitOpError,
        PRValidationError,
        PullRequestError,
        create_pull_request,
        current_branch,
        preflight_pull_request,
    )

    token = request.match_info.get("token", "")
    bot_token = request.app[_BOT_TOKEN_KEY]
    try:
        payload = verify_compose_token(token, bot_token=bot_token)
    except InvalidShareToken as exc:
        return web.Response(
            status=403,
            text=error_page(_token_reason(exc), title="Link expired"),
            content_type="text/html",
        )
    window_id = payload.share_id

    data = await request.post()
    if not consume_nonce(str(data.get("csrf", "")), window_id):
        return web.Response(
            status=403,
            text=error_page(
                "This form expired — reopen it from Telegram.", title="Expired"
            ),
            content_type="text/html",
        )
    title = str(data.get("title", "")).strip()
    if not title:
        return web.Response(
            status=400,
            text=error_page("A title is required.", title="Missing title"),
            content_type="text/html",
        )
    body = str(data.get("body", ""))[:_MAX_BODY_CHARS]
    base = str(data.get("base", "")).strip()
    draft = data.get("draft") is not None

    repo = _resolve_repo(window_id)
    if repo is None:
        return web.Response(
            status=404,
            text=error_page("No git repository for this session."),
            content_type="text/html",
        )
    try:
        head = await asyncio.to_thread(current_branch, repo)
        # Re-validate at submit (the branch could have moved since GET).
        await asyncio.to_thread(preflight_pull_request, repo, base=base, head=head)
        url = await asyncio.to_thread(
            create_pull_request,
            repo,
            title=title,
            body=body,
            base=base,
            head=head,
            draft=draft,
        )
    except (PRValidationError, PullRequestError, GitOpError) as exc:
        return web.Response(
            status=409,
            text=error_page(str(exc), title="Could not open PR"),
            content_type="text/html",
        )

    safe_url = html.escape(url)
    page = render_page(
        title="PR opened",
        body_html=f'<h1>✅ Pull request opened</h1><p><a href="{safe_url}">{safe_url}</a></p>',
    )
    return web.Response(text=page, content_type="text/html")


def _token_reason(exc: InvalidShareToken) -> str:
    msg = str(exc)
    if "expired" in msg:
        return "This compose link has expired. Reopen it from Telegram."
    return "This compose link is invalid."


def register_compose_routes(app: "web.Application") -> None:
    """Register the web PR composer routes on the aiohttp app."""
    app.router.add_get("/compose/{token}", _handle_compose)
    app.router.add_post("/compose/{token}/pr", _handle_compose_pr)
    logger.debug("ccgram-pro compose routes registered: GET/POST /compose/{token}")
