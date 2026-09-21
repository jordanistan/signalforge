import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import adsb  # noqa: E402

REF = (52.0, 4.0)  # near the published test messages (Netherlands)
IDENT = "8D4840D6202CC371C32CE0576098"  # callsign KLM1023, ICAO 4840D6
POS_ODD = "8D40621D58C382D690C8AC2863A7"  # ICAO 40621D, 38000 ft, ~52.2572 N 3.9194 E
POS_EVEN = "8D40621D58C386435CC412692AD6"
VELOCITY = "8D485020994409940838175B284F"  # ICAO 485020, 159 kt, track ~182.9, -832 ft/min


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def table(**kw):
    return adsb.AircraftTable(kw.pop("reference", REF), clock=kw.pop("clock", Clock()), **kw)


# ---- helpers ------------------------------------------------------------------------------------
@pytest.mark.parametrize("raw,expected", [
    ("*8D4840D6202CC371C32CE0576098;", IDENT),
    ("8d4840d6202cc371c32ce0576098", IDENT),  # lower case
    ("  *8D4840D6202CC371C32CE0576098;\n", IDENT),
    ("*8D4840D6202CC371C32CE0576098", IDENT),
    ("*5D4840D62F5A2B;", "5D4840D62F5A2B"),  # 14 hex chars: a short frame
])
def test_clean_frame_accepts_rtl_adsb_output(raw, expected):
    assert adsb.clean_frame(raw) == expected


@pytest.mark.parametrize("raw", ["", None, "*;", "*8D40;", "*ZZ4840D6202CC371C32CE0576098;", "8D4840D6202CC371C32CE057609",
                                 "8D4840D6202CC371C32CE05760981"])
def test_clean_frame_rejects_junk(raw):
    assert adsb.clean_frame(raw) is None


def test_distance_and_bearing():
    assert abs(adsb.distance_nm((30.0, -97.0), (31.0, -97.0)) - 60.0) < 0.3  # a degree of latitude is ~60 NM
    assert adsb.distance_nm((30, -97), (30, -97)) == 0
    assert adsb.bearing_deg((30, -97), (31, -97)) == pytest.approx(0, abs=0.5)
    assert adsb.bearing_deg((30, -97), (30, -96)) == pytest.approx(90, abs=1)
    assert adsb.bearing_deg((30, -97), (29, -97)) == pytest.approx(180, abs=0.5)
    assert adsb.bearing_deg((30, -97), (30, -98)) == pytest.approx(270, abs=1)


# ---- decoding -----------------------------------------------------------------------------------
def test_identification_sets_callsign():
    t = table()
    assert t.feed(IDENT) is True
    snap = t.snapshot()
    assert snap[0]["icao"] == "4840D6" and snap[0]["callsign"] == "KLM1023"


def test_a_single_position_frame_gives_altitude_but_no_position_yet():
    t = table()
    assert t.feed("*" + POS_ODD + ";")
    a = t.snapshot()[0]
    assert a["icao"] == "40621D" and a["alt_ft"] == 38000
    assert a["lat"] is None and a["dist_nm"] is None  # one CPR frame is ambiguous: wait for its pair


def test_even_odd_pair_gives_the_unambiguous_position():
    clock = Clock()
    t = table(clock=clock, max_range_nm=10_000)
    t.feed(POS_EVEN)
    clock.t += 1
    t.feed(POS_ODD)
    a = t.snapshot()[0]
    assert a["lat"] == pytest.approx(52.2572, abs=0.001) and a["lon"] == pytest.approx(3.9194, abs=0.001)
    assert a["dist_nm"] == pytest.approx(adsb.distance_nm(REF, (52.2572, 3.9194)), abs=0.2)
    assert 0 <= a["bearing"] < 360


def test_pair_order_does_not_matter():
    for first, second in ((POS_EVEN, POS_ODD), (POS_ODD, POS_EVEN)):
        clock = Clock()
        t = table(clock=clock, max_range_nm=10_000)
        t.feed(first)
        clock.t += 1
        t.feed(second)
        a = t.snapshot()[0]
        assert a["lat"] == pytest.approx(52.26, abs=0.02) and a["lon"] == pytest.approx(3.92, abs=0.05)


def test_frames_too_far_apart_in_time_are_not_paired():
    clock = Clock()
    t = table(clock=clock, max_range_nm=10_000)
    t.feed(POS_EVEN)
    clock.t += 30  # older than the pairing window
    t.feed(POS_ODD)
    assert t.snapshot()[0]["lat"] is None


def test_later_single_frames_are_decoded_against_the_aircrafts_own_last_fix():
    clock = Clock()
    t = table(clock=clock, max_range_nm=10_000)
    t.feed(POS_EVEN)
    clock.t += 1
    t.feed(POS_ODD)
    first = t.snapshot()[0]
    clock.t += 2
    t.feed(POS_ODD)  # a single frame: decoded relative to the previous fix
    again = t.snapshot()[0]
    assert (again["lat"], again["lon"]) == pytest.approx((first["lat"], first["lon"]), abs=0.001)
    assert again["msgs"] == 3


def test_reference_alias_trap_is_avoided():
    """Decoding against the receiver's location would put this Dutch aircraft ~120 NM from Austin. It must not."""
    clock = Clock()
    t = table(reference=(30.27, -97.74), clock=clock, max_range_nm=250)
    t.feed(POS_EVEN)
    clock.t += 1
    t.feed(POS_ODD)
    a = t.snapshot()[0]
    assert a["alt_ft"] == 38000
    assert a["lat"] is None and a["lon"] is None and a["dist_nm"] is None  # really ~5,000 NM away: not shown


def test_jump_ok_rejects_impossible_speeds():
    here, there = (30.0, -97.0), (31.0, -97.0)  # 60 NM apart
    assert adsb.jump_ok(here, there, 3600) is True  # 60 kt
    assert adsb.jump_ok(here, there, 60) is False  # 3,600 kt
    assert adsb.jump_ok(here, there, 0) is False
    assert adsb.jump_ok(here, here, 0) is True


def test_velocity_message_gives_speed_track_and_vertical_rate():
    t = table()
    t.feed(VELOCITY)
    a = t.snapshot()[0]
    assert a["gs_kt"] == 159 and a["track"] == pytest.approx(182.9, abs=0.1) and a["vrate"] == -832
    assert a["lat"] is None and a["dist_nm"] is None  # no position yet


def test_messages_for_one_aircraft_merge_into_a_single_row():
    t = table()
    for m in (IDENT, POS_ODD, POS_EVEN, VELOCITY):
        t.feed(m)
    rows = {r["icao"]: r for r in t.snapshot()}
    assert set(rows) == {"4840D6", "40621D", "485020"}
    assert rows["40621D"]["msgs"] == 2 and rows["40621D"]["alt_ft"] == 38000


# ---- bad input -----------------------------------------------------------------------------------
def test_corrupt_frames_are_rejected_and_counted():
    t = table()
    bad_crc = POS_ODD[:-1] + ("0" if POS_ODD[-1] != "0" else "1")  # flip the last checksum digit
    assert t.feed(bad_crc) is False
    assert t.feed("*8D40;") is False and t.feed("garbage") is False and t.feed(None) is False
    assert t.snapshot() == []
    assert (t.frames, t.accepted, t.rejected) == (4, 0, 4)
    assert t.feed(IDENT) is True and t.accepted == 1


def test_non_adsb_frames_are_ignored():
    t = table()
    assert t.feed("*5D4840D62F5A2B;") is False  # a short all-call reply (DF11): no ADS-B content
    assert t.snapshot() == []


# ---- aging and ordering ---------------------------------------------------------------------------
def test_stale_aircraft_are_purged():
    clock = Clock()
    t = table(clock=clock, ttl=60)
    t.feed(IDENT)
    clock.t += 30
    t.feed(VELOCITY)
    assert {r["icao"] for r in t.snapshot()} == {"4840D6", "485020"}
    clock.t += 40  # IDENT is now 70 s old, VELOCITY 40 s
    assert {r["icao"] for r in t.snapshot()} == {"485020"}
    clock.t += 100
    assert t.snapshot() == [] and t.aircraft == {}


def test_a_stale_position_is_dropped_but_the_aircraft_row_stays_while_it_is_heard():
    clock = Clock()
    t = table(clock=clock, ttl=60, max_range_nm=10_000)
    t.feed(POS_EVEN)
    clock.t += 1
    t.feed(POS_ODD)
    assert t.snapshot()[0]["lat"] is not None
    clock.t += 50
    t.feed("8D40621D202CC371C32CE0576098")  # any frame keeps this ICAO alive (bad CRC here: ignored)
    t2 = table(clock=clock, ttl=60, max_range_nm=10_000)
    t2.aircraft = {k: dict(v) for k, v in t.aircraft.items()}
    for a in t2.aircraft.values():
        a["last_seen"] = clock.t  # heard just now...
    clock.t += 30
    for a in t2.aircraft.values():
        a["last_seen"] = clock.t  # ...and again, but no new position for 80 s
    row = t2.snapshot()[0]
    assert row["lat"] is None and row["dist_nm"] is None and row["alt_ft"] == 38000


def test_snapshot_is_nearest_first_with_position_less_aircraft_last():
    clock = Clock()
    t = table(clock=clock, max_range_nm=10_000)
    t.feed(VELOCITY)  # no position
    t.feed(POS_EVEN)
    clock.t += 1
    t.feed(POS_ODD)  # has a position (from the pair)
    t.feed(IDENT)  # no position
    rows = t.snapshot()
    assert rows[0]["icao"] == "40621D"
    assert {r["icao"] for r in rows[1:]} == {"485020", "4840D6"} and all(r["dist_nm"] is None for r in rows[1:])


def test_surface_cpr_is_not_decoded_with_the_airborne_grid(monkeypatch):
    decoded = {"df": 17, "crc_valid": True, "icao": "ABC123", "typecode": 5,
               "cpr_format": 0, "cpr_lat": 93000, "cpr_lon": 51372}
    monkeypatch.setattr(adsb.pms, "decode", lambda _: decoded)
    tracker = table()
    assert tracker.feed(IDENT)
    decoded["cpr_format"] = 1
    assert tracker.feed(IDENT)
    assert tracker.snapshot()[0]["lat"] is None
    assert tracker.aircraft["ABC123"]["_cpr"] == {}


def test_distance_handles_antipodal_roundoff():
    assert adsb.distance_nm((45, 10), (-45, -170)) == pytest.approx(10807.3, abs=1)


@pytest.mark.parametrize("position", [(float("nan"), 4), (52, float("inf")), (95, 4), (52, 190)])
def test_invalid_decoded_positions_are_rejected(monkeypatch, position):
    monkeypatch.setattr(adsb, "airborne_position_pair", lambda *args, **kwargs: position)
    tracker = table()
    tracker.feed(POS_EVEN)
    tracker.feed(POS_ODD)
    assert tracker.snapshot()[0]["lat"] is None
