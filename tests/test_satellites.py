import asyncio
import json
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import satellites as sky  # noqa: E402

NOW = datetime(2026, 9, 21, 12, tzinfo=timezone.utc).timestamp()
HOME = (30.2672, -97.7431)
ISS = ("ISS (ZARYA)\n"
       "1 25544U 98067A   26263.78762384  .00007766  00000+0  14793-3 0  9996\n"
       "2 25544  51.6308 186.9475 0004819 163.3027 196.8121 15.49200337586572\n")
SO50 = ("SAUDISAT 1C (SO-50)\n"
        "1 27607U 02058C   26263.29660453  .00000587  00000+0  86947-4 0  9998\n"
        "2 27607  64.5524 194.6776 0071670 244.9843 114.3809 14.83220028278569\n")


def cache(tmp_path, text=ISS, age=0):
    for group in sky.GROUPS:
        path = tmp_path / f"{group}.tle"
        path.write_text(text)
        os.utime(path, (NOW - age, NOW - age))


def tracker(tmp_path, result=ISS, clock=lambda: NOW, location=lambda: HOME):
    calls = []

    async def fetch(url):
        calls.append(url)
        if isinstance(result, Exception):
            raise result
        return result

    return sky.SatTracker(location, tle_dir=tmp_path, fetch_text=fetch, clock=clock), calls


def test_parser_recovers_from_comments_truncation_and_zero_name_prefix():
    parsed = sky.parse_tle("# downloaded orbital data\nBROKEN RECORD\n1 short\n0 " + ISS + "\n" + SO50)
    assert set(parsed) == {"ISS (ZARYA)", "SO-50"}
    assert sky.downlink_for("SO-50")["mhz"] == 436.795


def test_parser_rejects_bad_checksums_mismatched_ids_and_truncated_lines():
    assert sky.parse_tle(ISS.replace("9996", "9997")) == {}
    assert sky.parse_tle(ISS.replace("2 25544", "2 25545")) == {}
    assert sky.parse_tle("ISS (ZARYA)\n1 25544\n2 25544\n") == {}
    assert sky.parse_tle("<html>rate limited</html>") == {}


async def test_fresh_cache_loads_without_network(tmp_path):
    cache(tmp_path, ISS + SO50)
    sat, calls = tracker(tmp_path, result=OSError("offline"))
    await sat.load()
    assert not calls and set(sat.sats) == {"ISS (ZARYA)", "SO-50"}
    assert sat.error is None and sat.tle_age == 0


async def test_refresh_failure_retains_cache_and_displays_warning(tmp_path):
    cache(tmp_path, age=sky.CACHE_SECS + 1)
    sat, calls = tracker(tmp_path, result=TimeoutError())
    await sat.load()
    assert sat.sats and len(calls) == 3
    assert "Could not refresh" in sat.error and "Using available" in sat.error
    assert (tmp_path / "stations.tle").read_text() == ISS


async def test_invalid_download_never_overwrites_good_cached_elements(tmp_path):
    cache(tmp_path, age=sky.CACHE_SECS + 1)
    sat, _ = tracker(tmp_path, result="ISS (ZARYA)\n1 broken\n2 broken\n")
    await sat.load()
    assert sat.sats and "ValueError" in sat.error
    assert (tmp_path / "stations.tle").read_text() == ISS


async def test_corrupt_fresh_cache_is_refreshed(tmp_path):
    cache(tmp_path, "bad gateway")
    sat, calls = tracker(tmp_path)
    await sat.load()
    assert len(calls) == 3 and "ISS (ZARYA)" in sat.sats
    assert sky.parse_tle((tmp_path / "stations.tle").read_text())


async def test_cache_save_failure_still_uses_downloaded_data(tmp_path, monkeypatch):
    def fail(*_):
        raise PermissionError("read only")

    monkeypatch.setattr(sky.os, "replace", fail)
    sat, _ = tracker(tmp_path)
    await sat.load()
    assert sat.sats and "offline use" in sat.error
    assert not list(tmp_path.iterdir())  # no abandoned temporary files


async def test_offline_no_cache_retries_after_five_minutes_not_each_snapshot(tmp_path):
    stamp = [NOW]
    sat, calls = tracker(tmp_path, result=OSError("offline"), clock=lambda: stamp[0])
    first = await sat.snapshot()
    assert first["sats"] == [] and "needs internet once" in first["error"]
    stamp[0] += 5
    await sat.snapshot()
    assert len(calls) == 3
    stamp[0] += 300
    await sat.snapshot()
    assert len(calls) == 6


async def test_snapshot_has_finite_positions_tracks_and_ordered_passes(tmp_path):
    cache(tmp_path, ISS + SO50)
    sat, calls = tracker(tmp_path)
    view = await sat.snapshot()
    assert not calls and len(view["sats"]) == 2
    assert view["error"] is None and view["tle_stale"] is False
    iss = next(row for row in view["sats"] if row["name"].startswith("ISS"))
    assert 300 < iss["alt_km"] < 500 and -90 <= iss["lat"] <= 90 and -180 <= iss["lon"] <= 180
    assert len(iss["track"]) == 31
    assert view["passes"] and view["passes"] == sorted(view["passes"], key=lambda p: p["rise"])
    for row in view["passes"]:
        assert row["rise"] < row["peak"] < row["set"] and row["set"] > NOW and row["max_el"] >= 10
    json.dumps(view, allow_nan=False)


async def test_pass_underway_keeps_its_original_peak_and_rise(tmp_path):
    cache(tmp_path)
    sat, _ = tracker(tmp_path)
    await sat.load()
    _, passes = sat._compute(NOW, True)
    original = passes[0]
    for stamp in ((original["rise"] + original["peak"]) / 2, (original["peak"] + original["set"]) / 2):
        _, current = sat._compute(stamp, True)
        ongoing = next(p for p in current if p["rise"] <= stamp < p["set"])
        assert ongoing["rise"] == pytest.approx(original["rise"], abs=2)
        assert ongoing["peak"] == pytest.approx(original["peak"], abs=2)
        assert ongoing["max_el"] == original["max_el"]


async def test_pass_starting_before_window_end_is_included_even_if_it_sets_after(tmp_path):
    cache(tmp_path)
    sat, _ = tracker(tmp_path)
    await sat.load()
    _, passes = sat._compute(NOW, True)
    original = passes[0]
    hours = ((original["rise"] + original["peak"]) / 2 - NOW) / 3600
    _, truncated = sat._compute(NOW, True, hours=hours)
    assert len(truncated) == 1
    assert truncated[0]["set"] == pytest.approx(original["set"], abs=2)


async def test_pass_cache_invalidates_for_changed_location_and_empty_results_are_cached(tmp_path, monkeypatch):
    cache(tmp_path)
    loc = [HOME]
    stamp = [NOW]
    sat, _ = tracker(tmp_path, location=lambda: loc[0], clock=lambda: stamp[0])
    flags = []

    def compute(now, want, **kwargs):
        flags.append((want, kwargs["location"]))
        return [], []

    monkeypatch.setattr(sat, "_compute", compute)
    await sat.snapshot()
    stamp[0] += 5
    await sat.snapshot()
    loc[0] = (40.0, -100.0)
    await sat.snapshot()
    assert [want for want, _ in flags] == [True, False, True]
    assert flags[-1][1] == loc[0]


async def test_cache_age_advances_between_loads_and_stale_epochs_are_disclosed(tmp_path):
    cache(tmp_path, age=3600)
    stamp = [NOW]
    sat, _ = tracker(tmp_path, clock=lambda: stamp[0])
    first = await sat.snapshot()
    stamp[0] += 1800
    later = await sat.snapshot()
    assert first["tle_age_h"] == 1 and later["tle_age_h"] == 1.5
    stamp[0] += 15 * 86400
    stale = await sat.snapshot()
    assert stale["tle_stale"] is True and "14 days" in stale["error"]


async def test_concurrent_snapshots_do_not_repeat_feed_requests(tmp_path):
    sat, calls = tracker(tmp_path, result=OSError("offline"))
    first, second = await asyncio.gather(sat.snapshot(), sat.snapshot())
    assert len(calls) == 3 and first["sats"] == second["sats"] == []


def test_multiple_culminations_keep_highest_peak(tmp_path):
    sat, _ = tracker(tmp_path)
    event_specs = [(NOW + 100, 0, 10), (NOW + 200, 1, 70), (NOW + 300, 1, 30), (NOW + 400, 2, 10)]
    event_times = [sat.ts.from_datetime(datetime.fromtimestamp(stamp, timezone.utc)) for stamp, _, _ in event_specs]

    class FakeOrbit:
        model = SimpleNamespace(no_kozai=2 * math.pi / 90)

        def find_events(self, *_args, **_kwargs):
            return event_times, [event for _, event, _ in event_specs]

        def __sub__(self, _):
            return self

        def at(self, tm):
            index = event_times.index(tm)
            return SimpleNamespace(altaz=lambda: (SimpleNamespace(degrees=event_specs[index][2]),
                                                  SimpleNamespace(degrees=120), None))

    result = sat._find_passes("SO-50", FakeOrbit(), None, NOW, 24, 10)
    assert len(result) == 1 and result[0]["max_el"] == 70 and result[0]["peak"] == NOW + 200


@pytest.mark.parametrize("loc", [(float("nan"), 10), (91, 0), (0, -181), (True, 1)])
def test_invalid_observer_coordinates_are_rejected(tmp_path, loc):
    sat, _ = tracker(tmp_path, location=lambda: loc)
    with pytest.raises(ValueError):
        sat._location()
