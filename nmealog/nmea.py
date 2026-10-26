"""NMEA 0183 parsing. Typed parsing uses pynmea2; unknown sentences fall back to raw fields."""
from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

import pynmea2

_HEX2 = re.compile(r"[0-9A-Fa-f]{2}")

# pynmea2 1.19.0 has no THS class, but the two data fields are well known.
_NAMED_FALLBACK_FIELDS: dict[str, tuple[str, ...]] = {
    "THS": ("heading", "mode_indicator"),
}

# Heading sentences whose value is followed by an E/W letter.  West is negative.
_DIRECTION_FIELDS: dict[tuple[str, str], str] = {
    ("HDG", "deviation"): "dev_dir",
    ("HDG", "variation"): "var_dir",
}

# GNS mode characters that mean a usable fix (N = no fix, S = simulator).
_FIX_MODES = "ADPRFEM"


@dataclass
class Position:
    latitude: float
    longitude: float
    source: str
    fix_quality: int | None = None
    speed_kn: float | None = None
    course_true: float | None = None
    satellites: int | None = None
    hdop: float | None = None


@dataclass
class Parsed:
    talker: str
    sentence: str
    checksum_ok: bool | None  # None when the sentence has no checksum
    fields: dict[str, str]
    position: Position | None = None
    gps_time: float | None = None  # epoch seconds, only when the sentence carries a full date


def checksum_status(line: str) -> bool | None:
    star = line.find("*")
    if star < 0:
        return None
    given = line[star + 1:star + 3]
    if not _HEX2.fullmatch(given):
        return False
    calc = 0
    for ch in line[1:star]:
        calc ^= ord(ch)
    return calc == int(given, 16)


def _header_names(header: str) -> tuple[str, str]:
    if header.startswith("P"):
        return "P", header[1:]  # proprietary: $P<manufacturer><type>
    if len(header) > 2:
        return header[:2], header[2:]
    return "", header


def _generic(body: str) -> tuple[str, str, dict[str, str]]:
    parts = body.split(",")
    talker, sentence = _header_names(parts[0])
    names = _NAMED_FALLBACK_FIELDS.get(sentence)
    fields: dict[str, str] = {}
    for i, value in enumerate(parts[1:]):
        if value == "":
            continue
        key = names[i] if names and i < len(names) else f"field_{i}"
        fields[key] = value
    return talker, sentence, fields


def as_float(value) -> float | None:
    """Parse a numeric NMEA field; None for empty, malformed, or non-finite values."""
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _as_int(value) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _flag(fields: Mapping[str, str], key: str) -> str | None:
    value = fields.get(key)
    return None if value is None else str(value).strip().upper()


def sentence_valid(sentence: str, fields: Mapping[str, str]) -> bool:
    """Return False when the sentence's own status field says its data must not be used.

    A missing field is not treated as invalid, so partial sentences still count.
    """
    if sentence == "GGA":
        quality = _as_int(fields.get("gps_qual"))
        return quality is None or quality > 0
    if sentence == "GNS":
        mode = _flag(fields, "mode_indicator")
        return mode is None or any(ch in _FIX_MODES for ch in mode)
    if sentence == "GLL":
        return _flag(fields, "status") in (None, "A") and _flag(fields, "faa_mode") != "N"
    if sentence == "RMC":
        mode = _flag(fields, "mode_indicator")
        return _flag(fields, "status") in (None, "A") and mode in (None, "A", "D")
    if sentence == "VTG":
        return _flag(fields, "faa_mode") != "N"
    if sentence == "ROT":
        return _flag(fields, "status") in (None, "A")
    if sentence == "THS":
        return _flag(fields, "mode_indicator") != "V"
    return True


def _typed(text: str):
    try:
        msg = pynmea2.parse(text, check=False)
    except Exception:  # noqa: BLE001  # NMEA parsing is inherently fragile — any exception means "not parseable"
        return None
    return msg if isinstance(msg, pynmea2.TalkerSentence) else None


def _typed_fields(msg) -> dict[str, str]:
    names = [f[1] for f in type(msg).fields]
    fields: dict[str, str] = {}
    for i, value in enumerate(msg.data):
        if value == "":
            continue
        fields[names[i] if i < len(names) else f"field_{i}"] = value
    for (sentence, field), direction in _DIRECTION_FIELDS.items():
        if (sentence == msg.sentence_type and field in fields and _flag(fields, direction) == "W"
                and not fields[field].startswith("-")):
            fields[field] = "-" + fields[field]
    return fields


def _position(msg) -> Position | None:
    try:
        st = msg.sentence_type
        if st == "GGA":
            quality = int(msg.gps_qual or 0)
            if quality <= 0:
                return None
            return Position(
                msg.latitude, msg.longitude, "GGA", fix_quality=quality,
                satellites=_as_int(msg.num_sats),
                hdop=as_float(msg.horizontal_dil),
            )
        if st == "GNS":
            # GNS carries one mode indicator per constellation: GPS, GLONASS,
            # Galileo and BeiDou.  A single non-N indicator is a usable fix.
            mode = str(msg.mode_indicator or "").strip().upper()
            if not any(ch in _FIX_MODES for ch in mode):
                return None
            return Position(
                msg.latitude, msg.longitude, "GNS",
                satellites=_as_int(msg.num_sats),
                hdop=as_float(msg.hdop),
            )
        if st == "GLL":
            if msg.status != "A":
                return None
            return Position(msg.latitude, msg.longitude, "GLL")
        if st == "RMC":
            if msg.status != "A":
                return None
            return Position(
                msg.latitude, msg.longitude, "RMC",
                speed_kn=as_float(msg.spd_over_grnd),
                course_true=as_float(msg.true_course),
            )
    except (AttributeError, TypeError, ValueError):
        return None
    return None


def _valid_position(pos: Position | None) -> Position | None:
    if pos is None:
        return None
    lat, lon = pos.latitude, pos.longitude
    if lat is None or lon is None or not (math.isfinite(lat) and math.isfinite(lon)):
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    if lat == 0 and lon == 0:  # the usual signature of an unset GPS
        return None
    return pos


def _gps_time(msg) -> float | None:
    try:
        dt = getattr(msg, "datetime", None)
    except Exception:  # noqa: BLE001  # pynmea2 may raise for non-standard message types
        return None
    if not isinstance(dt, datetime):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.timestamp()


def parse_line(line: str) -> Parsed:
    """Parse one sentence. Raises ValueError if the line is not an NMEA sentence."""
    text = line.strip()
    if not text or text[0] not in "$!":
        raise ValueError("not an NMEA sentence")
    star = text.find("*")
    body = text[1:star] if star >= 0 else text[1:]
    if not body:
        raise ValueError("empty sentence")

    checksum_ok = checksum_status(text)
    if checksum_ok is False:
        talker, sentence, fields = _generic(body)
        return Parsed(talker, sentence, False, fields)

    msg = _typed(text) if text[0] == "$" else None
    if msg is not None:
        fields = _typed_fields(msg)
        position = _position(msg) if sentence_valid(msg.sentence_type, fields) else None
        return Parsed(
            msg.talker,
            msg.sentence_type,
            checksum_ok,
            fields,
            _valid_position(position),
            _gps_time(msg),
        )
    talker, sentence, fields = _generic(body)
    return Parsed(talker, sentence, checksum_ok, fields)
