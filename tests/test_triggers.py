"""Event-based AI gating: what earns an LLM call, and what must not."""

from datetime import UTC, datetime, timedelta

from sentinel.data.discovery import DISCOVERY_KEY
from sentinel.db.settings_store import set_setting
from sentinel.pipeline.triggers import (
    HIGH_CONVICTION,
    LLMTrigger,
    discovery_events_by_symbol,
    evaluate_trigger,
    rank_and_cap,
)


def test_quiet_mediocre_candidate_earns_nothing():
    """No event, no breakout, no volume, ordinary conviction → no spend."""
    assert evaluate_trigger("KO", conviction=0.45, min_conviction=0.40) is None


def test_material_event_triggers():
    trigger = evaluate_trigger(
        "NVDA",
        conviction=0.5,
        events=[{"kind": "earnings_surprise", "score": 2.0, "detail": "EPS beat"}],
        min_conviction=0.40,
    )
    assert trigger is not None
    assert trigger.reason == "earnings"
    assert "EPS beat" in trigger.label


def test_breakout_triggers_without_any_stored_event():
    trigger = evaluate_trigger("NVDA", conviction=0.5, pct_from_52w_high=-0.4, min_conviction=0.4)
    assert trigger is not None and trigger.reason == "breakout"


def test_unusual_volume_triggers():
    trigger = evaluate_trigger("AMD", conviction=0.5, relative_volume=3.0, min_conviction=0.4)
    assert trigger is not None and trigger.reason == "volume"


def test_high_conviction_alone_is_enough():
    trigger = evaluate_trigger("MSFT", conviction=HIGH_CONVICTION + 0.05, min_conviction=0.4)
    assert trigger is not None and trigger.reason == "conviction"


def test_open_position_with_a_proposed_change_triggers():
    trigger = evaluate_trigger("AAPL", conviction=0.5, is_open_position=True, min_conviction=0.4)
    assert trigger is not None and trigger.reason == "position"


def test_conviction_floor_blocks_even_a_loud_event():
    """Noisy news on a weak setup stays free — the floor applies to the event
    path too, otherwise every headline would buy an opinion."""
    assert (
        evaluate_trigger(
            "MEME",
            conviction=0.10,
            events=[{"kind": "high_impact_news", "score": 2.5, "detail": "halted"}],
            min_conviction=0.40,
        )
        is None
    )


def test_user_request_always_wins():
    trigger = evaluate_trigger("XYZ", conviction=0.0, user_requested=True, min_conviction=0.9)
    assert trigger is not None
    assert trigger.reason == "requested"
    assert trigger.priority == 10.0


def test_highest_priority_event_is_chosen():
    trigger = evaluate_trigger(
        "NVDA",
        conviction=0.5,
        events=[
            {"kind": "fresh_filing", "score": 1.0, "detail": "8-K"},
            {"kind": "earnings_surprise", "score": 3.0, "detail": "big beat"},
        ],
        min_conviction=0.4,
    )
    assert trigger is not None and trigger.reason == "earnings"


def test_rank_and_cap_is_the_hard_budget():
    triggers = [LLMTrigger(symbol=f"S{i}", reason="conviction", priority=i) for i in range(10)]
    capped = rank_and_cap(triggers, 3)
    assert [t.symbol for t in capped] == ["S9", "S8", "S7"]
    assert rank_and_cap(triggers, 0) == []


def test_discovery_events_are_grouped_by_symbol(db):
    set_setting(
        db,
        DISCOVERY_KEY,
        {
            "as_of": datetime.now(UTC).isoformat(),
            "candidates": ["NVDA"],
            "events": [
                {"symbol": "NVDA", "kind": "breakout", "score": 2.0, "detail": "new high"},
                {"symbol": "AMD", "kind": "fresh_filing", "score": 1.0, "detail": "8-K"},
            ],
        },
    )
    grouped = discovery_events_by_symbol(db)
    assert set(grouped) == {"NVDA", "AMD"}
    assert grouped["NVDA"][0]["kind"] == "breakout"


def test_stale_discovery_events_are_ignored(db):
    """A dead worker must not pin yesterday's 'events' and keep buying reviews."""
    set_setting(
        db,
        DISCOVERY_KEY,
        {
            "as_of": (datetime.now(UTC) - timedelta(hours=48)).isoformat(),
            "events": [{"symbol": "NVDA", "kind": "breakout", "score": 2.0}],
        },
    )
    assert discovery_events_by_symbol(db) == {}
