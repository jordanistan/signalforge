#!/usr/bin/env python3
"""Line-oriented SDR decoders for the web GUI: pagers (POCSAG/FLEX via multimon-ng) and 433 MHz sensors (rtl_433).

Receive-only, like everything else here. Each decoder is a `Job`: it builds the shell pipeline that owns the dongle,
turns one output line at a time into events, and reports a JSON-able `view()`. RadioWatch (radiowatch.py) runs one
job at a time because the dongle is exclusive.

Privacy rules built in, because these signals are broadcast by other people's equipment:
  * Pager text is kept in memory only (a short ring buffer), never written to disk and never relayed anywhere.
    Pagers used by hospitals and clinics can carry personal details; treat what you see accordingly.
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

# ---- pagers ---------------------------------------------------------------------------------------------
PAGER_PRESETS = [  # (MHz, label): common US paging channels
    (929.6125, "929.6125 · US paging (929 MHz band)"),
    (931.0625, "931.0625 · US paging (931 MHz band)"),
    (929.9375, "929.9375 · US paging"),
    (152.48, "152.480 · VHF paging"),
    (157.45, "157.450 · VHF paging"),
    (158.10, "158.100 · VHF paging"),
]
SENSOR_PRESETS = [(433.92, "433.92 · ISM (most sensors)"), (315.0, "315.0 · ISM (US remotes/sensors)"),
                  (868.3, "868.3 · ISM (Europe)"), (915.0, "915.0 · ISM (US)")]

_POCSAG = re.compile(r"^(POCSAG\d+):\s*Address:\s*(\d+)\s+Function:\s*(\d)(?:\s+(Alpha|Numeric):\s?(.*))?$")
_FLEX_PIPE = re.compile(r"^FLEX\|[^|]*\|([^|]*)\|[^|]*\|(\d+)\|([A-Z]+)\|(.*)$")
_FLEX_TEXT = re.compile(r"^FLEX:\s*\S+\s+\S+\s+(\S+)\s+\S+\s+\[(\d+)\]\s+([A-Z]+)\s*(.*)$")
MAX_TEXT = 300


def clean_text(s, limit=MAX_TEXT):
    """Radio text is untrusted: printable characters only, bounded length."""
    s = str(s if s is not None else "").replace("\n", " ").replace("\r", " ").replace("\t", " ")
    return "".join(c for c in s if c.isprintable()).strip()[:limit]


def parse_pager_line(line):
    """multimon-ng output line -> {"proto","address","text","kind"} or None for chatter/noise."""
    line = (line or "").strip()
    m = _POCSAG.match(line)
    if m:
        proto, addr, func, kind, text = m.groups()
        return {"proto": proto, "address": addr, "function": int(func), "kind": (kind or "tone").lower(),
                "text": clean_text(text)}
    m = _FLEX_PIPE.match(line) or _FLEX_TEXT.match(line)
    if m:
        cfg, addr, kind, text = m.groups()
        return {"proto": "FLEX", "address": addr.lstrip("0") or "0", "function": None,
                "kind": {"ALN": "alpha", "NUM": "numeric", "TON": "tone", "BIN": "binary"}.get(kind, kind.lower()),
                "text": clean_text(text), "cfg": cfg}
    return None


class PagerJob:
    kind = "pager"
    label = "Pagers"
    default_mhz = 929.6125
    MAX_MESSAGES = 300

    def __init__(self, clock=time.time):
        self.clock = clock
        self.messages = deque(maxlen=self.MAX_MESSAGES)
        self.count = 0
        self.addresses = {}
        self.started = clock()

    def command(self, mhz, gain=40):
        rtl = ["rtl_fm", "-f", f"{mhz}M", "-M", "fm", "-s", "22050", "-g", str(gain), "-E", "dc", "-A", "fast", "-"]
        mm = ["multimon-ng", "-t", "raw", "-a", "POCSAG512", "-a", "POCSAG1200", "-a", "POCSAG2400", "-a", "FLEX",
              "-f", "alpha", "-"]
        return shell_pipe(rtl, mm)

    def feed(self, line):
        p = parse_pager_line(line)
        if not p:
            return False
        p["ts"] = self.clock()
        self.count += 1
        if len(self.messages) == self.messages.maxlen:
            expired = self.messages[0]["address"]
            self.addresses[expired] -= 1
            if not self.addresses[expired]:
                del self.addresses[expired]
        self.addresses[p["address"]] = self.addresses.get(p["address"], 0) + 1
        self.messages.append(p)
        return True

    def view(self):
        top = sorted(self.addresses.items(), key=lambda kv: -kv[1])[:8]
        return {"kind": self.kind, "count": self.count, "messages": list(self.messages)[-120:][::-1],
                "top_addresses": [{"address": a, "n": n} for a, n in top], "started": self.started}


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
    default_mhz = 433.92
    MAX_DEVICES = 60

    def __init__(self, clock=time.time):
        self.clock = clock
        self.devices = {}
        self.ignored = 0
        self.started = clock()
        self.frames = 0

    def command(self, mhz, gain=40):
        return shell_pipe(["rtl_433", "-f", f"{mhz}M", "-g", str(gain), "-F", "json", "-M", "level"])

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


JOBS = {"pager": PagerJob, "sensors": SensorJob}
PRESETS = {"pager": PAGER_PRESETS, "sensors": SENSOR_PRESETS}


def shell_pipe(*commands):
    """Join argv lists with pipes; the runner drains bounded diagnostics into memory."""
    parts = [" ".join(shlex.quote(a) for a in argv) for argv in commands]
    return " | ".join(parts)
