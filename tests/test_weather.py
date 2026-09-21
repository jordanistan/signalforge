import asyncio
import copy
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import weather as wx  # noqa: E402

NOW = datetime(2026, 9, 21, 12, tzinfo=timezone.utc).timestamp()
HOME = (30.2672, -97.7431)
POINT = f"{wx.API}/points/30.2672,-97.7431"
ALERTS = f"{wx.API}/alerts/active?point=30.2672,-97.7431"
FORECAST = f"{wx.API}/gridpoints/EWX/1,1/forecast"
HOURLY = FORECAST + "/hourly"
STATIONS = f"{wx.API}/gridpoints/EWX/1,1/stations"
OBS = f"{wx.API}/stations/KATT/observations/latest"


def alert(identifier="warning-1", severity="Severe"):
    return {"properties": {"id": identifier, "event": "Thunderstorm Warning", "severity": severity,
                           "headline": "A storm is approaching", "expires": "2026-09-21T14:00:00Z"}}


def feeds():
    return {
        POINT: {"properties": {"forecast": FORECAST, "forecastHourly": HOURLY, "observationStations": STATIONS,
                               "radarStation": "KEWX", "gridId": "EWX",
                               "relativeLocation": {"properties": {"city": "Austin", "state": "TX"}}}},
        FORECAST: {"properties": {"periods": [{"name": "Today", "temperature": 86, "temperatureUnit": "F",
                                               "probabilityOfPrecipitation": {"value": 40}}]}},
        HOURLY: {"properties": {"periods": [{"name": "", "temperature": 85, "temperatureUnit": "F"}]}},
        ALERTS: {"features": [alert()]},
        STATIONS: {"features": [{"properties": {"stationIdentifier": "KATT", "name": "Camp Mabry"}}]},
        OBS: {"properties": {"timestamp": "2026-09-21T11:30:00Z", "temperature": {"value": 30},
                              "windSpeed": {"value": 16.1}}},
    }


def service(payloads=None, location=lambda: HOME, on_alert=None, clock=lambda: NOW):
    payloads = feeds() if payloads is None else payloads
    events, calls = [], []

    async def fetch(url):
        calls.append(url)
        result = payloads[url]
        if isinstance(result, BaseException):
            raise result
        return copy.deepcopy(result)

    async def broadcast(event):
        events.append(copy.deepcopy(event))

    return wx.WeatherService(broadcast, location, on_alert=on_alert, fetch_json=fetch, clock=clock), payloads, events, calls


def test_observation_converts_units_and_rejects_nonfinite_or_malformed_fields():
    props = {"temperature": {"value": 20}, "dewpoint": {"value": 10}, "windSpeed": {"value": 16.1},
             "windDirection": {"value": 90}, "barometricPressure": {"value": 101325}, "visibility": {"value": 16093.4},
             "relativeHumidity": {"value": float("nan")}, "windGust": {"value": float("inf")}, "heatIndex": "bad"}
    obs = wx.parse_observation(props, " A\x00 Station ")
    assert (obs["temp_f"], obs["dew_f"], obs["wind_mph"], obs["wind_dir"]) == (68, 50, 10, "E")
    assert (obs["pressure_inhg"], obs["visibility_mi"]) == (29.92, 10)
    assert obs["humidity"] is None and obs["gust_mph"] is None and obs["feels_f"] is None
    assert obs["station"] == "A Station"
    json.dumps(obs, allow_nan=False)


@pytest.mark.parametrize("value", [True, False, "32", float("nan"), float("inf"), 10 ** 1000, None])
def test_num_rejects_values_that_are_not_finite_numbers(value):
    assert wx.num(value) is None


def test_forecast_sanitizes_text_and_invalid_precipitation():
    out = wx.parse_periods({"periods": [None, {"name": "x" * 100, "temperature": float("nan"),
                                               "probabilityOfPrecipitation": {"value": "bad"}}]}, 14)
    assert len(out) == 1 and len(out[0]["name"]) == 40
    assert out[0]["temp"] is None and out[0]["pop"] is None
    with pytest.raises(ValueError):
        wx.parse_periods({"periods": []}, 14)


def test_alert_feed_must_be_a_collection_and_sorts_severity():
    assert [a["severity"] for a in wx.parse_alerts({"features": [alert("m", "Minor"), alert("s"), alert("e", "Extreme")]})] == [
        "Extreme", "Severe", "Minor"]
    assert wx.parse_alerts({"features": []}) == []
    for malformed in ({}, {"features": None}, {"features": [None]}):
        with pytest.raises(ValueError):
            wx.parse_alerts(malformed)


async def test_refresh_publishes_complete_weather_without_announcing_existing_alerts():
    announcements = []
    svc, _, events, calls = service(on_alert=announcements.append)
    await svc.refresh()
    assert svc.error is None and svc.updated == NOW
    assert svc.data["place"] == "Austin, TX" and svc.data["obs"]["temp_f"] == 86
    assert set(svc.data["status"].values()) == {"current"}
    assert svc.data["section_updated"]["alerts"] == NOW
    assert not announcements and len(calls) == 6
    assert events[-1]["weather"] == svc.view()


async def test_failed_alert_refresh_keeps_warning_and_does_not_reannounce_recovery():
    announcements = []
    svc, payloads, _, _ = service(on_alert=announcements.append)
    await svc.refresh()
    payloads[ALERTS] = TimeoutError()
    await svc.refresh()
    assert svc.data["alerts"][0]["id"] == "warning-1"
    assert svc.data["status"]["alerts"] == "stale"
    assert "Weather alerts unavailable" in svc.error
    payloads[ALERTS] = {"features": [alert(), alert("warning-2")]}
    await svc.refresh()
    assert [a["id"] for a in announcements] == ["warning-2"]
    assert svc.data["status"]["alerts"] == "current" and svc.error is None


async def test_initial_alert_failure_is_unavailable_and_later_success_establishes_baseline():
    payloads = feeds()
    payloads[ALERTS] = {"detail": "An upstream error, not an empty alert collection"}
    announcements = []
    svc, _, _, _ = service(payloads, on_alert=announcements.append)
    await svc.refresh()
    assert svc.data["alerts"] == [] and svc.data["status"]["alerts"] == "unavailable"
    payloads[ALERTS] = {"features": [alert()]}
    await svc.refresh()
    assert not announcements


async def test_forecast_failure_does_not_hide_new_alerts_or_hourly_data():
    svc, payloads, _, _ = service()
    payloads[FORECAST] = TimeoutError()
    await svc.refresh()
    assert svc.data["status"]["forecast"] == "unavailable"
    assert svc.data["alerts"] and svc.data["hourly"]
    assert svc.data["status"]["alerts"] == "current"


async def test_total_failure_retains_data_and_original_timestamp_but_marks_all_sections_stale():
    stamp = [NOW]
    svc, payloads, _, _ = service(clock=lambda: stamp[0])
    await svc.refresh()
    stamp[0] += 600
    payloads[POINT] = OSError("offline")
    await svc.refresh()
    assert svc.updated == NOW and svc.data["forecast"][0]["temp"] == 86
    assert set(svc.data["status"].values()) == {"stale"}
    assert svc.data["section_updated"]["alerts"] == NOW


async def test_location_errors_are_reported_and_refresh_can_recover():
    loc = [None]
    svc, _, events, _ = service(location=lambda: loc[0])
    await svc.refresh()
    assert svc.error and svc.data is None and events
    loc[0] = HOME
    await svc.refresh()
    assert svc.error is None and svc.data


async def test_location_change_does_not_mix_previous_forecast_or_alert_baseline():
    loc = [HOME]
    announcements = []
    svc, payloads, _, _ = service(location=lambda: loc[0], on_alert=announcements.append)
    await svc.refresh()
    loc[0] = (40.0, -100.0)
    payloads[f"{wx.API}/points/40.0000,-100.0000"] = payloads[POINT]
    payloads[f"{wx.API}/alerts/active?point=40.0000,-100.0000"] = {"features": [alert("different-place")]}
    payloads[FORECAST] = TimeoutError()
    await svc.refresh()
    assert svc.data["forecast"] == [] and svc.data["status"]["forecast"] == "unavailable"
    assert svc.data["lat"] == 40.0 and not announcements


async def test_observation_tries_another_station_when_nearest_is_old():
    svc, payloads, _, _ = service()
    payloads[OBS]["properties"]["timestamp"] = "2026-09-20T00:00:00Z"
    payloads[STATIONS]["features"].append({"properties": {"stationIdentifier": "KAUS", "name": "Airport"}})
    payloads[f"{wx.API}/stations/KAUS/observations/latest"] = feeds()[OBS]
    await svc.refresh()
    assert "KAUS" in svc.data["obs"]["station"] and svc.data["status"]["obs"] == "current"


async def test_old_observation_remains_visible_as_stale():
    svc, payloads, _, _ = service()
    payloads[OBS]["properties"]["timestamp"] = "2026-09-20T00:00:00Z"
    await svc.refresh()
    assert svc.data["obs"]["temp_f"] == 86 and svc.data["status"]["obs"] == "stale"
    assert "observation" in svc.error


async def test_cancellation_does_not_become_a_weather_error():
    svc, payloads, _, _ = service()
    payloads[POINT] = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await svc.refresh()
    assert svc.error is None


async def test_nws_urls_cannot_redirect_fetching_to_other_hosts():
    svc, _, _, _ = service()
    for url in ("http://api.weather.gov/points/1,2", "https://example.org/", "https://api.weather.gov@127.0.0.1/"):
        with pytest.raises(ValueError):
            await svc._http_json(url)
    assert svc._session is None


async def test_broken_alert_callback_does_not_stop_refresh(caplog):
    def broken(_):
        raise RuntimeError("callback failure")

    svc, payloads, _, _ = service(on_alert=broken)
    await svc.refresh()
    payloads[ALERTS] = {"features": [alert("new")]}
    await svc.refresh()
    assert svc.error is None and "Weather alert callback failed" in caplog.text
