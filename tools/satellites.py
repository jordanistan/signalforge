#!/usr/bin/env python3
"""Satellite tracking for the web GUI: where the ISS, weather and amateur satellites are now, and when they pass
over you. Pure calculation from public orbital elements (TLEs from CelesTrak), cached on disk for 12 hours so it
keeps working offline. Nothing is transmitted; listening to a downlink uses the normal SDR "listen" feature.
"""
import asyncio
import math
import os
import re
import tempfile
import time
from datetime import datetime, timezone

import aiohttp
from brand import USER_AGENT
from skyfield.api import EarthSatellite, load, wgs84
from sgp4.io import verify_checksum

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TLE_DIR = os.path.join(ROOT, "data", "tle")
GROUPS = ("stations", "weather", "amateur")
CELESTRAK = "https://celestrak.org/NORAD/elements/gp.php?GROUP={}&FORMAT=tle"
CACHE_SECS = 12 * 3600
WANTED = re.compile(r"^(ISS \(ZARYA\)|CSS \(TIANHE\)|NOAA 1[589]|METEOR-M2 ?[34]|SO-50|AO-9[15]|PO-101|RS-44|FO-29|AO-7)(?![A-Za-z0-9])")
# Catalog names vary between feeds (for example SAUDISAT 1C, not SO-50).
AMATEUR_NAMES = {"07530": "AO-7", "24278": "FO-29", "27607": "SO-50", "43017": "AO-91",
                 "43678": "PO-101", "43770": "AO-95", "44909": "RS-44"}
DOWNLINKS = [  # (name pattern, MHz, what it is): typical downlinks that are audible with a simple antenna
    (re.compile(r"^ISS"), 145.800, "voice / SSTV pictures (FM)"),
    (re.compile(r"^NOAA 15"), 137.620, "weather-picture signal (APT)"),
    (re.compile(r"^NOAA 18"), 137.9125, "weather-picture signal (APT)"),
    (re.compile(r"^NOAA 19"), 137.100, "weather-picture signal (APT)"),
    (re.compile(r"^METEOR-M2"), 137.900, "weather pictures (LRPT, digital)"),
    (re.compile(r"^SO-50"), 436.795, "amateur FM repeater"),
]


def downlink_for(name):
    for pat, mhz, what in DOWNLINKS:
        if pat.match(name):
            return {"mhz": mhz, "what": what}
    return None


def _tle_records(text):
    """Find intact records without letting a comment or truncated record shift the entire feed."""
    lines = [l.rstrip() for l in text.splitlines() if l.strip()]
    for i in range(len(lines) - 2):
        name, l1, l2 = lines[i].strip(), lines[i + 1], lines[i + 2]
        if name.startswith("0 "):
            name = name[2:].strip()
        if not name or name.startswith(("1 ", "2 ")) or not l1.startswith("1 ") or not l2.startswith("2 "):
            continue
        if len(l1) != 69 or len(l2) != 69 or l1[2:7] != l2[2:7] or not l1[-1].isdigit() or not l2[-1].isdigit():
            continue
        try:
            verify_checksum(l1, l2)
            motion, inclination = float(l2[52:63]), float(l2[8:16])
            epoch = float(l1[20:32])
            if not (0 < motion < 20 and 0 <= inclination <= 180 and 1 <= epoch < 367 and l2[26:33].isdigit()):
                continue
        except (ValueError, OverflowError):
            continue
        yield AMATEUR_NAMES.get(l1[2:7], name[:80]), l1, l2


def parse_tle(text):
    """Validated 3-line TLE text -> {name: (line1, line2)} for the satellites we track."""
    return {name: (l1, l2) for name, l1, l2 in _tle_records(text) if WANTED.match(name)}


class SatTracker:
    def __init__(self, location, tle_dir=TLE_DIR, fetch_text=None, clock=time.time):
        self.location, self.tle_dir, self.clock = location, tle_dir, clock
        self._fetch = fetch_text or self._http_text
        self.ts = load.timescale(builtin=True)  # no downloads
        self.sats = {}
        self.error = None
        self.tle_age = None
        self._passes_at = None
        self._passes_location = None
        self._passes = []
        self._loaded_at = None
        self._tle_age_at = None
        self._compute_errors = []
        self._lock = asyncio.Lock()
        self._snapshot_lock = asyncio.Lock()

    async def _http_text(self, url):
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=25), headers={"User-Agent": USER_AGENT}) as s:
            async with s.get(url) as r:
                r.raise_for_status()
                return await r.text()

    async def load(self):
        """Read cached TLEs, refreshing any group older than 12 h. Failing to refresh is fine if a cache exists."""
        async with self._lock:
            found, oldest_age, errors = {}, None, []
            for g in GROUPS:
                path = os.path.join(self.tle_dir, f"{g}.tle")
                age, cached = None, ""
                try:
                    with open(path, encoding="utf-8", errors="replace") as f:
                        cached = f.read()
                    age = max(0, self.clock() - os.path.getmtime(path))
                except OSError:
                    pass
                records = list(_tle_records(cached))
                if age is None or age > CACHE_SECS or not records:
                    try:
                        text = await self._fetch(CELESTRAK.format(g))
                        downloaded = list(_tle_records(text))
                        if not downloaded:
                            raise ValueError("No valid orbital records")
                        # Validate before replacing a useful cache; replacement is atomic.
                        records, age = downloaded, 0
                        tmp_path = None
                        try:
                            os.makedirs(self.tle_dir, exist_ok=True)
                            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.tle_dir, delete=False) as f:
                                tmp_path = f.name
                                f.write("".join(f"{n}\n{l1}\n{l2}\n" for n, l1, l2 in downloaded))
                            os.replace(tmp_path, path)
                        except OSError:
                            errors.append(f"Could not save {g} orbital data for offline use.")
                        finally:
                            if tmp_path and os.path.exists(tmp_path):
                                os.unlink(tmp_path)
                    except Exception as e:
                        errors.append(f"Could not refresh {g} satellite data ({type(e).__name__}).")
                selected = {n: (l1, l2) for n, l1, l2 in records if WANTED.match(n)}
                if selected and age is not None:
                    oldest_age = age if oldest_age is None else max(oldest_age, age)
                for name, (l1, l2) in selected.items():
                    try:
                        sat = EarthSatellite(l1, l2, name, self.ts)
                        if not math.isfinite(float(sat.epoch.tt)):
                            raise ValueError("Invalid epoch")
                    except (ValueError, OverflowError):
                        errors.append(f"Invalid orbital data for {name}.")
                        continue
                    if name not in found or sat.epoch.tt > found[name].epoch.tt:
                        found[name] = sat
            if found:
                self.sats = found
                self.tle_age = oldest_age
                self._tle_age_at = self.clock()
            self.error = " ".join(errors) or None
            if self.sats and errors:
                self.error += " Using available orbital data."
            elif not self.sats:
                self.error = (self.error or "") + " No satellite data yet (needs internet once)."
            self._loaded_at = self.clock()
            self._passes_at = None

    def _location(self):
        lat, lon = self.location()
        if isinstance(lat, bool) or isinstance(lon, bool) or not -90 <= lat <= 90 or not -180 <= lon <= 180:
            raise ValueError("Invalid satellite observer location")
        return round(float(lat), 4), round(float(lon), 4)

    def _compute(self, now, want_passes, hours=24, min_el=10.0, location=None):
        topos = wgs84.latlon(*(location or self._location()))
        t = self.ts.from_datetime(datetime.fromtimestamp(now, timezone.utc))
        rows, errors = [], []
        for name, sat in self.sats.items():
            try:
                geo = sat.at(t)
                sub = wgs84.geographic_position_of(geo)
                alt, az, dist = (sat - topos).at(t).altaz()
                values = (sub.latitude.degrees, sub.longitude.degrees, sub.elevation.km, alt.degrees, az.degrees, dist.km)
                if geo.message or not all(math.isfinite(float(value)) for value in values):
                    raise ValueError("Invalid propagated orbit")
                track = []
                for m in range(-30, 61, 3):  # ground track: 30 min back, 60 min ahead
                    tt = self.ts.from_datetime(datetime.fromtimestamp(now + m * 60, timezone.utc))
                    p = wgs84.geographic_position_of(sat.at(tt))
                    lat, lon = float(p.latitude.degrees), float(p.longitude.degrees)
                    if not math.isfinite(lat) or not math.isfinite(lon):
                        raise ValueError("Invalid ground track")
                    track.append([round(lat, 2), round(lon, 2)])
                rows.append({"name": name, "lat": round(float(sub.latitude.degrees), 3), "lon": round(float(sub.longitude.degrees), 3),
                             "alt_km": round(float(sub.elevation.km)), "el": round(float(alt.degrees), 1), "az": round(float(az.degrees)) % 360,
                             "range_km": round(float(dist.km)), "above": bool(alt.degrees > 0), "track": track,
                             "downlink": downlink_for(name), "tle_epoch_days": round(float(t - sat.epoch), 1)})
            except (ValueError, OverflowError, ArithmeticError):
                errors.append(name)
        passes = self._passes
        if want_passes:
            passes = []
            for name, sat in self.sats.items():
                if name in errors:
                    continue
                try:
                    passes.extend(self._find_passes(name, sat, topos, now, hours, min_el))
                except Exception:
                    errors.append(name)
            passes.sort(key=lambda p: p["rise"])
        self._compute_errors = errors
        return rows, passes

    def _find_passes(self, name, sat, topos, now, hours, min_el):
        # Search beyond both edges so passes underway (even after their peak) and
        # passes ending just beyond the display window retain real rise/peak/set times.
        padding = min(24 * 3600, max(2 * 3600, 2 * math.pi / sat.model.no_kozai * 60))
        t0 = self.ts.from_datetime(datetime.fromtimestamp(now - padding, timezone.utc))
        end = now + hours * 3600
        t1 = self.ts.from_datetime(datetime.fromtimestamp(end + padding, timezone.utc))
        times, events = sat.find_events(topos, t0, t1, altitude_degrees=min_el)
        out, cur = [], None
        for tm, ev in zip(times, events):
            a, az, _d = (sat - topos).at(tm).altaz()
            stamp, elevation, bearing = tm.utc_datetime().timestamp(), float(a.degrees), round(float(az.degrees)) % 360
            if not math.isfinite(elevation):
                raise ValueError("Invalid pass elevation")
            if ev == 0:
                cur = {"name": name, "rise": stamp, "rise_az": bearing, "max_el": elevation, "peak": stamp}
            elif ev == 1 and cur:
                # Skyfield can return multiple culminations between one rise and set.
                if elevation > cur["max_el"]:
                    cur["max_el"], cur["peak"] = elevation, stamp
            elif ev == 2 and cur:
                cur.update({"set": stamp, "set_az": bearing, "downlink": downlink_for(name)})
                cur["max_el"] = round(cur["max_el"])
                if cur["set"] > now and cur["rise"] < end:
                    out.append(cur)
                cur = None
        return out

    async def snapshot(self):
        async with self._snapshot_lock:
            now = self.clock()
            interval = 3600 if self.sats else 300
            if self._loaded_at is None or not 0 <= now - self._loaded_at < interval:
                await self.load()
            now, location = self.clock(), self._location()
            want = self._passes_at is None or not 0 <= now - self._passes_at < 900 or self._passes_location != location
            rows, passes = await asyncio.to_thread(self._compute, now, want, location=location)
            if want:
                self._passes, self._passes_at, self._passes_location = passes, now, location
            age = None if self.tle_age is None else self.tle_age + max(0, now - self._tle_age_at)
            old_epochs = any(abs(row["tle_epoch_days"]) > 14 for row in rows)
            errors = [self.error] if self.error else []
            if self._compute_errors:
                errors.append("Could not calculate all positions or passes: " + ", ".join(self._compute_errors) + ".")
            if old_epochs:
                errors.append("Some orbital elements are over 14 days from their epoch; predictions may be inaccurate.")
            available = {row["name"] for row in rows}
            return {"type": "sats", "now": now, "location": list(location), "error": " ".join(errors) or None,
                    "tle_age_h": None if age is None else round(age / 3600, 1),
                    "tle_stale": old_epochs or (age is not None and age > CACHE_SECS), "sats": rows,
                    "passes": [p for p in self._passes if p["set"] > now and p["name"] in available][:40]}
