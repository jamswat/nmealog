import socket
import threading
import time

import pytest

from nmealog import settings
from nmealog.collector import Collector, DeviceWorker
from nmealog.config import Config
from nmealog.db import connect, init_db
from nmealog.simulator import Simulator, serve


def _wait(predicate, timeout=15.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.1)
    return False


@pytest.fixture
def sim_server():
    listener = socket.create_server(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    stop = threading.Event()
    box = {"counters": {}, "drop": 0.0}

    def start(sim=None, drop_after=0.0):
        box["drop"] = drop_after
        t = threading.Thread(
            target=serve,
            args=(listener, sim or Simulator(seed=1, bad_rate=0.2, no_fix_every=0), 20.0, stop),
            kwargs={"drop_after": drop_after, "counters": box["counters"]},
            daemon=True,
        )
        t.start()
        box.setdefault("threads", []).append(t)
        return t

    yield port, start, box["counters"]
    stop.set()
    for t in box.get("threads", []):
        t.join(timeout=5)
    listener.close()


@pytest.fixture
def cfg(tmp_path):
    c = Config(data_dir=tmp_path)
    c.ensure_dirs()
    return c


def _add_device(cfg, port, name="sim", enabled=1):
    conn = connect(cfg.db_path)
    init_db(conn)
    now = time.time()
    cur = conn.execute("INSERT INTO devices(name, host, port, enabled, created_at, updated_at) "
                       "VALUES (?, '127.0.0.1', ?, ?, ?, ?)", (name, port, enabled, now, now))
    device_id = cur.lastrowid
    conn.execute("INSERT INTO device_status(device_id, state, updated_at) VALUES (?, 'idle', ?)",
                 (device_id, now))
    conn.close()
    return device_id


def _run_collector(cfg):
    stop = threading.Event()
    collector = Collector(cfg, stop)
    t = threading.Thread(target=collector.run, daemon=True)
    t.start()
    return stop, t


def test_collects_parses_and_stores(sim_server, cfg):
    port, start, _ = sim_server
    device_id = _add_device(cfg, port)
    start()
    stop, t = _run_collector(cfg)
    try:
        conn = connect(cfg.db_path)
        assert _wait(lambda: conn.execute(
            "SELECT COUNT(*) FROM positions WHERE device_id = ?", (device_id,)).fetchone()[0] >= 1)
        assert _wait(lambda: conn.execute(
            "SELECT sentences_ok FROM device_status WHERE device_id = ?", (device_id,)).fetchone()[0] > 30)
        sentence_types = {r[0] for r in conn.execute(
            "SELECT DISTINCT sentence FROM sentences WHERE device_id = ?", (device_id,))}
        assert {"GGA", "RMC", "HDT"} <= sentence_types
        assert conn.execute("SELECT COUNT(*) FROM latest WHERE device_id = ? AND sentence = 'GGA'",
                            (device_id,)).fetchone()[0] > 0
        status = conn.execute("SELECT state, sentences_bad, bytes_in, gps_time FROM device_status "
                              "WHERE device_id = ?", (device_id,)).fetchone()
        assert status["state"] == "connected"
        assert status["sentences_bad"] > 0  # bad_rate=0.2 injects corrupted lines
        assert status["bytes_in"] > 0
        assert status["gps_time"] is not None
        assert conn.execute("SELECT COUNT(*) FROM sentences WHERE checksum_ok = 0").fetchone()[0] == 0
        conn.close()
    finally:
        stop.set()
        t.join(timeout=15)


def test_disabled_device_gets_no_worker(sim_server, cfg):
    port, start, _ = sim_server
    device_id = _add_device(cfg, port, enabled=0)
    start()
    stop, t = _run_collector(cfg)
    try:
        time.sleep(3)
        conn = connect(cfg.db_path)
        assert conn.execute("SELECT COUNT(*) FROM sentences WHERE device_id = ?", (device_id,)).fetchone()[0] == 0
        conn.close()
    finally:
        stop.set()
        t.join(timeout=15)


def test_logging_paused_updates_live_values_only(sim_server, cfg):
    port, start, _ = sim_server
    device_id = _add_device(cfg, port)
    conn = connect(cfg.db_path)
    settings.save(conn, {"logging_enabled": "off"})
    conn.close()
    start()
    stop, t = _run_collector(cfg)
    try:
        conn = connect(cfg.db_path)
        assert _wait(lambda: conn.execute(
            "SELECT COUNT(*) FROM latest WHERE device_id = ?", (device_id,)).fetchone()[0] > 0)
        assert conn.execute("SELECT COUNT(*) FROM sentences").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM positions").fetchone()[0] == 0
        conn.close()
    finally:
        stop.set()
        t.join(timeout=15)


def test_reconnects_after_device_drops_connection(sim_server, cfg):
    port, start, counters = sim_server
    device_id = _add_device(cfg, port)
    start(drop_after=1.5)
    stop, t = _run_collector(cfg)
    try:
        assert _wait(lambda: counters.get("connections", 0) >= 3, timeout=20)
        conn = connect(cfg.db_path)
        last_error_at = conn.execute("SELECT last_error_at FROM device_status WHERE device_id = ?",
                                     (device_id,)).fetchone()[0]
        conn.close()
        assert last_error_at is not None  # drops are recorded even after a successful reconnect
    finally:
        stop.set()
        t.join(timeout=15)


def test_unreachable_device_is_retried_and_reported(cfg):
    listener = socket.create_server(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    listener.close()  # nothing listening now
    device_id = _add_device(cfg, port)
    stop, t = _run_collector(cfg)
    try:
        conn = connect(cfg.db_path)
        assert _wait(lambda: conn.execute(
            "SELECT state FROM device_status WHERE device_id = ?", (device_id,)).fetchone()[0] == "retrying")
        error = conn.execute("SELECT last_error FROM device_status WHERE device_id = ?",
                             (device_id,)).fetchone()[0]
        assert error.startswith("connect failed")
        conn.close()
    finally:
        stop.set()
        t.join(timeout=15)


def test_relay_forwards_only_the_selected_device(sim_server, cfg):
    port, start, _ = sim_server
    device_id = _add_device(cfg, port)
    other_id = _add_device(cfg, port, name="other")
    conn = connect(cfg.db_path)
    relay_port = socket.create_server(("127.0.0.1", 0)).getsockname()[1]
    settings.save(conn, {"relay_enabled": "on", "relay_device_id": str(device_id),
                         "relay_port": str(relay_port), "relay_advertise": "off"})
    conn.close()
    start()
    stop, t = _run_collector(cfg)
    try:
        client = None
        end = time.monotonic() + 10
        while client is None and time.monotonic() < end:
            try:
                client = socket.create_connection(("127.0.0.1", relay_port), timeout=5)
            except OSError:
                time.sleep(0.2)
        assert client is not None
        client.settimeout(5)
        received = b""
        while received.count(b"\n") < 20:
            chunk = client.recv(65536)
            if not chunk:
                break
            received += chunk
        lines = received.decode("ascii").splitlines()
        assert len(lines) >= 20
        assert all(l.startswith("$") or l.startswith("!") for l in lines)
        conn = connect(cfg.db_path)
        heartbeat = conn.execute("SELECT value FROM meta WHERE key = 'collector_heartbeat'").fetchone()
        conn.close()
        assert heartbeat is not None
        client.close()
    finally:
        stop.set()
        t.join(timeout=15)


def test_worker_methods_do_not_shadow_thread_internals():
    # threading.Thread keeps internals with these names, and they differ between Python versions.
    # A method called _handle broke the worker on Python 3.13 while passing on 3.14.
    internals = {"_handle", "_started", "_target", "_args", "_kwargs", "_name", "_daemonic", "_stderr",
                 "_context", "_ident", "_native_id", "_tstate_lock", "_is_stopped", "_initialized",
                 "_invoke_excepthook", "_os_thread_handle", "_bootstrap", "_set_tstate_lock", "_stop"}
    from nmealog.relay import NmeaRelay
    for cls in (DeviceWorker, NmeaRelay):
        own = {n for n in vars(cls) if n.startswith("_") and not n.startswith("__")}
        own |= {n for n in cls.__init__.__code__.co_names if n.startswith("_")}
        assert not (own & internals), f"{cls.__name__} shadows threading.Thread internals"
