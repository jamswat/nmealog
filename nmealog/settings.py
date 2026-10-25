"""Runtime settings, stored in the database and edited from the web UI."""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .db import log_action


class ValidationError(ValueError):
    def __init__(self, errors: dict[str, str]):
        super().__init__("; ".join(errors.values()))
        self.errors = errors


@dataclass(frozen=True)
class Setting:
    key: str
    label: str
    kind: str  # "bool", "int" or "text"
    default: Any
    group: str
    help: str = ""
    minimum: int | None = None
    maximum: int | None = None
    unit: str = ""


SETTINGS: tuple[Setting, ...] = (
    Setting("logging_enabled", "Logging enabled", "bool", True, "Logging",
            "When off, devices stay connected and live values update, but no sentence or position history is written."),
    Setting("store_sentences", "Store every sentence", "bool", True, "Logging",
            "Keep each received sentence with its parsed fields. Turn off to keep only positions and live values."),
    Setting("store_invalid", "Store sentences with bad checksums", "bool", False, "Logging",
            "Bad checksums usually mean line noise or a wiring fault."),
    Setting("position_interval_s", "Position sample interval", "int", 60, "Logging",
            "Seconds between stored positions for each device.", 1, 86400, "s"),
    Setting("sentence_retention_days", "Keep sentences for", "int", 14, "Storage",
            "Older sentences are deleted. 0 = keep forever.", 0, 36500, "days"),
    Setting("position_retention_days", "Keep positions for", "int", 180, "Storage",
            "Older positions are deleted. 0 = keep forever.", 0, 36500, "days"),
    Setting("max_db_size_mb", "Maximum database size", "int", 100, "Storage",
            "When exceeded, the oldest sentences are deleted first, then the oldest positions. 0 = no limit.",
            0, 100000, "MB"),
    Setting("auto_maintenance", "Automatic pruning and size limit", "bool", True, "Maintenance",
            "Apply the retention and size settings on a schedule."),
    Setting("maintenance_interval_min", "Maintenance check interval", "int", 60, "Maintenance",
            "", 1, 10080, "min"),
    Setting("auto_backup", "Automatic backups", "bool", True, "Maintenance",
            "Take a consistent database backup on a schedule."),
    Setting("backup_interval_h", "Backup interval", "int", 24, "Maintenance",
            "", 1, 8760, "h"),
    Setting("backup_keep", "Backups to keep", "int", 14, "Maintenance",
            "Older backups beyond this count are deleted.", 1, 1000),
    Setting("connect_timeout_s", "Connect timeout", "int", 5, "Connection",
            "", 1, 60, "s"),
    Setting("idle_timeout_s", "Reconnect after silence of", "int", 60, "Connection",
            "Reconnect if a device sends nothing for this long.", 5, 3600, "s"),
    Setting("stale_after_s", "Show device as silent after", "int", 30, "Connection",
            "", 1, 3600, "s"),
    Setting("display_tz", "Display timezone", "text", "UTC", "Display",
            "IANA name, for example Europe/London. Data is always stored in UTC."),
    Setting("relay_enabled", "Advertise positions on the LAN", "bool", False, "LAN relay",
            "Serves the chosen device's sentences on a TCP port and advertises them over mDNS as "
            "_nmea-0183._tcp. Anyone on the local network can read them, including position. Off by default."),
    Setting("relay_device_id", "Source device", "device", 0, "LAN relay",
            "The device whose valid sentences are relayed. Choose none to disable.", 0, 2**31 - 1),
    Setting("relay_port", "TCP port", "int", 10110, "LAN relay",
            "The port clients connect to. 10110 is the conventional NMEA port.", 1, 65535),
    Setting("relay_advertise", "Advertise over mDNS", "bool", True, "LAN relay",
            "Lets Linux (GeoClue, Gnome Maps), Mac and other clients find the relay without typing an address."),
    Setting("relay_name", "Advertised name", "text", "nmealog", "LAN relay",
            "Shown to clients browsing for NMEA services."),
    # --- API ---
    Setting("api_enabled", "Enable JSON API", "bool", False, "API",
            "Allow read-only API access via /signalk/v1/ and /api/v1/ endpoints without Basic Auth."),
    Setting("api_allowed_subnets", "Allowed IP subnets", "text", "", "API",
            "Comma-separated CIDR subnets (e.g. 192.168.0.0/24,10.0.0.0/8). "
            "Connections from other IPs are rejected. Empty = allow any IP when API is enabled."),
)

BY_KEY = {s.key: s for s in SETTINGS}
_TRUE = {"1", "true", "on", "yes"}


def coerce(setting: Setting, raw: Any) -> Any:
    if setting.kind == "bool":
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() in _TRUE
    if setting.kind in ("int", "device"):
        if isinstance(raw, bool):
            raise ValueError(f"{setting.label} must be a whole number")
        try:
            value = int(str(raw).strip())
        except ValueError:
            raise ValueError(f"{setting.label} must be a whole number") from None
        if setting.minimum is not None and value < setting.minimum:
            raise ValueError(f"{setting.label} must be at least {setting.minimum}")
        if setting.maximum is not None and value > setting.maximum:
            raise ValueError(f"{setting.label} must be at most {setting.maximum}")
        return value
    value = str(raw).strip()
    if not value:
        raise ValueError(f"{setting.label} is required")
    if setting.key == "display_tz":
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError(f"Unknown timezone: {value}") from None
    return value


def defaults() -> dict[str, Any]:
    return {s.key: s.default for s in SETTINGS}


def load(conn: sqlite3.Connection) -> dict[str, Any]:
    values = defaults()
    for row in conn.execute("SELECT key, value FROM settings"):
        setting = BY_KEY.get(row[0])
        if setting is None:
            continue
        try:
            values[setting.key] = coerce(setting, json.loads(row[1]))
        except (ValueError, TypeError):
            pass
    return values


def save(conn: sqlite3.Connection, raw: dict[str, Any], actor: str = "web") -> list[str]:
    """Validate and store the settings in raw. Missing keys keep their current value."""
    current = load(conn)
    values: dict[str, Any] = {}
    errors: dict[str, str] = {}
    for setting in SETTINGS:
        if setting.key not in raw:
            values[setting.key] = current[setting.key]
            continue
        try:
            values[setting.key] = coerce(setting, raw[setting.key])
        except ValueError as exc:
            errors[setting.key] = str(exc)
    if errors:
        raise ValidationError(errors)

    changed = [k for k in values if values[k] != current[k]]
    if not changed:
        return []
    now = time.time()
    conn.execute("BEGIN IMMEDIATE")
    try:
        for key in changed:
            conn.execute(
                "INSERT INTO settings(key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                (key, json.dumps(values[key]), now),
            )
        detail = "; ".join(f"{k}: {current[k]} -> {values[k]}" for k in changed)
        log_action(conn, actor, "settings", detail)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return changed