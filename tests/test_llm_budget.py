"""Daily LLM cost cap ($0.50 from models.yaml) and automatic degraded mode."""

from datetime import UTC, datetime

import pytest

from sentinel.agents.analysts import news_analyst
from sentinel.data.context import SymbolContext
from sentinel.db.models import ApiUsage
from sentinel.providers.llm import client
from sentinel.providers.types import NewsItem


def _usage(cost: float) -> ApiUsage:
    return ApiUsage(
        provider="anthropic", endpoint="test", tokens_in=10, tokens_out=10, cost_usd=cost
    )


def test_cost_budget_is_fifty_cents_from_yaml():
    assert client.daily_cost_budget_usd() == pytest.approx(0.50)


def test_under_budget_passes(db):
    db.add(_usage(0.10))
    db.flush()
    client._check_budget(db)  # no raise


def test_cost_budget_blocks_when_hit(db):
    db.add(_usage(0.30))
    db.add(_usage(0.25))
    db.flush()
    assert client.cost_used_today(db) == pytest.approx(0.55)
    with pytest.raises(client.BudgetExceeded):
        client._check_budget(db)


def test_budget_exhaustion_degrades_to_deterministic(db, monkeypatch):
    """BudgetExceeded is an LLMError: call sites fall back automatically and
    flag the output deterministic_only — degraded mode needs no operator."""

    def broke(*args, **kwargs):
        raise client.BudgetExceeded("daily cost budget exhausted ($0.50/$0.50)")

    monkeypatch.setattr("sentinel.agents.analysts.complete_json", broke)
    sym_ctx = SymbolContext(
        symbol="NVDA",
        daily_bars=[],
        news=[
            NewsItem(
                provider_id="n1",
                symbol="NVDA",
                headline="headline",
                summary="",
                source="wire",
                url="",
                published_at=datetime.now(UTC),
            )
        ],
    )
    verdict = news_analyst(db, sym_ctx)
    assert verdict.deterministic_only
    assert verdict.score == 0
