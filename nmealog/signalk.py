"""Signal K data model transformation for nmealog.

Maps NMEA sentences to the Signal K resource tree as defined in the
Signal K specification v1.8.4 (https://signalk.org/specification/1.8.4/doc/).

Position, GNSS, heading, depth, wind and weather values are populated when
the device sends the corresponding NMEA 0183 sentences.  All angles are in
radians, as the specification requires.
"""
from __future__ import annotations

import math
import sqlite3
import time
from datetime import UTC, datetime
from typing import Any

from .nmea import as_float, sentence_valid

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

SIGNALK_VERSION = "1.8.4"
SELF_URN = "urn:mrn:signalk:uuid:nmealog"

# Values of navigation.gnss.methodQuality (Signal K 1.8.4 enum).  GGA quality
# codes 0-8 index this tuple directly.
_METHOD_QUALITY = (
    "no GPS",
    "GNSS Fix",
    "DGNSS fix",
    "GNSS Fix",  # GGA 3 is PPS mode, which has no Signal K equivalent of its own
    "RTK fixed integer",
    "RTK float",
    "Estimated (DR) mode",
    "Manual input",
    "Simulator mode",
)
# GNS mode indicators, one per constellation.  The first usable indicator wins.
_GNS_METHOD_QUALITY = {
    "A": "GNSS Fix",
    "D": "DGNSS fix",
    "P": "Precise GNSS",
    "R": "RTK fixed integer",
    "F": "RTK float",
    "E": "Estimated (DR) mode",
    "M": "Manual input",
    "S": "Simulator mode",
}


def _gga_method_quality(value: Any) -> str:
    quality = int(value)
    if not 0 <= quality < len(_METHOD_QUALITY):
        return "Error"
    return _METHOD_QUALITY[quality]


def _gns_method_quality(value: Any) -> str:
    for ch in str(value).strip().upper():
        if ch in _GNS_METHOD_QUALITY:
            return _GNS_METHOD_QUALITY[ch]
    return "no GPS"


def _radians(value: Any) -> float:
    return math.radians(float(value))


# ---------------------------------------------------------------------------
# NMEA → Signal K field mapping
# ---------------------------------------------------------------------------
# Each entry: (sentence, field) → dict with:
#   path       – Signal K dotted path (without "navigation." prefix)
#   attr       – which part of the path gets the value ("value" or "value.*")
#   pgn        – NMEA 2000 PGN number (0 when unknown)
#   description
#   converter  – callable to transform the raw string
#   units      – SI unit string (Signal K standard; angles are radians)
#   keep_invalid – publish even when the sentence reports no valid fix

SignalKMap = dict[str, Any]

_FIELD_MAP: dict[tuple[str, str], SignalKMap] = {
    # --- Position (GGA) ---
    ("GGA", "altitude"): {
        "ctx": "gnss.antennaAltitude",
        "pgn": 129029,
        "description": "GNSS Antenna Altitude",
        "units": "m",
        "converter": float,
    },
    ("GGA", "gps_qual"): {
        "ctx": "gnss.methodQuality",
        "pgn": 129029,
        "description": "GNSS Method Quality",
        "units": "",
        "converter": _gga_method_quality,
        "keep_invalid": True,  # quality 0 is reported as "no GPS"
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
    # --- Position (GNS) ---
    ("GGA", "geo_sep"): {
        "ctx": "gnss.geoidalSeparation",
        "pgn": 129029,
        "description": "GNSS Geoidal Separation",
        "units": "m",
        "converter": float,
    },
    ("GNS", "geo_sep"): {
        "ctx": "gnss.geoidalSeparation",
        "pgn": 129029,
        "description": "GNSS Geoidal Separation",
        "units": "m",
        "converter": float,
    },
    ("GNS", "altitude"): {
        "ctx": "gnss.antennaAltitude",
        "pgn": 129029,
        "description": "GNSS Antenna Altitude",
        "units": "m",
        "converter": float,
    },
    ("GNS", "mode_indicator"): {
        "ctx": "gnss.methodQuality",
        "pgn": 129029,
        "description": "GNSS Method Quality",
        "units": "",
        "converter": _gns_method_quality,
        "keep_invalid": True,  # all-N mode is reported as "no GPS"
    },
    ("GNS", "num_sats"): {
        "ctx": "gnss.satellites",
        "pgn": 129029,
        "description": "GNSS Satellites",
        "units": "",
        "converter": int,
    },
    ("GNS", "hdop"): {
        "ctx": "gnss.horizontalDilution",
        "pgn": 129029,
        "description": "GNSS Horizontal Dilution",
        "units": "",
        "converter": float,
    },
    # --- GNSS dilution (GSA) ---
    ("GSA", "hdop"): {
        "ctx": "gnss.horizontalDilution",
        "pgn": 129029,
        "description": "GNSS Horizontal Dilution",
        "units": "",
        "converter": float,
    },
    ("GSA", "pdop"): {
        "ctx": "gnss.positionDilution",
        "pgn": 129029,
        "description": "GNSS Position Dilution",
        "units": "",
        "converter": float,
    },
    # --- Position (RMC) ---
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
        "units": "rad",
        "converter": _radians,
    },
    # --- Position (GLL) ---
    ("VTG", "true_track"): {
        "ctx": "courseOverGroundTrue",
        "pgn": 129026,
        "description": "Course Over Ground (True)",
        "units": "rad",
        "converter": _radians,
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
        "units": "rad",
        "converter": _radians,
    },
    # --- Heading (THS) ---
    ("THS", "heading"): {
        "ctx": "headingTrue",
        "pgn": 127250,
        "description": "Heading (True)",
        "units": "rad",
        "converter": _radians,
    },
    # --- Heading (HDM) ---
    ("HDM", "heading"): {
        "ctx": "headingMagnetic",
        "pgn": 127250,
        "description": "Heading (Magnetic)",
        "units": "rad",
        "converter": _radians,
    },
    # --- Heading (HDG) ---
    # The HDG heading is the raw compass reading.  Magnetic and true headings are
    # derived from it, using deviation and variation, in build_vessel.
    ("HDG", "heading"): {
        "ctx": "headingCompass",
        "pgn": 127250,
        "description": "Heading (Compass)",
        "units": "rad",
        "converter": _radians,
    },
    ("HDG", "deviation"): {
        "ctx": "magneticDeviation",
        "pgn": 127250,
        "description": "Magnetic Deviation",
        "units": "rad",
        "converter": _radians,
    },
    ("RMC", "mag_variation"): {
        "ctx": "magneticVariation",
        "pgn": 127250,
        "description": "Magnetic Variation",
        "units": "rad",
        "converter": _radians,
    },
    ("HDG", "variation"): {
        "ctx": "magneticVariation",
        "pgn": 127250,
        "description": "Magnetic Variation",
        "units": "rad",
        "converter": _radians,
    },
    # --- Rate of Turn (ROT) ---
    ("ROT", "rate_of_turn"): {
        "ctx": "rateOfTurn",
        "pgn": 127251,
        "description": "Rate of Turn",
        "units": "rad/s",
        "converter": lambda v: float(v) * math.pi / 180 / 60,  # deg/min → rad/s
    },
    # --- Depth (DBT, DPT) ---
    ("DBT", "depth_meters"): {
        "ctx": "environment.depth.belowTransducer",
        "pgn": 128267,
        "description": "Depth Below Transducer",
        "units": "m",
        "converter": float,
    },
    ("DPT", "depth"): {
        "ctx": "environment.depth.belowTransducer",
        "pgn": 128267,
        "description": "Depth Below Transducer",
        "units": "m",
        "converter": float,
    },
    # --- Weather and wind (MDA) ---
    ("MDA", "b_pressure_bar"): {
        "ctx": "environment.outside.pressure",
        "pgn": 130314,
        "description": "Outside Pressure",
        "units": "Pa",
        "converter": lambda v: float(v) * 100000.0,  # bar → Pa
    },
    ("MDA", "air_temp"): {
        "ctx": "environment.outside.temperature",
        "pgn": 130316,
        "description": "Outside Temperature",
        "units": "K",
        "converter": lambda v: float(v) + 273.15,  # °C → K
    },
    ("MDA", "water_temp"): {
        "ctx": "environment.water.temperature",
        "pgn": 130312,
        "description": "Water Temperature",
        "units": "K",
        "converter": lambda v: float(v) + 273.15,  # °C → K
    },
    ("MDA", "rel_humidity"): {
        "ctx": "environment.outside.relativeHumidity",
        "pgn": 130313,
        "description": "Outside Relative Humidity",
        "units": "",
        "converter": lambda v: float(v) / 100.0,  # percent → ratio
    },
    ("MDA", "dew_point"): {
        "ctx": "environment.outside.dewPointTemperature",
        "pgn": 130316,
        "description": "Outside Dew Point Temperature",
        "units": "K",
        "converter": lambda v: float(v) + 273.15,  # °C → K
    },
    ("MDA", "direction_true"): {
        "ctx": "environment.wind.directionTrue",
        "pgn": 130306,
        "description": "Wind Direction (True)",
        "units": "rad",
        "converter": _radians,
    },
    ("MDA", "direction_magnetic"): {
        "ctx": "environment.wind.directionMagnetic",
        "pgn": 130306,
        "description": "Wind Direction (Magnetic)",
        "units": "rad",
        "converter": _radians,
    },
    ("MDA", "wind_speed_knots"): {
        "ctx": "environment.wind.speedTrue",
        "pgn": 130306,
        "description": "Wind Speed (True)",
        "units": "m/s",
        "converter": lambda v: float(v) * 0.514444,
    },
    ("MDA", "wind_speed_meters"): {
        "ctx": "environment.wind.speedTrue",
        "pgn": 130306,
        "description": "Wind Speed (True)",
        "units": "m/s",
        "converter": float,
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
        "meta": {"timeout": _timeout_for("position")},
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


def _sensor_node(
    ctx: str,
    value: Any,
    received_at: float,
    source_label: str,
    pgn: int,
    description: str,
    units: str,
) -> dict[str, Any]:
    return {
        "value": value,
        "$source": source_label,
        "timestamp": iso_time(received_at),
        "pgn": pgn,
        "description": description,
        "units": units,
        "meta": {"timeout": _timeout_for(ctx)},
    }


# MWV carries its reference and speed units in separate fields, so it cannot be
# represented by the one-field _FIELD_MAP table.
_MWV_REFERENCE_PATHS = {
    "R": ("angleApparent", "speedApparent"),
    "T": ("angleTrueWater", "speedTrue"),
}
_WIND_SPEED_TO_MS = {"N": 0.514444, "M": 1.0, "K": 1000.0 / 3600.0, "S": 0.44704}


def _bow_angle_radians(degrees: float) -> float:
    """Wind angles relative to the bow are signed: negative to port, positive to starboard."""
    return math.radians((degrees + 180.0) % 360.0 - 180.0)


def _apply_mwv(
    navigation: dict[str, Any],
    latest: list[dict[str, Any]],
    device_id: int,
    source_label: str,
    now: float,
) -> None:
    """Add apparent/true wind from an MWV sentence, honouring its reference and units."""
    rows = {
        row["field"]: row for row in latest
        if row["device_id"] == device_id and row["sentence"] == "MWV"
    }
    if not rows:
        return
    status = rows.get("status")
    if status is not None and str(status["value"]).strip().upper() != "A":
        return
    reference = rows.get("reference")
    paths = _MWV_REFERENCE_PATHS.get(str(reference["value"]).strip().upper()) if reference else None
    if paths is None:
        return
    angle_path, speed_path = paths

    angle = rows.get("wind_angle")
    if angle is not None and now - angle["received_at"] <= _timeout_for(f"environment.wind.{angle_path}"):
        value = as_float(angle["value"])
        if value is not None:
            _set_path(
                navigation, f"environment.wind.{angle_path}",
                _sensor_node(f"environment.wind.{angle_path}", _bow_angle_radians(value),
                             angle["received_at"], source_label, 130306,
                             "Wind Angle", "rad"),
            )

    speed = rows.get("wind_speed")
    units = rows.get("wind_speed_units")
    if speed is not None and units is not None:
        factor = _WIND_SPEED_TO_MS.get(str(units["value"]).strip().upper())
        value = as_float(speed["value"])
        ctx = f"environment.wind.{speed_path}"
        if factor is not None and value is not None and now - speed["received_at"] <= _timeout_for(ctx):
            _set_path(
                navigation, ctx,
                _sensor_node(ctx, value * factor, speed["received_at"], source_label, 130306,
                             "Wind Speed", "m/s"),
            )


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

# Seconds a value stays valid after it was received.  Values older than this are
# left out of the tree.  Slow-changing values get longer limits.
_DEFAULT_TIMEOUT_S = 30.0
_TIMEOUT_BY_PREFIX: tuple[tuple[str, float], ...] = (
    ("position", 60.0),
    ("gnss", 60.0),
    ("environment.outside", 300.0),
    ("environment.water", 300.0),
    ("magneticVariation", 600.0),
    ("magneticDeviation", 600.0),
)

_HEADING_COMPASS = {"pgn": 127250, "description": "Heading (Compass)", "units": "rad"}
_HEADING_MAGNETIC = {"pgn": 127250, "description": "Heading (Magnetic)", "units": "rad"}
_HEADING_TRUE = {"pgn": 127250, "description": "Heading (True)", "units": "rad"}


def _timeout_for(ctx: str) -> float:
    for prefix, seconds in _TIMEOUT_BY_PREFIX:
        if ctx == prefix or ctx.startswith(prefix + "."):
            return seconds
    return _DEFAULT_TIMEOUT_S


def _leaf(ctx: str, value: Any, received_at: float, source_label: str, mapping: dict[str, Any]) -> dict[str, Any]:
    return {
        "value": value,
        "$source": source_label,
        "timestamp": iso_time(received_at),
        "pgn": mapping["pgn"],
        "description": mapping["description"],
        "units": mapping["units"],
        "meta": {"timeout": _timeout_for(ctx)},
    }


def _derived_headings(hdg: dict[str, dict[str, Any]]) -> list[tuple[str, float, float, dict[str, Any]]]:
    """Magnetic and true heading from an HDG sensor reading.

    NMEA 0183 HDG notes: magnetic = sensor reading + deviation (east positive,
    so the signed value is added directly), and true = magnetic + variation.
    The fields are null when unknown, so a derived value needs its inputs.
    Each entry is (context, radians, received_at, mapping).
    """
    heading = hdg.get("heading")
    compass = as_float(heading["value"]) if heading else None
    if heading is None or compass is None:
        return []
    derived = [("headingCompass", math.radians(compass), heading["received_at"], _HEADING_COMPASS)]

    deviation = hdg.get("deviation")
    deviation_deg = as_float(deviation["value"]) if deviation else None
    if deviation is None or deviation_deg is None:
        return derived
    magnetic = (compass + deviation_deg) % 360.0
    magnetic_at = max(heading["received_at"], deviation["received_at"])
    derived.append(("headingMagnetic", math.radians(magnetic), magnetic_at, _HEADING_MAGNETIC))

    variation = hdg.get("variation")
    variation_deg = as_float(variation["value"]) if variation else None
    if variation is None or variation_deg is None:
        return derived
    true = (magnetic + variation_deg) % 360.0
    derived.append(("headingTrue", math.radians(true), max(magnetic_at, variation["received_at"]), _HEADING_TRUE))
    return derived


def build_vessel(
    device_id: int,
    device_name: str,
    latest: list[dict[str, Any]],
    position: dict[str, Any] | None,
    now: float | None = None,
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
    now
        Reference time in epoch seconds for staleness checks.  Defaults to the
        current time.

    Returns
    -------
    A Signal K ``navigation`` subtree.
    """
    now = time.time() if now is None else now
    navigation: dict[str, Any] = {}
    source_label = f"nmealog.{device_name}"

    # --- position from the latest fix in the positions table ---
    if position is not None and now - position["received_at"] <= _timeout_for("position"):
        navigation["position"] = _build_position_node(
            position["latitude"], position["longitude"],
            device_name, position["received_at"],
        )

    # A sentence's own status fields (RMC status, ROT status, ...) decide whether
    # its data may be published.  Entries flagged keep_invalid still report "no fix".
    device_rows = [row for row in latest if row["device_id"] == device_id]
    by_sentence: dict[str, dict[str, str]] = {}
    for row in device_rows:
        by_sentence.setdefault(row["sentence"], {})[row["field"]] = row["value"]
    valid_sentences = {name for name, fields in by_sentence.items() if sentence_valid(name, fields)}

    # Candidates are (context, value, received_at, mapping).  The newest fresh
    # candidate for each context is published.
    candidates: list[tuple[str, Any, float, dict[str, Any]]] = []
    for row in device_rows:
        mapping = _FIELD_MAP.get((row["sentence"], row["field"]))
        if mapping is None:
            continue
        if row["sentence"] not in valid_sentences and not mapping.get("keep_invalid"):
            continue
        try:
            converted = mapping["converter"](row["value"])
        except (ValueError, TypeError):
            continue
        candidates.append((mapping["ctx"], converted, row["received_at"], mapping))
    if "HDG" in valid_sentences:
        hdg = {row["field"]: row for row in device_rows if row["sentence"] == "HDG"}
        candidates.extend(_derived_headings(hdg))

    best: dict[str, tuple[Any, float, dict[str, Any]]] = {}
    for ctx, value, received_at, mapping in candidates:
        if now - received_at > _timeout_for(ctx):
            continue
        if ctx not in best or received_at >= best[ctx][1]:
            best[ctx] = (value, received_at, mapping)

    for ctx, (value, received_at, mapping) in best.items():
        node = navigation
        for part in ctx.split("."):
            node = node.setdefault(part, {})
        node.update(_leaf(ctx, value, received_at, source_label, mapping))

    _apply_mwv(navigation, latest, device_id, source_label, now)

    return navigation


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