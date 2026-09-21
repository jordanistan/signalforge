#!/usr/bin/env python3
"""Weather for the web GUI from the US National Weather Service API (api.weather.gov): current observation,
7-day and hourly forecast, active alerts and the radar station. No key needed; NWS asks for a contact User-Agent.

Everything that comes back over the internet is treated as untrusted text: it is trimmed, length-limited and only
ever shown with textContent in the browser. A failed refresh keeps the last good data and records the error.
"""
import asyncio
import logging
import math
import re
import time
from datetime import datetime
from urllib.parse import urlsplit

import aiohttp
from brand import USER_AGENT

API = "https://api.weather.gov"
REFRESH_SECS = 600
ALERT_RANK = {"Extreme": 4, "Severe": 3, "Moderate": 2, "Minor": 1, "Unknown": 0}
SECTIONS = {"forecast": "Forecast", "hourly": "Hourly forecast", "alerts": "Weather alerts", "obs": "Current observation"}
OBS_MAX_AGE_SECS = 3 * 3600
LOG = logging.getLogger(__name__)


def txt(v, limit=200):
    s = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", str(v if v is not None else ""))
    return re.sub(r"[ \t]+", " ", s).strip()[:limit]


def num(v):
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        return None
    try:
        value = float(v)
    except (OverflowError, ValueError):
        return None
    return value if math.isfinite(value) else None


def mapping(v):
    return v if isinstance(v, dict) else {}


def properties(data):
    props = mapping(data).get("properties")
    if not isinstance(props, dict):
        raise ValueError("Invalid NWS response")
    return props


def location_key(loc):
    lat, lon = loc
    lat, lon = num(lat), num(lon)
    if lat is None or lon is None or not -90 <= lat <= 90 or not -180 <= lon <= 180:
        raise ValueError("Invalid weather location")
    return round(lat, 4), round(lon, 4)


def observation_time(obs):
    try:
        stamp = datetime.fromisoformat(obs["time"].replace("Z", "+00:00"))
        return stamp.timestamp() if stamp.tzinfo else None
    except (KeyError, TypeError, ValueError, OverflowError):
        return None


def c_to_f(c):
    return None if c is None else round(c * 9 / 5 + 32)


def kmh_to_mph(k):
    return None if k is None else round(k * 0.621371)


def compass(deg):
    if deg is None:
        return None
    return ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"][round(deg / 22.5) % 16]


def parse_observation(props, station_name=""):
    """NWS latest-observation `properties` -> compact dict in US units."""
    props = mapping(props)
    v = lambda k: num(mapping(props.get(k)).get("value"))  # noqa: E731
    pa = v("barometricPressure")
    vis = v("visibility")
    return {"station": txt(station_name, 80), "time": txt(props.get("timestamp"), 40), "text": txt(props.get("textDescription"), 60),
            "temp_f": c_to_f(v("temperature")), "dew_f": c_to_f(v("dewpoint")),
            "humidity": None if v("relativeHumidity") is None else round(v("relativeHumidity")),
            "wind_mph": kmh_to_mph(v("windSpeed")), "gust_mph": kmh_to_mph(v("windGust")),
            "wind_dir": compass(v("windDirection")), "wind_deg": v("windDirection"),
            "pressure_inhg": None if pa is None else round(pa * 0.0002953, 2),
            "visibility_mi": None if vis is None else round(vis / 1609.34, 1),
            "feels_f": c_to_f(v("heatIndex") if v("heatIndex") is not None else v("windChill"))}


def parse_periods(props, limit):
    periods = mapping(props).get("periods")
    if not isinstance(periods, list) or not periods:
        raise ValueError("Missing forecast periods")
    out = []
    for p in periods[:limit]:
        if not isinstance(p, dict):
            continue
        pop = num(mapping(p.get("probabilityOfPrecipitation")).get("value"))
        out.append({"name": txt(p.get("name"), 40), "start": txt(p.get("startTime"), 40), "temp": num(p.get("temperature")),
                    "unit": txt(p.get("temperatureUnit"), 2) or "F", "day": bool(p.get("isDaytime")),
                    "short": txt(p.get("shortForecast"), 80), "detail": txt(p.get("detailedForecast"), 400),
                    "wind": txt(f"{p.get('windSpeed', '')} {p.get('windDirection', '')}", 30),
                    "pop": None if pop is None or not 0 <= pop <= 100 else round(pop)})
    if not out:
        raise ValueError("Missing forecast periods")
    return out


def parse_alerts(data):
    features = mapping(data).get("features")
    if not isinstance(features, list):
        raise ValueError("Missing alert collection")
    out = []
    for f in features[:200]:
        if not isinstance(f, dict) or not isinstance(f.get("properties"), dict):
            raise ValueError("Invalid alert record")
        p = f["properties"]
        out.append({"id": txt(p.get("id") or f.get("id"), 120), "event": txt(p.get("event"), 80),
                    "severity": txt(p.get("severity"), 12) or "Unknown", "urgency": txt(p.get("urgency"), 16),
                    "headline": txt(p.get("headline"), 240), "area": txt(p.get("areaDesc"), 200),
                    "expires": txt(p.get("expires") or p.get("ends"), 40), "sent": txt(p.get("sent"), 40),
                    "description": txt(p.get("description"), 900), "instruction": txt(p.get("instruction"), 500)})
    out.sort(key=lambda a: -ALERT_RANK.get(a["severity"], 0))
    return out


class WeatherService:
    """Fetches NWS data for the radio's position (or --home) and pushes it to the browsers."""

    def __init__(self, broadcast, location, on_alert=None, fetch_json=None, refresh_secs=REFRESH_SECS, clock=time.time):
        self.broadcast, self.location, self.on_alert = broadcast, location, on_alert
        self._fetch = fetch_json or self._http_json
        self.refresh_secs, self.clock = refresh_secs, clock
        self.data = None
        self.error = None
        self.updated = None
        self._task = None
        self._session = None
        self._known_alerts = None
        self._alert_location = None
        self._lock = asyncio.Lock()

    async def _http_json(self, url):
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.netloc != "api.weather.gov":
            raise ValueError("Invalid NWS service URL")
        if self._session is None:
            self._session = aiohttp.ClientSession(headers={"User-Agent": USER_AGENT, "Accept": "application/geo+json"},
                                                  timeout=aiohttp.ClientTimeout(total=20))
        async with self._session.get(url, allow_redirects=False) as r:
            r.raise_for_status()
            if not 200 <= r.status < 300:
                raise ValueError("Unexpected NWS redirect")
            return await r.json(content_type=None)

    def view(self):
        return {"data": self.data, "error": self.error, "updated": self.updated}

    async def start(self):
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def stop(self):
        t, self._task = self._task, None
        if t:
            t.cancel()
            await asyncio.gather(t, return_exceptions=True)
        if self._session:
            await self._session.close()
            self._session = None

    async def _loop(self):
        while True:
            await self.refresh()
            await asyncio.sleep(self.refresh_secs)

    async def refresh(self):
        async with self._lock:
            try:
                loc = location_key(self.location())
                self.data = await self._collect(loc)
                errors = self.data["errors"]
                self.error = " ".join(errors.values()) or None
                if any(status == "current" for status in self.data["status"].values()):
                    self.updated = self.clock()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # network down, NWS hiccup: keep showing the last good data
                self.error = f"Weather service unavailable ({type(e).__name__}). Showing the last data." if self.data else \
                    f"Weather service unavailable ({type(e).__name__}); check the internet connection."
                if self.data:
                    self.data = {**self.data, "status": {key: "stale" if self.data["status"].get(key) != "unavailable"
                                                       else "unavailable" for key in SECTIONS},
                                 "errors": {key: f"{label} could not be refreshed." for key, label in SECTIONS.items()}}
            await self.broadcast({"type": "weather", "weather": self.view()})
            if self.data and self.data["status"].get("alerts") == "current":
                loc = (self.data["lat"], self.data["lon"])
                if loc != self._alert_location:
                    self._known_alerts = None
                    self._alert_location = loc
                await self._announce(self.data["alerts"])

    async def _announce(self, alerts):
        ids = {a["id"] for a in alerts}
        if self._known_alerts is not None and self.on_alert:
            for a in alerts:
                if a["id"] not in self._known_alerts:
                    try:
                        self.on_alert(a)
                    except Exception:
                        LOG.exception("Weather alert callback failed")
        self._known_alerts = ids

    async def _collect(self, loc):
        lat, lon = loc
        pt = properties(await self._fetch(f"{API}/points/{lat:.4f},{lon:.4f}"))
        rel = mapping(mapping(pt.get("relativeLocation")).get("properties"))

        async def forecast(key, limit):
            return parse_periods(properties(await self._fetch(pt[key])), limit)

        async def alerts():
            return parse_alerts(await self._fetch(f"{API}/alerts/active?point={lat:.4f},{lon:.4f}"))

        results = await asyncio.gather(forecast("forecast", 14), forecast("forecastHourly", 24), alerts(),
                                       self._observation(pt.get("observationStations")), return_exceptions=True)
        radar = txt(pt.get("radarStation"), 8)
        data = {"place": txt(", ".join(str(rel[k]) for k in ("city", "state") if rel.get(k)), 80) or f"{lat:.4f}, {lon:.4f}",
                "lat": lat, "lon": lon, "office": txt(pt.get("gridId"), 8),
                "radar": radar if re.fullmatch(r"[A-Z0-9]{3,5}", radar) else "",
                "status": {}, "errors": {}, "section_updated": {}}
        previous = self.data if self.data and (self.data["lat"], self.data["lon"]) == loc else None
        for (key, label), result in zip(SECTIONS.items(), results):
            if isinstance(result, BaseException):
                if isinstance(result, asyncio.CancelledError):
                    raise result
                result = None
            if result is None:
                usable = previous and previous["status"].get(key) != "unavailable"
                data[key] = previous[key] if usable else (None if key == "obs" else [])
                data["status"][key] = "stale" if usable else "unavailable"
                data["errors"][key] = f"{label} unavailable." + (" Showing the last data." if usable else "")
                data["section_updated"][key] = previous.get("section_updated", {}).get(key) if usable else None
            else:
                data[key], data["status"][key] = result, "current"
                data["section_updated"][key] = self.clock()
                if key == "obs":
                    stamp = observation_time(result)
                    if stamp is None or not -300 <= self.clock() - stamp <= OBS_MAX_AGE_SECS:
                        data["status"][key] = "stale"
                        data["errors"][key] = "Latest observation is old or has no valid observation time."
        return data

    async def _observation(self, stations_url):
        if not stations_url:
            return None
        st = await self._fetch(stations_url)
        features = mapping(st).get("features")
        if not isinstance(features, list):
            return None
        fallback = None
        for f in features[:3]:  # the nearest stations; the first may be down
            props = mapping(mapping(f).get("properties"))
            sid = props.get("stationIdentifier")
            if not isinstance(sid, str) or not re.fullmatch(r"[A-Za-z0-9]{3,8}", sid):
                continue
            try:
                o = await self._fetch(f"{API}/stations/{sid}/observations/latest")
                obs = parse_observation(properties(o), f"{props.get('name') or sid} ({sid})")
            except Exception:
                continue
            if obs["temp_f"] is not None:
                stamp = observation_time(obs)
                if stamp is not None and -300 <= self.clock() - stamp <= OBS_MAX_AGE_SECS:
                    return obs
                if fallback is None or (stamp or 0) > (observation_time(fallback) or 0):
                    fallback = obs
        return fallback
