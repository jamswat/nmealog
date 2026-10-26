"""Unit tests for the NMEA → Signal K mapping layer (Signal K 1.8.4)."""
from __future__ import annotations

import math

import pytest

from nmealog.signalk import SIGNALK_VERSION, build_vessel, field_to_signalk


def _row(device_id: int, sentence: str, field: str, value: str, ts: float):
    return {
        "device_id": device_id,
        "sentence": sentence,
        "field": field,
        "value": value,
        "received_at": ts,
    }


def _rad(degrees: float) -> float:
    return math.radians(degrees)


def test_signal_k_version_is_the_spec_version_we_implement():
    assert SIGNALK_VERSION == "1.8.4"


def test_gns_maps_gnss_fields_and_position():
    now = 1000.0
    latest = [
        _row(1, "GNS", "lat", "3722.5581", now),
        _row(1, "GNS", "lon", "12202.2199", now),
        _row(1, "GNS", "altitude", "11.0", now),
        _row(1, "GNS", "mode_indicator", "ANNN", now),
        _row(1, "GNS", "num_sats", "9", now),
        _row(1, "GNS", "hdop", "0.8", now),
    ]
    position = {"latitude": 37.375968, "longitude": -122.036998, "received_at": now}

    nav = build_vessel(1, "gps", latest, position)

    assert nav["position"]["value"]["latitude"] == pytest.approx(37.375968)
    assert nav["position"]["value"]["longitude"] == pytest.approx(-122.036998)
    assert nav["gnss"]["methodQuality"]["value"] == "GNSS Fix"
    assert nav["gnss"]["satellites"]["value"] == 9
    assert nav["gnss"]["horizontalDilution"]["value"] == pytest.approx(0.8)


def test_gns_no_fix_mode_reports_no_gps_quality():
    nav = build_vessel(1, "gps", [_row(1, "GNS", "mode_indicator", "NNNN", 1000.0)], None)
    assert nav["gnss"]["methodQuality"]["value"] == "no GPS"


def test_gns_no_fix_does_not_publish_its_position():
    now = 1000.0
    latest = [
        _row(1, "GNS", "lat", "3722.5581", now),
        _row(1, "GNS", "lon", "12202.2199", now),
        _row(1, "GNS", "mode_indicator", "NNNN", now),
    ]
    nav = build_vessel(1, "gps", latest, None)
    assert "position" not in nav


def test_raw_nmea_coordinates_are_not_published_as_degrees():
    now = 1000.0
    latest = [
        _row(1, "GGA", "lat", "5436.6111", now),
        _row(1, "GGA", "lon", "00554.7199", now),
        _row(1, "GGA", "altitude", "28.2", now),
        _row(1, "GGA", "gps_qual", "2", now),
    ]
    nav = build_vessel(1, "gps", latest, None)
    assert "position" not in nav
    assert nav["gnss"]["antennaAltitude"]["value"] == pytest.approx(28.2)


@pytest.mark.parametrize("quality, expected", [
    ("0", "no GPS"),
    ("1", "GNSS Fix"),
    ("2", "DGNSS fix"),
    ("4", "RTK fixed integer"),
    ("5", "RTK float"),
    ("6", "Estimated (DR) mode"),
    ("8", "Simulator mode"),
    ("42", "Error"),
])
def test_gga_quality_codes_map_to_signal_k_enum(quality, expected):
    nav = build_vessel(1, "gps", [_row(1, "GGA", "gps_qual", quality, 1000.0)], None)
    assert nav["gnss"]["methodQuality"]["value"] == expected


def test_newest_speed_wins_between_rmc_and_vtg():
    now = 1000.0
    latest = [
        _row(1, "VTG", "spd_over_grnd_kts", "5.0", now),
        _row(1, "RMC", "spd_over_grnd", "8.5", now - 5),
        _row(1, "VTG", "true_track", "80.0", now),
        _row(1, "RMC", "true_course", "90.0", now - 5),
    ]

    nav = build_vessel(1, "gps", latest, None)

    assert nav["speedOverGround"]["value"] == pytest.approx(5.0 * 0.514444)
    assert nav["courseOverGroundTrue"]["value"] == pytest.approx(_rad(80.0))
    assert nav["courseOverGroundTrue"]["units"] == "rad"


def test_rmc_with_void_status_publishes_nothing():
    now = 1000.0
    latest = [
        _row(1, "RMC", "status", "V", now),
        _row(1, "RMC", "spd_over_grnd", "8.5", now),
        _row(1, "RMC", "true_course", "90.0", now),
    ]
    nav = build_vessel(1, "gps", latest, None)
    assert "speedOverGround" not in nav
    assert "courseOverGroundTrue" not in nav


def test_rmc_with_non_fix_mode_publishes_nothing():
    now = 1000.0
    latest = [
        _row(1, "RMC", "status", "A", now),
        _row(1, "RMC", "mode_indicator", "E", now),
        _row(1, "RMC", "spd_over_grnd", "8.5", now),
    ]
    nav = build_vessel(1, "gps", latest, None)
    assert "speedOverGround" not in nav


def test_vtg_with_no_fix_publishes_nothing():
    now = 1000.0
    latest = [
        _row(1, "VTG", "faa_mode", "N", now),
        _row(1, "VTG", "true_track", "80.0", now),
    ]
    nav = build_vessel(1, "gps", latest, None)
    assert "courseOverGroundTrue" not in nav


def test_rot_with_void_status_publishes_nothing():
    latest = [_row(1, "ROT", "rate_of_turn", "-12.0", 1000.0), _row(1, "ROT", "status", "V", 1000.0)]
    nav = build_vessel(1, "gps", latest, None)
    assert "rateOfTurn" not in nav


def test_ths_and_hdm_map_true_and_magnetic_heading_in_radians():
    now = 1000.0
    latest = [
        _row(1, "HDT", "heading", "180.0", now - 5),
        _row(1, "THS", "heading", "181.0", now),
        _row(1, "THS", "mode_indicator", "A", now),
        _row(1, "HDM", "heading", "179.0", now),
    ]

    nav = build_vessel(1, "gps", latest, None)

    assert nav["headingTrue"]["value"] == pytest.approx(_rad(181.0))
    assert nav["headingTrue"]["units"] == "rad"
    assert nav["headingMagnetic"]["value"] == pytest.approx(_rad(179.0))


def test_ths_with_invalid_status_is_not_published():
    now = 1000.0
    latest = [
        _row(1, "HDT", "heading", "180.0", now),
        _row(1, "THS", "heading", "181.0", now),
        _row(1, "THS", "mode_indicator", "V", now),
    ]
    nav = build_vessel(1, "gps", latest, None)
    assert nav["headingTrue"]["value"] == pytest.approx(_rad(180.0))


def test_hdg_variation_and_deviation_use_spec_paths_and_sign():
    now = 1000.0
    latest = [
        _row(1, "HDG", "heading", "101.1", now),
        _row(1, "HDG", "deviation", "-3.0", now),
        _row(1, "HDG", "variation", "-12.5", now),
    ]
    nav = build_vessel(1, "gps", latest, None)
    assert nav["magneticVariation"]["value"] == pytest.approx(_rad(-12.5))
    assert nav["magneticVariation"]["units"] == "rad"
    assert nav["magneticDeviation"]["value"] == pytest.approx(_rad(-3.0))
    assert "variation" not in nav
    assert "deviation" not in nav


def test_gnss_dilution_depth_and_weather():
    now = 1000.0
    latest = [
        _row(1, "GSA", "hdop", "1.1", now),
        _row(1, "GSA", "pdop", "1.8", now),
        _row(1, "DBT", "depth_meters", "3.8", now),
        _row(1, "MDA", "b_pressure_bar", "1.013", now),
        _row(1, "MDA", "air_temp", "15.0", now),
        _row(1, "MDA", "water_temp", "12.0", now),
        _row(1, "MDA", "rel_humidity", "50", now),
        _row(1, "MDA", "dew_point", "8.0", now),
        _row(1, "MDA", "direction_true", "90.0", now),
        _row(1, "MDA", "wind_speed_meters", "5.0", now),
    ]

    nav = build_vessel(1, "gps", latest, None)

    assert nav["gnss"]["horizontalDilution"]["value"] == pytest.approx(1.1)
    assert nav["gnss"]["positionDilution"]["value"] == pytest.approx(1.8)
    assert "satellitesInView" not in nav["gnss"]
    assert nav["environment"]["depth"]["belowTransducer"]["value"] == pytest.approx(3.8)
    assert nav["environment"]["outside"]["pressure"]["value"] == pytest.approx(101300.0)
    assert nav["environment"]["outside"]["temperature"]["value"] == pytest.approx(288.15)
    assert nav["environment"]["water"]["temperature"]["value"] == pytest.approx(285.15)
    assert nav["environment"]["outside"]["relativeHumidity"]["value"] == pytest.approx(0.5)
    assert nav["environment"]["outside"]["dewPointTemperature"]["value"] == pytest.approx(281.15)
    assert nav["environment"]["wind"]["directionTrue"]["value"] == pytest.approx(_rad(90.0))
    assert nav["environment"]["wind"]["directionTrue"]["units"] == "rad"
    assert nav["environment"]["wind"]["speedTrue"]["value"] == pytest.approx(5.0)


def test_mwv_apparent_wind_uses_reference_units_and_signed_angle():
    now = 1000.0
    latest = [
        _row(1, "MWV", "wind_angle", "045.0", now),
        _row(1, "MWV", "reference", "R", now),
        _row(1, "MWV", "wind_speed", "10.0", now),
        _row(1, "MWV", "wind_speed_units", "N", now),
        _row(1, "MWV", "status", "A", now),
    ]

    nav = build_vessel(1, "gps", latest, None)

    assert nav["environment"]["wind"]["angleApparent"]["value"] == pytest.approx(_rad(45.0))
    assert nav["environment"]["wind"]["angleApparent"]["units"] == "rad"
    assert nav["environment"]["wind"]["speedApparent"]["value"] == pytest.approx(10.0 * 0.514444)
    assert "angleTrueWater" not in nav["environment"]["wind"]


@pytest.mark.parametrize("raw, expected_degrees", [
    ("270.0", -90.0),  # 270° clockwise from the bow is 90° to port
    ("359.0", -1.0),
    ("180.0", -180.0),
    ("090.0", 90.0),
])
def test_mwv_angle_is_signed_negative_to_port(raw, expected_degrees):
    latest = [
        _row(1, "MWV", "wind_angle", raw, 1000.0),
        _row(1, "MWV", "reference", "R", 1000.0),
        _row(1, "MWV", "wind_speed", "1.0", 1000.0),
        _row(1, "MWV", "wind_speed_units", "M", 1000.0),
        _row(1, "MWV", "status", "A", 1000.0),
    ]
    nav = build_vessel(1, "gps", latest, None)
    assert nav["environment"]["wind"]["angleApparent"]["value"] == pytest.approx(_rad(expected_degrees))


def test_mwv_speed_in_statute_mph_is_converted():
    latest = [
        _row(1, "MWV", "wind_angle", "090.0", 1000.0),
        _row(1, "MWV", "reference", "T", 1000.0),
        _row(1, "MWV", "wind_speed", "10.0", 1000.0),
        _row(1, "MWV", "wind_speed_units", "S", 1000.0),
        _row(1, "MWV", "status", "A", 1000.0),
    ]
    nav = build_vessel(1, "gps", latest, None)
    assert nav["environment"]["wind"]["speedTrue"]["value"] == pytest.approx(10.0 * 0.44704)


def test_mwv_true_wind_uses_true_paths():
    now = 1000.0
    latest = [
        _row(1, "MWV", "wind_angle", "090.0", now),
        _row(1, "MWV", "reference", "T", now),
        _row(1, "MWV", "wind_speed", "5.0", now),
        _row(1, "MWV", "wind_speed_units", "M", now),
        _row(1, "MWV", "status", "A", now),
    ]

    nav = build_vessel(1, "gps", latest, None)

    assert nav["environment"]["wind"]["angleTrueWater"]["value"] == pytest.approx(_rad(90.0))
    assert nav["environment"]["wind"]["speedTrue"]["value"] == pytest.approx(5.0)


def test_mwv_void_status_is_ignored():
    now = 1000.0
    latest = [
        _row(1, "MWV", "wind_angle", "045.0", now),
        _row(1, "MWV", "reference", "R", now),
        _row(1, "MWV", "wind_speed", "10.0", now),
        _row(1, "MWV", "wind_speed_units", "N", now),
        _row(1, "MWV", "status", "V", now),
    ]

    nav = build_vessel(1, "gps", latest, None)

    assert "environment" not in nav


def test_field_lookup_includes_new_sentences():
    assert field_to_signalk("GNS", "lat") is None  # position comes from the positions table
    assert field_to_signalk("GGA", "altitude")["ctx"] == "gnss.antennaAltitude"
    assert field_to_signalk("THS", "heading")["ctx"] == "headingTrue"
    assert field_to_signalk("HDM", "heading")["ctx"] == "headingMagnetic"
    assert field_to_signalk("DBT", "depth_meters")["ctx"] == "environment.depth.belowTransducer"
    assert field_to_signalk("GSV", "num_sv_in_view") is None
