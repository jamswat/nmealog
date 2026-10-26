"""Signal K data model transformation for nmealog.

Maps NMEA sentences to the Signal K resource tree as defined in the
Signal K specification v1.0.0 (https://signalk.org).

Only position and heading paths are populated; nmealog does not collect
environment, propulsion or other marine data.
"""
from __future__ import annotations

import math
import sqlite3
from datetime import UTC, datetime
from typing import Any

__all__ = [
    "SELF_URN",
    "SIGNALK_VERSION",
    "build_vessel",
    "build_vessel_for_device",
    "fetch_latest",
    "field_to_signalk",
    "iso_time",
    "self_device",
]

SIGNALK_VERSION = "1.0.0"
SELF_URN = "urn:mrn:signalk:uuid:nmealog"

# ---------------------------------------------------------------------------
# NMEA → Signal K field mapping
# ---------------------------------------------------------------------------
# Each entry: (sentence, field) → dict with:
#   path       – Signal K dotted path (without "navigation." prefix)
#   attr       – which part of the path gets the value ("value" or "value.*")
#   pgn        – NMEA 2000 PGN number (0 when unknown)
#   description
#   converter  – callable to transform the raw string
#   units      – SI unit string (Signal K standard)

SignalKMap = dict[str, Any]

_FIELD_MAP: dict[tuple[str, str], SignalKMap] = {
    # --- Position (GGA) ---
    ("GGA", "lat"): {
        "ctx": "position",
        "pgn": 129025,
        "description": "Position",
        "units": "deg",
        "converter": float,
    },
    ("GGA", "lon"): {
        "ctx": "position",
        "pgn": 129025,
        "description": "Position",
        "units": "deg",
        "converter": float,
    },
    ("GGA", "altitude"): {
        "ctx": "position",
        "pgn": 129025,
        "description": "Position",
        "units": "m",
        "converter": float,
    },
    ("GGA", "gps_qual"): {
        "ctx": "gnss.methodQuality",
        "pgn": 129029,
        "description": "GNSS Method Quality",
        "units": "",
        "converter": int,
    },
    ("GGA", "num_sats"): {
        "ctx": "gnss.satellites",
        "pgn": 129029,
        "description": "GNSS Satellites",
        "units": "",
        "converter": int,
    },
    ("GGA", "horizontal_dil"): {
        "ctx": "gnss.horizontalDilution",
        "pgn": 129029,
        "description": "GNSS Horizontal Dilution",
        "units": "",
        "converter": float,
    },
    # --- Position (RMC) ---
    ("RMC", "lat"): {
        "ctx": "position",
        "pgn": 129025,
        "description": "Position",
        "units": "deg",
        "converter": float,
    },
    ("RMC", "lon"): {
        "ctx": "position",
        "pgn": 129025,
        "description": "Position",
        "units": "deg",
        "converter": float,
    },
    ("RMC", "spd_over_grnd"): {
        "ctx": "speedOverGround",
        "pgn": 129026,
        "description": "Speed Over Ground",
        "units": "m/s",
        "converter": lambda v: float(v) * 0.514444,  # kn → m/s
    },
    ("RMC", "true_course"): {
        "ctx": "courseOverGroundTrue",
        "pgn": 129026,
        "description": "Course Over Ground (True)",
        "units": "deg",
        "converter": float,
    },
    # --- Position (GLL) ---
    ("GLL", "lat"): {
        "ctx": "position",
        "pgn": 129025,
        "description": "Position",
        "units": "deg",
        "converter": float,
    },
    ("GLL", "lon"): {
        "ctx": "position",
        "pgn": 129025,
        "description": "Position",
        "units": "deg",
        "converter": float,
    },
    # --- Speed / Course (VTG) ---
    ("VTG", "true_track"): {
        "ctx": "courseOverGroundTrue",
        "pgn": 129026,
        "description": "Course Over Ground (True)",
        "units": "deg",
        "converter": float,
    },
    ("VTG", "spd_over_grnd_kts"): {
        "ctx": "speedOverGround",
        "pgn": 129026,
        "description": "Speed Over Ground",
        "units": "m/s",
        "converter": lambda v: float(v) * 0.514444,
    },
    # --- Heading (HDT) ---
    ("HDT", "heading"): {
        "ctx": "headingTrue",
        "pgn": 127250,
        "description": "Heading (True)",
        "units": "deg",
        "converter": float,
    },
    # --- Heading (HDG) ---
    ("HDG", "heading"): {
        "ctx": "headingMagnetic",
        "pgn": 127250,
        "description": "Heading (Magnetic)",
        "units": "deg",
        "converter": float,
    },
    ("HDG", "deviation"): {
        "ctx": "deviation",
        "pgn": 127250,
        "description": "Magnetic Deviation",
        "units": "deg",
        "converter": float,
    },
    ("HDG", "variation"): {
        "ctx": "variation",
        "pgn": 127250,
        "description": "Magnetic Variation",
        "units": "deg",
        "converter": float,
    },
    # --- Rate of Turn (ROT) ---
    ("ROT", "rate_of_turn"): {
        "ctx": "rateOfTurn",
        "pgn": 127251,
        "description": "Rate of Turn",
        "units": "rad/s",
        "converter": lambda v: float(v) * math.pi / 180 / 60,  # deg/min → rad/s
    },
}


def field_to_signalk(sentence: str, field: str) -> SignalKMap | None:
    """Return the Signal K mapping for an NMEA (sentence, field) pair, or None."""
    return _FIELD_MAP.get((sentence, field))


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def iso_time(epoch: float) -> str:
    """Format a Unix epoch timestamp as ISO 8601 in UTC."""
    return datetime.fromtimestamp(epoch, UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _build_position_node(
    latitude: float,
    longitude: float,
    source_name: str,
    received_at: float,
) -> dict[str, Any]:
    return {
        "value": {"latitude": latitude, "longitude": longitude},
        "$source": f"nmealog.{source_name}",
        "timestamp": iso_time(received_at),
        "pgn": 129025,
        "description": "Position",
    }


def _set_path(root: dict[str, Any], dotted: str, value: Any) -> None:
    """Set a value in a nested dict from a dotted path like 'gnss.satellites'."""
    parts = dotted.split(".")
    target = root
    for part in parts[:-1]:
        target = target.setdefault(part, {})
    target[parts[-1]] = value


def _get_path(root: dict[str, Any], dotted: str) -> Any:
    """Walk a dotted path in a nested dict; return None if any key is missing."""
    parts = dotted.split(".")
    target = root
    for part in parts:
        if not isinstance(target, dict):
            return None
        target = target.get(part)
        if target is None:
            return None
    return target


# ---------------------------------------------------------------------------
# fetch helpers
# ---------------------------------------------------------------------------

def fetch_latest(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Return all rows from the *latest* table, ordered by device."""
    return [
        dict(r) for r in conn.execute(
            "SELECT device_id, sentence, field, value, received_at FROM latest ORDER BY device_id"
        ).fetchall()
    ]


def fetch_positions(conn: sqlite3.Connection, device_id: int | None = None,
                    since: float | None = None, until: float | None = None,
                    limit: int = 0) -> list[dict[str, Any]]:
    """Return position rows, optionally filtered."""
    clauses: list[str] = []
    params: list[Any] = []
    if device_id is not None:
        clauses.append("device_id = ?")
        params.append(device_id)
    if since is not None:
        clauses.append("received_at >= ?")
        params.append(since)
    if until is not None:
        clauses.append("received_at <= ?")
        params.append(until)
    where = " AND ".join(clauses) if clauses else "1=1"
    sql = "SELECT * FROM positions WHERE " + where + " ORDER BY received_at"
    if limit > 0:
        sql += " LIMIT ?"
        params.append(limit)
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def fetch_devices(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Return all enabled device records."""
    return [dict(r) for r in conn.execute(
        "SELECT id, name FROM devices WHERE enabled = 1 ORDER BY name"
    ).fetchall()]


# ---------------------------------------------------------------------------
# tree builders
# ---------------------------------------------------------------------------

def build_vessel(
    device_id: int,
    device_name: str,
    latest: list[dict[str, Any]],
    position: dict[str, Any] | None,
) -> dict[str, Any]:
    """Build a Signal K vessel resource tree for one device.

    Parameters
    ----------
    device_id
        nmealog device id.
    device_name
        Display name of the device.
    latest
        Rows from the *latest* table for this device.
    position
        A single dict from the *positions* table, or *None*.

    Returns
    -------
    A Signal K ``navigation`` subtree.
    """
    navigation: dict[str, Any] = {}
    source_label = f"nmealog.{device_name}"

    # --- position from the latest fix in the positions table ---
    if position is not None:
        navigation["position"] = _build_position_node(
            position["latitude"], position["longitude"],
            device_name, position["received_at"],
        )

    # --- map each live value ---
    # We collect fields by context to group them
    ctx_values: dict[str, dict[str, Any]] = {}  # ctx → {"field": value, "ts": epoch}
    ctx_timestamps: dict[str, float] = {}

    for row in latest:
        if row["device_id"] != device_id:
            continue
        mapping = _FIELD_MAP.get((row["sentence"], row["field"]))
        if mapping is None:
            continue
        ctx = mapping["ctx"]
        try:
            converted = mapping["converter"](row["value"])
        except (ValueError, TypeError):
            continue
        ts = row["received_at"]
        if ctx == "position":
            # position fields go into the value sub-object
            if ctx not in ctx_values:
                ctx_values[ctx] = {}
                ctx_timestamps[ctx] = 0
            ctx_values[ctx][row["field"]] = converted
            ctx_timestamps[ctx] = max(ctx_timestamps[ctx], ts)
        else:
            if ctx not in ctx_values:
                ctx_values[ctx] = {}
                ctx_timestamps[ctx] = 0
            ctx_values[ctx]["value"] = converted
            ctx_timestamps[ctx] = max(ctx_timestamps[ctx], ts)

    # Write the collected values into the navigation tree
    for ctx, data in ctx_values.items():
        parts = ctx.split(".")
        current = navigation
        for part in parts:
            current = current.setdefault(part, {})
        if ctx == "position" and "value" not in current:
            # Build position.value from the collected fields.
            # Map NMEA field names (lat, lon) to Signal K names (latitude, longitude).
            pos_value = {}
            for nmea_fld, sk_fld in (("lat", "latitude"), ("lon", "longitude"), ("altitude", "altitude")):
                if nmea_fld in data:
                    pos_value[sk_fld] = data[nmea_fld]
            if pos_value:
                current["value"] = pos_value
        elif ctx != "position":
            current["value"] = data["value"]

        # Add metadata if this container is fresh
        mapping_for_ctx = _FIELD_MAP.get(
            _reverse_lookup(ctx),
            {"pgn": 0, "description": "", "units": ""},
        )

        # Only set metadata on the context node if we haven't already
        if "$source" not in current:
            current["$source"] = source_label
            current["timestamp"] = iso_time(ctx_timestamps[ctx])
            current["pgn"] = mapping_for_ctx["pgn"]
            current["description"] = mapping_for_ctx["description"]
            current["units"] = mapping_for_ctx["units"]

    return navigation


def _reverse_lookup(ctx: str) -> tuple[str, str]:
    """Find any (sentence, field) that maps to *ctx*."""
    for (sen, fld), m in _FIELD_MAP.items():
        if m["ctx"] == ctx:
            return (sen, fld)
    return ("", "")


def build_vessel_for_device(conn: sqlite3.Connection, device_id: int) -> dict[str, Any] | None:
    """Build the Signal K navigation tree for a single enabled device.

    This avoids building the full tree for every vessel when only one is
    requested.  Returns *None* when the device does not exist or is disabled.
    """
    dev = conn.execute(
        "SELECT id, name FROM devices WHERE id = ? AND enabled = 1", (device_id,)
    ).fetchone()
    if dev is None:
        return None
    latest = [dict(r) for r in conn.execute(
        "SELECT device_id, sentence, field, value, received_at FROM latest "
        "WHERE device_id = ? ORDER BY device_id", (device_id,)).fetchall()]
    pos = conn.execute(
        "SELECT * FROM positions WHERE device_id = ? ORDER BY received_at DESC LIMIT 1",
        (device_id,),
    ).fetchone()
    return build_vessel(device_id, dev["name"], latest, dict(pos) if pos is not None else None)


def self_device(conn: sqlite3.Connection) -> int | None:
    """Return the device id that Signal K treats as *self*.

    The vessel with the most recent position fix; failing that, the first
    device that has reported any live values.
    """
    row = conn.execute("SELECT device_id FROM positions ORDER BY received_at DESC LIMIT 1").fetchone()
    if row is not None:
        return row[0]
    row = conn.execute("SELECT device_id FROM latest LIMIT 1").fetchone()
    return row[0] if row is not None else None


def build_full_tree(conn: sqlite3.Connection) -> dict[str, Any]:
    """Build the full Signal K response with all vessels.

    Returns a dict suitable for JSON serialization:
    ``{"version": ..., "self": ..., "vessels": {..., "self": {...}}}``
    """
    devices = fetch_devices(conn)
    all_latest = fetch_latest(conn)
    vessels: dict[str, dict[str, Any]] = {}
    self_vessel_id: str | None = None

    for dev in devices:
        did = dev["id"]
        name = dev["name"]
        dev_latest = [r for r in all_latest if r["device_id"] == did]
        # Get the most recent position for this device
        pos = conn.execute(
            "SELECT * FROM positions WHERE device_id = ? ORDER BY received_at DESC LIMIT 1",
            (did,),
        ).fetchone()
        pos_dict = dict(pos) if pos else None

        vessel_id = f"urn:mrn:signalk:uuid:nmealog-{did}"
        nav = build_vessel(did, name, dev_latest, pos_dict)
        if nav:
            vessels[vessel_id] = {"navigation": nav}
            if self_vessel_id is None:
                self_vessel_id = vessel_id  # first device with data is "self"

    # Always include the self alias, pointing to the first vessel
    if self_vessel_id is None and devices:
        # No data yet, but we have devices
        pass

    # Build the response
    result: dict[str, Any] = {
        "version": SIGNALK_VERSION,
        "self": self_vessel_id or SELF_URN,
    }

    if vessels:
        result["vessels"] = vessels
        # If self is set, also include an alias under "self" key
        if self_vessel_id and self_vessel_id in vessels:
            result["vessels"]["self"] = vessels[self_vessel_id]

    return result


def build_tree_for_path(conn: sqlite3.Connection, path: str) -> dict[str, Any] | None:
    """Resolve a Signal K path relative to ``/signalk/v1/api/vessels/self/``.

    Returns the subtree at *path*, or *None* if nothing is available.
    """
    full = build_full_tree(conn)
    if "vessels" not in full:
        return None
    self_key = full.get("self")
    if self_key and self_key in full.get("vessels", {}):
        target = full["vessels"][self_key]
    elif "self" in full.get("vessels", {}):
        target = full["vessels"]["self"]
    else:
        return None

    if not path:
        return target
    parts = path.strip("/").split("/")
    current = target
    for part in parts:
        if not isinstance(current, dict):
            return None
        current = current.get(part)
        if current is None:
            return None
    return current