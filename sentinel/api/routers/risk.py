"""Risk-profile endpoints: view active profile, edit (creates a new version),
list version history. There is deliberately NO endpoint that bypasses or
overrides a risk check."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import ValidationError
from sqlalchemy.orm import Session

from sentinel.db.base import get_db
from sentinel.risk.profile import RiskProfile
from sentinel.risk.store import get_active_profile, list_versions, save_profile

router = APIRouter(prefix="/api/risk", tags=["risk"])


@router.get("/profile")
def active_profile(db: Session = Depends(get_db)) -> RiskProfile:
    return get_active_profile(db)


@router.put("/profile")
def update_profile(params: dict, db: Session = Depends(get_db)) -> RiskProfile:
    """Full or partial update; validated; persists as a new version."""
    current = get_active_profile(db).model_dump()
    current.update({k: v for k, v in params.items() if k != "version"})
    try:
        profile = RiskProfile(**current)
    except ValidationError as exc:
        raise HTTPException(422, str(exc)) from exc
    return save_profile(db, profile)


@router.get("/profile/versions")
def versions(db: Session = Depends(get_db)) -> list[dict]:
    return list_versions(db)
