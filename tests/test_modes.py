"""Operating modes: the cost policy that governs whether an LLM runs at all."""

import pytest

from sentinel.modes import (
    DEFAULT_MODE,
    VALID_MODES,
    all_policies,
    get_mode,
    get_policy,
    policy_for,
    set_mode,
)


def test_default_is_smart(db):
    assert get_mode(db) == "smart" == DEFAULT_MODE


def test_free_mode_allows_no_llm_anywhere():
    policy = policy_for("free")
    assert policy.scan_depth == "none"
    assert policy.on_demand_depth == "none"
    assert policy.max_llm_candidates_per_scan == 0
    assert not policy.allows_any_llm


def test_smart_mode_caps_scheduled_spend_but_allows_user_research():
    policy = policy_for("smart")
    assert policy.scan_depth == "review"  # one combined call per finalist
    assert policy.on_demand_depth == "full"  # you asked, so you get the deep pass
    assert policy.max_llm_candidates_per_scan <= 5
    assert policy.depth_for(on_demand=False) == "review"
    assert policy.depth_for(on_demand=True) == "full"


def test_research_mode_is_the_expensive_one():
    smart, research = policy_for("smart"), policy_for("research")
    assert research.scan_depth == "full"
    assert research.max_llm_candidates_per_scan >= smart.max_llm_candidates_per_scan
    assert "cost" in research.label.lower() or "cost" in research.description.lower()


def test_unknown_mode_falls_back_to_default():
    assert policy_for("").mode == DEFAULT_MODE
    assert policy_for(None).mode == DEFAULT_MODE
    assert policy_for("turbo").mode == DEFAULT_MODE


def test_set_and_get_round_trip(db):
    assert set_mode(db, "FREE") == "free"
    assert get_mode(db) == "free"
    assert get_policy(db).scan_depth == "none"


def test_set_mode_rejects_garbage(db):
    with pytest.raises(ValueError):
        set_mode(db, "yolo")
    assert get_mode(db) == DEFAULT_MODE  # unchanged


def test_every_mode_is_described_for_the_ui():
    policies = all_policies()
    assert [p.mode for p in policies] == list(VALID_MODES)
    assert all(p.label and p.description for p in policies)
