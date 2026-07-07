"""Provider credential endpoints backing the Settings UI / onboarding wizard.

Values are write-only: responses only ever contain masked previews.
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from sentinel.db.base import get_db
from sentinel.providers.base import ProviderCheck
from sentinel.providers.credentials import (
    CREDENTIAL_FIELDS,
    credential_status,
    store_credential,
)
from sentinel.providers.registry import (
    CredentialsMissing,
    build_filings,
    build_macro,
    build_market_data,
    build_research,
)

router = APIRouter(prefix="/api/providers", tags=["providers"])


class CredentialIn(BaseModel):
    provider: str
    field: str
    value: str


@router.get("")
def list_providers(db: Session = Depends(get_db)) -> dict:
    """Masked overview of configured credentials for the Settings UI."""
    return {
        "fields": {p: list(f.keys()) for p, f in CREDENTIAL_FIELDS.items()},
        "configured": credential_status(db),
    }


@router.put("/credentials")
def put_credential(body: CredentialIn, db: Session = Depends(get_db)) -> dict:
    if not body.value.strip():
        raise HTTPException(422, "value must not be empty")
    try:
        store_credential(db, body.provider, body.field, body.value.strip())
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"ok": True}


@router.post("/{provider}/test")
def test_provider(provider: str, db: Session = Depends(get_db)) -> ProviderCheck:
    """'Test connection' button: performs one real call against the provider."""
    from collections.abc import Callable
    from typing import Any

    builders: dict[str, Callable[[Session], Any]] = {
        "alpaca": build_market_data,
        "finnhub": build_research,
        "fred": build_macro,
        "edgar": build_filings,
    }
    if provider == "telegram":
        try:
            from sentinel.alerts.telegram import build_telegram  # Phase 4

            return build_telegram(db).validate()
        except ImportError:
            return ProviderCheck(
                provider="telegram", ok=False, detail="alert channel not built yet (Phase 4)"
            )
        except CredentialsMissing as exc:
            return ProviderCheck(provider="telegram", ok=False, detail=str(exc))
    if provider == "anthropic":
        try:
            from sentinel.providers.llm.client import validate_llm  # Phase 3

            return validate_llm(db)
        except ImportError:
            return ProviderCheck(
                provider="anthropic", ok=False, detail="LLM client not built yet (Phase 3)"
            )
    builder = builders.get(provider)
    if builder is None:
        raise HTTPException(404, f"unknown provider {provider}")
    try:
        return builder(db).validate()
    except CredentialsMissing as exc:
        return ProviderCheck(provider=provider, ok=False, detail=str(exc))
