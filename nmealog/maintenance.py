"""Database maintenance: retention, size limit, backups, restore and manual actions."""
from __future__ import annotations

import logging
import os
import re
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .db import SCHEMA_VERSION, transaction

log = logging.getLogger("nmealog.maintenance")

CHUNK = 5000
MB = 1024 * 1024
DATA_TABLES = ("sentences", "positions")
BACKUP_NAME = re.compile(r"^nmealog-\d{8}-\d{6}(?:-\d+)?\.sqlite3$")


def db_file_bytes(db_path: Path) -> int:
    total = 0
    for suffix in ("", "-wal"):
        p = Path(f"{db_path}{suffix}")
        if p.exists():
            total += p.stat().st_size
    return total


def reclaim_space(conn: sqlite3.Connection) -> None:
    """Return free pages to the filesystem without an exclusive lock."""
    conn.execute("PRAGMA incremental_vacuum").fetchall()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()


def _delete_chunk(conn: sqlite3.Connection, table: str, where: str = "1=1",
                  params: tuple = (), order: bool = True) -> int:
    if table not in DATA_TABLES:
        raise ValueError(f"unknown table {table}")
    order_sql = "ORDER BY received_at" if order else ""
    with transaction(conn):
        cur = conn.execute(
            f"DELETE FROM {table} WHERE rowid IN "
            f"(SELECT rowid FROM {table} WHERE {where} {order_sql} LIMIT ?)",
            (*params, CHUNK),
        )
        return cur.rowcount


def _delete_all_matching(conn: sqlite3.Connection, table: str, where: str,
                         params: tuple = (), order: bool = True) -> int:
    total = 0
    while True:
        n = _delete_chunk(conn, table, where, params, order)
        total += n
        if n < CHUNK:
            return total


def prune_by_age(conn: sqlite3.Connection, settings: dict[str, Any], now: float) -> dict[str, int]:
    removed = {}
    for table, key in (("sentences", "sentence_retention_days"),
                       ("positions", "position_retention_days")):
        days = settings[key]
        removed[table] = 0 if days <= 0 else _delete_all_matching(
            conn, table, "received_at < ?", (now - days * 86400,))
    return removed


def enforce_size_limit(conn: sqlite3.Connection, db_path: Path, max_mb: int) -> dict[str, int]:
    removed = {"sentences": 0, "positions": 0}
    if max_mb <= 0:
        return removed
    limit = max_mb * MB
    reclaim_space(conn)
    while db_file_bytes(db_path) > limit:
        table = _table_with_rows(conn)
        if table is None:
            break
        n = _delete_chunk(conn, table)
        removed[table] += n
        reclaim_space(conn)
        if n == 0:
            break
    return removed


def _table_with_rows(conn: sqlite3.Connection) -> str | None:
    for table in ("sentences", "positions"):
        if conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone():
            return table
    return None


def apply_retention(conn: sqlite3.Connection, db_path: Path, settings: dict[str, Any],
                    now: float) -> dict[str, int]:
    removed = prune_by_age(conn, settings, now)
    size_removed = enforce_size_limit(conn, db_path, settings["max_db_size_mb"])
    for table, n in size_removed.items():
        removed[table] += n
    reclaim_space(conn)
    return removed


def delete_data(conn: sqlite3.Connection, table: str, before: float,
                device_id: int | None = None) -> int:
    if device_id is None:
        return _delete_all_matching(conn, table, "received_at < ?", (before,))
    return _delete_all_matching(conn, table, "received_at < ? AND device_id = ?",
                                (before, device_id))


def clear_device(conn: sqlite3.Connection, device_id: int) -> dict[str, int]:
    removed = {}
    for table in DATA_TABLES:
        removed[table] = _delete_all_matching(conn, table, "device_id = ?", (device_id,), order=False)
    with transaction(conn):
        removed["latest"] = conn.execute("DELETE FROM latest WHERE device_id = ?", (device_id,)).rowcount
    return removed


def db_stats(conn: sqlite3.Connection, db_path: Path, data_dir: Path) -> dict[str, Any]:
    def pragma(name: str):
        return conn.execute(f"PRAGMA {name}").fetchone()[0]

    counts = {}
    for table in ("sentences", "positions", "latest", "devices", "maintenance_log"):
        counts[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    ranges = {}
    for table in DATA_TABLES:
        row = conn.execute(f"SELECT MIN(received_at), MAX(received_at) FROM {table}").fetchone()
        ranges[table] = (row[0], row[1])
    page_size = pragma("page_size")
    free_pages = pragma("freelist_count")
    usage = shutil.disk_usage(data_dir)
    return {
        "file_bytes": db_file_bytes(db_path),
        "wal_bytes": Path(f"{db_path}-wal").stat().st_size if Path(f"{db_path}-wal").exists() else 0,
        "page_size": page_size,
        "page_count": pragma("page_count"),
        "free_bytes": free_pages * page_size,
        "auto_vacuum": pragma("auto_vacuum"),
        "journal_mode": pragma("journal_mode"),
        "counts": counts,
        "ranges": ranges,
        "disk_free": usage.free,
        "disk_total": usage.total,
    }


FALLBACK_ROW_OVERHEAD = {"sentences": 80, "positions": 110}  # row + index bytes, when dbstat is missing


def _measured_bytes(conn: sqlite3.Connection, table: str) -> int | None:
    """Bytes used by a table and its indexes, from dbstat. None if this SQLite build lacks dbstat."""
    try:
        row = conn.execute(
            "SELECT COALESCE(SUM(pgsize), 0) FROM dbstat WHERE name = ? OR name IN "
            "(SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = ?)",
            (table, table),
        ).fetchone()
        return int(row[0])
    except sqlite3.OperationalError:
        return None


def growth_estimate(conn: sqlite3.Connection, db_path: Path, settings: dict[str, Any],
                    now: float, window_s: float = 86400) -> dict[str, Any]:
    """Project database growth from the most recent data.

    Uses the rows written in the last window_s seconds (or since the first row, if that is shorter).
    Bytes per row come from dbstat when available, and from an overhead estimate otherwise.
    """
    oldest = None
    for table in DATA_TABLES:
        row = conn.execute(f"SELECT MIN(received_at) FROM {table}").fetchone()[0]
        if row is not None:
            oldest = row if oldest is None else min(oldest, row)
    span = 0.0 if oldest is None else min(window_s, now - oldest)
    current = db_file_bytes(db_path)
    result: dict[str, Any] = {"enough_data": span >= 3600, "span_hours": span / 3600,
                              "current_bytes": current, "tables": {}, "method": "estimated"}
    if not result["enough_data"]:
        return result

    cutoff = now - span
    daily_total = 0.0
    steady_total: float | None = 0.0
    for table, key in (("sentences", "sentence_retention_days"), ("positions", "position_retention_days")):
        if table == "sentences" and not settings["store_sentences"]:
            rows_in_window, payload_bytes = 0, 0
        elif table == "sentences":
            rows_in_window, payload_bytes = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(LENGTH(raw) + LENGTH(fields)), 0) "
                "FROM sentences WHERE received_at >= ?", (cutoff,)).fetchone()
        else:
            rows_in_window = conn.execute(
                "SELECT COUNT(*) FROM positions WHERE received_at >= ?", (cutoff,)).fetchone()[0]
            payload_bytes = 0

        total_rows = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        measured = _measured_bytes(conn, table)
        if measured is not None and total_rows > 0:
            bytes_per_row = measured / total_rows
            result["method"] = "measured"
        else:
            avg_payload = payload_bytes / rows_in_window if rows_in_window else 0
            bytes_per_row = avg_payload + FALLBACK_ROW_OVERHEAD[table]

        rows_per_day = rows_in_window * 86400 / span
        daily = rows_per_day * bytes_per_row
        retention = settings[key]
        steady = daily * retention if retention > 0 else None
        result["tables"][table] = {
            "rows_per_day": rows_per_day,
            "bytes_per_row": bytes_per_row,
            "daily_bytes": daily,
            "retention_days": retention,
            "steady_bytes": steady,
        }
        daily_total += daily
        steady_total = None if (steady is None or steady_total is None) else steady_total + steady

    cap = settings["max_db_size_mb"] * MB
    result.update({
        "daily_bytes": daily_total,
        "steady_bytes": steady_total,
        "cap_bytes": cap,
        "days_to_cap": None,
        "effective_days": None,
    })
    if cap > 0 and daily_total > 0:
        if current < cap:
            result["days_to_cap"] = (cap - current) / daily_total
        if steady_total is None or steady_total > cap:
            result["effective_days"] = cap / daily_total
    return result


def integrity_check(conn: sqlite3.Connection) -> list[str]:
    return [row[0] for row in conn.execute("PRAGMA integrity_check").fetchall()]


def _unique_backup_path(backup_dir: Path, stamp: str) -> Path:
    path = backup_dir / f"nmealog-{stamp}.sqlite3"
    n = 1
    while path.exists():
        n += 1
        path = backup_dir / f"nmealog-{stamp}-{n}.sqlite3"
    return path


def create_backup(conn: sqlite3.Connection, backup_dir: Path, keep: int) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    final = _unique_backup_path(backup_dir, stamp)
    tmp = final.with_name(final.name + ".tmp")
    dst = sqlite3.connect(str(tmp))
    try:
        conn.backup(dst)
        dst.execute("PRAGMA journal_mode = DELETE")  # single self-contained file
    finally:
        dst.close()
    os.replace(tmp, final)
    prune_backups(backup_dir, keep)
    return final


def list_backups(backup_dir: Path) -> list[dict[str, Any]]:
    if not backup_dir.exists():
        return []
    items = []
    for p in backup_dir.iterdir():
        if BACKUP_NAME.match(p.name) and p.is_file():
            st = p.stat()
            items.append({"name": p.name, "size": st.st_size, "modified": st.st_mtime})
    return sorted(items, key=lambda i: i["name"], reverse=True)


def prune_backups(backup_dir: Path, keep: int) -> list[str]:
    deleted = []
    for item in list_backups(backup_dir)[keep:]:
        (backup_dir / item["name"]).unlink(missing_ok=True)
        deleted.append(item["name"])
    return deleted


def backup_path(backup_dir: Path, name: str) -> Path:
    if not BACKUP_NAME.match(name):
        raise FileNotFoundError(name)
    path = backup_dir / name
    if not path.is_file():
        raise FileNotFoundError(name)
    return path


def delete_backup(backup_dir: Path, name: str) -> None:
    backup_path(backup_dir, name).unlink()


def validate_database(path: Path) -> None:
    """Raise ValueError unless path is a healthy nmealog database of the current schema."""
    try:
        c = sqlite3.connect(str(path))
        try:
            if c.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise ValueError("database failed the integrity check")
            tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            missing = {"meta", "settings", "devices", "sentences", "positions"} - tables
            if missing:
                raise ValueError("not an nmealog database (missing " + ", ".join(sorted(missing)) + ")")
            row = c.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
            if not row or int(row[0]) != SCHEMA_VERSION:
                version = row[0] if row else "unknown"
                raise ValueError(f"schema version {version} is not supported (need {SCHEMA_VERSION})")
        finally:
            c.close()
    except sqlite3.DatabaseError as exc:
        raise ValueError(f"not a valid SQLite database: {exc}") from exc


def restore_database(conn: sqlite3.Connection, src_path: Path) -> None:
    """Copy src_path over the live database. Caller must validate it first."""
    src = sqlite3.connect(str(src_path))
    try:
        src.backup(conn)
    finally:
        src.close()
