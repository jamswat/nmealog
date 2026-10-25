"""Tests for the Signal K, GeoJSON and Current-state API endpoints."""
from __future__ import annotations

import json
import time

import pytest
from werkzeug.security import generate_password_hash

from nmealog.config import Config
from nmealog.db import connect, init_db
from nmealog.web import create_app

PASSWORD = "correct horse"
AUTH = {"Authorization": "Basic " + __import__("base64").b64encode(f"admin:{PASSWORD}".encode()).decode()}


def _enable_api(conn):
    """Insert settings rows that enable the API."""
    now = time.time()
    for key, value in (("api_enabled", True), ("api_allowed_subnets", "")):
        conn.execute(
            "INSERT OR REPLACE INTO settings(key, value, updated_at) VALUES (?, ?, ?)",
            (key, json.dumps(value), now),
        )


def _add_device(conn, name: str = "gps", host: str = "10.0.0.1", port: int = 4001):
    now = time.time()
    conn.execute(
        "INSERT INTO devices(id, name, host, port, enabled, notes, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, 1, '', ?, ?)",
        (1, name, host, port, now, now),
    )
    conn.execute(
        "INSERT INTO device_status(device_id, state, updated_at) VALUES (?, 'connected', ?)",
        (1, now),
    )


def _add_position(conn, device_id: int = 1, lat: float = 50.775, lon: float = -1.295,
                  source: str = "GGA", ts: float | None = None):
    ts = ts or time.time()
    conn.execute(
        "INSERT INTO positions(device_id, received_at, latitude, longitude, source, "
        "fix_quality, speed_kn, course_true, satellites, hdop) VALUES (?, ?, ?, ?, ?, 1, 8.5, 90.0, 8, 0.9)",
        (device_id, ts, lat, lon, source),
    )


def _add_latest(conn, device_id: int, sentence: str, field: str, value: str,
                ts: float | None = None):
    ts = ts or time.time()
    conn.execute(
        "INSERT OR REPLACE INTO latest(device_id, sentence, field, value, received_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (device_id, sentence, field, value, ts),
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def cfg(tmp_path):
    return Config(data_dir=tmp_path, user="admin", password_hash=generate_password_hash(PASSWORD))


@pytest.fixture
def app(cfg):
    a = create_app(cfg)
    a.config["TESTING"] = True
    conn = connect(cfg.db_path)
    _enable_api(conn)
    _add_device(conn)
    _add_position(conn)
    _add_latest(conn, 1, "GGA", "latitude", "5030.1234")
    _add_latest(conn, 1, "GGA", "longitude", "00117.4567")
    _add_latest(conn, 1, "GGA", "gps_qual", "1")
    _add_latest(conn, 1, "GGA", "num_sats", "8")
    _add_latest(conn, 1, "GGA", "horizontal_dil", "0.9")
    _add_latest(conn, 1, "RMC", "spd_over_grnd", "8.5")
    _add_latest(conn, 1, "RMC", "true_course", "90.0")
    _add_latest(conn, 1, "VTG", "sog", "8.5")
    _add_latest(conn, 1, "VTG", "cog_t", "90.0")
    _add_latest(conn, 1, "HDT", "heading", "180.5")
    _add_latest(conn, 1, "ROT", "rate", "0.5")
    conn.close()
    return a


@pytest.fixture
def client(app):
    return app.test_client()


# ---------------------------------------------------------------------------
# Signal K
# ---------------------------------------------------------------------------


class TestSignalK:
    def test_api_root(self, client):
        resp = client.get("/signalk/v1/api/")
        assert resp.status_code == 200
        assert resp.mimetype == "application/vnd.signalk+json"
        data = resp.get_json()
        assert data["version"] == "1.0.0"
        assert data["server"]["id"] == "nmealog"
        assert "endpoints" in data

    def test_vessels_list(self, client):
        resp = client.get("/signalk/v1/api/vessels/")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["version"] == "1.0.0"
        assert "vessels" in data
        assert "self" in data["vessels"]

    def test_self_vessel_has_navigation(self, client):
        resp = client.get("/signalk/v1/api/vessels/self/")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "navigation" in data
        nav = data["navigation"]

        # Position
        assert "position" in nav
        pos = nav["position"]
        assert "value" in pos
        assert "latitude" in pos["value"]
        assert "longitude" in pos["value"]
        assert "$source" in pos
        assert "timestamp" in pos
        assert pos["pgn"] == 129025

    def test_position_path(self, client):
        resp = client.get("/signalk/v1/api/vessels/self/navigation/position")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "value" in data
        assert "latitude" in data["value"]
        assert "longitude" in data["value"]
        assert data["pgn"] == 129025

    def test_heading_path(self, client):
        resp = client.get("/signalk/v1/api/vessels/self/navigation/headingTrue")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "value" in data
        assert data["pgn"] == 127250
        assert data["description"] == "Heading (True)"

    def test_speed_path(self, client):
        resp = client.get("/signalk/v1/api/vessels/self/navigation/speedOverGround")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "value" in data
        # Speed should be converted to m/s
        import math
        assert abs(data["value"] - 8.5 * 0.514444) < 0.001
        assert data["units"] == "m/s"

    def test_rate_of_turn_path(self, client):
        resp = client.get("/signalk/v1/api/vessels/self/navigation/rateOfTurn")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "value" in data
        # 0.5 deg/min  → rad/s
        expected = 0.5 * 3.14159 / 180 / 60
        assert abs(data["value"] - expected) < 0.001
        assert data["units"] == "rad/s"

    def test_gnss_path(self, client):
        resp = client.get("/signalk/v1/api/vessels/self/navigation/gnss")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "methodQuality" in data
        assert "satellites" in data
        assert "horizontalDilution" in data

    def test_unknown_vessel_returns_404(self, client):
        resp = client.get("/signalk/v1/api/vessels/nonexistent-vessel/")
        assert resp.status_code == 404

    def test_unknown_path_returns_404(self, client):
        resp = client.get("/signalk/v1/api/vessels/self/navigation/doesnotexist")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# GeoJSON
# ---------------------------------------------------------------------------


class TestGeoJSON:
    def test_positions_json(self, client):
        resp = client.get("/api/v1/positions")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "points" in data
        assert len(data["points"]) > 0
        assert "tz" in data

    def test_positions_geojson_mimetype(self, client):
        resp = client.get("/api/v1/positions.geojson")
        assert resp.status_code == 200
        assert resp.mimetype == "application/geo+json"

    def test_positions_geojson_structure(self, client):
        resp = client.get("/api/v1/positions.geojson")
        data = resp.get_json()
        assert data["type"] == "FeatureCollection"
        assert "features" in data
        assert len(data["features"]) > 0
        feat = data["features"][0]
        assert feat["type"] == "Feature"
        assert feat["geometry"]["type"] == "Point"
        # GeoJSON uses [longitude, latitude] order per RFC 7946
        assert len(feat["geometry"]["coordinates"]) == 2
        assert "deviceName" in feat["properties"]
        assert "receivedAt" in feat["properties"]
        assert "speedKn" in feat["properties"]

    def test_geojson_via_format_param(self, client):
        resp = client.get("/api/v1/positions?format=geojson")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["type"] == "FeatureCollection"

    def test_position_time_filter(self, client, cfg):
        """Add historical positions then query with since."""
        conn = connect(cfg.db_path)
        _add_position(conn, ts=time.time() - 3600)
        _add_position(conn, ts=time.time() - 7200)
        conn.close()
        now = time.time()
        since = now - 4000  # should include the most recent position
        resp = client.get(f"/api/v1/positions.geojson?since={since}")
        data = resp.get_json()
        assert len(data["features"]) > 0

    def test_position_limit(self, client, cfg):
        """Insert many positions and check limit works."""
        conn = connect(cfg.db_path)
        for i in range(10):
            _add_position(conn, lat=50.0 + i * 0.01, ts=time.time() - i * 10)
        conn.close()
        resp = client.get("/api/v1/positions?limit=3")
        data = resp.get_json()
        assert len(data["points"]) <= 3

    def test_position_device_filter(self, client, cfg):
        resp = client.get("/api/v1/positions.geojson?device=1")
        data = resp.get_json()
        assert len(data["features"]) > 0
        for f in data["features"]:
            assert f["properties"]["deviceId"] == 1


# ---------------------------------------------------------------------------
# Current state
# ---------------------------------------------------------------------------


class TestCurrent:
    def test_current_structure(self, client):
        resp = client.get("/api/v1/current")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "server" in data
        assert "devices" in data
        assert len(data["devices"]) > 0

    def test_current_device_fields(self, client):
        resp = client.get("/api/v1/current")
        data = resp.get_json()
        device = data["devices"][0]
        assert "id" in device
        assert "name" in device
        assert "state" in device
        assert "position" in device
        assert "values" in device
        assert device["position"]["latitude"] == 50.775
        assert device["position"]["longitude"] == -1.295
        assert device["position"]["source"] == "GGA"

    def test_current_live_values(self, client):
        resp = client.get("/api/v1/current")
        data = resp.get_json()
        device = data["devices"][0]
        values = device["values"]
        assert "GGA" in values
        assert values["GGA"]["latitude"]["value"] == "5030.1234"

    def test_current_server_info(self, client):
        resp = client.get("/api/v1/current")
        data = resp.get_json()
        assert "version" in data["server"]
        assert "time" in data["server"]
        assert "collectorAlive" in data["server"]


# ---------------------------------------------------------------------------
# GeoJSON validation with the geojson library
# ---------------------------------------------------------------------------


class TestGeoJSONLibrary:
    """Validate GeoJSON output using the *geojson* Python library."""

    def test_valid_geojson_feature_collection(self, client):
        resp = client.get("/api/v1/positions.geojson")
        import geojson
        fc = geojson.loads(resp.get_data(as_text=True))
        assert fc.is_valid, fc.errors()

    def test_geojson_feature_properties(self, client):
        resp = client.get("/api/v1/positions.geojson")
        import geojson
        fc = geojson.loads(resp.get_data(as_text=True))
        for feature in fc["features"]:
            props = feature["properties"]
            assert "deviceId" in props
            assert "deviceName" in props
            assert "receivedAt" in props
            assert "speedKn" in props is not None


# ---------------------------------------------------------------------------
# API access control
# ---------------------------------------------------------------------------


class TestAPIAccessControl:
    def test_api_disabled_returns_404(self, tmp_path):
        """When api_enabled is False, API paths should return 404."""
        cfg = Config(data_dir=tmp_path, user="admin",
                     password_hash=generate_password_hash(PASSWORD))
        app = create_app(cfg)
        app.config["TESTING"] = True
        client = app.test_client()
        resp = client.get("/api/v1/current")
        assert resp.status_code == 404

    def test_api_enabled_does_not_require_basic_auth(self, client):
        """API endpoints should skip Basic Auth."""
        resp = client.get("/api/v1/current", headers={})  # no auth header
        assert resp.status_code == 200

    def test_api_subnet_restriction(self, tmp_path):
        """When subnets are set, a disallowed IP gets 403."""
        cfg = Config(data_dir=tmp_path, user="admin",
                     password_hash=generate_password_hash(PASSWORD))
        app = create_app(cfg)
        app.config["TESTING"] = True
        client = app.test_client()

        # Enable API with a restrictive subnet
        conn = connect(cfg.db_path)
        now = time.time()
        for key, value in (("api_enabled", True), ("api_allowed_subnets", "10.0.0.0/8")):
            conn.execute(
                "INSERT OR REPLACE INTO settings(key, value, updated_at) VALUES (?, ?, ?)",
                (key, json.dumps(value), now),
            )
        conn.close()

        # Remote address is 127.0.0.1 by default in test client → should be blocked
        resp = client.get("/api/v1/current")
        assert resp.status_code == 403

    def test_api_subnet_allowed(self, tmp_path):
        """When subnets include 127.0.0.0/8, loopback is allowed."""
        cfg = Config(data_dir=tmp_path, user="admin",
                     password_hash=generate_password_hash(PASSWORD))
        app = create_app(cfg)
        app.config["TESTING"] = True
        client = app.test_client()

        conn = connect(cfg.db_path)
        now = time.time()
        for key, value in (("api_enabled", True), ("api_allowed_subnets", "127.0.0.0/8")):
            conn.execute(
                "INSERT OR REPLACE INTO settings(key, value, updated_at) VALUES (?, ?, ?)",
                (key, json.dumps(value), now),
            )
        conn.close()

        resp = client.get("/api/v1/current")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Signal K content type
# ---------------------------------------------------------------------------


class TestSignalKContentType:
    def test_signalk_mimetype(self, client):
        resp = client.get("/signalk/v1/api/")
        assert resp.mimetype == "application/vnd.signalk+json"

    def test_api_v1_mimetype_json(self, client):
        resp = client.get("/api/v1/current")
        assert resp.mimetype == "application/json"