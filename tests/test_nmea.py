import pytest

from nmealog.nmea import checksum_status, parse_line
from nmealog.simulator import sentence

from conftest import FIXTURES


def test_checksum_states():
    good = sentence("GPGLL,4807.038,N,01131.000,E,123519,A").strip()
    assert checksum_status(good) is True
    assert checksum_status(good[:-2] + "00") is False
    assert checksum_status("$GPGLL,4807.038,N,01131.000,E,123519,A") is None
    assert checksum_status("$GPGLL,4807.038*ZZ") is False


def test_gga_with_fix_gives_position_and_fields():
    p = parse_line(sentence("GPGGA,135123.00,5436.6107,N,00554.7198,W,2,12,0.8,31.5,M,53.7,M,,"))
    assert (p.talker, p.sentence, p.checksum_ok) == ("GP", "GGA", True)
    assert p.fields["num_sats"] == "12"
    assert p.position.source == "GGA"
    assert p.position.latitude == pytest.approx(54.610178, abs=1e-5)
    assert p.position.longitude == pytest.approx(-5.911997, abs=1e-5)
    assert p.position.satellites == 12


def test_gga_without_fix_has_no_position():
    p = parse_line(sentence("GPGGA,135123.00,5436.6107,N,00554.7198,W,0,00,,,M,,M,,"))
    assert p.position is None


def test_rmc_gives_position_speed_course_and_gps_time():
    p = parse_line(sentence("GPRMC,123519,A,4807.038,N,01131.000,E,022.4,084.4,230394,003.1,W"))
    assert p.position.speed_kn == pytest.approx(22.4)
    assert p.position.course_true == pytest.approx(84.4)
    assert p.gps_time == pytest.approx(764426119.0)  # 1994-03-23 12:35:19 UTC


def test_void_status_has_no_position():
    p = parse_line(sentence("GPRMC,123519,V,4807.038,N,01131.000,E,022.4,084.4,230394,003.1,W"))
    assert p.position is None


def test_zero_island_position_is_rejected():
    p = parse_line(sentence("GPGLL,0000.0000,N,00000.0000,E,123519,A"))
    assert p.position is None


def test_bad_checksum_is_flagged_and_has_no_position():
    line = sentence("GPGGA,135123.00,5436.6107,N,00554.7198,W,2,12,0.8,31.5,M,53.7,M,,")
    p = parse_line(line[:-4] + "00")
    assert p.checksum_ok is False
    assert p.position is None


def test_unknown_and_proprietary_sentences_use_generic_fields():
    p = parse_line(sentence("GPALR,123519,001,A,A,Alarm text"))
    assert p.sentence == "ALR" and p.checksum_ok is True
    assert p.fields["field_0"] == "123519"
    q = parse_line(sentence("PMTK001,0,3"))
    assert q.talker == "P" and q.sentence == "MTK001"


def test_ais_sentence_is_generic():
    p = parse_line("!AIVDM,1,1,,A,15M67FC000G?ufbE`FepT@3n00Sa,0*5C")
    assert (p.talker, p.sentence) == ("AI", "VDM")


def test_non_nmea_lines_raise():
    for junk in ("", "hello", "6108,N,00554.7198,W"):
        with pytest.raises(ValueError):
            parse_line(junk)


def test_live_gps_capture_parses_cleanly():
    lines = (FIXTURES / "live_gps_gbaships.nmea").read_bytes().decode("ascii", "replace").splitlines()
    parsed, bad, errors = 0, 0, 0
    for line in lines:
        try:
            p = parse_line(line)
        except ValueError:
            errors += 1  # the first line of a capture is usually a fragment
            continue
        parsed += 1
        bad += p.checksum_ok is False
    assert parsed > 100
    assert bad == 0
    assert errors <= 2
