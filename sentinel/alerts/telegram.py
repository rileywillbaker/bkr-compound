"""Telegram channel (spec §6, python-telegram-bot).

The bot token and chat id live in the encrypted credential store (Settings →
Alerts), falling back to TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID env vars.
python-telegram-bot v21 is async; callers here are sync scheduler jobs and
threadpool API handlers, so each call runs its own short event loop.
"""

import asyncio

import structlog
from sqlalchemy.orm import Session

from sentinel.providers.credentials import get_credential
from sentinel.providers.registry import CredentialsMissing
from sentinel.providers.types import ProviderCheck

log = structlog.get_logger()


class TelegramChannel:
    def __init__(self, token: str, chat_id: str):
        self._token = token
        self._chat_id = chat_id

    def send(self, text: str) -> tuple[bool, str]:
        """Send one message. Returns (ok, error_detail)."""
        from telegram import Bot

        async def _send() -> None:
            await Bot(self._token).send_message(chat_id=self._chat_id, text=text)

        try:
            asyncio.run(_send())
            return True, ""
        except Exception as exc:
            log.warning("telegram send failed", error=str(exc))
            return False, str(exc)

    def validate(self) -> ProviderCheck:
        """'Test connection': verify the token via getMe, then send a real
        message to the configured chat so delivery is proven end-to-end
        (a valid token with a wrong/unreachable chat_id must not pass)."""
        from telegram import Bot

        from sentinel.alerts.format import FOOTER

        async def _check() -> str:
            bot = Bot(self._token)
            me = await bot.get_me()
            await bot.send_message(
                chat_id=self._chat_id,
                text=f"B-Quant test connection OK — alerts will arrive here. {FOOTER}",
            )
            return str(me.username)

        try:
            username = asyncio.run(_check())
            return ProviderCheck(
                provider="telegram",
                ok=True,
                detail=f"test message sent from @{username} — check your Telegram",
            )
        except Exception as exc:
            detail = str(exc)
            lowered = detail.lower()
            if "chat not found" in lowered or "forbidden" in lowered:
                detail += (
                    " — Telegram bots cannot message you until you message them:"
                    " open Telegram, send your bot any message (e.g. 'hi'),"
                    " and double-check the chat ID is your numeric user ID"
                )
            return ProviderCheck(provider="telegram", ok=False, detail=detail)


def build_telegram(db: Session) -> TelegramChannel:
    token = get_credential(db, "telegram", "bot_token")
    chat_id = get_credential(db, "telegram", "chat_id")
    missing = [f for f, v in [("bot_token", token), ("chat_id", chat_id)] if not v]
    if missing:
        raise CredentialsMissing("telegram", missing)
    return TelegramChannel(token, chat_id)


def telegram_configured(db: Session) -> bool:
    try:
        build_telegram(db)
        return True
    except CredentialsMissing:
        return False


def send_telegram(db: Session, text: str) -> tuple[bool, str]:
    try:
        channel = build_telegram(db)
    except CredentialsMissing as exc:
        return False, str(exc)
    return channel.send(text)
