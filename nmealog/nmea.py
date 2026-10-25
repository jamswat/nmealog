"""NMEA 0183 parsing. Typed parsing uses pynmea2; unknown sentences fall back to raw fields."""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone

import pynmea2

_HEX2 = re.compile(r"[0-9A-Fa-f]{2}")


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
    fields = {f"field_{i}": v for i, v in enumerate(parts[1:]) if v != ""}
    return talker, sentence, fields


def _typed(text: str):
    try:
        msg = pynmea2.parse(text, check=False)
    except Exception:
        return None
    return msg if isinstance(msg, pynmea2.TalkerSentence) else None


def _typed_fields(msg) -> dict[str, str]:
    names = [f[1] for f in type(msg).fields]
    fields: dict[str, str] = {}
    for i, value in enumerate(msg.data):
        if value == "":
            continue
        fields[names[i] if i < len(names) else f"field_{i}"] = value
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
                satellites=int(msg.num_sats) if msg.num_sats else None,
                hdop=float(msg.horizontal_dil) if msg.horizontal_dil else None,
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
                speed_kn=float(msg.spd_over_grnd) if msg.spd_over_grnd else None,
                course_true=float(msg.true_course) if msg.true_course else None,
            )
    except Exception:
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
    except Exception:
        return None
    if not isinstance(dt, datetime):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
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
        return Parsed(
            msg.talker,
            msg.sentence_type,
            checksum_ok,
            _typed_fields(msg),
            _valid_position(_position(msg)),
            _gps_time(msg),
        )
    talker, sentence, fields = _generic(body)
    return Parsed(talker, sentence, checksum_ok, fields)
