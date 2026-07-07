"""Phase 6: signal resolution, stats/calibration math, hit-rate priors."""

from datetime import datetime, timedelta
from decimal import Decimal

from sentinel.db.models import BarRow, EvaluationRow, SignalRow
from sentinel.evaluation.priors import (
    MIN_RESOLVED_FOR_REAL_RATE,
    NEUTRAL_HIT_RATE,
    strategy_hit_rate,
)
from sentinel.evaluation.resolve import resolve_open_signals, resolve_signal, run_nightly
from sentinel.evaluation.stats import (
    brier_score,
    calibration_buckets,
    performance_summary,
    recompute_strategy_stats,
)

T0 = datetime(2026, 6, 1, 15, 0)


def make_signal(
    db,
    signal_id: str = "sig-1",
    entry: float = 100.0,
    stop: float = 95.0,
    target: float = 110.0,
    horizon: str = "swing_days",
    confidence: float = 0.8,
    decision: str | None = None,
    strategy: str = "momentum_swing",
    regime: str = "bull_trend",
) -> SignalRow:
    row = SignalRow(
        id=signal_id,
        created_at=T0,
        run_id="run-1",
        ticker="NVDA",
        action="BUY",
        shares=10,
        max_entry_price=Decimal(str(entry)),
        stop_loss=Decimal(str(stop)),
        take_profit=Decimal(str(target)),
        confidence=confidence,
        risk_score=4,
        time_horizon=horizon,
        strategy=strategy,
        regime=regime,
        user_decision=decision,
    )
    db.add(row)
    db.flush()
    return row


def add_bars(db, days: list[tuple[float, float, float, float]], symbol: str = "NVDA"):
    """days = [(open, high, low, close), ...] daily bars starting at T0's date."""
    for i, (o, h, low, c) in enumerate(days):
        db.add(
            BarRow(
                symbol=symbol,
                timeframe="1Day",
                ts=T0.replace(hour=0) + timedelta(days=i),
                open=Decimal(str(o)),
                high=Decimal(str(h)),
                low=Decimal(str(low)),
                close=Decimal(str(c)),
                volume=1_000_000,
            )
        )
    db.flush()


def test_target_hit(db):
    signal = make_signal(db)
    add_bars(db, [(100, 105, 98, 104), (104, 111, 103, 109)])
    ev = resolve_signal(db, signal)
    assert ev is not None
    assert ev.outcome == "target_hit"
    assert ev.exit_price == 110.0
    assert ev.r_multiple == 2.0  # (110-100)/(100-95)
    assert ev.win is True
    assert ev.holding_days == 2


def test_stop_hit(db):
    signal = make_signal(db)
    add_bars(db, [(100, 103, 94, 96)])
    ev = resolve_signal(db, signal)
    assert ev is not None
    assert ev.outcome == "stop_hit"
    assert ev.r_multiple == -1.0
    assert ev.win is False


def test_stop_beats_target_in_same_bar(db):
    """Pessimistic tie-break: a bar spanning both levels counts as a stop."""
    signal = make_signal(db)
    add_bars(db, [(100, 115, 90, 100)])
    ev = resolve_signal(db, signal)
    assert ev is not None
    assert ev.outcome == "stop_hit"


def test_horizon_expiry_exits_at_close(db):
    signal = make_signal(db, horizon="intraday")  # 1 trading day budget
    add_bars(db, [(100, 104, 99, 102)])
    ev = resolve_signal(db, signal)
    assert ev is not None
    assert ev.outcome == "expired"
    assert ev.exit_price == 102.0
    assert ev.r_multiple == 0.4  # (102-100)/5
    assert ev.win is True


def test_still_open_returns_none(db):
    signal = make_signal(db)  # swing budget = 10 bars
    add_bars(db, [(100, 104, 99, 102)] * 3)  # only 3 quiet bars
    assert resolve_signal(db, signal) is None
    assert resolve_open_signals(db) == []


def test_skipped_signals_still_resolve_and_feed_missed_wins(db):
    signal = make_signal(db, decision="skipped")
    add_bars(db, [(100, 111, 99, 110)])
    resolved = resolve_open_signals(db)
    assert len(resolved) == 1
    summary = performance_summary(db)
    assert summary["missed_wins"][0]["signal_id"] == signal.id


def test_resolution_is_idempotent(db):
    make_signal(db)
    add_bars(db, [(100, 111, 99, 110)])
    assert len(resolve_open_signals(db)) == 1
    assert len(resolve_open_signals(db)) == 0  # already evaluated


def test_run_nightly_records_event_and_stats(db):
    make_signal(db)
    add_bars(db, [(100, 111, 99, 110)])
    result = run_nightly(db)
    assert result["resolved"] == 1
    assert result["stats_rows"] == 2  # (strategy, regime) + (strategy, "*")


# ------------------------------------------------------------------- stats --
def _evaluation(confidence: float, win: bool, r: float = 1.0, decision=None):
    return EvaluationRow(
        signal_id=f"e-{confidence}-{win}-{r}",
        ticker="NVDA",
        outcome="target_hit" if win else "stop_hit",
        entry_price=100,
        exit_price=100 + r * 5,
        r_multiple=r if win else -1.0,
        return_pct=r * 5 if win else -5.0,
        win=win,
        holding_days=3,
        confidence=confidence,
        strategy="momentum_swing",
        regime="bull_trend",
        user_decision=decision,
    )


def test_brier_score_math():
    assert brier_score([]) is None
    evals = [_evaluation(1.0, True), _evaluation(0.0, False)]
    assert brier_score(evals) == 0.0  # perfect calibration
    evals = [_evaluation(1.0, False)]
    assert brier_score(evals) == 1.0  # maximally wrong


def test_calibration_buckets():
    evals = [_evaluation(0.85, True), _evaluation(0.82, False), _evaluation(0.15, False)]
    buckets = calibration_buckets(evals)
    high = next(b for b in buckets if b["predicted"] == 0.85)
    assert high["count"] == 2 and high["realized"] == 0.5
    low = next(b for b in buckets if b["predicted"] == 0.15)
    assert low["realized"] == 0.0


def test_recompute_strategy_stats_rollup(db):
    for i in range(3):
        e = _evaluation(0.8, i < 2, r=2.0)
        e.signal_id = f"s-{i}"
        e.regime = "bull_trend" if i < 2 else "range"
        db.add(e)
    db.flush()
    rows = recompute_strategy_stats(db)
    by_key = {(r.strategy, r.regime): r for r in rows}
    assert by_key[("momentum_swing", "*")].resolved_count == 3
    assert by_key[("momentum_swing", "bull_trend")].resolved_count == 2
    assert by_key[("momentum_swing", "bull_trend")].hit_rate == 1.0
    assert by_key[("momentum_swing", "range")].hit_rate == 0.0
    # recompute again: no duplicate rows
    assert len(recompute_strategy_stats(db)) == 3


# ------------------------------------------------------------------ priors --
def test_prior_neutral_below_threshold(db):
    for i in range(MIN_RESOLVED_FOR_REAL_RATE - 1):
        e = _evaluation(0.8, True)
        e.signal_id = f"p-{i}"
        db.add(e)
    db.flush()
    recompute_strategy_stats(db)
    assert strategy_hit_rate(db, "momentum_swing") == NEUTRAL_HIT_RATE
    assert strategy_hit_rate(db, "unknown_strategy") == NEUTRAL_HIT_RATE


def test_prior_blended_above_threshold(db):
    for i in range(40):
        e = _evaluation(0.8, i < 32)  # 80% hit rate over 40
        e.signal_id = f"q-{i}"
        db.add(e)
    db.flush()
    recompute_strategy_stats(db)
    rate = strategy_hit_rate(db, "momentum_swing")
    assert NEUTRAL_HIT_RATE < rate < 0.8  # pulled toward neutral, not raw
    regime_rate = strategy_hit_rate(db, "momentum_swing", regime="bull_trend")
    assert regime_rate > NEUTRAL_HIT_RATE
