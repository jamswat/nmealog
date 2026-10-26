import base64
import io
import json
import re
import time

import pytest
from werkzeug.security import generate_password_hash

from nmealog import maintenance
from nmealog.config import Config
from nmealog.db import connect, init_db
from nmealog.web import create_app

PASSWORD = "correct horse"
AUTH = {"Authorization": "Basic " + base64.b64encode(f"admin:{PASSWORD}".encode()).decode()}


@pytest.fixture
def cfg(tmp_path):
    return Config(data_dir=tmp_path, user="admin", password_hash=generate_password_hash(PASSWORD))


@pytest.fixture
def app(cfg):
    app = create_app(cfg)
    app.config["TESTING"] = True
    return app


@pytest.fixture
def client(app):
    return app.test_client()


def _csrf(client, path="/settings"):
    html = client.get(path, headers=AUTH).get_data(as_text=True)
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


def test_redirects_to_setup_when_no_credentials(tmp_path):
    app = create_app(Config(data_dir=tmp_path, allow_no_auth=False))
    app.config["TESTING"] = True
    client = app.test_client()
    resp = client.get("/")
    assert resp.status_code == 302
    assert resp.location.endswith("/setup")


def test_setup_page_renders_and_creates_credentials(tmp_path):
    app = create_app(Config(data_dir=tmp_path, allow_no_auth=False))
    app.config["TESTING"] = True
    client = app.test_client()

    # GET shows the form
    page = client.get("/setup")
    assert page.status_code == 200
    assert b"Create an administrator" in page.data

    # POST with mismatched passwords
    token = _csrf_for(client, "/setup")
    resp = client.post("/setup", data={"csrf": token, "username": "admin",
                                        "password": "secret123", "confirm": "secret999"})
    assert resp.status_code == 400
    assert b"do not match" in resp.data

    # Successful creation
    resp = client.post("/setup", data={"csrf": token, "username": "captain",
                                        "password": "secret123", "confirm": "secret123"})
    assert resp.status_code == 302
    # Now auth is required - hitting / should return 401 (no auth header)
    assert client.get("/").status_code == 401
    # Setup page redirects to / when already configured
    assert client.get("/setup").status_code == 302


def _csrf_for(client, path):
    html = client.get(path).get_data(as_text=True)
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


def test_requires_basic_auth(client):
    assert client.get("/").status_code == 401
    bad = {"Authorization": "Basic " + base64.b64encode(b"admin:wrong").decode()}
    assert client.get("/", headers=bad).status_code == 401


def test_health_is_open_and_reports_db(client):
    resp = client.get("/health")
    assert resp.status_code == 200 and resp.get_json() == {"ok": True}


def test_pages_render(client):
    for path in ("/", "/track", "/sentences", "/devices", "/settings", "/maintenance"):
        resp = client.get(path, headers=AUTH)
        assert resp.status_code == 200, path


def test_software_version_is_shown_in_the_header(client):
    from nmealog import __version__

    html = client.get("/", headers=AUTH).get_data(as_text=True)
    assert f"v{__version__}" in html


def test_post_without_csrf_is_rejected(client):
    resp = client.post("/devices", headers=AUTH, data={"name": "x", "host": "10.0.0.1", "port": "4001"})
    assert resp.status_code == 400


def test_device_create_validates_and_saves(client, cfg):
    token = _csrf(client, "/devices")
    bad = client.post("/devices", headers=AUTH, data={"csrf": token, "name": "", "host": "bad host!", "port": "99999"})
    assert bad.status_code == 400
    ok = client.post("/devices", headers=AUTH, data={"csrf": token, "name": "gps", "host": "192.168.1.100",
                                                     "port": "4001", "enabled": "on"})
    assert ok.status_code == 302
    conn = connect(cfg.db_path)
    row = conn.execute("SELECT name, host, port, enabled FROM devices").fetchone()
    assert tuple(row) == ("gps", "192.168.1.100", 4001, 1)
    assert conn.execute("SELECT state FROM device_status").fetchone()[0] == "connecting"
    conn.close()


def test_device_name_must_be_unique(client, cfg):
    token = _csrf(client, "/devices")
    data = {"csrf": token, "name": "gps", "host": "10.0.0.1", "port": "4001"}
    assert client.post("/devices", headers=AUTH, data=data).status_code == 302
    assert client.post("/devices", headers=AUTH, data=data).status_code == 400


def test_device_delete_and_clear(client, cfg):
    token = _csrf(client, "/devices")
    client.post("/devices", headers=AUTH, data={"csrf": token, "name": "gps", "host": "10.0.0.1", "port": "4001"})
    conn = connect(cfg.db_path)
    device_id = conn.execute("SELECT id FROM devices").fetchone()[0]
    conn.execute("INSERT INTO positions(device_id, received_at, latitude, longitude, source) "
                 "VALUES (?, ?, 50, -1, 'GGA')", (device_id, time.time()))
    conn.close()
    assert client.post(f"/devices/{device_id}/clear", headers=AUTH, data={"csrf": token}).status_code == 302
    conn = connect(cfg.db_path)
    assert conn.execute("SELECT COUNT(*) FROM positions").fetchone()[0] == 0
    conn.close()
    assert client.post(f"/devices/{device_id}/delete", headers=AUTH, data={"csrf": token}).status_code == 302
    conn = connect(cfg.db_path)
    assert conn.execute("SELECT COUNT(*) FROM devices").fetchone()[0] == 0
    conn.close()


def test_settings_save_and_reject(client, cfg):
    token = _csrf(client)
    resp = client.post("/settings", headers=AUTH, data={
        "csrf": token, "max_db_size_mb": "250", "position_interval_s": "30", "display_tz": "Europe/London",
        "logging_enabled": "on", "auto_backup": "on"})
    assert resp.status_code == 302
    conn = connect(cfg.db_path)
    from nmealog import settings
    loaded = settings.load(conn)
    assert loaded["max_db_size_mb"] == 250
    assert loaded["store_sentences"] is False  # unchecked box means off
    assert loaded["display_tz"] == "Europe/London"
    conn.close()
    bad = client.post("/settings", headers=AUTH, data={"csrf": token, "position_interval_s": "0",
                                                       "display_tz": "Nowhere/Land"})
    assert bad.status_code == 400
    assert b"must be at least 1" in bad.data


def test_backup_download_restore_flow(client, cfg):
    token = _csrf(client, "/maintenance")
    assert client.post("/maintenance/backup", headers=AUTH, data={"csrf": token}).status_code == 302
    backups = maintenance.list_backups(cfg.backup_dir)
    assert len(backups) == 1
    name = backups[0]["name"]

    dl = client.get(f"/maintenance/backups/{name}", headers=AUTH)
    assert dl.status_code == 200 and dl.data[:16] == b"SQLite format 3\x00"

    conn = connect(cfg.db_path)
    conn.execute("INSERT INTO devices(name, host, port, created_at, updated_at) VALUES ('later', 'h', 1, 0, 0)")
    conn.close()

    no_confirm = client.post(f"/maintenance/backups/{name}/restore", headers=AUTH,
                             data={"csrf": token, "confirm": "nope"})
    assert no_confirm.status_code == 302
    conn = connect(cfg.db_path)
    assert conn.execute("SELECT COUNT(*) FROM devices").fetchone()[0] == 1
    conn.close()

    ok = client.post(f"/maintenance/backups/{name}/restore", headers=AUTH,
                     data={"csrf": token, "confirm": "RESTORE"})
    assert ok.status_code == 302
    conn = connect(cfg.db_path)
    assert conn.execute("SELECT COUNT(*) FROM devices").fetchone()[0] == 0
    conn.close()
    assert len(maintenance.list_backups(cfg.backup_dir)) == 2  # safety copy was taken first


def test_restore_upload_rejects_garbage(client, cfg):
    token = _csrf(client, "/maintenance")
    resp = client.post("/maintenance/restore", headers=AUTH, content_type="multipart/form-data", data={
        "csrf": token, "confirm": "RESTORE",
        "file": (io.BytesIO(b"not a database" * 50), "junk.sqlite3")})
    assert resp.status_code == 302
    assert not any(p.name.startswith("upload-") for p in cfg.backup_dir.iterdir())


def test_maintenance_actions(client, cfg):
    token = _csrf(client, "/maintenance")
    for action in ("checkpoint", "reclaim", "optimize", "integrity", "prune", "enforce-size", "vacuum"):
        resp = client.post(f"/maintenance/{action}", headers=AUTH, data={"csrf": token})
        assert resp.status_code == 302, action
    assert client.post("/maintenance/nonsense", headers=AUTH, data={"csrf": token}).status_code == 404


def test_delete_data_requires_typed_confirmation(client, cfg):
    conn = connect(cfg.db_path)
    init_db(conn)
    conn.execute("INSERT INTO devices(id, name, host, port, created_at, updated_at) VALUES (1, 'gps', 'h', 1, 0, 0)")
    conn.execute("INSERT INTO sentences(device_id, received_at, talker, sentence, raw, fields) "
                 "VALUES (1, 1000, 'GP', 'GGA', 'x', '{}')")
    conn.close()
    token = _csrf(client, "/maintenance")
    form = {"csrf": token, "table": "sentences", "before": "2030-01-01", "device": ""}
    client.post("/maintenance/delete-data", headers=AUTH, data={**form, "confirm": "yes"})
    conn = connect(cfg.db_path)
    assert conn.execute("SELECT COUNT(*) FROM sentences").fetchone()[0] == 1
    conn.close()
    client.post("/maintenance/delete-data", headers=AUTH, data={**form, "confirm": "DELETE"})
    conn = connect(cfg.db_path)
    assert conn.execute("SELECT COUNT(*) FROM sentences").fetchone()[0] == 0
    conn.close()


def test_export_csv(client, cfg):
    conn = connect(cfg.db_path)
    init_db(conn)
    now = time.time()
    conn.execute("INSERT INTO devices(id, name, host, port, created_at, updated_at) VALUES (1, 'gps', 'h', 1, 0, 0)")
    conn.execute("INSERT INTO positions(device_id, received_at, latitude, longitude, source) "
                 "VALUES (1, ?, 50.5, -1.25, 'GGA')", (now,))
    conn.close()
    resp = client.get("/export?table=positions", headers=AUTH)
    assert resp.status_code == 200
    assert resp.mimetype == "text/csv"
    lines = resp.get_data(as_text=True).splitlines()
    assert lines[0].startswith("received_utc,device,latitude")
    assert "50.5" in lines[1]


def test_export_rejects_unknown_table(client):
    assert client.get("/export?table=users", headers=AUTH).status_code == 400


def test_map_page_and_positions_api(client, cfg):
    conn = connect(cfg.db_path)
    init_db(conn)
    now = time.time()
    conn.execute("INSERT INTO devices(id, name, host, port, created_at, updated_at) VALUES (1, 'gps', 'h', 1, 0, 0)")
    for i in range(3):
        conn.execute("INSERT INTO positions(device_id, received_at, latitude, longitude, source, speed_kn, course_true) "
                     "VALUES (1, ?, ?, -1.25, 'RMC', 8.0, 90.0)", (now - 60 * (3 - i), 50.5 + i * 0.001))
    conn.close()
    page = client.get("/map", headers=AUTH)
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    assert "leaflet.js" in html and "map.js" in html and 'id="map"' in html

    api = client.get("/api/positions?device=1", headers=AUTH)
    assert api.status_code == 200
    data = api.get_json()
    assert data["tz"] == "UTC"
    assert [p["lat"] for p in data["points"]] == pytest.approx([50.5, 50.501, 50.502])  # oldest first
    assert data["points"][0]["device"] == "gps"

    limited = client.get("/api/positions?limit=2", headers=AUTH).get_json()
    assert len(limited["points"]) == 2
    assert client.get("/api/positions?limit=abc", headers=AUTH).status_code == 400
    assert client.get("/api/positions?device=99", headers=AUTH).get_json()["points"] == []


def test_map_api_requires_auth(client):
    assert client.get("/api/positions").status_code == 401


def test_relay_settings_save_and_device_dropdown(client, cfg):
    conn = connect(cfg.db_path)
    init_db(conn)
    conn.execute("INSERT INTO devices(id, name, host, port, created_at, updated_at) VALUES (7, 'gps', 'h', 1, 0, 0)")
    conn.close()
    page = client.get("/settings", headers=AUTH).get_data(as_text=True)
    assert 'name="relay_device_id"' in page and '<option value="7">gps</option>' in page
    token = _csrf(client)
    resp = client.post("/settings", headers=AUTH, data={
        "csrf": token, "relay_enabled": "on", "relay_device_id": "7", "relay_port": "10110",
        "relay_advertise": "on", "relay_name": "ship"})
    assert resp.status_code == 302
    conn = connect(cfg.db_path)
    from nmealog import settings
    loaded = settings.load(conn)
    assert loaded["relay_enabled"] is True and loaded["relay_device_id"] == 7
    assert loaded["relay_port"] == 10110 and loaded["relay_name"] == "ship"
    conn.close()
    bad = client.post("/settings", headers=AUTH, data={"csrf": token, "relay_port": "70000"})
    assert bad.status_code == 400


def test_maintenance_page_performance_section(client, cfg):
    page = client.get("/maintenance", headers=AUTH).get_data(as_text=True)
    assert "Performance" in page
    assert "No collector heartbeat yet" in page

    conn = connect(cfg.db_path)
    heartbeat = {
        "ts": time.time(), "pid": 4242, "workers": ["gps", "gyro"],
        "storage_error": None, "relay": None,
        "perf": {
            "pending_rows": 0, "rows_written_total": 1234, "flushes": 57,
            "sentences_per_s": 9.5, "positions_per_s": 0.02, "bytes_per_s": 2500.0,
            "flush_ms_avg": 2.5, "flush_ms_max": 9.0, "window_s": 60.0,
        },
    }
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('collector_heartbeat', ?)",
                 (json.dumps(heartbeat),))
    conn.close()
    page = client.get("/maintenance", headers=AUTH).get_data(as_text=True)
    assert "Performance" in page
    assert "pid 4242" in page
    assert "2 device workers" in page
    assert "9.5" in page                       # sentences per second
    assert "2.5 ms avg, 9.0 ms max" in page
    assert "1,234" in page                    # rows written this run (comma-formatted)
    assert "collector is behind" not in page

    conn = connect(cfg.db_path)
    heartbeat["ts"] = time.time() - 600        # stale heartbeat → flagged
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('collector_heartbeat', ?)",
                 (json.dumps(heartbeat),))
    conn.close()
    page = client.get("/maintenance", headers=AUTH).get_data(as_text=True)
    assert "not reporting" in page


def test_device_test_page_renders_for_a_device(client, cfg, monkeypatch):
    from nmealog import web

    monkeypatch.setattr(web.socket, "create_connection", _refuse)
    token = _csrf(client, "/devices")
    client.post("/devices", headers=AUTH, data={"csrf": token, "name": "gps", "host": "10.0.0.9", "port": "4001"})
    conn = connect(cfg.db_path)
    device_id = conn.execute("SELECT id FROM devices").fetchone()[0]
    conn.close()
    resp = client.post(f"/devices/{device_id}/test", headers=AUTH, data={"csrf": token})
    assert resp.status_code == 200
    assert b"Could not connect" in resp.data
    assert f"/devices/{device_id}".encode() in resp.data


def _refuse(*args, **kwargs):
    raise ConnectionRefusedError("refused in test")
