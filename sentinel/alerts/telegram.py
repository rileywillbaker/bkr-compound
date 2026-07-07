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
        """'Test connection': verify the token by calling getMe."""
        from telegram import Bot

        async def _me():
            return await Bot(self._token).get_me()

        try:
            me = asyncio.run(_me())
            return ProviderCheck(
                provider="telegram", ok=True, detail=f"bot @{me.username} reachable"
            )
        except Exception as exc:
            return ProviderCheck(provider="telegram", ok=False, detail=str(exc))


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
