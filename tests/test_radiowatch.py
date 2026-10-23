import asyncio
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import radiowatch as rw  # noqa: E402

NOAA_LINE = "2026-09-20,22:00:00,162390000,162560000,2500.00,4," + ",".join(["-60.0"] * 68)


def csv_with(freqs_db, lo=162_390_000, hi=162_560_000, step=2500.0, n=68, sec=0):
    """One rtl_power sweep line (timestamp = `sec` seconds) with a few bins bumped to given dB values."""
    bins = [-60.0] * n
    for hz, db in freqs_db.items():
        bins[round((hz - lo) / step)] = db
    return f"2026-09-20,22:00:{sec:02d},{lo},{hi},{step},4," + ",".join(str(b) for b in bins)


def sweeps(*specs, **kw):
    """Several sweeps, one per spec dict, each one second after the previous (like rtl_power -i 1)."""
    return "\n".join(csv_with(spec, sec=i, **kw) for i, spec in enumerate(specs))


# ---- summarize --------------------------------------------------------------------------------
def test_summarize_empty_and_flat_input():
    assert rw.summarize("") == []
    assert rw.summarize(NOAA_LINE + "\n" + NOAA_LINE) == []  # nothing above the floor


def test_summarize_finds_a_peak_and_reports_duty_cycle():
    text = sweeps(*[{162_400_000: -30.0}] * 10)  # on in every sweep: duty 1.0
    out = rw.summarize(text)
    assert len(out) == 1 and out[0]["mhz"] == 162.4 and out[0]["duty"] == 1.0 and out[0]["peak_db"] == 30.0


def test_summarize_intermittent_signal_has_partial_duty():
    text = sweeps({162_400_000: -30.0}, {}, {162_400_000: -30.0}, {})
    out = rw.summarize(text)
    assert out[0]["duty"] == 0.5


def test_summarize_merges_adjacent_bins_into_one_channel():
    line = csv_with({162_400_000: -30.0, 162_402_500: -28.0, 162_405_000: -29.0})  # one carrier, 3 bins wide
    out = rw.summarize(line)
    assert len(out) == 1 and out[0]["peak_db"] == 32.0  # the strongest bin represents the channel


def test_summarize_keeps_far_apart_signals_separate_and_sorts_by_duty_then_peak():
    text = sweeps({162_400_000: -20.0, 162_500_000: -45.0}, {162_500_000: -45.0}, {162_500_000: -45.0},
                  {162_400_000: -20.0, 162_500_000: -45.0})  # strong one on half the time, weak one always
    out = rw.summarize(text)
    assert [c["mhz"] for c in out] == [162.5, 162.4]  # duty 1.0 ranks above duty 0.5 even though weaker


def test_summarize_respects_margin_and_top_limit():
    line = csv_with({162_400_000: -55.0})  # only 5 dB above the floor
    assert rw.summarize(line, margin=10.0) == []
    assert rw.summarize(line, margin=4.0) != []
    lo, step, n = 100_000_000, 25_000.0, 400  # a wider band so signals can sit far apart
    many = csv_with({lo + i * 200_000: -20.0 for i in range(30)}, lo=lo, hi=lo + 10_000_000, step=step, n=n)
    assert len(rw.summarize(many, top=100)) == 30
    assert len(rw.summarize(many, top=5)) == 5


def test_summarize_ignores_garbage_rows():
    assert rw.summarize("not,a,valid,row\n\n" + csv_with({162_400_000: -30.0})) == [
        {"mhz": 162.4, "duty": 1.0, "peak_db": 30.0}]


# ---- labels, validation, error text -------------------------------------------------------------
def test_label_for_known_and_unknown_frequencies():
    assert rw.label_for(162.4) == "NOAA Weather Radio"
    assert rw.label_for(146.94) == "2 m repeater 146.94"
    assert rw.label_for(146.52, "2 m ham") == "2 m ham"  # falls back to the band name


def test_valid_mhz_range_and_bad_input():
    assert rw.valid_mhz("146.52") == 146.52
    assert rw.valid_mhz(146.520001) == 146.52  # rounded
    assert rw.valid_mhz("0.1") is None and rw.valid_mhz("9999") is None
    assert rw.valid_mhz("nan") is None and rw.valid_mhz(None) is None and rw.valid_mhz("abc") is None


@pytest.mark.parametrize("stderr,fragment", [
    ("usb_claim_interface error -6", "in use by another program"),
    ("Resource busy", "in use by another program"),
    ("No supported devices found.", "No SDR dongle found"),
    ("usb_open error access denied", "permission"),
    ("some other odd failure line", "some other odd failure line"),
    ("", ""),
])
def test_friendly_error_messages(stderr, fragment):
    assert fragment.lower() in rw.friendly_error(stderr).lower() if fragment else rw.friendly_error(stderr) == ""


# ---- RadioWatch: survey loop, using a fake survey function --------------------------------------
def fake_survey(results):
    """results: list of (csv_text, stderr) consumed in order, then repeats the last one."""
    calls = []

    async def survey(band, seconds):
        calls.append(band[0])
        idx = min(len(calls) - 1, len(results) - 1)
        return results[idx]

    survey.calls = calls
    return survey


async def collect(n, hub_attr="broadcast"):
    events = []

    async def broadcast(obj):
        events.append(obj)

    return events, broadcast


async def wait_for(cond, timeout=2.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        await asyncio.sleep(0.02)
    return False


async def test_not_available_when_tools_are_missing():
    events, broadcast = await collect(0)
    rwatch = rw.RadioWatch(broadcast, available=False, settle=0)
    await rwatch.start()
    assert rwatch.enabled is False and "not installed" in rwatch.error
    assert events[-1]["radio"]["available"] is False


async def test_survey_loop_publishes_channels_per_band():
    events, broadcast = await collect(0)
    line = csv_with({162_400_000: -30.0})
    survey = fake_survey([(line, "")])
    rwatch = rw.RadioWatch(broadcast, bands=[rw.BANDS[1]], survey_seconds=0, pause=0, survey=survey, available=True, settle=0)
    await rwatch.start()
    assert await wait_for(lambda: rwatch.channels.get("MURS / weather"))
    v = rwatch.view()
    assert v["enabled"] is True and v["channels"][0]["mhz"] == 162.4 and v["channels"][0]["label"] == "NOAA Weather Radio"
    assert v["channels"][0]["band"] == "MURS / weather" and v["channels"][0]["digital"] is False
    await rwatch.stop()
    assert rwatch.enabled is False


async def test_survey_failure_stops_and_reports_a_friendly_error():
    events, broadcast = await collect(0)
    survey = fake_survey([("", "usb_claim_interface error -6")])
    rwatch = rw.RadioWatch(broadcast, bands=[rw.BANDS[0]], survey_seconds=0, pause=0, survey=survey, available=True, settle=0)
    await rwatch.start()
    assert await wait_for(lambda: not rwatch.enabled)
    assert "in use by another program" in rwatch.error
    assert any("in use by another program" in ((e.get("radio") or {}).get("error") or "") for e in events)


async def test_digital_band_flag_is_reported():
    events, broadcast = await collect(0)
    line = csv_with({851_400_000: -30.0}, lo=851_000_000, hi=860_000_000, step=12500.0, n=720)
    survey = fake_survey([(line, "")])
    digital_band = next(b for b in rw.BANDS if b[0] == "800 MHz digital")
    rwatch = rw.RadioWatch(broadcast, bands=[digital_band], survey_seconds=0, pause=0, survey=survey, available=True, settle=0)
    await rwatch.start()
    assert await wait_for(lambda: rwatch.channels.get("800 MHz digital"))
    assert rwatch.view()["channels"][0]["digital"] is True
    await rwatch.stop()


# ---- RadioWatch: listening, using a fake audio process -------------------------------------------
class FakeProc:
    def __init__(self):
        self.killed = False
        self._waited = asyncio.Event()

    async def wait(self):
        await self._waited.wait()

    def kill_now(self):
        self.killed = True
        self._waited.set()


def kill_fake(proc, sig=15):
    proc.kill_now()


async def test_open_audio_pauses_survey_and_reports_listening_frequency():
    events, broadcast = await collect(0)
    survey = fake_survey([(csv_with({162_400_000: -30.0}), "")])
    procs = []

    async def spawn(mhz, squelch):
        p = FakeProc()
        procs.append((mhz, squelch, p))
        return p

    rwatch = rw.RadioWatch(broadcast, bands=[rw.BANDS[1]], survey_seconds=0, pause=0, survey=survey,
                           spawn_audio=spawn, available=True, kill=kill_fake)
    await rwatch.start()
    await rwatch.open_audio("162.4", squelch=20)
    assert rwatch.listening == 162.4 and procs[-1][:2] == (162.4, 20)
    assert rwatch.view()["listening"] == 162.4
    assert not rwatch._paused.is_set()  # survey is paused while listening
    procs[-1][2].kill_now()
    await rwatch.close_audio()
    assert rwatch.listening is None and procs[-1][2].killed
    assert rwatch._paused.is_set()
    await rwatch.stop()


async def test_open_audio_rejects_bad_frequency_without_touching_the_dongle():
    events, broadcast = await collect(0)
    spawned = []

    async def spawn(mhz, squelch):
        spawned.append(mhz)
        return FakeProc()

    rwatch = rw.RadioWatch(broadcast, bands=[], spawn_audio=spawn, available=True, kill=kill_fake, settle=0)
    with pytest.raises(ValueError):
        await rwatch.open_audio("not a number")
    with pytest.raises(ValueError):
        await rwatch.open_audio("99999")
    assert spawned == []


async def test_opening_a_new_frequency_closes_the_previous_stream():
    events, broadcast = await collect(0)
    procs = []

    async def spawn(mhz, squelch):
        p = FakeProc()
        procs.append(p)
        return p

    rwatch = rw.RadioWatch(broadcast, bands=[], spawn_audio=spawn, available=True, kill=kill_fake, settle=0)
    await rwatch.open_audio("146.52")
    await rwatch.open_audio("146.94")
    assert procs[0].killed and not procs[1].killed
    assert rwatch.listening == 146.94


async def test_shutdown_releases_everything():
    events, broadcast = await collect(0)
    proc = FakeProc()

    async def spawn(mhz, squelch):
        return proc

    survey = fake_survey([(csv_with({}), "")])
    rwatch = rw.RadioWatch(broadcast, bands=[rw.BANDS[0]], survey_seconds=0, pause=0, survey=survey,
                           spawn_audio=spawn, available=True, kill=kill_fake)
    await rwatch.start()
    await rwatch.open_audio("146.52")
    await rwatch.shutdown()
    assert proc.killed and rwatch.listening is None and rwatch.enabled is False


def test_view_reports_bands_even_before_any_survey_result():
    v = rw.RadioWatch(lambda o: None, bands=[rw.BANDS[0]], available=True, settle=0).view()
    assert v["bands"] == [{"name": "2 m ham", "lo": 144.0, "hi": 148.0, "digital": False}]
    assert v["channels"] == [] and v["enabled"] is False


# ---- releasing the dongle: cancellation must kill the running scan --------------------------------
import os  # noqa: E402
import signal  # noqa: E402


def _alive(pid):
    try:
        os.kill(pid, 0)
        with open(f"/proc/{pid}/stat") as f:
            return f.read().split()[2] != "Z"  # a zombie is not holding anything
    except (ProcessLookupError, FileNotFoundError):
        return False


async def test_run_command_returns_output_and_stderr():
    out, err = await rw.run_command(["sh", "-c", "echo hello; echo oops >&2"], 5)
    assert out.strip() == "hello" and err.strip() == "oops"


async def test_run_command_times_out_and_kills_the_process():
    started = time.time()
    out, err = await rw.run_command(["sleep", "30"], 0.3)
    assert (out, err) == ("", "timed out") and time.time() - started < 5


async def test_cancelling_run_command_kills_the_whole_process_group_before_returning():
    pidfile = f"/tmp/radiowatch-test-{os.getpid()}.pid"
    task = asyncio.ensure_future(rw.run_command(["sh", "-c", f"sleep 30 & echo $! > {pidfile}; wait"], 60))
    for _ in range(100):
        await asyncio.sleep(0.02)
        if os.path.exists(pidfile) and open(pidfile).read().strip():
            break
    child = int(open(pidfile).read().strip())
    assert _alive(child)  # a grandchild, like rtl_power under a shell
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.1)
    assert not _alive(child)  # gone: the dongle would be free
    os.remove(pidfile)


class Blocking:
    """A survey that runs until cancelled, like a long rtl_power scan."""

    def __init__(self):
        self.started = asyncio.Event()
        self.cancelled = 0
        self.completed = 0

    async def __call__(self, band, seconds):
        self.started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        self.completed += 1
        return csv_with({162_400_000: -30.0}), ""


async def test_listening_preempts_a_scan_in_progress_and_the_survey_resumes_afterwards():
    events, broadcast = await collect(0)
    survey = Blocking()
    procs = []

    async def spawn(mhz, squelch):
        # by the time the audio process would open the dongle, the scan must already be dead
        assert survey.cancelled == 1
        p = FakeProc()
        procs.append(p)
        return p

    rwatch = rw.RadioWatch(broadcast, bands=[rw.BANDS[1]], survey_seconds=0, pause=0, survey=survey,
                           spawn_audio=spawn, available=True, kill=kill_fake)
    await rwatch.start()
    await asyncio.wait_for(survey.started.wait(), 2)
    await rwatch.open_audio("162.4")
    assert survey.cancelled == 1 and rwatch.enabled and rwatch.listening == 162.4
    survey.started.clear()
    await asyncio.sleep(0.1)
    assert not survey.started.is_set()  # no new scan while listening
    await rwatch.close_audio()
    await asyncio.wait_for(survey.started.wait(), 2)  # scanning resumes on its own
    await rwatch.stop()


async def test_stop_cancels_the_scan_in_progress():
    events, broadcast = await collect(0)
    survey = Blocking()
    rwatch = rw.RadioWatch(broadcast, bands=[rw.BANDS[1]], survey_seconds=0, pause=0, survey=survey, available=True, settle=0)
    await rwatch.start()
    await asyncio.wait_for(survey.started.wait(), 2)
    await rwatch.stop()
    assert survey.cancelled == 1 and survey.completed == 0 and rwatch.enabled is False


async def test_a_scan_that_fails_once_is_retried_before_the_survey_gives_up():
    events, broadcast = await collect(0)
    line = csv_with({162_400_000: -30.0})
    survey = fake_survey([("", "usb_claim_interface error -6"), ("", "usb_claim_interface error -6"), (line, "")])
    rwatch = rw.RadioWatch(broadcast, bands=[rw.BANDS[1]], survey_seconds=0, pause=0, survey=survey,
                           available=True, settle=0, retries=3)
    await rwatch.start()
    assert await wait_for(lambda: rwatch.channels.get("MURS / weather"))
    assert rwatch.enabled and rwatch.error is None  # it kept going after two failed attempts
    await rwatch.stop()
    assert len(survey.calls) >= 3


async def test_persistent_failure_still_stops_after_the_retries():
    events, broadcast = await collect(0)
    survey = fake_survey([("", "usb_claim_interface error -6")])
    rwatch = rw.RadioWatch(broadcast, bands=[rw.BANDS[1]], survey_seconds=0, pause=0, survey=survey,
                           available=True, settle=0, retries=2)
    await rwatch.start()
    assert await wait_for(lambda: not rwatch.enabled)
    assert "in use by another program" in rwatch.error and len(survey.calls) == 3  # first try + 2 retries


async def test_survey_waits_for_the_dongle_to_settle_after_listening_ends():
    events, broadcast = await collect(0)
    survey = fake_survey([(csv_with({162_400_000: -30.0}), "")])

    async def spawn(mhz, squelch):
        return FakeProc()

    rwatch = rw.RadioWatch(broadcast, bands=[rw.BANDS[1]], survey_seconds=0, pause=0, survey=survey,
                           spawn_audio=spawn, available=True, kill=kill_fake, settle=0.3)
    await rwatch.open_audio("162.4")
    calls_before = len(survey.calls)
    t0 = time.time()
    await rwatch.close_audio()
    assert time.time() - t0 >= 0.25  # close_audio waited for USB to release the dongle
    assert len(survey.calls) == calls_before


# ---- aircraft (ADS-B) mode -----------------------------------------------------------------------
POS_EVEN = "8D40621D58C386435CC412692AD6"
POS_ODD = "8D40621D58C382D690C8AC2863A7"
IDENT = "8D4840D6202CC371C32CE0576098"


class FakeAdsbProc:
    """Stands in for `rtl_adsb`: lines pushed by the test come out of stdout.readline()."""

    def __init__(self, stderr=b""):
        self.q = asyncio.Queue()
        self.stderr_data = stderr
        self.killed = False
        self._done = asyncio.Event()
        outer = self

        class Out:
            async def readline(self):
                return await outer.q.get()

        class Err:
            async def read(self, size=-1):
                result, outer.stderr_data = outer.stderr_data, b""
                return result

        self.stdout, self.stderr = Out(), Err()

    def push(self, text):
        self.q.put_nowait(text.encode() + b"\n")

    def end(self):
        self.q.put_nowait(b"")

    def kill_now(self):
        self.killed = True
        self._done.set()
        self.q.put_nowait(b"")

    async def wait(self):
        await self._done.wait()


def air_watch(events_broadcast, proc, **kw):
    async def spawn_adsb():
        return proc

    return rw.RadioWatch(events_broadcast, bands=[rw.BANDS[1]], reference=(52.0, 4.0), available=True, settle=0,
                         kill=kill_fake, spawn_adsb=spawn_adsb, survey_seconds=0, pause=0, **kw)


async def test_aircraft_mode_decodes_frames_and_broadcasts_a_snapshot():
    events, broadcast = await collect(0)
    proc = FakeAdsbProc()
    rwatch = air_watch(broadcast, proc)
    await rwatch.start_aircraft()
    assert rwatch.aircraft_enabled and rwatch.view()["aircraft_enabled"] is True
    for f in ("*" + IDENT + ";", "*" + POS_EVEN + ";", "*" + POS_ODD + ";", "*8D40621D58C382D690C8AC286300;"):
        proc.push(f)  # the last one has a bad checksum
    assert await wait_for(lambda: any(e["type"] == "aircraft" and len(e["aircraft"]) == 2 for e in events), 4)
    snap = [e for e in events if e["type"] == "aircraft" and len(e["aircraft"]) == 2][-1]
    by = {a["icao"]: a for a in snap["aircraft"]}
    assert by["4840D6"]["callsign"] == "KLM1023"
    assert by["40621D"]["alt_ft"] == 38000 and by["40621D"]["lat"] == pytest.approx(52.26, abs=0.02)
    assert snap["reference"] == [52.0, 4.0] and snap["stats"]["accepted"] >= 3 and snap["stats"]["frames"] == 4
    await rwatch.stop_aircraft()
    assert proc.killed and not rwatch.aircraft_enabled


async def test_aircraft_mode_reports_a_busy_dongle_and_stops_itself():
    events, broadcast = await collect(0)
    proc = FakeAdsbProc(stderr=b"usb_claim_interface error -6")
    rwatch = air_watch(broadcast, proc)
    await rwatch.start_aircraft()
    proc.end()
    assert await wait_for(lambda: not rwatch.aircraft_enabled)
    assert "in use by another program" in rwatch.error
    assert any(e["type"] == "aircraft" and e["enabled"] is False for e in events)


async def test_starting_a_survey_stops_aircraft_mode_and_vice_versa():
    events, broadcast = await collect(0)
    proc = FakeAdsbProc()
    survey = fake_survey([(csv_with({162_400_000: -30.0}), "")])
    rwatch = air_watch(broadcast, proc, survey=survey)
    await rwatch.start_aircraft()
    await asyncio.sleep(0.05)  # let the receiver process start
    await rwatch.start()  # survey
    assert not rwatch.aircraft_enabled and rwatch.enabled and proc.killed
    proc2 = FakeAdsbProc()

    async def spawn2():
        return proc2

    rwatch._spawn_adsb = spawn2
    await rwatch.start_aircraft()  # back to aircraft: the survey must stop
    await asyncio.sleep(0.05)
    assert rwatch.aircraft_enabled and not rwatch.enabled
    await rwatch.shutdown()
    assert proc2.killed


async def test_listening_stops_aircraft_mode():
    events, broadcast = await collect(0)
    proc = FakeAdsbProc()
    audio = []

    async def spawn_audio(mhz, squelch):
        p = FakeProc()
        audio.append(p)
        return p

    rwatch = air_watch(broadcast, proc, spawn_audio=spawn_audio)
    await rwatch.start_aircraft()
    await asyncio.sleep(0.05)
    await rwatch.open_audio("118.3")
    assert not rwatch.aircraft_enabled and proc.killed and rwatch.listening == 118.3
    await rwatch.shutdown()


async def test_aircraft_view_reports_the_assumed_reference_and_is_empty_before_start():
    events, broadcast = await collect(0)
    rwatch = rw.RadioWatch(broadcast, bands=[], available=True)
    v = rwatch.aircraft_view()
    assert v["enabled"] is False and v["aircraft"] == [] and v["stats"] is None
    assert v["reference"] == list(rw.DEFAULT_REFERENCE) and v["reference_assumed"] is True
    assert rw.RadioWatch(broadcast, bands=[], available=True, reference=(30.0, -97.0)).aircraft_view()["reference_assumed"] is False


async def test_aircraft_start_when_tools_are_missing():
    events, broadcast = await collect(0)
    rwatch = rw.RadioWatch(broadcast, bands=[], available=False)
    await rwatch.start_aircraft()
    assert not rwatch.aircraft_enabled and "not installed" in rwatch.error


class Stubborn:
    """A process that ignores the polite stop and only dies to the forced one."""

    def __init__(self):
        self.signals = []
        self._done = asyncio.Event()

    async def wait(self):
        await self._done.wait()

    def kill(self, proc, sig=15):
        self.signals.append(sig)
        if sig == 9:
            self._done.set()


async def test_a_process_that_ignores_the_polite_stop_is_forced_to_exit(monkeypatch):
    events, broadcast = await collect(0)
    p = Stubborn()
    rwatch = rw.RadioWatch(broadcast, bands=[], available=True, settle=0, kill=p.kill)
    monkeypatch.setattr(asyncio, "wait_for", _fast_wait_for(asyncio.wait_for))
    await rwatch._terminate(p)
    assert p.signals == [15, 9]  # TERM first, then KILL
    assert p._done.is_set()


async def test_a_polite_process_is_not_force_killed(monkeypatch):
    events, broadcast = await collect(0)
    proc = FakeProc()
    signals = []

    def kill(pr, sig=15):
        signals.append(sig)
        pr.kill_now()

    rwatch = rw.RadioWatch(broadcast, bands=[], available=True, settle=0, kill=kill)
    await rwatch._terminate(proc)
    assert signals == [15]


def _fast_wait_for(real):
    async def wait_for(aw, timeout):
        return await real(aw, min(timeout, 0.05))
    return wait_for


def test_adsb_source_is_line_buffered_when_stdbuf_exists():
    import shutil
    assert shutil.which("stdbuf"), "coreutils stdbuf is needed so rtl_adsb frames are not held back in a pipe buffer"


def test_adsb_command_uses_fixed_gain_and_line_buffering():
    assert rw.adsb_cmd(have_stdbuf=True) == ["stdbuf", "-oL", "rtl_adsb", "-g", "40"]
    assert rw.adsb_cmd(have_stdbuf=False) == ["rtl_adsb", "-g", "40"]
    assert rw.adsb_cmd(gain=45.5, have_stdbuf=False) == ["rtl_adsb", "-g", "45.5"]
    assert rw.adsb_cmd(gain=0, have_stdbuf=False) == ["rtl_adsb"]  # 0 = automatic gain


@pytest.mark.parametrize("installed,available", [
    ({"rtl_power"}, {"survey"}),
    ({"rtl_fm", "ffmpeg"}, {"audio"}),
    ({"rtl_adsb"}, {"aircraft"}),
    ({"rtl_433"}, {"sensors"}),
    (set(), set()),
])
def test_capabilities_require_only_the_tools_used_by_each_mode(monkeypatch, installed, available):
    monkeypatch.setattr(rw.shutil, "which", lambda name: f"/usr/bin/{name}" if name in installed else None)
    radio = rw.RadioWatch(lambda _: None)
    caps = radio.view()["capabilities"]
    assert {mode for mode, value in caps.items() if value["available"]} == available
    assert radio.available is bool(available)
    assert all(value["error"] for value in caps.values() if not value["available"])


def test_missing_pymodes_only_disables_aircraft(monkeypatch):
    monkeypatch.setattr(rw.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(rw.adsb, "pms", None)
    caps = rw.RadioWatch(lambda _: None).capabilities()
    assert "pyModeS" in caps["aircraft"]["error"]
    assert all(c["available"] for mode, c in caps.items() if mode != "aircraft")


def test_survey_ignores_nonfinite_csv_cells():
    valid = csv_with({162_400_000: -30.0})
    invalid = "2026-09-20,22:00:01,nan,162560000,2500,4,-20,-60"
    assert rw.summarize(valid + "\n" + invalid) == rw.summarize(valid)
    assert rw.summarize(valid.replace("-60.0", "nan", 1)) == rw.summarize(valid)


async def test_run_command_forces_a_process_that_ignores_term(tmp_path):
    started = time.monotonic()
    pidfile = tmp_path / "stubborn.pid"
    code = ("import os, signal, time; "
            "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            f"open({str(pidfile)!r}, 'w').write(str(os.getpid())); time.sleep(30)")
    out, err = await asyncio.wait_for(rw.run_command([sys.executable, "-c", code], 0.3), 5)
    assert (out, err) == ("", "timed out")
    assert time.monotonic() - started < 5
    assert not _alive(int(pidfile.read_text()))


async def test_retry_scan_is_preempted_before_audio_opens(tmp_path):
    events, broadcast = await collect(0)
    blocker, calls = Blocking(), []

    async def survey(band, seconds):
        calls.append(band)
        if len(calls) == 1:
            return "", "busy"
        return await blocker(band, seconds)

    async def spawn(mhz, squelch):
        assert blocker.cancelled == 1
        return FakeProc()

    radio = rw.RadioWatch(broadcast, bands=[rw.BANDS[0]], survey=survey, spawn_audio=spawn,
                         available=True, settle=0, kill=kill_fake, snapshot=str(tmp_path / "radio.json"))
    await radio.start()
    await asyncio.wait_for(blocker.started.wait(), 2)
    await radio.open_audio(146.52)
    assert blocker.cancelled == 1 and not radio._paused.is_set()
    await radio.shutdown()


async def test_survey_spawn_failure_is_reported_and_disables_mode(tmp_path):
    events, broadcast = await collect(0)

    async def missing(band, seconds):
        raise FileNotFoundError("rtl_power disappeared")

    radio = rw.RadioWatch(broadcast, bands=[rw.BANDS[0]], survey=missing, available=True,
                         settle=0, snapshot=str(tmp_path / "radio.json"))
    await radio.start()
    assert await wait_for(lambda: not radio.enabled)
    assert "rtl_power disappeared" in radio.error
    await radio.shutdown()


async def test_aircraft_spawn_failure_is_reported_and_disables_mode(tmp_path):
    events, broadcast = await collect(0)

    async def missing():
        raise FileNotFoundError("rtl_adsb disappeared")

    radio = rw.RadioWatch(broadcast, spawn_adsb=missing, available=True, settle=0,
                         snapshot=str(tmp_path / "radio.json"))
    await radio.start_aircraft()
    assert await wait_for(lambda: not radio.aircraft_enabled)
    assert "rtl_adsb disappeared" in radio.error
    assert radio._air_proc is None
    await radio.shutdown()


async def test_failed_audio_spawn_restores_survey_permission(tmp_path):
    events, broadcast = await collect(0)

    async def missing(mhz, squelch):
        raise FileNotFoundError("audio program disappeared")

    radio = rw.RadioWatch(broadcast, spawn_audio=missing, available=True, settle=0,
                         snapshot=str(tmp_path / "radio.json"))
    with pytest.raises(FileNotFoundError):
        await radio.open_audio(146.52)
    assert radio._paused.is_set() and radio.listening is None and radio._audio is None
    assert "audio program disappeared" in radio.error


async def test_unavailable_audio_does_not_attempt_to_spawn(monkeypatch, tmp_path):
    events, broadcast = await collect(0)
    monkeypatch.setattr(rw.shutil, "which", lambda name: "/bin/rtl_power" if name == "rtl_power" else None)

    async def unexpected(mhz, squelch):
        pytest.fail("must not spawn audio without ffmpeg")

    radio = rw.RadioWatch(broadcast, spawn_audio=unexpected, snapshot=str(tmp_path / "radio.json"))
    with pytest.raises(RuntimeError, match="ffmpeg"):
        await radio.open_audio(146.52)
    assert radio.available and radio.listening is None


def decoder_watch(broadcast, proc, tmp_path, **kwargs):
    async def spawn(command):
        return proc

    return rw.RadioWatch(broadcast, available=True, settle=0, spawn_job=spawn, job_check=lambda cls: "",
                         kill=kill_fake, snapshot=str(tmp_path / "radio.json"), **kwargs)


@pytest.mark.parametrize("kind,line,key", [
    ("sensors", '{"model":"WeatherStation","id":42,"temperature_C":20.5,"channel":"fixture text"}', "devices"),
])
async def test_decoder_jobs_publish_and_keep_results_after_stop(tmp_path, kind, line, key):
    events, broadcast = await collect(0)
    proc = FakeAdsbProc()
    radio = decoder_watch(broadcast, proc, tmp_path)
    await radio.start_job(kind)
    proc.push(line)
    assert await wait_for(lambda: any(e["type"] == "job" and e["job"].get(key) for e in events))
    assert radio.job_mhz == rw.decoders.JOBS[kind].default_mhz
    await radio.stop_job()
    assert not radio.job_enabled and proc.killed
    assert radio.job_view()[key]
    assert "fixture text" not in (tmp_path / "radio.json").read_text()


async def test_decoder_process_failure_reports_bounded_in_memory_diagnostics(tmp_path):
    events, broadcast = await collect(0)
    proc = FakeAdsbProc(stderr=b"x" * 10000 + b"\nusb_claim_interface error -6")
    radio = decoder_watch(broadcast, proc, tmp_path)
    await radio.start_job("sensors")
    proc.end()
    assert await wait_for(lambda: not radio.job_enabled)
    assert "in use by another program" in radio.job_error
    assert proc.killed and radio._job_proc is None
    assert list(tmp_path.iterdir()) == [tmp_path / "radio.json"]
    await radio.shutdown()


async def test_decoder_spawn_failure_is_reported_and_disables_mode(tmp_path):
    events, broadcast = await collect(0)

    async def missing(command):
        raise OSError("decoder launch failed")

    radio = rw.RadioWatch(broadcast, available=True, settle=0, spawn_job=missing, job_check=lambda cls: "",
                         snapshot=str(tmp_path / "radio.json"))
    await radio.start_job("sensors")
    assert await wait_for(lambda: not radio.job_enabled)
    assert radio.job_error == "decoder launch failed" and radio._job_proc is None
    await radio.shutdown()


async def test_invalid_or_missing_decoder_does_not_interrupt_an_active_job(tmp_path):
    events, broadcast = await collect(0)
    proc = FakeAdsbProc()
    radio = decoder_watch(broadcast, proc, tmp_path)
    await radio.start_job("sensors", 433.92)
    for kind, frequency in (("unknown", 100), ("pager", 929.6125), ("sensors", "bad"), ("sensors", 2000)):
        with pytest.raises(ValueError):
            await radio.start_job(kind, frequency)
    radio._job_check = lambda cls: "rtl_433 not installed."
    await radio.start_job("sensors", 915)
    assert radio.job_enabled and radio.job_mhz == 433.92 and not proc.killed
    assert radio.job_error == "rtl_433 not installed."
    await radio.shutdown()


async def test_audio_start_stops_decoder_before_opening_dongle(tmp_path):
    events, broadcast = await collect(0)
    decoder = FakeAdsbProc()

    async def spawn_audio(mhz, squelch):
        assert decoder.killed
        return FakeProc()

    radio = decoder_watch(broadcast, decoder, tmp_path, spawn_audio=spawn_audio)
    await radio.start_job("sensors")
    assert await wait_for(lambda: radio._job_proc is not None)
    await radio.open_audio(162.4)
    assert not radio.job_enabled and radio.listening == 162.4
    await radio.shutdown()


async def test_simultaneous_audio_requests_are_serialized_and_old_disconnect_is_harmless(tmp_path):
    events, broadcast = await collect(0)
    entered, release = asyncio.Event(), asyncio.Event()
    procs = []

    async def spawn(mhz, squelch):
        if not procs:
            entered.set()
            await release.wait()
        proc = FakeProc()
        procs.append(proc)
        return proc

    radio = rw.RadioWatch(broadcast, spawn_audio=spawn, available=True, settle=0, kill=kill_fake,
                         snapshot=str(tmp_path / "radio.json"))
    first = asyncio.create_task(radio.open_audio(146.52))
    await entered.wait()
    second = asyncio.create_task(radio.open_audio(162.4))
    await asyncio.sleep(0)
    assert not procs
    release.set()
    old, current = await asyncio.gather(first, second)
    assert old.killed and not current.killed and radio.listening == 162.4
    await radio.close_audio(old)
    assert radio._audio is current and not current.killed
    await radio.shutdown()
    assert current.killed


async def test_audio_eof_reports_dongle_error_and_releases_stream(tmp_path):
    events, broadcast = await collect(0)
    proc = FakeAdsbProc(stderr=b"usb_claim_interface error -6")

    async def spawn(mhz, squelch):
        return proc

    radio = rw.RadioWatch(broadcast, spawn_audio=spawn, available=True, settle=0, kill=kill_fake,
                         snapshot=str(tmp_path / "radio.json"))
    await radio.open_audio(146.52)
    await radio.audio_ended(proc)
    assert "in use by another program" in radio.error
    assert radio._audio is None and radio.listening is None and proc.killed
    assert radio._audio_stderr is None and radio._paused.is_set()


def test_radio_module_imports_without_the_optional_aircraft_decoder():
    import subprocess
    code = """
import builtins, sys
sys.path.insert(0, 'tools')
original_import = builtins.__import__
def guarded_import(name, *args, **kwargs):
    if name.startswith('pyModeS'):
        raise ImportError('pyModeS intentionally unavailable')
    return original_import(name, *args, **kwargs)
builtins.__import__ = guarded_import
import radiowatch
assert radiowatch.adsb.pms is None
assert 'pyModeS' in radiowatch.RadioWatch(lambda _: None).capability_error('aircraft')
"""
    result = subprocess.run([sys.executable, "-c", code], cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


# ---- ADS-B receiver choice and frame statistics ----------------------------------------------------
def test_adsb_prefers_the_better_demodulators_when_installed():
    assert rw.adsb_program(lambda n: n in {"rtl_adsb"}) == "rtl_adsb"
    assert rw.adsb_program(lambda n: n in {"rtl_adsb", "dump1090"}) == "dump1090"
    assert rw.adsb_program(lambda n: n in {"rtl_adsb", "dump1090-fa", "readsb"}) == "readsb"
    assert rw.adsb_program(lambda n: False) is None


def test_adsb_command_lines_for_each_receiver():
    assert rw.adsb_cmd(program="readsb", have_stdbuf=True) == ["stdbuf", "-oL", "readsb", "--device-type", "rtlsdr",
                                                              "--raw", "--gain", "40"]
    assert rw.adsb_cmd(program="dump1090-fa", have_stdbuf=False)[:4] == ["dump1090-fa", "--device-type", "rtlsdr", "--raw"]
    assert rw.adsb_cmd(program="dump1090", have_stdbuf=False, gain=0) == ["dump1090", "--raw"]


@pytest.mark.parametrize("installed", [{"readsb"}, {"dump1090-fa"}, {"dump1090"}, {"rtl_adsb"}])
def test_any_adsb_receiver_enables_aircraft_mode(monkeypatch, installed):
    monkeypatch.setattr(rw.shutil, "which", lambda name: f"/usr/bin/{name}" if name in installed else None)
    assert rw.RadioWatch(lambda _: None).capability_error("aircraft") == ""


def test_missing_adsb_receiver_names_the_alternatives(monkeypatch):
    monkeypatch.setattr(rw.shutil, "which", lambda name: None)
    assert "rtl_adsb (or readsb/dump1090)" in rw.RadioWatch(lambda _: None).capability_error("aircraft")


async def test_aircraft_stats_break_down_rejected_frames():
    events, broadcast = await collect(0)
    proc = FakeAdsbProc()
    rwatch = air_watch(broadcast, proc)
    await rwatch.start_aircraft()
    for line in ["*" + IDENT.lower() + ";\r", "*8d4840d6202cc371c32ce0576000;\r", "*a0001838ca3e51f0a8000047a36a;\r",
                 "garbage"]:
        proc.push(line)
    assert await wait_for(lambda: rwatch.table.frames == 4, 4)
    stats = rwatch.aircraft_view()["stats"]
    assert stats["accepted"] == 1 and stats["crc_failed"] == 1 and stats["other_df"] == 1 and stats["malformed"] == 1
    assert "receiver" in rwatch.aircraft_view()
    await rwatch.shutdown()


def test_survey_snapshot_path_is_configurable(tmp_path):
    path = tmp_path / "state" / "radio-activity.json"
    radio = rw.RadioWatch(lambda _: None, snapshot=str(path))
    assert radio.snapshot == str(path)
    assert rw.RadioWatch(lambda _: None).snapshot == rw.SNAPSHOT  # tests redirect this away from the live run/
    assert not rw.SNAPSHOT.startswith(str(Path(rw.ROOT) / "run"))


# ---- receivers never outlive a killed server --------------------------------------------------------
def test_orphan_guard_prefix():
    assert rw.orphan_guard(["rtl_433", "-f", "433.92M"], have_setpriv=True) == ["setpriv", "--pdeathsig", "TERM",
                                                                                "rtl_433", "-f", "433.92M"]
    assert rw.orphan_guard(["rtl_433"], have_setpriv=False) == ["rtl_433"]


def test_decoder_pipelines_guard_every_program():
    wrap = lambda argv: ["G", *argv]  # noqa: E731
    import decoders
    assert decoders.SensorJob().command(decoders.AUTO, wrap=wrap).count("G ") == 1
    assert decoders.SensorJob().command(433.92, wrap=wrap).startswith("G rtl_433 ")
    assert decoders.SensorJob().command(433.92) == "rtl_433 -f 433.92M -g 40 -F json -M level"


@pytest.mark.skipif(not __import__("shutil").which("setpriv"), reason="util-linux setpriv not installed")
def test_a_killed_server_takes_its_receiver_pipeline_with_it(tmp_path):
    """Stand-in for a server killed with SIGKILL while a decoder pipeline (sh -c 'a | b') runs: both programs
    must exit instead of holding the dongle. Uses `sleep` in place of the receivers."""
    import subprocess
    pidfile = tmp_path / "receiver.pid"
    receiver = ["sh", "-c", f"echo $$ > {pidfile}; exec sleep 60"]
    pipeline = rw.decoders.shell_pipe(rw.orphan_guard(receiver), rw.orphan_guard(["cat"]))
    owner_code = ("import subprocess, sys, time; "
                  f"subprocess.Popen({rw.orphan_guard(['sh', '-c', pipeline])!r}, stdout=subprocess.PIPE, "
                  "start_new_session=True); time.sleep(60)")
    owner = subprocess.Popen([sys.executable, "-c", owner_code])
    try:
        for _ in range(100):
            if pidfile.exists() and pidfile.read_text().strip():
                break
            time.sleep(0.05)
        receiver_pid = int(pidfile.read_text())
        owner.kill()  # SIGKILL: no cleanup code runs
        owner.wait(5)
        for _ in range(100):
            try:
                os.kill(receiver_pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            os.kill(receiver_pid, signal.SIGKILL)
            pytest.fail("receiver outlived its owner")
    finally:
        if owner.poll() is None:
            owner.kill()
