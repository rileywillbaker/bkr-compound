from sentinel.config import Settings


def test_watchlist_parsing():
    s = Settings(watchlist=" spy, qqq ,nvda ")
    assert s.watchlist_symbols == ["SPY", "QQQ", "NVDA"]


def test_defaults_are_safe():
    s = Settings()
    assert s.starting_equity > 0
    assert s.llm_daily_token_budget > 0


def test_dev_flag():
    assert Settings(app_env="dev").is_dev
    assert not Settings(app_env="prod").is_dev
