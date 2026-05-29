"""``/model`` — pick the Claude model + reasoning level for the current/next session."""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from telegram import Update
    from telegram.ext import ContextTypes

logger = structlog.get_logger()

_PREFIX = "ccgrampro:model:"

# Display order matches Claude Code's own --model documentation: opus →
# sonnet → haiku, with the layer's "extra-high" reasoning default for
# opus and shorter reasoning for the smaller variants.
_MODEL_OPTIONS: list[tuple[str, str, str]] = [
    ("Opus + Extra High thinking (best)", "opus", "extra-high"),
    ("Opus + High thinking", "opus", "high"),
    ("Sonnet + Medium thinking (faster)", "sonnet", "medium"),
    ("Haiku + Low thinking (cheapest)", "haiku", "low"),
]


async def model_command(update: "Update", context: "ContextTypes.DEFAULT_TYPE") -> None:
    # Lazy: PTB types only needed on the handler/send path.
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    del context

    if update.message is None:
        return
    buttons = [
        [InlineKeyboardButton(label, callback_data=f"{_PREFIX}{idx}")]
        for idx, (label, _, _) in enumerate(_MODEL_OPTIONS)
    ]
    await update.message.reply_text(
        "*🧠 Pick a model + reasoning*\n\n_Applies to your next topic creation._",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def model_picker_callback(
    update: "Update", context: "ContextTypes.DEFAULT_TYPE"
) -> None:
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
        await query.answer("Invalid option", show_alert=True)
        return
    if idx < 0 or idx >= len(_MODEL_OPTIONS):
        await query.answer("Unknown option", show_alert=True)
        return
    label, model, reasoning = _MODEL_OPTIONS[idx]
    user_data = context.user_data if context.user_data is not None else {}
    user_data["ccgrampro_pending_model"] = model
    user_data["ccgrampro_pending_reasoning"] = reasoning
    await query.answer(f"Model: {model}")
    await query.edit_message_text(
        text=f"✅ Next session: **{label}** (`{model}` / `{reasoning}`).",
        parse_mode="Markdown",
        reply_markup=None,
    )


__all__ = ["model_command", "model_picker_callback"]
