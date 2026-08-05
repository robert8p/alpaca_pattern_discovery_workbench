from app.config import get_settings
from app.db import DatabaseConfigurationError, database_target, validate_database_target


def _set_url(monkeypatch, value: str) -> None:
    monkeypatch.setenv("DATABASE_URL", value)
    get_settings.cache_clear()


def test_session_pooler_is_accepted(monkeypatch):
    _set_url(monkeypatch, "postgresql://postgres.ref:secret@aws-0-eu-west-1.pooler.supabase.com:5432/postgres")
    assert database_target()["mode"] == "session_pooler"
    validate_database_target()


def test_transaction_pooler_is_rejected(monkeypatch):
    _set_url(monkeypatch, "postgresql://postgres.ref:secret@aws-0-eu-west-1.pooler.supabase.com:6543/postgres")
    try:
        validate_database_target()
    except DatabaseConfigurationError as exc:
        assert "6543" in str(exc)
        assert "5432" in str(exc)
    else:
        raise AssertionError("transaction pooler should be rejected")


def test_target_summary_never_contains_password(monkeypatch):
    _set_url(monkeypatch, "postgresql://postgres.ref:super-secret@aws-0-eu-west-1.pooler.supabase.com:5432/postgres")
    assert "super-secret" not in repr(database_target())
