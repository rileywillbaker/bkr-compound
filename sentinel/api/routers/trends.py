"""Trend Discovery Agent API.

Read endpoints serve whatever the scheduled 07:45/09:50 jobs last produced, so
the UI is cheap and instant. The two POST endpoints let the user force a run:
collection is free and always allowed, while report generation honours the
operating mode exactly as the scheduled job does (Free mode spends nothing).
"""

from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from sentinel import DISCLAIMER
from sentinel.db.base import get_db
from sentinel.db.models import SocialMentionRow, TrendDocumentRow, TrendSnapshotRow
from sentinel.trends import agent, collect, scoring
from sentinel.trends import report as report_mod
from sentinel.trends.sources.etf import HOLDINGS_ENDPOINTS, tracked_etfs
from sentinel.trends.taxonomy import THEMES, get_theme

router = APIRouter(prefix="/api/trends", tags=["trends"])


@router.get("")
def list_trends(db: Session = Depends(get_db)) -> dict:
    """Latest per-theme snapshots, strongest first."""
    snapshots = scoring.latest_snapshots(db, limit=len(THEMES))

    def _name(theme_key: str) -> str:
        """A snapshot can outlive its taxonomy entry if a theme is renamed or
        removed; fall back to the stored key rather than dropping the row."""
        theme = get_theme(theme_key)
        return theme.name if theme is not None else theme_key

    return {
        "as_of": snapshots[0].day.isoformat() if snapshots else None,
        "trends": [
            {
                "theme": s.theme,
                "name": _name(s.theme),
                "score": s.score,
                "legitimacy": s.legitimacy,
                "explanation": (s.evidence or {}).get("explanation", ""),
                "hype_flags": (s.evidence or {}).get("hype_flags", []),
                "components": s.components or {},
                "symbols": s.symbols or [],
                "coverage_gaps": (s.evidence or {}).get("coverage_gaps", []),
                "computed_at": s.computed_at.isoformat() if s.computed_at else None,
            }
            for s in snapshots
        ],
        "disclaimer": DISCLAIMER,
    }


@router.get("/themes")
def list_themes() -> dict:
    """The taxonomy itself — what the agent is capable of noticing."""
    return {
        "themes": [
            {
                "key": t.key,
                "name": t.name,
                "description": t.description,
                "etfs": list(t.etfs),
                "seeds": list(t.seeds),
                "policy_driven": t.policy_driven,
                "keyword_count": len(t.keywords),
            }
            for t in THEMES
        ],
        "tracked_etfs": tracked_etfs(),
        "holdings_sources": [
            {"etf": e.etf, "issuer": e.issuer, "verified": e.verified}
            for e in HOLDINGS_ENDPOINTS
        ],
    }


@router.get("/themes/{theme_key}")
def theme_detail(theme_key: str, db: Session = Depends(get_db)) -> dict:
    """One theme's current snapshot plus its recent score history."""
    theme = get_theme(theme_key)
    if theme is None:
        raise HTTPException(status_code=404, detail=f"unknown theme '{theme_key}'")

    history = db.execute(
        select(TrendSnapshotRow)
        .where(TrendSnapshotRow.theme == theme_key)
        .order_by(TrendSnapshotRow.day.desc())
        .limit(30)
    ).scalars().all()
    latest = history[0] if history else None

    return {
        "theme": {
            "key": theme.key,
            "name": theme.name,
            "description": theme.description,
            "etfs": list(theme.etfs),
            "seeds": list(theme.seeds),
        },
        "current": (
            {
                "day": latest.day.isoformat(),
                "score": latest.score,
                "legitimacy": latest.legitimacy,
                "components": latest.components or {},
                "evidence": latest.evidence or {},
                "symbols": latest.symbols or [],
            }
            if latest
            else None
        ),
        "history": [
            {"day": s.day.isoformat(), "score": s.score, "legitimacy": s.legitimacy}
            for s in reversed(history)
        ],
        "disclaimer": DISCLAIMER,
    }


@router.get("/report")
def get_report(db: Session = Depends(get_db)) -> dict:
    """The most recent stored trend report."""
    row = agent.latest_report(db)
    if row is None:
        return {
            "report": None,
            "text": "",
            "note": "no trend report has been generated yet",
            "disclaimer": DISCLAIMER,
        }
    return {
        "report": row.payload,
        "text": row.text,
        "day": row.day.isoformat(),
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "llm_used": row.llm_used,
        "alert_sent": row.alert_sent,
        "disclaimer": DISCLAIMER,
    }


@router.post("/report")
def build_report(
    send: bool = Query(default=False, description="also send the Telegram alert"),
    db: Session = Depends(get_db),
) -> dict:
    """Regenerate the trend report now.

    Honours the operating mode: in Free mode this makes no LLM calls at all
    and still produces the complete report.
    """
    report = agent.generate_report(db)
    text = report_mod.send_report(db, report) if send else report_mod.compose(report)
    return {
        "report": report.model_dump(mode="json"),
        "text": text,
        "llm_calls": report.llm_calls,
        "mode": report.mode,
        "disclaimer": DISCLAIMER,
    }


@router.post("/collect")
def run_collection(db: Session = Depends(get_db)) -> dict:
    """Force a free-source collection pass and rescore every theme.

    Always free — no LLM is involved at any point in collection or scoring.
    Can take a few minutes: it politely paces dozens of public endpoints.
    """
    result = collect.collect_all(db)
    scores = scoring.score_all(db)
    return {
        "collection": result.model_dump(mode="json"),
        "themes_scored": len(scores),
        "top": [{"theme": s.theme, "score": s.score} for s in scores[:5]],
        "llm_calls": 0,
        "disclaimer": DISCLAIMER,
    }


@router.get("/documents")
def list_documents(
    channel: str | None = Query(default=None, pattern="^(news|gov|social)$"),
    theme: str | None = None,
    days: int = Query(default=3, ge=1, le=30),
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
) -> dict:
    """The raw evidence behind the scores — every score must be auditable."""
    from datetime import UTC, datetime

    cutoff = datetime.now(UTC) - timedelta(days=days)
    query = (
        select(TrendDocumentRow)
        .where(TrendDocumentRow.published_at >= cutoff)
        .order_by(TrendDocumentRow.published_at.desc())
        .limit(limit * 3 if theme else limit)
    )
    if channel:
        query = query.where(TrendDocumentRow.channel == channel)

    rows = db.execute(query).scalars().all()
    if theme:
        rows = [r for r in rows if theme in (r.themes or [])][:limit]

    return {
        "documents": [
            {
                "source": r.source,
                "channel": r.channel,
                "title": r.title,
                "url": r.url,
                "published_at": r.published_at.isoformat() if r.published_at else None,
                "themes": r.themes or [],
                "symbols": r.symbols or [],
                "sentiment": round(r.sentiment, 3),
                "engagement": r.engagement,
            }
            for r in rows
        ],
        "disclaimer": DISCLAIMER,
    }


@router.get("/social")
def social_summary(
    days: int = Query(default=3, ge=1, le=30),
    limit: int = Query(default=25, ge=1, le=100),
    db: Session = Depends(get_db),
) -> dict:
    """Trending tickers by mention volume, with sentiment and growth.

    Attention, not quality — the same caveat the report carries.
    """
    recent_start = date.today() - timedelta(days=days)
    baseline_start = date.today() - timedelta(days=days * 4)

    rows = db.execute(
        select(
            SocialMentionRow.symbol,
            SocialMentionRow.day,
            SocialMentionRow.mentions,
            SocialMentionRow.sentiment,
            SocialMentionRow.positive,
            SocialMentionRow.negative,
        ).where(SocialMentionRow.day >= baseline_start)
    ).all()

    aggregated: dict[str, dict] = {}
    for symbol, day, mentions, sentiment, positive, negative in rows:
        bucket = aggregated.setdefault(
            symbol,
            {
                "symbol": symbol,
                "mentions": 0,
                "baseline_mentions": 0,
                "positive": 0,
                "negative": 0,
                "_scores": [],
            },
        )
        if day >= recent_start:
            bucket["mentions"] += int(mentions or 0)
            bucket["positive"] += int(positive or 0)
            bucket["negative"] += int(negative or 0)
            bucket["_scores"].append(float(sentiment or 0.0))
        else:
            bucket["baseline_mentions"] += int(mentions or 0)

    out = []
    baseline_days = max(1, days * 3)
    for bucket in aggregated.values():
        scores = bucket.pop("_scores")
        recent_daily = bucket["mentions"] / max(1, days)
        baseline_daily = bucket["baseline_mentions"] / baseline_days
        bucket["sentiment"] = round(sum(scores) / len(scores), 3) if scores else 0.0
        bucket["growth_pct"] = (
            round((recent_daily / baseline_daily - 1) * 100, 1) if baseline_daily > 0 else None
        )
        out.append(bucket)

    out.sort(key=lambda b: -b["mentions"])
    return {
        "trending": out[:limit],
        "window_days": days,
        "note": "mention volume measures attention, not quality",
        "disclaimer": DISCLAIMER,
    }


@router.get("/etf-activity")
def etf_activity(
    theme: str | None = None,
    db: Session = Depends(get_db),
) -> dict:
    """What thematic ETFs are accumulating, where free holdings data exists."""
    themes = [get_theme(theme)] if theme else list(THEMES)
    themes = [t for t in themes if t is not None]
    if theme and not themes:
        raise HTTPException(status_code=404, detail=f"unknown theme '{theme}'")

    out = []
    for t in themes:
        accumulation = scoring.etf_accumulation(db, list(t.etfs))
        if not accumulation and theme is None:
            continue
        out.append(
            {
                "theme": t.key,
                "name": t.name,
                "etfs": list(t.etfs),
                "accumulating": [
                    {
                        "symbol": a.symbol,
                        "etf": a.etf,
                        "weight_now": a.weight_now,
                        "weight_before": a.weight_before,
                        "weight_change": a.weight_change,
                        "shares_change_pct": a.shares_change_pct,
                        "newly_added": a.newly_added,
                    }
                    for a in accumulation[:15]
                ],
            }
        )
    return {
        "themes": out,
        "note": (
            "Holdings come from issuers that publish them free of charge. Where a "
            "theme is absent, no free holdings data was available — that is not "
            "evidence of an absence of buying."
        ),
        "disclaimer": DISCLAIMER,
    }
