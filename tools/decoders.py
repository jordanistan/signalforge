#!/usr/bin/env python3
"""Line-oriented SDR decoder for the web GUI: ISM-band environment sensors (rtl_433).

Receive-only, like everything else here. Each decoder is a `Job`: it builds the shell pipeline that owns the dongle,
turns one output line at a time into events, and reports a JSON-able `view()`. RadioWatch (radiowatch.py) runs one
job at a time because the dongle is exclusive.

Privacy rules built in, because these signals are broadcast by other people's equipment:
  * There is deliberately no pager decoder: in the US, reading other people's display-pager messages is generally
    prohibited (ECPA), and they often carry medical details. The band survey only reports whether a paging
    channel is busy, never message content.
  * Vehicle tyre-pressure (TPMS) sensors carry a fixed ID per car, which makes them a way to follow a vehicle, and
    door/alarm/remote sensors reveal what a household is doing. Only environment sensors (temperature, humidity,
    wind, rain, pressure...) are listed; everything else is counted as "ignored" and its content is dropped.
"""
import json
import math
import re
import shlex
import time
from collections import deque

AUTO = "auto"  # hop across the US/most-common ISM bands so nothing near you is missed
AUTO_FREQS, AUTO_HOP_S = (433.92, 315.0, 915.0), 60
SENSOR_PRESETS = [(AUTO, "Auto · 433.92 / 315 / 915, switching every 60 s"), (433.92, "433.92 · ISM (most sensors)"), (315.0, "315.0 · ISM (US remotes/sensors)"),
                  (868.3, "868.3 · ISM (Europe)"), (915.0, "915.0 · ISM (US)")]

MAX_TEXT = 300


def clean_text(s, limit=MAX_TEXT):
    """Radio text is untrusted: printable characters only, bounded length."""
    s = str(s if s is not None else "").replace("\n", " ").replace("\r", " ").replace("\t", " ")
    return "".join(c for c in s if c.isprintable()).strip()[:limit]


# ---- 433 MHz sensors ------------------------------------------------------------------------------------
ENV_FIELDS = {  # rtl_433 JSON key -> (label, unit)
    "temperature_C": ("Temp", "°C"), "temperature_1_C": ("Temp 1", "°C"), "temperature_2_C": ("Temp 2", "°C"),
    "humidity": ("Humidity", "%"), "wind_avg_km_h": ("Wind", "km/h"), "wind_max_km_h": ("Gust", "km/h"),
    "wind_dir_deg": ("Wind dir", "°"), "rain_mm": ("Rain", "mm"), "pressure_hPa": ("Pressure", "hPa"),
    "light_lux": ("Light", "lux"), "uv": ("UV", ""),
    "moisture": ("Soil moisture", "%"), "lightning_count": ("Lightning", "strikes"),
    "strike_distance": ("Strike dist", "km"), "storm_dist": ("Storm dist", "km"),
}
BLOCKED = re.compile(r"tpms|tire|tyre|remote|keyfob|key-fob|fob|garage|doorbell|door|window|contact|security|alarm|smoke|"
                     r"motion|pir|lock|car|vehicle", re.I)


# Fields only vehicle (tyre-pressure), remote-control and door/alarm devices report, whatever the model is called
DEVICE_KEYS = {"pressure_kPa", "pressure_PSI", "pressure_bar", "tpms", "code", "button", "cmd", "command", "state",
               "alarm", "tamper", "opened", "contact_open", "motion", "unit_id_tag"}


def parse_sensor_line(line):
    """rtl_433 JSON line -> (device dict, None) for an environment sensor, (None, 'ignored') for a device we do not
    list, or (None, None) when the line is not a decoded device."""
    line = (line or "").strip()
    if not line.startswith("{"):
        return None, None
    try:
        d = json.loads(line)
    except ValueError:
        return None, None
    if not isinstance(d, dict) or "model" not in d:
        return None, None
    if not isinstance(d["model"], str) or not d["model"].strip():
        return None, "ignored"
    model = clean_text(d["model"], 60)
    readings = {}
    for key, (label, unit) in ENV_FIELDS.items():
        v = d.get(key)
        value = finite_number(v)
        if value is not None:
            readings[key] = {"label": label, "value": round(value, 1), "unit": unit}
    if BLOCKED.search(d["model"]) or DEVICE_KEYS.intersection(d) or not readings:
        return None, "ignored"
    ident = clean_text(d.get("id", d.get("channel", "")), 24)
    dev = {"key": f"{model}/{ident}/{clean_text(d.get('channel', ''), 8)}", "model": model, "id": ident,
           "channel": clean_text(d.get("channel", ""), 8), "readings": readings}
    if type(d.get("battery_ok")) in (int, float, bool) and d["battery_ok"] in (0, 1):
        dev["battery_ok"] = bool(d["battery_ok"])
    rssi = finite_number(d.get("rssi"))
    if rssi is not None:
        dev["rssi"] = round(rssi, 1)
    return dev, None


def finite_number(value):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    try:
        value = float(value)
    except OverflowError:
        return None
    return value if math.isfinite(value) else None


class SensorJob:
    kind = "sensors"
    label = "433 MHz sensors"
    default_mhz = AUTO
    MAX_DEVICES = 60

    def __init__(self, clock=time.time):
        self.clock = clock
        self.devices = {}
        self.ignored = 0
        self.started = clock()
        self.frames = 0

    def command(self, mhz, gain=40, wrap=None):
        """`mhz` is a frequency, or AUTO to let rtl_433 hop between AUTO_FREQS every AUTO_HOP_S seconds."""
        freqs = AUTO_FREQS if mhz == AUTO else (mhz,)
        argv = ["rtl_433", *[a for f in freqs for a in ("-f", f"{f}M")]]
        if len(freqs) > 1:
            argv += ["-H", str(AUTO_HOP_S)]
        return shell_pipe((wrap or list)([*argv, "-g", str(gain), "-F", "json", "-M", "level"]))

    def feed(self, line):
        dev, why = parse_sensor_line(line)
        if why == "ignored":
            self.ignored += 1
            return True
        if not dev:
            return False
        self.frames += 1
        old = self.devices.get(dev["key"])
        dev["first_seen"] = old["first_seen"] if old else self.clock()
        dev["last_seen"] = self.clock()
        dev["count"] = (old["count"] if old else 0) + 1
        hist = old["history"] if old else {}
        for key, r in dev["readings"].items():
            hist[key] = (hist.get(key, []) + [r["value"]])[-30:]
        dev["history"] = hist
        self.devices[dev["key"]] = dev
        if len(self.devices) > self.MAX_DEVICES:
            oldest = min(self.devices.values(), key=lambda x: x["last_seen"])
            del self.devices[oldest["key"]]
        return True

    def view(self):
        devs = sorted(self.devices.values(), key=lambda x: -x["last_seen"])
        return {"kind": self.kind, "devices": devs, "ignored": self.ignored, "frames": self.frames, "started": self.started}


JOBS = {"sensors": SensorJob}
PRESETS = {"sensors": SENSOR_PRESETS}


def shell_pipe(*commands):
    """Join argv lists with pipes; the runner drains bounded diagnostics into memory."""
    parts = [" ".join(shlex.quote(a) for a in argv) for argv in commands]
    return " | ".join(parts)
