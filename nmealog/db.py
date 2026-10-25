from __future__ import annotations

import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA_VERSION = 1
BUSY_TIMEOUT_S = 15

STATEMENTS = (
    """CREATE TABLE IF NOT EXISTS meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        updated_at REAL NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS devices (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL UNIQUE,
        host TEXT NOT NULL,
        port INTEGER NOT NULL CHECK (port BETWEEN 1 AND 65535),
        enabled INTEGER NOT NULL DEFAULT 1,
        notes TEXT NOT NULL DEFAULT '',
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS device_status (
        device_id INTEGER PRIMARY KEY REFERENCES devices(id) ON DELETE CASCADE,
        state TEXT NOT NULL DEFAULT 'idle',
        connected_at REAL,
        last_seen REAL,
        last_error TEXT,
        last_error_at REAL,
        sentences_ok INTEGER NOT NULL DEFAULT 0,
        sentences_bad INTEGER NOT NULL DEFAULT 0,
        bytes_in INTEGER NOT NULL DEFAULT 0,
        gps_time REAL,
        clock_offset_s REAL,
        updated_at REAL NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS sentences (
        id INTEGER PRIMARY KEY,
        device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
        received_at REAL NOT NULL,
        talker TEXT NOT NULL,
        sentence TEXT NOT NULL,
        checksum_ok INTEGER,
        raw TEXT NOT NULL,
        fields TEXT NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS sentences_time ON sentences(received_at)",
    "CREATE INDEX IF NOT EXISTS sentences_device_type ON sentences(device_id, sentence, received_at)",
    # Composite index for "latest position per device" and device+time-range filters.
    "CREATE INDEX IF NOT EXISTS sentences_device_time ON sentences(device_id, received_at)",
    """CREATE TABLE IF NOT EXISTS latest (
        device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
        sentence TEXT NOT NULL,
        field TEXT NOT NULL,
        value TEXT NOT NULL,
        received_at REAL NOT NULL,
        PRIMARY KEY (device_id, sentence, field)) WITHOUT ROWID""",
    """CREATE TABLE IF NOT EXISTS positions (
        id INTEGER PRIMARY KEY,
        device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
        received_at REAL NOT NULL,
        latitude REAL NOT NULL,
        longitude REAL NOT NULL,
        source TEXT NOT NULL,
        fix_quality INTEGER,
        speed_kn REAL,
        course_true REAL,
        satellites INTEGER,
        hdop REAL)""",
    "CREATE INDEX IF NOT EXISTS positions_time ON positions(received_at)",
    "CREATE INDEX IF NOT EXISTS positions_device_time ON positions(device_id, received_at)",
    """CREATE TABLE IF NOT EXISTS maintenance_log (
        id INTEGER PRIMARY KEY,
        at REAL NOT NULL,
        actor TEXT NOT NULL,
        action TEXT NOT NULL,
        detail TEXT NOT NULL DEFAULT '')""",
)


def connect(path: Path | str) -> sqlite3.Connection:
    path = str(path)
    new_file = not os.path.exists(path) or os.path.getsize(path) == 0
    conn = sqlite3.connect(path, timeout=BUSY_TIMEOUT_S, isolation_level=None)
    conn.row_factory = sqlite3.Row
    if new_file:
        # auto_vacuum must be set before the first table is created.
        conn.execute("PRAGMA auto_vacuum = INCREMENTAL")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA temp_store = MEMORY")
    conn.execute("PRAGMA cache_size = -8192")  # 8 MiB per connection
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def init_db(conn: sqlite3.Connection) -> None:
    with transaction(conn):
        for statement in STATEMENTS:
            conn.execute(statement)
        conn.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
    version = int(get_meta(conn, "schema_version", "0"))
    if version != SCHEMA_VERSION:
        raise RuntimeError(f"database schema version {version}, expected {SCHEMA_VERSION}")


def get_meta(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else default


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )


def log_action(conn: sqlite3.Connection, actor: str, action: str, detail: str = "") -> None:
    conn.execute(
        "INSERT INTO maintenance_log(at, actor, action, detail) VALUES (?, ?, ?, ?)",
        (time.time(), actor, action, detail),
    )
