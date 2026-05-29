"""``/project`` — show the predefined-project picker.

Tapping a project records the choice on the sidecar (or, when no topic
is bound yet, in a per-user "next session" override). Phase 1's plumbing
is enough today because :mod:`ccgram_pro.config` is already the source
of project metadata.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from ..config import Project, load_projects

if TYPE_CHECKING:
    from telegram import Update
    from telegram.ext import ContextTypes

logger = structlog.get_logger()

_PREFIX = "ccgrampro:project:"


def _project_button_text(project: Project) -> str:
    return f"📁 {project.label}"


async def project_command(
    update: "Update", context: "ContextTypes.DEFAULT_TYPE"
) -> None:
    """Render the predefined-projects keyboard."""
    # Lazy: PTB types only needed on the handler/send path.
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    del context  # not used — picker has no per-call state today

    if update.message is None:
        return

    projects = load_projects()
    if not projects:
        await update.message.reply_text(
            "_No projects configured. Add `[[project]]` entries to "
            "`~/.ccgram/layer/projects.toml`._",
            parse_mode="Markdown",
        )
        return

    buttons = [
        [
            InlineKeyboardButton(
                _project_button_text(p),
                callback_data=f"{_PREFIX}{idx}",
            )
        ]
        for idx, p in enumerate(projects)
    ]
    keyboard = InlineKeyboardMarkup(buttons)
    body_lines = [
        "*📁 Pick a project*",
        "",
        "_When you create your next topic, the directory picker will start in the chosen project's path. Pick again to switch._",
    ]
    await update.message.reply_text(
        "\n".join(body_lines),
        parse_mode="Markdown",
        reply_markup=keyboard,
    )


async def project_picker_callback(
    update: "Update", context: "ContextTypes.DEFAULT_TYPE"
) -> None:
    """Handle a tap on a project button. Records the choice for the user."""
    query = update.callback_query
    if query is None or not query.data:
        return
    data = query.data
    if not data.startswith(_PREFIX):
        await query.answer("Invalid callback", show_alert=True)
        return
    try:
        idx = int(data[len(_PREFIX) :])
    except ValueError:
        await query.answer("Invalid project index", show_alert=True)
        return

    projects = load_projects()
    if idx < 0 or idx >= len(projects):
        await query.answer("Project no longer exists", show_alert=True)
        return
    project = projects[idx]

    # Persist on user_data so the next topic-creation picker honors it.
    user_data = context.user_data if context.user_data is not None else {}
    user_data["ccgrampro_pending_project"] = str(project.path)
    user_data["ccgrampro_pending_model"] = project.default_model
    user_data["ccgrampro_pending_reasoning"] = project.default_reasoning

    await query.answer(f"Project: {project.label}")
    await query.edit_message_text(
        text=(
            f"✅ Next session will start in **{project.label}** "
            f"(`{project.path}`) with model `{project.default_model}` "
            f"reasoning `{project.default_reasoning}`."
        ),
        parse_mode="Markdown",
        reply_markup=None,
    )


__all__ = ["project_command", "project_picker_callback"]
