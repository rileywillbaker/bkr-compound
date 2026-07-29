"""Swing screener: liquidity + movement + structure filters, capped & ranked.
It only ADDS filters on top of the base screener — never relaxes one."""

from datetime import UTC, datetime

from sentinel.agents.technicals import TechnicalSnapshot
from sentinel.data.context import MarketContext, SymbolContext
from sentinel.swing.screen import SwingScreenParams, swing_screen


def snap(symbol: str, **kw) -> TechnicalSnapshot:
    base = dict(
        symbol=symbol,
        close=100.0,
        bars_used=250,
        avg_dollar_volume20=30_000_000.0,
        atr_pct=3.0,
        pct_from_52w_high=-1.0,  # near the high → clean structure
        relative_volume=1.6,
        above_sma20=True,
        above_sma50=True,
        above_sma200=True,
        rsi14=55.0,
    )
    base.update(kw)
    return TechnicalSnapshot(**base)


def ctx_for(technicals: dict[str, TechnicalSnapshot]) -> MarketContext:
    # Sector and market cap are present because the base screener's quality
    # filters require them — a name with unknown coverage is excluded before
    # any swing-specific filter gets a say (and the risk engine would veto it
    # anyway). See test_screener for those filters in isolation.
    return MarketContext(
        as_of=datetime.now(UTC),
        spy_bars=[],
        macro={},
        symbols={
            s: SymbolContext(
                symbol=s,
                daily_bars=[],
                news=[],
                sector="Technology",
                market_cap=50_000.0,
            )
            for s in technicals
        },
    )


def test_only_liquid_moving_clean_names_pass():
    techs = {
        "MOVER": snap("MOVER"),
        "ILLIQUID": snap("ILLIQUID", avg_dollar_volume20=1_000_000.0),  # base liquidity fail
        "QUIET": snap("QUIET", atr_pct=1.0),  # below movement floor
        "NOSTRUCT": snap("NOSTRUCT", support=None, pct_from_52w_high=-10.0),  # no level
        "SHORTHIST": snap("SHORTHIST", bars_used=40),  # insufficient history
    }
    eligible, scanned = swing_screen(ctx_for(techs), techs, SwingScreenParams())
    assert scanned == 5
    assert [c.symbol for c in eligible] == ["MOVER"]
    assert eligible[0].screen.eligible


def test_atr_ceiling_still_enforced():
    # Above the risk-profile ATR cap → excluded (safety limit never relaxed).
    techs = {"WILD": snap("WILD", atr_pct=12.0)}
    eligible, _ = swing_screen(ctx_for(techs), techs, SwingScreenParams())
    assert eligible == []


def test_cap_keeps_highest_prescore():
    techs = {
        "HOT": snap("HOT", relative_volume=3.0),  # richer volume → higher prescore
        "WARM": snap("WARM", relative_volume=1.6),
    }
    eligible, _ = swing_screen(
        ctx_for(techs), techs, SwingScreenParams(max_candidates=1)
    )
    assert [c.symbol for c in eligible] == ["HOT"]
