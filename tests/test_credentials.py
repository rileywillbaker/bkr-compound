import pytest

from sentinel.providers import credentials as creds


def test_encrypt_decrypt_roundtrip():
    token = creds.encrypt_value("sk-test-12345")
    assert token != "sk-test-12345"
    assert creds.decrypt_value(token) == "sk-test-12345"


def test_mask():
    assert creds.mask("") == ""
    assert creds.mask("abc") == "••••"
    assert creds.mask("sk-abcdef") == "•••• cdef"


def test_store_and_resolve_db_wins(db, monkeypatch):
    from sentinel.config import get_settings

    monkeypatch.setenv("FINNHUB_API_KEY", "env-value")
    get_settings.cache_clear()
    try:
        # env fallback before anything stored
        assert creds.get_credential(db, "finnhub", "api_key") == "env-value"
        # DB value wins once stored
        creds.store_credential(db, "finnhub", "api_key", "db-value")
        assert creds.get_credential(db, "finnhub", "api_key") == "db-value"
        # updating overwrites
        creds.store_credential(db, "finnhub", "api_key", "db-value-2")
        assert creds.get_credential(db, "finnhub", "api_key") == "db-value-2"
    finally:
        get_settings.cache_clear()


def test_unknown_credential_rejected(db):
    with pytest.raises(ValueError):
        creds.store_credential(db, "nope", "api_key", "x")
    with pytest.raises(ValueError):
        creds.get_credential(None, "finnhub", "nope")


def test_status_is_masked(db):
    creds.store_credential(db, "fred", "api_key", "abcdefgh12345678")
    status = creds.credential_status(db)
    assert status["fred"]["api_key"].endswith("5678")
    assert "abcdefgh" not in status["fred"]["api_key"]
