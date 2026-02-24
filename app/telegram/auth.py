"""Telegram authentication and admin authorization."""

from __future__ import annotations

from functools import wraps
from typing import Callable

from telegram import Update
from telegram.ext import ContextTypes

from ..config import settings


def is_admin(chat_id: int) -> bool:
    """Return True if the given chat_id is in the admin allow-list."""
    if not settings.admin_chat_ids:
        # If no admin IDs configured, allow everyone (dev mode)
        return True
    return chat_id in settings.admin_chat_ids


def admin_only(func: Callable) -> Callable:
    """Decorator: restrict a command handler to admin users."""

    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        if update.effective_user is None:
            return
        if not is_admin(update.effective_user.id):
            await update.message.reply_text(
                "⛔ Access denied. You are not authorised to use this command."
            )
            return
        return await func(update, context, *args, **kwargs)

    return wrapper


async def verify_pin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    Check if the user's message contains the correct PIN.
    Usage: called inside PIN-protected command handlers.
    Uses constant-time comparison to prevent timing attacks.
    """
    import secrets
    args = context.args or []
    if not args:
        await update.message.reply_text("🔐 Please provide the PIN: /command <PIN>")
        return False
    if not secrets.compare_digest(args[0], settings.telegram_pin):
        await update.message.reply_text("❌ Incorrect PIN.")
        return False
    return True
