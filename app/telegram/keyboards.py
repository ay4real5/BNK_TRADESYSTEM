"""Inline keyboard layouts for trade confirmation flows."""

from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup


def trade_confirmation_keyboard(signal_id: int) -> InlineKeyboardMarkup:
    """
    Returns keyboard for confirming / rejecting / snoozing a trade signal.

    Callback data format:  trade:<action>:<signal_id>
    """
    keyboard = [
        [
            InlineKeyboardButton("✅ Confirm", callback_data=f"trade:confirm:{signal_id}"),
            InlineKeyboardButton("❌ Reject", callback_data=f"trade:reject:{signal_id}"),
        ],
        [
            InlineKeyboardButton("⏰ Snooze 30m", callback_data=f"trade:snooze:{signal_id}"),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)


def mode_selection_keyboard() -> InlineKeyboardMarkup:
    """Keyboard for selecting trading mode."""
    keyboard = [
        [
            InlineKeyboardButton("👁 ASSIST", callback_data="mode:assist"),
            InlineKeyboardButton("📋 PAPER", callback_data="mode:paper"),
        ],
        [
            InlineKeyboardButton("⚡ LIVE (PIN required)", callback_data="mode:live"),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)


def confirm_live_keyboard() -> InlineKeyboardMarkup:
    """Double-confirm keyboard for enabling LIVE mode."""
    keyboard = [
        [
            InlineKeyboardButton("✅ YES — Enable LIVE", callback_data="mode:live:confirm"),
            InlineKeyboardButton("❌ Cancel", callback_data="mode:live:cancel"),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)
