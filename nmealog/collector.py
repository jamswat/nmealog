"""Collector: one persistent TCP connection per device, parsed and written to SQLite in batches."""
from __future__ import annotations

import json
import logging
import os
import socket
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Any

from . import maintenance, settings as app_settings
from .config import Config
from .db import connect, get_meta, init_db, log_action, set_meta, transaction
from .nmea import Parsed, parse_line
from .relay import NmeaRelay

log = logging.getLogger("nmealog.collector")

MAX_LINE_BYTES = 512  # NMEA 0183 sentences are at most 82 characters
RECV_TIMEOUT_S = 1.0
FLUSH_INTERVAL_S = 1.0
MAX_PENDING = 20000
RECONNECT_MAX_S = 60.0
HEARTBEAT_INTERVAL_S = 10.0
TICK_S = 2.0


@dataclass(frozen=True)
class DeviceSpec:
    id: int
    name: str
    host: str
    port: int


class Shared:
    """State shared by the manager and the device workers."""

    def __init__(self, settings: dict[str, Any]):
        self.settings = settings
        self.storage_error: str | None = None
        self.relay: NmeaRelay | None = None
        self.relay_source_id = 0


class DeviceWorker(threading.Thread):
    def __init__(self, spec: DeviceSpec, db_path, shared: Shared):
        super().__init__(name=f"device-{spec.id}", daemon=True)
        self.spec = spec
        self.db_path = db_path
        self.shared = shared
        self._stop_event = threading.Event()
        self.conn: sqlite3.Connection | None = None
        self._last_flush = 0.0
        self._last_position_at = 0.0
        self._reset_pending()

    def _reset_pending(self) -> None:
        self.pending_sentences: list[tuple] = []
        self.pending_positions: list[tuple] = []
        self.pending_latest: dict[tuple[str, str], tuple[str, float]] = {}
        self.ok_delta = 0
        self.bad_delta = 0
        self.bytes_delta = 0
        self.last_seen: float | None = None
        self.gps_time: float | None = None
        self.clock_offset: float | None = None

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        self.conn = connect(self.db_path)
        try:
            self.conn.execute(
                "INSERT OR IGNORE INTO device_status(device_id, state, updated_at) VALUES (?, 'connecting', ?)",
                (self.spec.id, time.time()),
            )
            self._run_forever()
        except Exception:
            log.exception("%s: worker crashed", self.spec.name)
        finally:
            self._flush()
            self._status(state="stopped")
            self.conn.close()

    def _run_forever(self) -> None:
        spec = self.spec
        delay = 1.0
        while not self._stop_event.is_set():
            self._status(state="connecting")
            try:
                sock = socket.create_connection(
                    (spec.host, spec.port),
                    timeout=self.shared.settings["connect_timeout_s"],
                )
            except OSError as exc:
                self._status(state="retrying", last_error=f"connect failed: {exc}",
                             last_error_at=time.time())
                log.warning("%s: connect to %s:%s failed: %s", spec.name, spec.host, spec.port, exc)
                self._stop_event.wait(delay)
                delay = min(delay * 2, RECONNECT_MAX_S)
                continue

            delay = 1.0
            log.info("%s: connected to %s:%s", spec.name, spec.host, spec.port)
            self._status(state="connected", connected_at=time.time(), last_error=None)
            reason = self._read(sock)
            sock.close()
            self._flush()
            if not self._stop_event.is_set():
                log.warning("%s: disconnected: %s", spec.name, reason)
                self._status(state="retrying", last_error=reason, last_error_at=time.time())
                self._stop_event.wait(1.0)

    def _read(self, sock: socket.socket) -> str:
        sock.settimeout(RECV_TIMEOUT_S)
        buf = bytearray()
        last_data = time.monotonic()
        while not self._stop_event.is_set():
            idle_limit = self.shared.settings["idle_timeout_s"]
            try:
                chunk = sock.recv(4096)
            except TimeoutError:
                if time.monotonic() - last_data > idle_limit:
                    return f"no data for {idle_limit} s"
                self._maybe_flush()
                continue
            except OSError as exc:
                return f"connection error: {exc}"
            if not chunk:
                return "closed by device"
            last_data = time.monotonic()
            self.bytes_delta += len(chunk)
            buf.extend(chunk)
            while True:
                nl = buf.find(b"\n")
                if nl < 0:
                    break
                line = bytes(buf[:nl])
                del buf[:nl + 1]
                self._handle_line(line)
            if len(buf) > MAX_LINE_BYTES:
                self.bad_delta += 1
                buf.clear()
            self._maybe_flush()
        return "stopped"

    def _handle_line(self, raw: bytes) -> None:
        text = raw.decode("ascii", errors="replace").strip()
        if not text:
            return
        now = time.time()
        try:
            parsed: Parsed = parse_line(text)
        except ValueError:
            self.bad_delta += 1
            return
        self.last_seen = now
        settings = self.shared.settings
        store = settings["logging_enabled"]

        if parsed.checksum_ok is False:
            self.bad_delta += 1
            if store and settings["store_invalid"]:
                self._queue_sentence(now, parsed, text)
            return

        self.ok_delta += 1
        relay = self.shared.relay
        if relay is not None and self.spec.id == self.shared.relay_source_id:
            relay.publish(text)
        if parsed.gps_time is not None:
            self.gps_time = parsed.gps_time
            self.clock_offset = now - parsed.gps_time

        for field, value in parsed.fields.items():
            self.pending_latest[(parsed.sentence, field)] = (value, now)

        if store and settings["store_sentences"]:
            self._queue_sentence(now, parsed, text)

        pos = parsed.position
        if store and pos is not None and now - self._last_position_at >= settings["position_interval_s"]:
            self._last_position_at = now
            self.pending_positions.append((
                self.spec.id, now, pos.latitude, pos.longitude, pos.source,
                pos.fix_quality, pos.speed_kn, pos.course_true, pos.satellites, pos.hdop,
            ))

    def _queue_sentence(self, now: float, parsed: Parsed, text: str) -> None:
        checksum = None if parsed.checksum_ok is None else int(parsed.checksum_ok)
        self.pending_sentences.append((
            self.spec.id, now, parsed.talker, parsed.sentence, checksum, text,
            json.dumps(parsed.fields, separators=(",", ":")),
        ))

    def _maybe_flush(self) -> None:
        if time.monotonic() - self._last_flush >= FLUSH_INTERVAL_S:
            self._flush()

    def _flush(self) -> None:
        self._last_flush = time.monotonic()
        has_work = (self.pending_sentences or self.pending_positions or self.pending_latest
                    or self.ok_delta or self.bad_delta or self.bytes_delta or self.last_seen)
        if not has_work:
            return
        try:
            with transaction(self.conn):
                self.conn.executemany(
                    "INSERT INTO sentences(device_id, received_at, talker, sentence, checksum_ok, raw, fields) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    self.pending_sentences,
                )
                self.conn.executemany(
                    "INSERT INTO positions(device_id, received_at, latitude, longitude, source, "
                    "fix_quality, speed_kn, course_true, satellites, hdop) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    self.pending_positions,
                )
                self.conn.executemany(
                    "INSERT INTO latest(device_id, sentence, field, value, received_at) VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(device_id, sentence, field) DO UPDATE SET "
                    "value = excluded.value, received_at = excluded.received_at",
                    [(self.spec.id, s, f, v, t) for (s, f), (v, t) in self.pending_latest.items()],
                )
                self.conn.execute(
                    "UPDATE device_status SET sentences_ok = sentences_ok + ?, "
                    "sentences_bad = sentences_bad + ?, bytes_in = bytes_in + ?, "
                    "last_seen = COALESCE(?, last_seen), gps_time = COALESCE(?, gps_time), "
                    "clock_offset_s = COALESCE(?, clock_offset_s), updated_at = ? WHERE device_id = ?",
                    (self.ok_delta, self.bad_delta, self.bytes_delta, self.last_seen, self.gps_time,
                     self.clock_offset, time.time(), self.spec.id),
                )
            self._reset_pending()
            self.shared.storage_error = None
        except sqlite3.IntegrityError as exc:
            # Usually the device was deleted while this batch was in flight.
            log.warning("%s: dropped %d sentences: %s", self.spec.name, len(self.pending_sentences), exc)
            self._reset_pending()
        except sqlite3.Error as exc:
            log.error("%s: write failed, keeping batch: %s", self.spec.name, exc)
            self.shared.storage_error = str(exc)
            for attr in ("pending_sentences", "pending_positions"):
                pending = getattr(self, attr)
                if len(pending) > MAX_PENDING:
                    del pending[:len(pending) - MAX_PENDING]

    def _status(self, **fields: Any) -> None:
        fields["updated_at"] = time.time()
        assignments = ", ".join(f"{k} = ?" for k in fields)
        try:
            self.conn.execute(
                f"UPDATE device_status SET {assignments} WHERE device_id = ?",
                (*fields.values(), self.spec.id),
            )
        except sqlite3.Error as exc:
            log.warning("%s: could not update status: %s", self.spec.name, exc)


class Collector:
    def __init__(self, cfg: Config, stop_event: threading.Event | None = None):
        self.cfg = cfg
        self.stop_event = stop_event or threading.Event()
        self.workers: dict[int, DeviceWorker] = {}
        self.shared: Shared | None = None

    def run(self) -> None:
        self.cfg.ensure_dirs()
        conn = connect(self.cfg.db_path)
        init_db(conn)
        self.shared = Shared(app_settings.load(conn))
        log.info("collector started, database %s", self.cfg.db_path)
        last_heartbeat = 0.0
        relay_key = None
        try:
            while not self.stop_event.is_set():
                try:
                    self.shared.settings = app_settings.load(conn)
                    relay_key = self._sync_relay(self.shared.settings, relay_key)
                    self._reconcile(conn)
                    now = time.time()
                    if now - last_heartbeat >= HEARTBEAT_INTERVAL_S:
                        self._heartbeat(conn, now)
                        last_heartbeat = now
                    self._maintenance(conn, self.shared.settings, now)
                except sqlite3.Error as exc:
                    log.error("manager database error: %s", exc)
                self.stop_event.wait(TICK_S)
        finally:
            for worker in self.workers.values():
                worker.stop()
            for worker in self.workers.values():
                worker.join(timeout=10)
            self.workers.clear()
            self._stop_relay()
            conn.close()
            log.info("collector stopped")

    def _sync_relay(self, settings: dict[str, Any], current_key):
        """Start, restart or stop the LAN relay when its settings change. Returns the active key."""
        key = None
        if settings["relay_enabled"]:
            key = (settings["relay_port"], settings["relay_advertise"], settings["relay_name"])
        self.shared.relay_source_id = settings["relay_device_id"]
        if key == current_key:
            return current_key
        self._stop_relay()
        if key is not None:
            port, advertise, name = key
            self.shared.relay = NmeaRelay(port, advertise, name)
            self.shared.relay.start()
            log.info("LAN relay enabled on port %s", port)
        else:
            log.info("LAN relay disabled")
        return key

    def _stop_relay(self) -> None:
        if self.shared.relay is not None:
            self.shared.relay.stop()
            self.shared.relay.join(timeout=5)
            self.shared.relay = None

    def _reconcile(self, conn: sqlite3.Connection) -> None:
        rows = conn.execute("SELECT id, name, host, port, enabled FROM devices").fetchall()
        wanted = {r["id"]: DeviceSpec(r["id"], r["name"], r["host"], r["port"])
                  for r in rows if r["enabled"]}
        for device_id, worker in list(self.workers.items()):
            spec = wanted.get(device_id)
            if spec is None or spec != worker.spec or not worker.is_alive():
                worker.stop()
                worker.join(timeout=10)
                del self.workers[device_id]
        for device_id, spec in wanted.items():
            if device_id not in self.workers:
                worker = DeviceWorker(spec, self.cfg.db_path, self.shared)
                worker.start()
                self.workers[device_id] = worker

    def _heartbeat(self, conn: sqlite3.Connection, now: float) -> None:
        payload = {
            "ts": now,
            "pid": os.getpid(),
            "workers": sorted(w.spec.name for w in self.workers.values() if w.is_alive()),
            "storage_error": self.shared.storage_error,
            "relay": self.shared.relay.status() if self.shared.relay else None,
        }
        set_meta(conn, "collector_heartbeat", json.dumps(payload))

    def _maintenance(self, conn: sqlite3.Connection, settings: dict[str, Any], now: float) -> None:
        if settings["auto_maintenance"] and now - float(get_meta(conn, "last_maintenance_at", "0")) \
                >= settings["maintenance_interval_min"] * 60:
            set_meta(conn, "last_maintenance_at", str(now))
            removed = maintenance.apply_retention(conn, self.cfg.db_path, settings, now)
            if any(removed.values()):
                detail = ", ".join(f"{k} {v}" for k, v in removed.items())
                log_action(conn, "auto", "prune", detail)
                log.info("maintenance removed %s", detail)

        if settings["auto_backup"] and now - float(get_meta(conn, "last_backup_at", "0")) \
                >= settings["backup_interval_h"] * 3600:
            path = maintenance.create_backup(conn, self.cfg.backup_dir, settings["backup_keep"])
            set_meta(conn, "last_backup_at", str(now))
            log_action(conn, "auto", "backup", path.name)
            log.info("backup written to %s", path)
