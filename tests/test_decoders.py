"""Decode recorded-format lines and enforce bounded, environment-only state without RF hardware."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import decoders


def sensor(**values):
    return json.dumps({"model": "WeatherStation", "id": 42, "temperature_C": 21.23, **values})


@pytest.mark.parametrize("line,expected", [
    ("POCSAG1200: Address: 123456 Function: 3 Alpha: test message", ("POCSAG1200", "123456", "alpha", "test message")),
    ("POCSAG512: Address: 0 Function: 0 Numeric: 12345", ("POCSAG512", "0", "numeric", "12345")),
    ("POCSAG2400: Address: 456 Function: 1", ("POCSAG2400", "456", "tone", "")),
    ("FLEX|2026-09-21 12:00:00|1600/2/K/A|00.001|000123|ALN|test flex", ("FLEX", "123", "alpha", "test flex")),
    ("FLEX: 2026-09-21 12:00:00 1600/2/K/A 00.001 [000123] NUM 5678", ("FLEX", "123", "numeric", "5678")),
])
def test_supported_pager_line_formats(line, expected):
    parsed = decoders.parse_pager_line(line)
    assert tuple(parsed[k] for k in ("proto", "address", "kind", "text")) == expected


@pytest.mark.parametrize("line", [None, "", "Enabled demodulators: POCSAG1200", "POCSAG1200: bad frame", "FLEX|broken"])
def test_pager_chatter_does_not_create_messages(line):
    assert decoders.parse_pager_line(line) is None


def test_untrusted_text_is_printable_bounded_and_preserves_zero():
    assert decoders.clean_text("a\x00b\rc\td\u202ee\x7f") == "ab c de"
    assert decoders.clean_text(0) == "0"
    assert len(decoders.clean_text("x" * 1000)) == decoders.MAX_TEXT


def test_pager_history_and_address_statistics_are_bounded():
    job = decoders.PagerJob(clock=lambda: 1000)
    for address in range(job.MAX_MESSAGES + 50):
        assert job.feed(f"POCSAG1200: Address: {address} Function: 3 Alpha: text")
    assert job.count == job.MAX_MESSAGES + 50
    assert len(job.messages) == len(job.addresses) == job.MAX_MESSAGES
    assert "0" not in job.addresses
    view = job.view()
    assert len(view["messages"]) == 120
    assert view["messages"][0]["address"] == str(job.MAX_MESSAGES + 49)
    assert len(view["top_addresses"]) == 8


def test_environment_readings_have_safe_numeric_values_and_metadata():
    device, why = decoders.parse_sensor_line(sensor(id=0, channel=0, humidity=55, battery_ok=0, rssi=-12.25))
    assert why is None
    assert device["id"] == device["channel"] == "0"
    assert device["readings"]["temperature_C"] == {"label": "Temp", "value": 21.2, "unit": "°C"}
    assert device["readings"]["humidity"]["value"] == 55
    assert device["battery_ok"] is False and device["rssi"] == -12.2


@pytest.mark.parametrize("values", [
    {"model": "Car-TPMS", "pressure_kPa": 240}, {"model": "DoorContact"},
    {"model": "GarageRemote"}, {"model": "Unknown", "button": 1},
    {"model": "WeatherStation", "tamper": False},
    {"model": "a" * 65 + "TPMS"}, {"model": ""}, {"model": None},
    {"temperature_C": True}, {"temperature_C": "21"},
])
def test_non_environment_devices_and_invalid_readings_are_discarded(values):
    assert decoders.parse_sensor_line(sensor(**values)) == (None, "ignored")


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf"), 10 ** 400])
def test_nonfinite_measurements_never_reach_browser_json(value):
    device, why = decoders.parse_sensor_line(sensor(temperature_C=value, humidity=45, rssi=value))
    assert why is None
    assert set(device["readings"]) == {"humidity"}
    assert "rssi" not in device
    json.dumps(device, allow_nan=False)


@pytest.mark.parametrize("line", [None, "", "noise", "{invalid", "[]", "{}", '{"temperature_C":23}'])
def test_sensor_chatter_does_not_create_devices(line):
    assert decoders.parse_sensor_line(line) == (None, None)


def test_sensor_retention_counts_history_and_eviction():
    now = [1000.0]
    job = decoders.SensorJob(clock=lambda: now[0])
    for i in range(40):
        job.feed(sensor(temperature_C=i))
        now[0] += 1
    device = job.view()["devices"][0]
    assert device["count"] == job.frames == 40
    assert device["first_seen"] == 1000 and device["last_seen"] == 1039
    assert device["history"]["temperature_C"] == list(range(10, 40))
    assert job.feed(sensor(model="Door", id="private-id"))
    assert job.ignored == 1 and len(job.devices) == 1
    assert "private-id" not in json.dumps(job.view())
    for i in range(job.MAX_DEVICES):
        now[0] += 1
        job.feed(sensor(id=f"new-{i}"))
    assert len(job.devices) == job.MAX_DEVICES
    assert device["key"] not in job.devices
    assert job.view()["devices"][0]["id"] == f"new-{job.MAX_DEVICES - 1}"


def test_decoder_commands_use_pipelines_without_persisting_payloads():
    pager = decoders.PagerJob().command(929.6125)
    assert "rtl_fm" in pager and " | multimon-ng" in pager
    assert all(name in pager for name in ("POCSAG512", "POCSAG1200", "POCSAG2400", "FLEX"))
    assert decoders.SensorJob().command(433.92) == "rtl_433 -f 433.92M -g 40 -F json -M level"
    assert ">" not in pager
    assert decoders.shell_pipe(["echo", "a; exit 9"]) == "echo 'a; exit 9'"
