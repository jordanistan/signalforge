"""make doctor: read-only diagnostics that must agree with what the console actually accepts."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import doctor  # noqa: E402
import radiowatch as rw  # noqa: E402

AIRCRAFT = next(spec for spec in doctor.SDR_MODES if spec["mode"] == "aircraft")


@pytest.mark.parametrize("installed", [set(), {"rtl_adsb"}, {"readsb"}, {"dump1090-fa"}, {"dump1090"},
                                       {"rtl_power", "rtl_fm", "ffmpeg", "rtl_433"}])
def test_doctor_sdr_modes_match_radiowatch_capabilities(monkeypatch, installed):
    which = lambda name: f"/usr/bin/{name}" if name in installed else None  # noqa: E731
    monkeypatch.setattr(rw.shutil, "which", which)
    caps = rw.RadioWatch(lambda _: None).capabilities()
    for spec in doctor.SDR_MODES:
        status = doctor.sdr_mode_status(spec, which=which)
        assert status["available"] == caps[spec["mode"]]["available"], spec["mode"]
        assert bool(status["hint"]) == (not status["available"])


def test_doctor_uses_the_same_adsb_receiver_list_as_the_air_page():
    assert AIRCRAFT["any_of"] == list(rw.ADSB_PROGRAMS)
    status = doctor.sdr_mode_status(AIRCRAFT, which=lambda n: n == "readsb" and "/usr/bin/readsb")
    assert status["available"] and status["program"] == "readsb"
    missing = doctor.sdr_mode_status(AIRCRAFT, which=lambda n: None)
    assert not missing["available"] and "rtl_adsb" in missing["missing"][0] and "pacman" in missing["hint"]


def test_missing_pymodes_is_reported_for_aircraft():
    status = doctor.sdr_mode_status(AIRCRAFT, which=lambda n: "/usr/bin/" + n, find_spec=lambda name: None)
    assert status["missing"] == ["pyModeS"]


def test_dongle_detection_and_tv_driver_warning(tmp_path):
    dongle = {"vid": "0bda", "pid": "2838", "product": "RTL2838UHIDIR", "dev": None}
    other = {"vid": "10c4", "pid": "ea60", "product": "CP2102", "dev": None}
    assert doctor.dongle_status([other], modules=str(tmp_path))["present"] is False
    ok = doctor.dongle_status([dongle, other], modules=str(tmp_path))
    assert ok["present"] and ok["hint"] is None and ok["devices"] == [dongle]
    (tmp_path / doctor.DVB_MODULE).mkdir()
    assert "modprobe -r" in doctor.dongle_status([dongle], modules=str(tmp_path))["hint"]


def test_missing_serial_port_has_an_actionable_hint(tmp_path):
    s = doctor.serial_status(str(tmp_path / "ttyUSB9"))
    assert not s["exists"] and "plugged in" in s["hint"]


def test_doctor_json_cli_reports_every_sdr_mode(tmp_path):
    out = subprocess.run([sys.executable, str(ROOT / "tools/doctor.py"), "--json", "--port", str(tmp_path / "none"),
                          "--state-dir", str(tmp_path)], capture_output=True, text=True, timeout=30)
    report = json.loads(out.stdout)
    assert set(report["sdr_modes"]) == {"survey", "audio", "aircraft", "sensors"}
    assert all("freq" in m and "label" in m for m in report["sdr_modes"].values())
    assert report["capabilities"]["aircraft"]["available"] == report["sdr_modes"]["aircraft"]["available"]
    text = subprocess.run([sys.executable, str(ROOT / "tools/doctor.py"), "--port", str(tmp_path / "none"),
                           "--state-dir", str(tmp_path)], capture_output=True, text=True, timeout=30).stdout
    assert "SDR modes" in text and "1090 MHz" in text
