import pytest

from nmealog import settings
from nmealog.db import connect, init_db


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "s.sqlite3")
    init_db(c)
    yield c
    c.close()


def test_defaults_load_on_empty_database(conn):
    assert settings.load(conn) == settings.defaults()


def test_save_and_reload(conn):
    changed = settings.save(conn, {"max_db_size_mb": "250", "store_invalid": "on"})
    assert set(changed) == {"max_db_size_mb", "store_invalid"}
    loaded = settings.load(conn)
    assert loaded["max_db_size_mb"] == 250
    assert loaded["store_invalid"] is True


def test_missing_keys_keep_current_value(conn):
    settings.save(conn, {"backup_keep": "7"})
    settings.save(conn, {"backup_interval_h": "12"})
    assert settings.load(conn)["backup_keep"] == 7


def test_invalid_values_are_rejected_with_per_field_errors(conn):
    with pytest.raises(settings.ValidationError) as exc:
        settings.save(conn, {"position_interval_s": "0", "display_tz": "Mars/Olympus", "backup_keep": "x"})
    assert set(exc.value.errors) == {"position_interval_s", "display_tz", "backup_keep"}
    assert settings.load(conn)["position_interval_s"] == 60  # nothing was written


def test_timezone_is_validated(conn):
    settings.save(conn, {"display_tz": "Europe/London"})
    assert settings.load(conn)["display_tz"] == "Europe/London"


def test_bool_coercion():
    s = settings.BY_KEY["logging_enabled"]
    assert settings.coerce(s, "off") is False
    assert settings.coerce(s, "on") is True
    assert settings.coerce(s, True) is True


def test_changes_are_logged(conn):
    settings.save(conn, {"max_db_size_mb": "300"}, actor="tester")
    row = conn.execute("SELECT actor, action, detail FROM maintenance_log").fetchone()
    assert row["actor"] == "tester" and row["action"] == "settings"
    assert "max_db_size_mb" in row["detail"]
