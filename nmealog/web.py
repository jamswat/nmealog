"""Web management interface. Server-rendered, no JavaScript build step."""
from __future__ import annotations

import csv
import hmac
import io
import ipaddress
import json
import logging
import os
import re
import secrets
import shutil
import socket
import sqlite3
import time
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from email.utils import formatdate, parsedate_to_datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

log = logging.getLogger("nmealog.web")

# Simple in-memory rate limiter for failed auth attempts
_LOGIN_ATTEMPTS: dict[str, list[float]] = {}
_LOGIN_LOCKOUT_S = 30
_LOGIN_MAX_ATTEMPTS = 10

from flask import (
    Flask,
    Response,
    abort,
    flash,
    g,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

from . import maintenance
from . import settings as app_settings
from . import signalk as signalk_module
from .config import Config, load_config
from .db import connect, get_meta, init_db, log_action, set_meta
from .settings import ValidationError

RESTORE_MAX_BYTES = 512 * 1024 * 1024
EXPORT_LIMIT = 1_000_000
HOSTNAME = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$")
NAV = (
    ("status", "Status"),
    ("track", "Track"),
    ("map_page", "Map"),
    ("sentences", "Sentences"),
    ("devices", "Devices"),
    ("settings_page", "Settings"),
    ("maintenance_page", "Maintenance"),
    ("change_password", "Password"),
)


def _load_secret(cfg: Config) -> str:
    path = cfg.secret_key_path
    if path.exists():
        return path.read_text().strip()
    key = secrets.token_urlsafe(48)
    cfg.ensure_dirs()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(key)
    return key


def create_app(cfg: Config | None = None) -> Flask:
    cfg = cfg or load_config()
    cfg.ensure_dirs()
    if cfg.password_hash and not cfg.user:
        raise RuntimeError("NMEALOG_USER must be set when NMEALOG_PASSWORD_HASH is set.")

    if cfg.allow_no_auth:
        log.warning("INSECURE: NMEALOG_ALLOW_NO_AUTH is active — the web UI has no authentication.")

    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=_load_secret(cfg),
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=cfg.secure_cookies,
        MAX_CONTENT_LENGTH=RESTORE_MAX_BYTES,
    )
    if cfg.trust_proxy:
        # Only nginx should reach this port; it sets these headers.
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

    conn = connect(cfg.db_path)
    init_db(conn)
    conn.close()

    def get_db() -> sqlite3.Connection:
        if "db" not in g:
            g.db = connect(cfg.db_path)
        return g.db

    def settings() -> dict[str, Any]:
        if "settings" not in g:
            g.settings = app_settings.load(get_db())
        return g.settings

    @app.teardown_appcontext
    def _close_db(_exc):
        db = g.pop("db", None)
        if db is not None:
            db.close()

    # --- API helpers ------------------------------------------------------

    def _real_ip() -> str:
        """Return the real client IP.

        When *trust_proxy* is True, ProxyFix has already updated
        *request.remote_addr* from X-Forwarded-For.
        """
        return request.remote_addr or ""

    # --- API IP restriction (runs before auth, returns 403 when blocked) --

    @app.before_request
    def _api_ip_check():
        """Enforce IP-subnet access controls for API paths.

        When *api_enabled* is False, API paths return 404.
        When *api_allowed_subnets* is set, the client IP must match.
        """
        if not request.path.startswith(("/signalk/v1/", "/api/v1/")):
            return None

        s = settings()
        if not s.get("api_enabled", False):
            return Response(
                json.dumps({"error": "API disabled"}),
                404, mimetype="application/json",
            )

        allowed = s.get("api_allowed_subnets", "").strip()
        if not allowed:
            return None  # unrestricted

        client_ip = _real_ip()
        try:
            addr = ipaddress.IPv4Address(client_ip)
            subnets = [ipaddress.IPv4Network(sub.strip())
                       for sub in allowed.split(",") if sub.strip()]
            if any(addr in net for net in subnets):
                return None
        except (ValueError, ipaddress.AddressValueError):
            pass

        return Response(
            json.dumps({"error": "Access denied from this IP"}),
            403, mimetype="application/json",
        )

    # --- auth -------------------------------------------------------------

    @app.before_request
    def _authenticate():
        if request.endpoint in ("health", "static", "setup"):
            return None
        if request.path.startswith(("/signalk/v1/", "/api/v1/")):
            return None  # API uses IP-based access control
        if cfg.allow_no_auth:
            return None

        # Resolve credentials: env var overrides (backward compat) or DB-stored (web setup)
        user = cfg.user if cfg.password_hash else None
        pw_hash = cfg.password_hash
        if not pw_hash:
            db = get_db()
            pw_hash = get_meta(db, "auth_password_hash")
            user = get_meta(db, "auth_user") or user

        if user is None or pw_hash is None:
            return redirect(url_for("setup"))

        # Rate limiting
        client_ip = _real_ip()
        now = time.time()
        attempts = _LOGIN_ATTEMPTS.setdefault(client_ip, [])
        # Purge old attempts
        _LOGIN_ATTEMPTS[client_ip] = [t for t in attempts if now - t < _LOGIN_LOCKOUT_S]
        if len(_LOGIN_ATTEMPTS[client_ip]) >= _LOGIN_MAX_ATTEMPTS:
            log.warning("rate-limited login attempt from %s", client_ip)
            return Response("Too many failed attempts. Try again later.", 429,
                            {"Retry-After": str(_LOGIN_LOCKOUT_S)})

        auth = request.authorization
        ok = False
        if auth is not None and auth.username and auth.password:
            user_ok = hmac.compare_digest(auth.username.encode(), user.encode())
            pass_ok = check_password_hash(pw_hash, auth.password)
            ok = user_ok and pass_ok
        if not ok:
            _LOGIN_ATTEMPTS[client_ip].append(now)
            log.warning("failed login attempt from %s for user %s",
                        client_ip, auth.username if auth else "(no auth header)")
            return Response("Authentication required", 401,
                            {"WWW-Authenticate": 'Basic realm="nmealog", charset="UTF-8"'})
        # Successful login: clear attempts for this IP
        _LOGIN_ATTEMPTS.pop(client_ip, None)
        return None

    @app.before_request
    def _csrf():
        if request.method != "POST":
            return
        expected = session.get("csrf")
        sent = request.form.get("csrf", "")
        if not expected or not hmac.compare_digest(expected.encode(), sent.encode()):
            abort(400, "The form expired or is invalid. Reload the page and try again.")
        return

    @app.context_processor
    def _inject():
        token = session.setdefault("csrf", secrets.token_urlsafe(32))
        return {"csrf": token, "nav": NAV}

    # --- template helpers -------------------------------------------------

    def _tz() -> ZoneInfo:
        return ZoneInfo(settings()["display_tz"])

    @app.template_filter("ts")
    def fmt_ts(value):
        if value is None:
            return "-"
        return datetime.fromtimestamp(float(value), _tz()).strftime("%Y-%m-%d %H:%M:%S")

    @app.template_filter("ago")
    def fmt_ago(value):
        if value is None:
            return "never"
        delta = int(time.time() - float(value))
        if delta < 0:
            return "in the future"
        if delta < 60:
            return f"{delta} s ago"
        if delta < 3600:
            return f"{delta // 60} min ago"
        if delta < 86400:
            return f"{delta // 3600} h ago"
        return f"{delta // 86400} d ago"

    @app.template_filter("size")
    def fmt_size(value):
        value = float(value or 0)
        for unit in ("B", "KB", "MB", "GB"):
            if value < 1024 or unit == "GB":
                return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
            value /= 1024
        return f"{value:.1f} GB"

    @app.template_filter("fields")
    def fmt_fields(value):
        try:
            data = json.loads(value)
        except (TypeError, ValueError):
            return value
        return "; ".join(f"{k}={v}" for k, v in data.items())

    def _day_bounds(start: str, end: str) -> tuple[float | None, float | None]:
        tz = _tz()
        lo = hi = None
        try:
            if start:
                lo = datetime.strptime(start, "%Y-%m-%d").replace(tzinfo=tz).timestamp()
            if end:
                hi = (datetime.strptime(end, "%Y-%m-%d") + timedelta(days=1)).replace(tzinfo=tz).timestamp()
        except ValueError:
            abort(400, "Dates must be in YYYY-MM-DD format.")
        return lo, hi

    def _filters(args, table: str):
        clauses, params = [], []
        if args.get("device"):
            try:
                device_id = int(args["device"])
            except (ValueError, OverflowError):
                abort(400, "device must be a whole number")
            clauses.append(f"{table}.device_id = ?")
            params.append(device_id)
        if args.get("sentence") and table == "sentences":
            clauses.append("sentences.sentence = ?")
            params.append(args["sentence"].upper()[:5])
        lo, hi = _day_bounds(args.get("start", ""), args.get("end", ""))
        if lo is not None:
            clauses.append(f"{table}.received_at >= ?")
            params.append(lo)
        if hi is not None:
            clauses.append(f"{table}.received_at < ?")
            params.append(hi)
        # API time-frame parameters (Unix epoch timestamps)
        since = args.get("since")
        if since:
            try:
                clauses.append(f"{table}.received_at >= ?")
                params.append(float(since))
            except ValueError:
                abort(400, "since must be a Unix epoch timestamp")
        until = args.get("until")
        if until:
            try:
                clauses.append(f"{table}.received_at <= ?")
                params.append(float(until))
            except ValueError:
                abort(400, "until must be a Unix epoch timestamp")
        if args.get("contains") and table == "sentences":
            clauses.append("sentences.raw LIKE ?")
            params.append(f"%{args['contains'][:80]}%")
        where = " AND ".join(clauses) if clauses else "1=1"
        return where, params

    def _devices():
        return get_db().execute("SELECT * FROM devices ORDER BY name").fetchall()

    # --- status -----------------------------------------------------------

    @app.get("/")
    def status():
        db = get_db()
        s = settings()
        now = time.time()
        rows = db.execute(
            "SELECT d.id, d.name, d.host, d.port, d.enabled, s.state, s.last_seen, s.last_error, "
            "s.last_error_at, s.sentences_ok, s.sentences_bad, s.bytes_in, s.gps_time, s.clock_offset_s "
            "FROM devices d LEFT JOIN device_status s ON s.device_id = d.id ORDER BY d.name"
        ).fetchall()
        live: dict[int, dict[str, dict[str, tuple[str, float]]]] = defaultdict(lambda: defaultdict(dict))
        for r in db.execute("SELECT device_id, sentence, field, value, received_at FROM latest"):
            live[r[0]][r[1]][r[2]] = (r[3], r[4])
        devices = []
        for r in rows:
            item = dict(r)
            last_pos = db.execute(
                "SELECT received_at, latitude, longitude, source FROM positions "
                "WHERE device_id = ? ORDER BY received_at DESC LIMIT 1", (r["id"],)).fetchone()
            item["position"] = dict(last_pos) if last_pos else None
            item["fix"] = _fix_label(live[r["id"]])
            item["live"] = live[r["id"]]
            devices.append(item)

        heartbeat = None
        raw = get_meta(db, "collector_heartbeat")
        if raw:
            heartbeat = json.loads(raw)
        warnings = []
        if heartbeat is None or now - heartbeat["ts"] > 60:
            warnings.append("The collector is not running or has not reported in over 60 s.")
        elif heartbeat.get("storage_error"):
            warnings.append(f"The collector cannot write to the database: {heartbeat['storage_error']}")
        if s["relay_enabled"] and s["relay_device_id"] == 0:
            warnings.append("The LAN relay is enabled but has no source device, so it sends nothing.")
        if heartbeat and heartbeat.get("relay") and heartbeat["relay"].get("error"):
            warnings.append(f"LAN relay: {heartbeat['relay']['error']}")
        if not s["logging_enabled"]:
            warnings.append("Logging is paused. Live values update, but no history is being written.")
        for d in devices:
            if d["enabled"] and d["bytes_in"] and not d["sentences_ok"]:
                warnings.append(f"{d['name']} has sent {d['bytes_in']} bytes but no valid NMEA sentence. "
                                "It may be binary or at the wrong baud rate.")
            if d["enabled"] and d["state"] == "connected" and d["last_seen"] \
                    and now - d["last_seen"] > s["stale_after_s"]:
                warnings.append(f"{d['name']} is connected but has been silent for {int(now - d['last_seen'])} s.")
            if d["clock_offset_s"] is not None and abs(d["clock_offset_s"]) > 5 and d["gps_time"] \
                    and now - d["gps_time"] < 300:
                warnings.append(f"{d['name']}: the GPS clock differs from the server clock by "
                                f"{d['clock_offset_s']:+.1f} s. Check time sync.")
        disk = shutil.disk_usage(cfg.data_dir)
        if disk.free < 1024**3:
            warnings.append(f"Low disk space: {disk.free / 1024**3:.1f} GB free.")
        return render_template("status.html", title="Status", devices=devices,
                               warnings=warnings, heartbeat=heartbeat, s=s)

    def _fix_label(sentences: dict[str, dict[str, tuple[str, float]]]) -> str:
        gga = sentences.get("GGA", {})
        if "gps_qual" in gga:
            quality = gga["gps_qual"][0]
            return {"0": "no fix", "1": "GPS", "2": "DGPS"}.get(quality, f"quality {quality}")
        for name in ("RMC", "GLL"):
            status_value = sentences.get(name, {}).get("status")
            if status_value:
                return "valid" if status_value[0] == "A" else "no fix"
        return "-"

    # --- track and sentence browsing --------------------------------------

    @app.get("/track")
    def track():
        db = get_db()
        where, params = _filters(request.args, "positions")
        rows = db.execute(
            f"SELECT positions.*, devices.name AS device_name FROM positions "
            f"JOIN devices ON devices.id = positions.device_id WHERE {where} "
            f"ORDER BY positions.received_at DESC LIMIT 1000", params).fetchall()
        return render_template("track.html", title="Track", rows=rows, devices=_devices(),
                               args=request.args, s=settings(), export_query=request.query_string.decode())

    @app.get("/map")
    def map_page():
        s = settings()
        return render_template("map.html", title="Map", devices=_devices(), args=request.args,
                               s=s, export_query=request.query_string.decode())

    def _clamp_limit(raw: str | None, default: int, maximum: int) -> int:
        """Parse *raw* as a limit, clamp to [1, maximum], and return the value.
        On parse failure, abort with 400.
        """
        try:
            value = int(raw) if raw is not None else default
        except (ValueError, OverflowError):
            abort(400, "limit must be a whole number")
        if value < 1:
            value = 1
        elif value > maximum:
            value = maximum
        return value

    def _warn_on_conflicting_params(args: dict[str, str]) -> str | None:
        """Return a warning string when *since* and *until* are inverted, or None."""
        since = args.get("since")
        until = args.get("until")
        if since and until:
            try:
                if float(since) > float(until):
                    return "since is after until — the result set will be empty"
            except (ValueError, TypeError):
                pass
        return None

    @app.get("/api/positions")
    def api_positions():
        db = get_db()
        where, params = _filters(request.args, "positions")
        limit = _clamp_limit(request.args.get("limit"), 2000, 20000)
        rows = db.execute(
            f"SELECT positions.received_at, positions.latitude, positions.longitude, positions.speed_kn, "
            f"positions.course_true, positions.source, devices.id AS device_id, devices.name AS device_name "
            f"FROM positions JOIN devices ON devices.id = positions.device_id WHERE {where} "
            f"ORDER BY positions.received_at DESC LIMIT ?", (*params, limit)).fetchall()
        points = [{
            "t": r["received_at"], "lat": r["latitude"], "lon": r["longitude"],
            "speed": r["speed_kn"], "course": r["course_true"], "source": r["source"],
            "device": r["device_name"],
        } for r in reversed(rows)]
        return {"tz": settings()["display_tz"], "points": points}

    @app.get("/sentences")
    def sentences():
        db = get_db()
        where, params = _filters(request.args, "sentences")
        rows = db.execute(
            f"SELECT sentences.*, devices.name AS device_name FROM sentences "
            f"JOIN devices ON devices.id = sentences.device_id WHERE {where} "
            f"ORDER BY sentences.received_at DESC LIMIT 300", params).fetchall()
        types = db.execute("SELECT DISTINCT sentence FROM latest ORDER BY sentence").fetchall()
        return render_template("sentences.html", title="Sentences", rows=rows, devices=_devices(),
                               types=[t[0] for t in types], args=request.args, s=settings(),
                               export_query=request.query_string.decode())

    @app.get("/export")
    def export():
        table = request.args.get("table", "positions")
        if table not in ("positions", "sentences"):
            abort(400)
        where, params = _filters(request.args, table)
        if table == "positions":
            cols = "positions.received_at, devices.name, positions.latitude, positions.longitude, " \
                   "positions.source, positions.fix_quality, positions.speed_kn, positions.course_true, " \
                   "positions.satellites, positions.hdop"
            header = ["received_utc", "device", "latitude", "longitude", "source", "fix_quality",
                      "speed_kn", "course_true", "satellites", "hdop"]
        else:
            cols = "sentences.received_at, devices.name, sentences.talker, sentences.sentence, " \
                   "sentences.checksum_ok, sentences.fields, sentences.raw"
            header = ["received_utc", "device", "talker", "sentence", "checksum_ok", "fields", "raw"]
        sql = (f"SELECT {cols} FROM {table} JOIN devices ON devices.id = {table}.device_id "
               f"WHERE {where} ORDER BY {table}.received_at LIMIT {EXPORT_LIMIT}")
        cfg_path = cfg.db_path

        def generate():
            out = io.StringIO()
            writer = csv.writer(out)
            writer.writerow(header)
            yield out.getvalue()
            db = connect(cfg_path)
            try:
                for row in db.execute(sql, params):
                    out.seek(0)
                    out.truncate()
                    values = list(row)
                    values[0] = datetime.fromtimestamp(values[0], UTC).isoformat()
                    writer.writerow(values)
                    yield out.getvalue()
            finally:
                db.close()

        filename = f"nmealog-{table}-{datetime.now(UTC):%Y%m%d-%H%M%S}.csv"
        return Response(generate(), mimetype="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="{filename}"'})

    # --- API: Signal K ---------------------------------------------------

    def _json_response(data: Any, status: int = 200, headers: dict[str, str] | None = None) -> Response:
        return Response(
            json.dumps(data, default=str),
            status=status,
            mimetype="application/vnd.signalk+json" if request.path.startswith("/signalk/")
            else "application/json",
            headers=headers,
        )

    def _http_date(epoch: float) -> str:
        return formatdate(epoch, usegmt=True)

    def _not_modified(last_modified: float | None) -> Response | None:
        """Return a 304 Response when *If-Modified-Since* covers *last_modified*.

        HTTP dates only have one-second resolution, so both sides are floored
        to whole seconds before comparing.
        """
        if last_modified is None:
            return None
        ims = request.headers.get("If-Modified-Since")
        if not ims:
            return None
        try:
            since = parsedate_to_datetime(ims)
        except (TypeError, ValueError, OverflowError):
            return None
        if since.tzinfo is None:
            since = since.replace(tzinfo=UTC)
        if int(last_modified) <= int(since.timestamp()):
            return Response(status=304)
        return None

    def _cache_headers(last_modified: float | None) -> dict[str, str]:
        headers = {"Cache-Control": "no-cache"}  # clients must re-validate, but cheaply
        if last_modified is not None:
            headers["Last-Modified"] = _http_date(last_modified)
        return headers

    def _latest_epoch(db) -> float | None:
        """Newest received_at across live values and positions (both MAX lookups are indexed)."""
        values = [
            db.execute("SELECT MAX(received_at) FROM positions").fetchone()[0],
            db.execute("SELECT MAX(received_at) FROM latest").fetchone()[0],
        ]
        values = [v for v in values if v is not None]
        return max(values) if values else None

    def _iso_ts(epoch: float | None) -> str | None:
        if epoch is None:
            return None
        return datetime.fromtimestamp(epoch, UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")

    @app.route("/signalk/v1/api/")
    @app.route("/signalk/v1/api/<path:sk_path>")
    def signalk_api(sk_path: str | None = None):
        """Signal K REST API.

        Serves a Signal K 1.0.0 resource tree with position and heading data
        from all devices.  The *self* vessel is the device with the most
        recent position fix.
        """
        if not sk_path or sk_path.strip("/") == "":
            # API root
            return _json_response({
                "version": signalk_module.SIGNALK_VERSION,
                "server": {
                    "id": "nmealog",
                    "version": __import__("nmealog").__version__,
                },
                "endpoints": {
                    "v1": {
                        "version": signalk_module.SIGNALK_VERSION,
                        "signalk-http": request.url_root.rstrip("/") + "/signalk/v1/api/",
                    }
                },
            })

        parts = sk_path.strip("/").split("/")
        db = get_db()
        if parts[0] == "vessels":
            if len(parts) == 1:
                # Full vessel list
                last_modified = _latest_epoch(db)
                resp_304 = _not_modified(last_modified)
                if resp_304 is not None:
                    return resp_304
                tree = signalk_module.build_full_tree(db)
                return _json_response(tree, headers=_cache_headers(last_modified))

            vessel_id = parts[1]
            subpath = "/".join(parts[2:]) if len(parts) > 2 else ""

            # Resolve the requested vessel without building the whole tree.
            if vessel_id == "self":
                did = signalk_module.self_device(db)
            else:
                prefix = "urn:mrn:signalk:uuid:nmealog-"
                if not vessel_id.startswith(prefix):
                    return _json_response({"error": "Vessel not found"}, 404)
                try:
                    did = int(vessel_id[len(prefix):])
                except ValueError:
                    return _json_response({"error": "Vessel not found"}, 404)
            if did is None:
                return _json_response({"error": "Vessel not found"}, 404)

            last_modified = _latest_epoch(db)
            resp_304 = _not_modified(last_modified)
            if resp_304 is not None:
                return resp_304

            navigation = signalk_module.build_vessel_for_device(db, did) or {}
            if not navigation:
                return _json_response({"error": "Vessel not found"}, 404)
            target_vessel = {"navigation": navigation}
            headers = _cache_headers(last_modified)

            if not subpath:
                return _json_response(target_vessel, headers=headers)

            current = target_vessel
            for part in subpath.split("/"):
                if not isinstance(current, dict):
                    return _json_response({"error": "Path not found"}, 404)
                current = current.get(part)
                if current is None:
                    return _json_response({"error": "Path not found"}, 404)
            return _json_response(current, headers=headers)

        return _json_response({"error": "Not found"}, 404)

    # --- API: GeoJSON positions -------------------------------------------

    @app.route("/api/v1/positions")
    @app.route("/api/v1/positions.geojson")
    def api_v1_positions():
        """Return position history as JSON or GeoJSON FeatureCollection.

        Query parameters:
          device   – device id to filter
          since    – Unix epoch, inclusive lower bound
          until    – Unix epoch, inclusive upper bound
          format   – "geojson" to force GeoJSON (or use .geojson URL)
          limit    – max rows (default 20000, max 20000)
        """
        db = get_db()
        where, params = _filters(request.args, "positions")
        limit = _clamp_limit(request.args.get("limit"), 20000, 20000)

        last_modified = db.execute(
            f"SELECT MAX(positions.received_at) FROM positions WHERE {where}", params).fetchone()[0]
        resp_304 = _not_modified(last_modified)
        if resp_304 is not None:
            return resp_304
        headers = _cache_headers(last_modified)

        rows = db.execute(
            f"SELECT positions.*, devices.name AS device_name FROM positions "
            f"JOIN devices ON devices.id = positions.device_id WHERE {where} "
            f"ORDER BY positions.received_at DESC LIMIT ?", (*params, limit)
        ).fetchall()

        is_geojson = request.path.endswith(".geojson") or request.args.get("format") == "geojson"

        if is_geojson:
            features = [{
                "type": "Feature",
                "geometry": {
                    "type": "Point",
                    "coordinates": [r["longitude"], r["latitude"]],
                },
                "properties": {
                    "deviceId": r["device_id"],
                    "deviceName": r["device_name"],
                    "receivedAt": _iso_ts(r["received_at"]),
                    "source": r["source"],
                    "fixQuality": r["fix_quality"],
                    "speedKn": r["speed_kn"],
                    "courseTrue": r["course_true"],
                    "satellites": r["satellites"],
                    "hdop": r["hdop"],
                },
            } for r in rows]
            return Response(
                json.dumps({"type": "FeatureCollection", "features": features}, default=str),
                mimetype="application/geo+json",
                headers=headers,
            )

        # Default JSON format (same shape as the existing /api/positions)
        points = [{
            "t": r["received_at"], "lat": r["latitude"], "lon": r["longitude"],
            "speed": r["speed_kn"], "course": r["course_true"], "source": r["source"],
            "device": r["device_name"],
        } for r in rows]
        return _json_response({"tz": settings()["display_tz"], "points": points}, headers=headers)

    # --- API: Current state -----------------------------------------------

    _KNOWN_CURRENT_PARAMS = frozenset()  # no query parameters are accepted

    @app.get("/api/v1/current")
    def api_v1_current():
        """Return the latest position and live values per device.

        This is a simple, flat JSON endpoint suitable for scripts and
        external dashboards.  No time-range filtering — always returns the
        most recent data.  Query parameters are silently ignored.
        """
        db = get_db()
        now = time.time()

        last_modified = _latest_epoch(db)
        resp_304 = _not_modified(last_modified)
        if resp_304 is not None:
            return resp_304
        headers = _cache_headers(last_modified)

        devices_rows = db.execute(
            "SELECT d.id, d.name, d.host, d.port, d.enabled, "
            "s.state, s.last_seen, s.last_error, s.sentences_ok, s.sentences_bad, s.bytes_in "
            "FROM devices d LEFT JOIN device_status s ON s.device_id = d.id "
            "ORDER BY d.name"
        ).fetchall()

        live_raw = db.execute(
            "SELECT device_id, sentence, field, value, received_at FROM latest"
        ).fetchall()

        live_by_device: dict[int, dict[str, dict[str, Any]]] = {}
        for r in live_raw:
            live_by_device.setdefault(r["device_id"], {}).setdefault(r["sentence"], {})[r["field"]] = {
                "value": r["value"],
                "receivedAt": _iso_ts(r["received_at"]),
            }

        result_devices = []
        for r in devices_rows:
            did = r["id"]
            last_pos = db.execute(
                "SELECT received_at, latitude, longitude, source, fix_quality, "
                "speed_kn, course_true, satellites, hdop FROM positions "
                "WHERE device_id = ? ORDER BY received_at DESC LIMIT 1",
                (did,),
            ).fetchone()

            device_obj: dict[str, Any] = {
                "id": did,
                "name": r["name"],
                "host": r["host"],
                "port": r["port"],
                "enabled": bool(r["enabled"]),
                "state": r["state"] or "idle",
                "lastSeen": _iso_ts(r["last_seen"]),
                "lastError": r["last_error"],
                "sentencesOk": r["sentences_ok"],
                "sentencesBad": r["sentences_bad"],
                "bytesIn": r["bytes_in"],
            }

            if last_pos:
                device_obj["position"] = {
                    "latitude": last_pos["latitude"],
                    "longitude": last_pos["longitude"],
                    "source": last_pos["source"],
                    "fixQuality": last_pos["fix_quality"],
                    "speedKn": last_pos["speed_kn"],
                    "courseTrue": last_pos["course_true"],
                    "satellites": last_pos["satellites"],
                    "hdop": last_pos["hdop"],
                    "receivedAt": _iso_ts(last_pos["received_at"]),
                }

            device_obj["values"] = live_by_device.get(did, {})
            result_devices.append(device_obj)

        # Collector heartbeat
        heartbeat_raw = get_meta(db, "collector_heartbeat")
        heartbeat = json.loads(heartbeat_raw) if heartbeat_raw else None

        return _json_response({
            "server": {
                "version": __import__("nmealog").__version__,
                "time": _iso_ts(now),
                "collectorAlive": bool(heartbeat and now - heartbeat.get("ts", 0) < 60),
            },
            "devices": result_devices,
        }, headers=headers)

    # --- devices ----------------------------------------------------------

    def _validate_device(form) -> tuple[dict[str, Any], dict[str, str]]:
        errors: dict[str, str] = {}
        name = form.get("name", "").strip()
        host = form.get("host", "").strip()
        port_raw = form.get("port", "").strip()
        notes = form.get("notes", "").strip()[:500]
        if not name or len(name) > 50:
            errors["name"] = "Name is required (50 characters at most)."
        if not host:
            errors["host"] = "Host is required."
        else:
            try:
                ipaddress.ip_address(host)
            except ValueError:
                if not HOSTNAME.match(host) or len(host) > 253:
                    errors["host"] = "Enter an IPv4 address or a host name."
        try:
            port = int(port_raw)
            if not 1 <= port <= 65535:
                raise ValueError
        except ValueError:
            port = None
            errors["port"] = "Port must be between 1 and 65535."
        return {"name": name, "host": host, "port": port, "notes": notes,
                "enabled": 1 if form.get("enabled") == "on" else 0}, errors

    def _device_rows():
        return get_db().execute(
            "SELECT d.*, s.state, s.last_seen, s.last_error, s.sentences_ok, s.sentences_bad "
            "FROM devices d LEFT JOIN device_status s ON s.device_id = d.id ORDER BY d.name").fetchall()

    @app.get("/devices")
    def devices():
        return render_template("devices.html", title="Devices", devices=_device_rows(), device=None,
                               errors={}, values={"enabled": "on", "port": ""})

    @app.post("/devices")
    def devices_create():
        values, errors = _validate_device(request.form)
        if errors:
            flash("Fix the highlighted fields.", "error")
            return render_template("devices.html", title="Devices", devices=_device_rows(), device=None,
                                   errors=errors, values=request.form), 400
        db = get_db()
        now = time.time()
        try:
            db.execute("BEGIN IMMEDIATE")
            cur = db.execute(
                "INSERT INTO devices(name, host, port, enabled, notes, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (values["name"], values["host"], values["port"], values["enabled"], values["notes"], now, now))
            db.execute("INSERT INTO device_status(device_id, state, updated_at) VALUES (?, 'connecting', ?)",
                       (cur.lastrowid, now))
            log_action(db, "web", "device add", f"{values['name']} {values['host']}:{values['port']}")
            db.execute("COMMIT")
        except sqlite3.IntegrityError:
            db.execute("ROLLBACK")
            flash("A device with that name already exists.", "error")
            return render_template("devices.html", title="Devices", devices=_device_rows(), device=None,
                                   errors={"name": "Already in use."}, values=request.form), 400
        flash(f"Added {values['name']}.", "ok")
        return redirect(url_for("devices"))

    @app.get("/devices/<int:device_id>")
    def device_edit(device_id: int):
        db = get_db()
        device = db.execute("SELECT * FROM devices WHERE id = ?", (device_id,)).fetchone()
        if device is None:
            abort(404)
        counts = {
            "sentences": db.execute("SELECT COUNT(*) FROM sentences WHERE device_id = ?", (device_id,)).fetchone()[0],
            "positions": db.execute("SELECT COUNT(*) FROM positions WHERE device_id = ?", (device_id,)).fetchone()[0],
        }
        status_row = db.execute("SELECT * FROM device_status WHERE device_id = ?", (device_id,)).fetchone()
        return render_template("device_edit.html", title=device["name"], device=device,
                               counts=counts, status=status_row, errors={}, values=dict(device), s=settings())

    @app.post("/devices/<int:device_id>")
    def device_update(device_id: int):
        db = get_db()
        if db.execute("SELECT 1 FROM devices WHERE id = ?", (device_id,)).fetchone() is None:
            abort(404)
        values, errors = _validate_device(request.form)
        if errors:
            flash("Fix the highlighted fields.", "error")
            return render_template("device_edit.html", title="Device", device={"id": device_id},
                                   counts={}, status=None, errors=errors, values=request.form, s=settings()), 400
        try:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE devices SET name = ?, host = ?, port = ?, enabled = ?, notes = ?, updated_at = ? "
                       "WHERE id = ?",
                       (values["name"], values["host"], values["port"], values["enabled"], values["notes"],
                        time.time(), device_id))
            log_action(db, "web", "device update", f"{values['name']} {values['host']}:{values['port']} "
                                                  f"enabled={values['enabled']}")
            db.execute("COMMIT")
        except sqlite3.IntegrityError:
            db.execute("ROLLBACK")
            flash("A device with that name already exists.", "error")
            return redirect(url_for("device_edit", device_id=device_id))
        flash("Device saved. The collector picks up changes within a few seconds.", "ok")
        return redirect(url_for("device_edit", device_id=device_id))

    @app.post("/devices/<int:device_id>/delete")
    def device_delete(device_id: int):
        db = get_db()
        row = db.execute("SELECT name FROM devices WHERE id = ?", (device_id,)).fetchone()
        if row is None:
            abort(404)
        maintenance.clear_device(db, device_id)
        db.execute("DELETE FROM devices WHERE id = ?", (device_id,))
        log_action(db, "web", "device delete", row["name"])
        flash(f"Deleted {row['name']} and its data.", "ok")
        return redirect(url_for("devices"))

    @app.post("/devices/<int:device_id>/clear")
    def device_clear(device_id: int):
        db = get_db()
        row = db.execute("SELECT name FROM devices WHERE id = ?", (device_id,)).fetchone()
        if row is None:
            abort(404)
        removed = maintenance.clear_device(db, device_id)
        detail = f"{row['name']}: " + ", ".join(f"{k} {v}" for k, v in removed.items())
        log_action(db, "web", "device clear", detail)
        flash(f"Cleared data for {row['name']} ({detail}).", "ok")
        return redirect(url_for("device_edit", device_id=device_id))

    @app.post("/devices/<int:device_id>/test")
    def device_test(device_id: int):
        db = get_db()
        row = db.execute("SELECT id, name, host, port FROM devices WHERE id = ?", (device_id,)).fetchone()
        if row is None:
            abort(404)
        lines, error = _sample_lines(row["host"], row["port"], settings()["connect_timeout_s"])
        return render_template("device_test.html", title=f"Test {row['name']}", device=row,
                               lines=lines, error=error)

    def _sample_lines(host: str, port: int, timeout: float, seconds: float = 4.0):
        lines: list[str] = []
        error = None
        try:
            with socket.create_connection((host, port), timeout=timeout) as sock:
                sock.settimeout(1.0)
                buf = b""
                end = time.monotonic() + seconds
                while time.monotonic() < end and len(lines) < 40:
                    try:
                        chunk = sock.recv(4096)
                    except TimeoutError:
                        continue
                    if not chunk:
                        error = "The device closed the connection."
                        break
                    buf += chunk
                    while b"\n" in buf and len(lines) < 40:
                        line, buf = buf.split(b"\n", 1)
                        lines.append(line.decode("ascii", "replace").strip())
        except OSError as exc:
            error = f"Could not connect: {exc}"
        if not lines and error is None:
            error = "Connected, but no data arrived in 4 seconds."
        return lines, error

    # --- settings ---------------------------------------------------------

    @app.get("/settings")
    def settings_page():
        return render_template("settings.html", title="Settings", groups=_groups(settings()),
                               errors={}, s=settings(), devices=_devices())

    def _groups(values: dict[str, Any]):
        groups: dict[str, list] = {}
        for setting in app_settings.SETTINGS:
            groups.setdefault(setting.group, []).append((setting, values[setting.key]))
        return groups

    @app.post("/settings")
    def settings_save():
        form = request.form.to_dict()
        raw = {}
        for setting in app_settings.SETTINGS:
            if setting.kind == "bool":
                raw[setting.key] = "on" if setting.key in request.form else "off"
            elif setting.key in form:
                raw[setting.key] = form[setting.key]
        try:
            changed = app_settings.save(get_db(), raw, actor="web")
        except ValidationError as exc:
            flash("Some settings were not saved.", "error")
            current = {**settings(), **{k: request.form.get(k) for k in exc.errors}}
            return render_template("settings.html", title="Settings", groups=_groups(current),
                                   errors=exc.errors, s=settings(), devices=_devices()), 400
        flash("Settings saved." if changed else "No changes.", "ok")
        return redirect(url_for("settings_page"))

    # --- maintenance ------------------------------------------------------

    @app.get("/maintenance")
    def maintenance_page():
        db = get_db()
        stats = maintenance.db_stats(db, cfg.db_path, cfg.data_dir)
        log_rows = db.execute("SELECT * FROM maintenance_log ORDER BY id DESC LIMIT 100").fetchall()
        growth = maintenance.growth_estimate(db, cfg.db_path, settings(), time.time())
        perf = None
        raw = get_meta(db, "collector_heartbeat")
        if raw:
            heartbeat = json.loads(raw)
            if heartbeat.get("perf") and heartbeat.get("ts") is not None:
                perf = dict(heartbeat["perf"])
                perf["ts"] = heartbeat["ts"]
                perf["pid"] = heartbeat.get("pid")
                perf["workers"] = heartbeat.get("workers", [])
                perf["fresh"] = time.time() - heartbeat["ts"] < 90
        return render_template("maintenance.html", title="Maintenance", stats=stats, growth=growth,
                               backups=maintenance.list_backups(cfg.backup_dir), log_rows=log_rows,
                               devices=_devices(), s=settings(), perf=perf)

    def _run(label: str, action):
        db = get_db()
        try:
            result = action(db)
        except sqlite3.OperationalError as exc:
            flash(f"{label} failed: {exc}. The database may be busy; try again.", "error")
            return redirect(url_for("maintenance_page"))
        log_action(db, "web", label.lower(), "" if result is None else str(result))
        flash(f"{label}: {result}" if result is not None else f"{label} done.", "ok")
        return redirect(url_for("maintenance_page"))

    def _fmt(removed: dict[str, int]) -> str:
        return ", ".join(f"{k} {v} removed" for k, v in removed.items() if v)

    def _do_checkpoint(db):
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
        return "WAL truncated"

    def _do_vacuum(db):
        db.execute("VACUUM")
        return "database rebuilt"

    def _do_optimize(db):
        db.execute("ANALYZE")
        db.execute("PRAGMA optimize").fetchall()
        return "statistics updated"

    def _do_integrity(db):
        problems = maintenance.integrity_check(db)
        return "ok" if problems == ["ok"] else "problems found: " + "; ".join(problems[:10])

    def _do_prune(db):
        return _fmt(maintenance.prune_by_age(db, settings(), time.time())) or "nothing to remove"

    def _do_size(db):
        removed = maintenance.enforce_size_limit(db, cfg.db_path, settings()["max_db_size_mb"])
        return _fmt(removed) or "already under the limit"

    def _do_backup(db):
        return maintenance.create_backup(db, cfg.backup_dir, settings()["backup_keep"]).name

    maintenance_actions = {
        "checkpoint": ("Checkpoint", _do_checkpoint),
        "reclaim": ("Reclaim space", lambda db: (maintenance.reclaim_space(db), "free pages returned to disk")[1]),
        "vacuum": ("Vacuum", _do_vacuum),
        "optimize": ("Optimize", _do_optimize),
        "integrity": ("Integrity check", _do_integrity),
        "prune": ("Prune by age", _do_prune),
        "enforce-size": ("Enforce size limit", _do_size),
        "backup": ("Backup", _do_backup),
    }

    @app.post("/maintenance/<action>")
    def maintenance_action(action: str):
        if action not in maintenance_actions:
            abort(404)
        label, fn = maintenance_actions[action]
        return _run(label, fn)

    @app.post("/maintenance/delete-data")
    def maintenance_delete_data():
        db = get_db()
        table = request.form.get("table", "")
        if table not in ("sentences", "positions"):
            abort(400)
        if request.form.get("confirm", "") != "DELETE":
            flash("Type DELETE in the confirmation box to continue.", "error")
            return redirect(url_for("maintenance_page"))
        try:
            before = datetime.strptime(request.form.get("before", ""), "%Y-%m-%d") \
                .replace(tzinfo=_tz()).timestamp()
        except ValueError:
            flash("Enter a valid cut-off date.", "error")
            return redirect(url_for("maintenance_page"))
        device = request.form.get("device") or None
        n = maintenance.delete_data(db, table, before, int(device) if device else None)
        log_action(db, "web", "delete data", f"{table} before {request.form['before']} "
                                             f"device={device or 'all'}: {n} rows")
        flash(f"Deleted {n} {table} rows.", "ok")
        return redirect(url_for("maintenance_page"))

    @app.get("/maintenance/backups/<name>")
    def backup_download(name: str):
        try:
            path = maintenance.backup_path(cfg.backup_dir, name)
        except FileNotFoundError:
            abort(404)
        return Response(path.open("rb"), mimetype="application/octet-stream",
                        headers={"Content-Disposition": f'attachment; filename="{name}"',
                                 "Content-Length": str(path.stat().st_size)})

    @app.post("/maintenance/backups/<name>/delete")
    def backup_delete(name: str):
        try:
            maintenance.delete_backup(cfg.backup_dir, name)
        except FileNotFoundError:
            abort(404)
        log_action(get_db(), "web", "backup delete", name)
        flash(f"Deleted {name}.", "ok")
        return redirect(url_for("maintenance_page"))

    @app.post("/maintenance/backups/<name>/restore")
    def backup_restore(name: str):
        try:
            path = maintenance.backup_path(cfg.backup_dir, name)
        except FileNotFoundError:
            abort(404)
        return _restore_from(path)

    @app.post("/maintenance/restore")
    def restore_upload():
        upload = request.files.get("file")
        if upload is None or not upload.filename:
            flash("Choose a backup file to upload.", "error")
            return redirect(url_for("maintenance_page"))
        tmp = cfg.backup_dir / f"upload-{secrets.token_hex(8)}.tmp"
        upload.save(tmp)
        try:
            return _restore_from(tmp)
        finally:
            tmp.unlink(missing_ok=True)

    def _restore_from(path: Path):
        if request.form.get("confirm", "") != "RESTORE":
            flash("Type RESTORE in the confirmation box to replace the current database.", "error")
            return redirect(url_for("maintenance_page"))
        db = get_db()
        try:
            maintenance.validate_database(path)
        except ValueError as exc:
            flash(f"Restore refused: {exc}", "error")
            return redirect(url_for("maintenance_page"))
        safety = maintenance.create_backup(db, cfg.backup_dir, settings()["backup_keep"])
        maintenance.restore_database(db, path)
        log_action(db, "web", "restore", f"from {request.form.get('source', 'upload')}; "
                                         f"previous state saved as {safety.name}")
        flash(f"Database restored. The previous state was saved as {safety.name}.", "ok")
        return redirect(url_for("maintenance_page"))

    # --- API: Metrics -----------------------------------------------------

    @app.get("/api/v1/metrics")
    def api_v1_metrics():
        """Return collector performance metrics and database health info."""
        db = get_db()
        now = time.time()
        raw = get_meta(db, "collector_heartbeat")
        heartbeat = json.loads(raw) if raw else None
        stats = maintenance.db_stats(db, cfg.db_path, cfg.data_dir)
        s = settings()
        growth = maintenance.growth_estimate(db, cfg.db_path, s, now)

        result: dict[str, Any] = {
            "server": {
                "time": _iso_ts(now),
                "version": __import__("nmealog").__version__,
            },
            "collector": {
                "alive": bool(heartbeat and now - heartbeat.get("ts", 0) < 60),
            },
            "database": {
                "fileBytes": stats["file_bytes"],
                "walBytes": stats["wal_bytes"],
                "pageCount": stats["page_count"],
                "freeBytes": stats["free_bytes"],
                "diskFreeBytes": stats["disk_free"],
                "counts": stats["counts"],
            },
            "settings": {
                "loggingEnabled": s["logging_enabled"],
                "storeSentences": s["store_sentences"],
                "sentenceRetentionDays": s["sentence_retention_days"],
                "positionRetentionDays": s["position_retention_days"],
                "maxDbSizeMb": s["max_db_size_mb"],
                "apiEnabled": s["api_enabled"],
            },
            "growth": growth,
        }

        if heartbeat:
            result["collector"]["heartbeat"] = {
                "ts": heartbeat["ts"],
                "ageS": now - heartbeat["ts"],
            }
            if heartbeat.get("perf"):
                result["collector"]["performance"] = heartbeat["perf"]
            if heartbeat.get("relay"):
                result["collector"]["relay"] = heartbeat["relay"]
            if heartbeat.get("storage_error"):
                result["collector"]["storageError"] = heartbeat["storage_error"]

        return _json_response(result)

    # --- health -----------------------------------------------------------

    @app.get("/health")
    def health():
        try:
            conn = connect(cfg.db_path)
            conn.execute("SELECT 1").fetchone()
            conn.close()
        except sqlite3.Error as exc:
            return {"ok": False, "error": str(exc)}, 503
        return {"ok": True}

    # --- setup (first-run credential creation) ---------------------------

    # --- change password --------------------------------------------------

    @app.route("/password", methods=["GET", "POST"])
    def change_password():
        db = get_db()
        if request.method == "GET":
            return render_template("password.html", title="Change password", errors={}, values={})

        # POST
        current_pw = request.form.get("current_password", "")
        new_pw = request.form.get("new_password", "")
        confirm = request.form.get("confirm", "")
        errors: dict[str, str] = {}

        # Verify current password
        pw_hash = cfg.password_hash or get_meta(db, "auth_password_hash")
        if not pw_hash or not check_password_hash(pw_hash, current_pw):
            errors["current_password"] = "Current password is incorrect."
        if len(new_pw) < 8:
            errors["new_password"] = "Password must be at least 8 characters."
        if new_pw != confirm:
            errors["confirm"] = "Passwords do not match."
        if errors:
            return render_template("password.html", title="Change password",
                                   errors=errors, values={}), 400

        new_hash = generate_password_hash(new_pw)
        set_meta(db, "auth_password_hash", new_hash)
        log_action(db, "web", "password change", "")
        flash("Password changed.", "ok")
        return redirect(url_for("status"))

    @app.route("/setup", methods=["GET", "POST"])
    def setup():
        db = get_db()
        if cfg.password_hash or get_meta(db, "auth_password_hash"):
            return redirect(url_for("status"))

        if request.method == "GET":
            return render_template("setup.html", title="Setup", errors={}, values={})

        # POST
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        confirm = request.form.get("confirm", "")
        errors: dict[str, str] = {}
        if not username or len(username) > 50:
            errors["username"] = "Username is required (50 characters at most)."
        if len(password) < 8:
            errors["password"] = "Password must be at least 8 characters."
        if password != confirm:
            errors["confirm"] = "Passwords do not match."
        if errors:
            return render_template("setup.html", title="Setup", errors=errors,
                                   values={"username": username}), 400

        pw_hash = generate_password_hash(password)
        set_meta(db, "auth_user", username)
        set_meta(db, "auth_password_hash", pw_hash)
        flash("Account created. Sign in with your new credentials.", "ok")
        return redirect(url_for("status"))

    @app.errorhandler(400)
    def _bad_request(err):
        return render_template("error.html", title="Error", message=err.description), 400

    @app.errorhandler(404)
    def _not_found(err):
        return render_template("error.html", title="Not found", message="Not found."), 404

    return app
