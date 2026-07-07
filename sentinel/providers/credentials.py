"""Encrypted provider-credential store.

Keys pasted in the Settings UI are stored in the `provider_credentials` table,
encrypted with Fernet. The Fernet key is derived from APP_SECRET_KEY, so the
database alone never reveals a key. Resolution order: database value first,
environment/.env second.

Values are write-only from the client's perspective: the API returns only a
masked preview (last 4 chars) after saving.
"""

import base64
import hashlib
from datetime import datetime

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select
from sqlalchemy.orm import Session

from sentinel.config import get_settings

# Field registry: provider -> settings attribute per field name.
# Used for env fallback and for the Settings UI to know what to render.
CREDENTIAL_FIELDS: dict[str, dict[str, str]] = {
    "alpaca": {"api_key": "alpaca_api_key", "api_secret": "alpaca_api_secret"},
    "finnhub": {"api_key": "finnhub_api_key"},
    "fred": {"api_key": "fred_api_key"},
    "anthropic": {"api_key": "anthropic_api_key"},
    "telegram": {"bot_token": "telegram_bot_token", "chat_id": "telegram_chat_id"},
    "edgar": {"user_agent": "edgar_user_agent"},
}


def _fernet() -> Fernet:
    secret = get_settings().app_secret_key.encode()
    digest = hashlib.sha256(secret).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_value(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def decrypt_value(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken as exc:
        raise ValueError(
            "Cannot decrypt stored credential — APP_SECRET_KEY changed?"
        ) from exc


def mask(value: str) -> str:
    if not value:
        return ""
    return "•••• " + value[-4:] if len(value) > 4 else "••••"


def store_credential(db: Session, provider: str, field: str, value: str) -> None:
    from sentinel.db.models import ProviderCredential

    if provider not in CREDENTIAL_FIELDS or field not in CREDENTIAL_FIELDS[provider]:
        raise ValueError(f"Unknown credential {provider}.{field}")
    row = db.execute(
        select(ProviderCredential).where(
            ProviderCredential.provider == provider,
            ProviderCredential.field == field,
        )
    ).scalar_one_or_none()
    if row is None:
        row = ProviderCredential(provider=provider, field=field)
        db.add(row)
    row.encrypted_value = encrypt_value(value)
    row.updated_at = datetime.utcnow()
    db.flush()


def get_credential(db: Session | None, provider: str, field: str) -> str:
    """Resolve a credential: DB (encrypted) first, then environment settings."""
    if db is not None:
        from sentinel.db.models import ProviderCredential

        row = db.execute(
            select(ProviderCredential).where(
                ProviderCredential.provider == provider,
                ProviderCredential.field == field,
            )
        ).scalar_one_or_none()
        if row is not None and row.encrypted_value:
            return decrypt_value(row.encrypted_value)

    attr = CREDENTIAL_FIELDS.get(provider, {}).get(field)
    if attr is None:
        raise ValueError(f"Unknown credential {provider}.{field}")
    return str(getattr(get_settings(), attr, "") or "")


def credential_status(db: Session | None) -> dict[str, dict[str, str]]:
    """Masked overview of what's configured, for the Settings UI."""
    out: dict[str, dict[str, str]] = {}
    for provider, fields in CREDENTIAL_FIELDS.items():
        out[provider] = {}
        for field in fields:
            try:
                out[provider][field] = mask(get_credential(db, provider, field))
            except ValueError:
                out[provider][field] = "(undecryptable)"
    return out
