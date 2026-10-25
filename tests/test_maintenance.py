import sqlite3
import time

import pytest

from nmealog import maintenance, settings
from nmealog.db import connect, init_db


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "m.sqlite3"
    conn = connect(path)
    init_db(conn)
    conn.execute("INSERT INTO devices(id, name, host, port, created_at, updated_at) "
                 "VALUES (1, 'gps', '127.0.0.1', 4001, 0, 0)")
    yield conn, path
    conn.close()


def _fill_sentences(conn, count, start, step=1.0, payload="x" * 200, device_id=1):
    rows = [(device_id, start + i * step, "GP", "GGA", 1, payload, "{}") for i in range(count)]
    conn.executemany("INSERT INTO sentences(device_id, received_at, talker, sentence, checksum_ok, raw, fields) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?)", rows)


def test_prune_by_age_removes_only_old_rows(db):
    conn, _ = db
    now = time.time()
    _fill_sentences(conn, 100, now - 200 * 86400)  # 200 days old
    _fill_sentences(conn, 50, now - 86400)         # 1 day old
    conn.execute("INSERT INTO positions(device_id, received_at, latitude, longitude, source) "
                 "VALUES (1, ?, 50, -1, 'GGA')", (now - 200 * 86400,))
    s = dict(settings.defaults(), sentence_retention_days=14, position_retention_days=180)
    removed = maintenance.prune_by_age(conn, s, now)
    assert removed == {"sentences": 100, "positions": 1}
    assert conn.execute("SELECT COUNT(*) FROM sentences").fetchone()[0] == 50


def test_zero_retention_keeps_everything(db):
    conn, _ = db
    now = time.time()
    _fill_sentences(conn, 10, now - 10 * 365 * 86400)
    s = dict(settings.defaults(), sentence_retention_days=0, position_retention_days=0)
    assert maintenance.prune_by_age(conn, s, now) == {"sentences": 0, "positions": 0}


def test_size_limit_is_enforced_oldest_first(db):
    conn, path = db
    now = time.time()
    _fill_sentences(conn, 30000, now - 30000, payload="y" * 400)
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    before = maintenance.db_file_bytes(path)
    limit_mb = max(1, (before // 2) // maintenance.MB)
    assert before > limit_mb * maintenance.MB
    removed = maintenance.enforce_size_limit(conn, path, limit_mb)
    assert removed["sentences"] > 0
    assert maintenance.db_file_bytes(path) <= limit_mb * maintenance.MB
    oldest = conn.execute("SELECT MIN(received_at) FROM sentences").fetchone()[0]
    assert oldest > now - 30000  # the oldest rows were the ones removed


def test_size_limit_zero_means_unlimited(db):
    conn, path = db
    _fill_sentences(conn, 100, time.time())
    assert maintenance.enforce_size_limit(conn, path, 0) == {"sentences": 0, "positions": 0}


def test_clear_device_removes_its_data_only(db):
    conn, _ = db
    conn.execute("INSERT INTO devices(id, name, host, port, created_at, updated_at) "
                 "VALUES (2, 'gyro', '127.0.0.1', 4001, 0, 0)")
    now = time.time()
    _fill_sentences(conn, 5, now)
    _fill_sentences(conn, 3, now, device_id=2)
    removed = maintenance.clear_device(conn, 1)
    assert removed["sentences"] == 5
    assert conn.execute("SELECT COUNT(*) FROM sentences WHERE device_id = 2").fetchone()[0] == 3


def test_backup_roundtrip_and_retention(db, tmp_path):
    conn, _ = db
    _fill_sentences(conn, 20, time.time())
    backup_dir = tmp_path / "backups"
    made = []
    for _ in range(3):
        made.append(maintenance.create_backup(conn, backup_dir, keep=2))
        time.sleep(1.05)  # distinct timestamps
    listed = maintenance.list_backups(backup_dir)
    assert len(listed) == 2
    maintenance.validate_database(made[-1])
    copy = sqlite3.connect(made[-1])
    assert copy.execute("SELECT COUNT(*) FROM sentences").fetchone()[0] == 20
    assert copy.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    copy.close()


def test_restore_replaces_live_data(db, tmp_path):
    conn, _ = db
    _fill_sentences(conn, 10, time.time())
    backup = maintenance.create_backup(conn, tmp_path / "b", keep=5)
    conn.execute("DELETE FROM sentences")
    maintenance.restore_database(conn, backup)
    assert conn.execute("SELECT COUNT(*) FROM sentences").fetchone()[0] == 10


def test_validate_rejects_non_database(tmp_path):
    junk = tmp_path / "junk.sqlite3"
    junk.write_bytes(b"this is not a database" * 100)
    with pytest.raises(ValueError):
        maintenance.validate_database(junk)


def test_validate_rejects_other_sqlite_file(tmp_path):
    other = tmp_path / "other.sqlite3"
    c = sqlite3.connect(other)
    c.execute("CREATE TABLE t (x)")
    c.commit()
    c.close()
    with pytest.raises(ValueError, match="not an nmealog database"):
        maintenance.validate_database(other)


def test_backup_name_validation(tmp_path):
    with pytest.raises(FileNotFoundError):
        maintenance.backup_path(tmp_path, "../../etc/passwd")


def test_integrity_check_ok(db):
    conn, _ = db
    assert maintenance.integrity_check(conn) == ["ok"]


def test_auto_vacuum_is_incremental_for_new_databases(db):
    conn, _ = db
    assert conn.execute("PRAGMA auto_vacuum").fetchone()[0] == 2
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_growth_estimate_projects_rate_and_cap(db):
    conn, path = db
    now = time.time()
    # one sentence per minute for two hours, 150 bytes each
    rows = [(1, now - 7200 + i * 60, "GP", "GGA", 1, "x" * 100, "{" + "y" * 48 + "}")
            for i in range(120)]
    conn.executemany("INSERT INTO sentences(device_id, received_at, talker, sentence, checksum_ok, raw, fields) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
    s = dict(settings.defaults(), sentence_retention_days=14, position_retention_days=180,
             max_db_size_mb=1, store_sentences=True)
    est = maintenance.growth_estimate(conn, path, s, now)
    assert est["enough_data"]
    assert est["tables"]["sentences"]["rows_per_day"] == pytest.approx(1440, rel=0.02)
    assert est["tables"]["sentences"]["bytes_per_row"] > 150
    assert est["tables"]["sentences"]["steady_bytes"] > est["cap_bytes"]
    assert est["effective_days"] == pytest.approx(est["cap_bytes"] / est["daily_bytes"])
    assert est["days_to_cap"] is None  # the cap is already exceeded by the projection


def test_growth_estimate_without_retention_is_unbounded(db):
    conn, path = db
    now = time.time()
    _fill_sentences(conn, 200, now - 3600 * 2)
    s = dict(settings.defaults(), sentence_retention_days=0, max_db_size_mb=0)
    est = maintenance.growth_estimate(conn, path, s, now)
    assert est["steady_bytes"] is None
    assert est["cap_bytes"] == 0 and est["effective_days"] is None and est["days_to_cap"] is None


def test_growth_estimate_needs_an_hour_of_data(db):
    conn, path = db
    est = maintenance.growth_estimate(conn, path, settings.defaults(), time.time())
    assert est["enough_data"] is False
