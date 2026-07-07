"""Versioned persistence for RiskProfile. Every edit creates a new version;
the newest version is the active one. Rejections and approvals both record
which version they were checked against."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from sentinel.risk.profile import RiskProfile


def get_active_profile(db: Session) -> RiskProfile:
    from sentinel.db.models import RiskProfileRow

    row = db.execute(
        select(RiskProfileRow).order_by(RiskProfileRow.version.desc()).limit(1)
    ).scalars().first()
    if row is None:
        profile = RiskProfile()
        save_profile(db, profile)  # persist defaults as version 1
        return profile
    params = dict(row.params or {})
    params["version"] = row.version
    return RiskProfile(**params)


def save_profile(db: Session, profile: RiskProfile) -> RiskProfile:
    """Persist as a NEW version (append-only)."""
    from sentinel.db.models import RiskProfileRow

    latest = db.execute(
        select(RiskProfileRow.version).order_by(RiskProfileRow.version.desc()).limit(1)
    ).scalar_one_or_none()
    next_version = (latest or 0) + 1
    params = profile.model_dump()
    params.pop("version", None)
    db.add(RiskProfileRow(version=next_version, params=params))
    db.flush()
    return profile.model_copy(update={"version": next_version})


def list_versions(db: Session) -> list[dict]:
    from sentinel.db.models import RiskProfileRow

    rows = db.execute(
        select(RiskProfileRow).order_by(RiskProfileRow.version.desc())
    ).scalars().all()
    return [
        {"version": r.version, "created_at": r.created_at, "params": r.params} for r in rows
    ]
