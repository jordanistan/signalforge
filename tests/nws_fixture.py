"""Canned api.weather.gov documents for tests (shape copied from live responses)."""
import copy

API = "https://api.weather.gov"


def period(name, temp, short, day=True, pop=None, wind="5 mph", direction="S"):
    return {"name": name, "startTime": "2026-09-21T06:00:00-05:00", "temperature": temp, "temperatureUnit": "F", "isDaytime": day,
            "shortForecast": short, "detailedForecast": f"{short}. High near {temp}.", "windSpeed": wind, "windDirection": direction,
            "probabilityOfPrecipitation": {"value": pop}}


POINTS = {"properties": {"gridId": "EWX", "radarStation": "KGRK", "forecast": f"{API}/gridpoints/EWX/156,91/forecast",
                         "forecastHourly": f"{API}/gridpoints/EWX/156,91/forecast/hourly",
                         "observationStations": f"{API}/gridpoints/EWX/156,91/stations",
                         "relativeLocation": {"properties": {"city": "Austin", "state": "TX"}}}}
FORECAST = {"properties": {"periods": [period("Today", 95, "Sunny", True, 10), period("Tonight", 76, "Mostly Clear", False, None),
                                       period("Tuesday", 93, "Chance Thunderstorms", True, 40)]}}
HOURLY = {"properties": {"periods": [period(f"h{i}", 80 + i, "Clear", True, i * 3) for i in range(24)]}}
STATIONS = {"features": [{"properties": {"stationIdentifier": "KATT", "name": "Austin Camp Mabry"}}]}
OBS = {"properties": {"timestamp": "2026-09-21T04:51:00+00:00", "textDescription": "Clear",
                      "temperature": {"value": 26.7}, "dewpoint": {"value": 20.0}, "relativeHumidity": {"value": 69.0},
                      "windSpeed": {"value": 10.0}, "windGust": {"value": None}, "windDirection": {"value": 170},
                      "barometricPressure": {"value": 101300}, "visibility": {"value": 16090}, "heatIndex": {"value": 28.3},
                      "windChill": {"value": None}}}
ALERT = {"id": "urn:alert:1", "properties": {"id": "urn:alert:1", "event": "Flash Flood Warning", "severity": "Severe",
                                             "urgency": "Immediate", "headline": "Flash Flood Warning until 9 PM CDT",
                                             "areaDesc": "Travis, TX", "expires": "2026-09-21T21:00:00-05:00",
                                             "description": "Heavy rain is causing flooding.\n\nMove to higher ground.",
                                             "instruction": "Turn around, don't drown."}}


class FakeNws:
    """fetch_json stand-in. `alerts` is a mutable list of alert features; `fail` makes every call raise."""

    def __init__(self, alerts=None):
        self.alerts = alerts if alerts is not None else []
        self.fail = False
        self.calls = []

    async def __call__(self, url):
        self.calls.append(url)
        if self.fail:
            raise OSError("network down")
        if "/points/" in url:
            return copy.deepcopy(POINTS)
        if url.endswith("/forecast/hourly"):
            return copy.deepcopy(HOURLY)
        if url.endswith("/forecast"):
            return copy.deepcopy(FORECAST)
        if url.endswith("/stations"):
            return copy.deepcopy(STATIONS)
        if "/observations/latest" in url:
            return copy.deepcopy(OBS)
        if "/alerts/active" in url:
            return {"features": copy.deepcopy(self.alerts)}
        raise AssertionError(url)
