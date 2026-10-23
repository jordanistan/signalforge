#!/usr/bin/env python3
"""Radio panel engine for the web GUI: survey bands with the RTL-SDR and stream a channel's audio to the browser.

Receive-only. Owns the SDR dongle while surveying or listening (only one program can use it at a time), and
lets go of it when stopped. Uses the rtl-sdr command line tools (rtl_power, rtl_fm) and ffmpeg.

`survey_once(band)` runs rtl_power for a few seconds and `summarize()` turns the readings into channels with a duty
cycle (how much of the time they were on) and a peak level. Voice channels are intermittent; continuous ones are
usually data, beacons or digital control channels.
"""
import asyncio
import csv
import io
import json
import math
import os
import shutil
import statistics
import sys
import time
from functools import wraps

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from adsb import AircraftTable  # noqa: E402
import adsb  # noqa: E402
import decoders  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SNAPSHOT = os.path.join(ROOT, "run", "radio-activity.json")

# name, low MHz, high MHz, bin kHz, digital?
BANDS = [
    ("2 m ham", 144.0, 148.0, 5.0, False),
    ("MURS / weather", 151.0, 163.0, 12.5, False),
    ("70 cm ham", 440.0, 450.0, 12.5, False),
    ("UHF business / GMRS", 460.0, 470.0, 6.25, False),
    ("800 MHz digital", 851.0, 860.0, 12.5, True),
    ("Pagers 929-932", 929.0, 932.0, 12.5, True),
]
LABELS = [  # (low, high MHz, label)
    (162.39, 162.56, "NOAA Weather Radio"),
    (146.935, 146.945, "2 m repeater 146.94"),
    (151.8, 151.95, "MURS"), (154.5, 154.62, "MURS"),
    (462.5, 462.75, "GMRS/FRS"), (467.5, 467.75, "GMRS/FRS"),
    (929.0, 932.0, "Pager channel"),
]
MIN_MHZ, MAX_MHZ = 24.0, 1766.0  # what the R820T tuner can receive
DEFAULT_REFERENCE = (30.2672, -97.7431)  # downtown Austin: used for aircraft distances unless --home is given


def label_for(mhz, band_name=""):
    for lo, hi, name in LABELS:
        if lo <= mhz <= hi:
            return name
    return band_name


def summarize(csv_text, margin=10.0, merge_khz=15.0, top=40):
    """rtl_power CSV -> channels above `margin` dB over the noise floor, strongest duty first.
    Returns [{"mhz", "duty", "peak_db"}] where duty is 0..1 (fraction of sweeps the channel was on)."""
    sweeps = {}
    for r in csv.reader(io.StringIO(csv_text)):
        if len(r) < 8:
            continue
        try:
            lo, step = float(r[2]), float(r[4])
        except ValueError:
            continue
        if not math.isfinite(lo) or not math.isfinite(step) or step <= 0:
            continue
        for i, v in enumerate(r[6:]):
            try:
                value, freq = float(v), lo + i * step
                if math.isfinite(value) and math.isfinite(freq):
                    sweeps.setdefault(r[0] + r[1], {})[round(freq)] = value
            except (ValueError, OverflowError):
                pass
    if not sweeps:
        return []
    every = [v for d in sweeps.values() for v in d.values()]
    floor = statistics.median(every)
    freqs = sorted({f for d in sweeps.values() for f in d})
    hits = []
    for f in freqs:
        vals = [d[f] for d in sweeps.values() if f in d]
        peak = max(vals) - floor
        if peak > margin:
            hits.append((f, sum(1 for v in vals if v > floor + margin) / len(vals), peak))
    groups, cur = [], []
    for h in hits:
        if cur and h[0] - cur[-1][0] > merge_khz * 1000:
            groups.append(cur)
            cur = []
        cur.append(h)
    if cur:
        groups.append(cur)
    out = []
    for g in groups:
        best = max(g, key=lambda x: x[2])
        out.append({"mhz": round(best[0] / 1e6, 4), "duty": round(max(x[1] for x in g), 2), "peak_db": round(best[2], 1)})
    return sorted(out, key=lambda c: (-c["duty"], -c["peak_db"]))[:top]


def rtl_power_cmd(lo, hi, bin_khz, seconds, gain=38):
    return ["rtl_power", "-f", f"{lo}M:{hi}M:{bin_khz}k", "-g", str(gain), "-i", "1", "-e", f"{int(seconds)}s", "-"]


def rtl_fm_cmd(mhz, squelch=0, gain=38):
    return ["rtl_fm", "-f", f"{mhz}M", "-M", "fm", "-s", "22050", "-r", "22050", "-g", str(gain), "-l", str(int(squelch)),
            "-E", "deemp", "-"]


FFMPEG_CMD = ["ffmpeg", "-loglevel", "error", "-f", "s16le", "-ar", "22050", "-ac", "1", "-i", "-",
              "-f", "mp3", "-b:a", "48k", "-flush_packets", "1", "pipe:1"]


def valid_mhz(value):
    try:
        v = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return round(v, 4) if MIN_MHZ <= v <= MAX_MHZ else None


def friendly_error(stderr):
    s = (stderr or "").lower()
    if "usb_claim_interface" in s or "resource busy" in s or "is busy" in s:
        return "The SDR dongle is in use by another program (close gqrx or any scanner) and try again."
    if "no supported devices" in s or "no device" in s or "cannot open" in s:
        return "No SDR dongle found. Check that it is plugged in."
    if "permission denied" in s or "access" in s:
        return "No permission to open the SDR dongle (udev rules)."
    return (stderr or "").strip().splitlines()[-1][:160] if (stderr or "").strip() else ""


def orphan_guard(argv, have_setpriv=None):
    """Prefix a receiver program so the kernel stops it if its parent dies (setpriv --pdeathsig, util-linux).
    Normal stops kill the whole process group; this covers the server being killed outright (SIGKILL, crash),
    which would otherwise leave rtl_433 / rtl_fm running and holding the dongle. The death signal survives exec,
    and each program in a shell pipeline gets its own, so the chain unwinds as each parent exits."""
    have_setpriv = shutil.which("setpriv") is not None if have_setpriv is None else have_setpriv
    return ["setpriv", "--pdeathsig", "TERM", *argv] if have_setpriv else list(argv)


async def run_command(cmd, timeout):
    """Run cmd, return (stdout, stderr). If we are cancelled (survey stopped, or a listen request pre-empts it) or
    time out, the whole process group is killed and reaped BEFORE returning, so the SDR dongle is free again."""
    p = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                                             start_new_session=True)
    try:
        out, err = await asyncio.wait_for(p.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        await terminate_process(p)
        return "", "timed out"
    except BaseException:  # includes CancelledError
        await asyncio.shield(terminate_process(p))
        raise
    return out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


async def _run_survey(band, seconds):
    """Default survey runner: rtl_power for `seconds`; returns (csv_text, stderr_text)."""
    _name, lo, hi, step, _digital = band
    return await run_command(orphan_guard(rtl_power_cmd(lo, hi, step, seconds)), seconds + 25)


def _kill(proc, sig=15):
    try:
        os.killpg(proc.pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


async def terminate_process(proc, kill=_kill):
    """Reap a subprocess with bounded TERM/KILL escalation, including shell children."""
    kill(proc)
    try:
        await asyncio.wait_for(proc.wait(), 2)
    except asyncio.TimeoutError:
        try:
            kill(proc, 9)
        except TypeError:
            kill(proc)
        await asyncio.wait_for(proc.wait(), 2)
    # A shell can exit before a child that ignored TERM. Its process group still
    # belongs to this job and must not keep hold of the dongle.
    if kill is _kill:
        _kill(proc, 9)


async def collect_stderr(proc, output):
    """Drain diagnostics continuously so a full stderr pipe cannot freeze a receiver."""
    if getattr(proc, "stderr", None) is None:
        return
    while chunk := await proc.stderr.read(4096):
        output.extend(chunk)
        del output[:-2000]


def serialized(method):
    """Serialize mode changes across browsers while allowing their nested stop calls."""
    @wraps(method)
    async def call(self, *args, **kwargs):
        current = asyncio.current_task()
        if self._control_owner is current:
            return await method(self, *args, **kwargs)
        async with self._control_lock:
            self._control_owner = current
            try:
                return await method(self, *args, **kwargs)
            finally:
                self._control_owner = None
    return call


ADSB_GAIN = 40.0  # dB. Measured on this dongle: automatic gain heard 1 valid aircraft message in 35 s, 40 dB heard 31
# ADS-B receivers, best first. readsb and dump1090 demodulate far better than rtl_adsb (which has no error correction
# and prints many noise frames); all three print the same `*<hex>;` raw lines with --raw, so the parser is shared.
ADSB_PROGRAMS = ("readsb", "dump1090-fa", "dump1090", "rtl_adsb")


def adsb_program(which=None):
    """The best installed ADS-B receiver program, or None."""
    which = which or shutil.which
    return next((name for name in ADSB_PROGRAMS if which(name)), None)


def adsb_cmd(gain=ADSB_GAIN, have_stdbuf=None, program="rtl_adsb"):
    """ADS-B receiver command line printing raw frames on stdout. stdbuf makes it line-buffered: through a pipe it
    would otherwise hold frames back in a 4 KB block and the map would sit empty. Fixed gain matters a lot for ADS-B."""
    have_stdbuf = shutil.which("stdbuf") is not None if have_stdbuf is None else have_stdbuf
    if program in ("readsb", "dump1090-fa"):
        cmd = [program, "--device-type", "rtlsdr", "--raw"] + (["--gain", f"{gain:g}"] if gain else [])
    elif program == "dump1090":
        cmd = [program, "--raw"] + (["--gain", f"{gain:g}"] if gain else [])
    else:
        cmd = ["rtl_adsb"] + (["-g", f"{gain:g}"] if gain else [])
    return (["stdbuf", "-oL"] + cmd) if have_stdbuf else cmd


async def _spawn_adsb():
    """Default ADS-B source: the best installed receiver, printing one `*<hex>;` line per received frame."""
    return await asyncio.create_subprocess_exec(*orphan_guard(adsb_cmd(program=adsb_program() or "rtl_adsb")),
                                                stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.PIPE, start_new_session=True)


async def _spawn_audio(mhz, squelch):
    """Default audio source: rtl_fm piped through ffmpeg to MP3. Returns the process whose stdout is the stream."""
    cmd = decoders.shell_pipe(orphan_guard(rtl_fm_cmd(mhz, squelch)), orphan_guard(FFMPEG_CMD))
    return await asyncio.create_subprocess_exec(*orphan_guard(["sh", "-c", cmd]), stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.PIPE, start_new_session=True)


async def _spawn_job(shell_cmd):
    """Default decoder source: a shell pipeline (rtl_433) whose stdout is the decoded text."""
    return await asyncio.create_subprocess_exec(*orphan_guard(["sh", "-c", shell_cmd]), stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.PIPE, start_new_session=True, cwd=ROOT)


def job_error(kind_cls):
    """Which decoder programs are missing (empty string if all are installed)."""
    need = {"sensors": ("rtl_433",)}.get(kind_cls.kind, ())
    missing = [t for t in need if not shutil.which(t)]
    return f"{', '.join(missing)} not installed." if missing else ""


class RadioWatch:
    """Surveys bands in a loop while enabled and streams audio on request. Only one job uses the dongle at a time."""

    def __init__(self, broadcast, bands=BANDS, survey_seconds=15, pause=5, survey=_run_survey, spawn_audio=_spawn_audio,
                 snapshot=None, available=None, kill=None, settle=0.8, retries=3, reference=None,
                 spawn_adsb=_spawn_adsb, spawn_job=_spawn_job, job_check=job_error):
        self.broadcast, self.bands = broadcast, list(bands)
        self.survey_seconds, self.pause = survey_seconds, pause
        self._survey, self._spawn_audio = survey, spawn_audio
        self.snapshot = snapshot or SNAPSHOT  # resolved now so tests (and --state-dir) can redirect it
        self._kill = kill or _kill  # injectable so tests need no real process
        self.settle, self.retries = settle, retries  # USB needs a moment to release the dongle between jobs
        self.reference = tuple(reference) if reference else DEFAULT_REFERENCE
        self.reference_assumed = reference is None
        self._spawn_adsb = spawn_adsb
        self._spawn_job, self._job_check = spawn_job, job_check
        self._available_override = available
        self._control_lock = asyncio.Lock()
        self._control_owner = None
        self.job = None  # a decoders.SensorJob while running or after it stopped (its results stay visible)
        self.job_enabled = False
        self.job_mhz = None
        self.job_error = None
        self._job_task = None
        self._job_proc = None
        self.aircraft_enabled = False
        self.table = None
        self._air_task = None
        self._air_proc = None
        self._adsb_source = None  # name of the receiver program in use, for the UI
        self.enabled = False
        self.channels = {}  # band name -> [channel dicts]
        self.updated = None
        self.error = None
        self.listening = None  # MHz being streamed, or None
        self._task = None
        self._survey_task = None
        self._preempted = False
        self._audio = None
        self._audio_stderr = None
        self._audio_diagnostic = bytearray()
        self._paused = asyncio.Event()
        self._paused.set()  # set = surveys may run

    # ---- state -------------------------------------------------------------------------
    def capability_error(self, mode):
        """Required software for each receiver mode; this does not probe the hardware."""
        if self._available_override is False:
            return "SDR tools are not installed."
        if mode in decoders.JOBS:
            return self._job_check(decoders.JOBS[mode])
        if self._available_override is True:
            return ""
        required = {"survey": ("rtl_power",), "audio": ("rtl_fm", "ffmpeg"), "aircraft": ()}
        missing = [name for name in required[mode] if not shutil.which(name)]
        if mode == "aircraft" and adsb_program() is None:
            missing.append("rtl_adsb (or readsb/dump1090)")
        if mode == "aircraft" and (adsb.pms is None or not callable(getattr(adsb.pms, "decode", None))):
            missing.append("compatible pyModeS")
        return f"{', '.join(missing)} not installed." if missing else ""

    def capabilities(self):
        return {mode: {"available": not error, "error": error or None}
                for mode in ("survey", "audio", "aircraft", "sensors")
                for error in (self.capability_error(mode),)}

    @property
    def available(self):
        return any(c["available"] for c in self.capabilities().values())

    def view(self):
        chans = []
        for name, lo, hi, _step, digital in self.bands:
            for c in self.channels.get(name, []):
                chans.append({**c, "band": name, "digital": digital, "label": label_for(c["mhz"], name)})
        chans.sort(key=lambda c: (-c["duty"], -c["peak_db"]))
        return {"available": self.available, "enabled": self.enabled, "listening": self.listening, "error": self.error,
                "capabilities": self.capabilities(),
                "aircraft_enabled": self.aircraft_enabled, "job": self.job.kind if self.job_enabled else None,
                "reference": list(self.reference),
                "reference_assumed": self.reference_assumed,
                "updated": self.updated, "bands": [{"name": b[0], "lo": b[1], "hi": b[2], "digital": b[4]} for b in self.bands],
                "channels": chans[:60]}

    async def _publish(self):
        v = self.view()
        try:  # atomic: a reader never sees half a file
            os.makedirs(os.path.dirname(self.snapshot), mode=0o700, exist_ok=True)
            tmp = self.snapshot + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(v, f)
            os.replace(tmp, self.snapshot)
        except OSError:
            pass
        await self.broadcast({"type": "radio", "radio": v})

    # ---- survey loop --------------------------------------------------------------------
    @serialized
    async def start(self):
        missing = self.capability_error("survey")
        if missing:
            self.error = missing
            await self._publish()
            return
        if self.enabled:
            return
        await self.stop_aircraft()  # one program at a time on the dongle
        await self.stop_job()
        self.enabled, self.error = True, None
        self._task = asyncio.create_task(self._loop())
        await self._publish()

    @serialized
    async def stop(self):
        self.enabled = False
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        await self._publish()

    async def _loop(self):
        try:
            while self.enabled:
                for band in self.bands:
                    attempts = 0
                    while self.enabled:
                        await self._paused.wait()
                        self._survey_task = asyncio.ensure_future(self._survey(band, self.survey_seconds))
                        try:
                            csv_text, err = await self._survey_task
                        except asyncio.CancelledError:
                            if self._preempted and self.enabled:
                                self._preempted = False
                                continue  # resume this band once audio releases the dongle
                            raise
                        finally:
                            self._survey_task = None
                        if csv_text.strip():
                            break
                        if attempts >= self.retries:
                            self.error = friendly_error(err) or "The survey returned no data."
                            self.enabled = False
                            await self._publish()
                            return
                        attempts += 1
                        await asyncio.sleep(self.settle or 0)
                    if not self.enabled:
                        return
                    self.error = None
                    self.channels[band[0]] = summarize(csv_text)
                    self.updated = time.time()
                    await self._publish()
                await asyncio.sleep(self.pause)
        except Exception as exc:
            self.enabled = False
            self.error = friendly_error(str(exc)) or "The survey stopped."
            await self._publish()

    # ---- aircraft (ADS-B) ---------------------------------------------------------------
    def aircraft_view(self):
        return {"enabled": self.aircraft_enabled, "reference": list(self.reference),
                "reference_assumed": self.reference_assumed,
                "aircraft": self.table.snapshot() if self.table else [],
                "receiver": self._adsb_source,
                "stats": self.table.stats() if self.table else None}

    @serialized
    async def start_aircraft(self):
        missing = self.capability_error("aircraft")
        if missing:
            self.error = missing
            await self._publish()
            return
        if self.aircraft_enabled:
            return
        await self.stop()  # stops the survey and lets go of the dongle
        await self.close_audio()
        await self.stop_job()
        self.table = AircraftTable(self.reference)
        self._adsb_source = adsb_program() if self._spawn_adsb is _spawn_adsb else None
        self.aircraft_enabled, self.error = True, None
        self._air_task = asyncio.create_task(self._aircraft_loop())
        await self._publish()

    @serialized
    async def stop_aircraft(self):
        if not self.aircraft_enabled and self._air_task is None:
            return
        self.aircraft_enabled = False
        t, self._air_task = self._air_task, None
        if t:
            t.cancel()
            await asyncio.gather(t, return_exceptions=True)
        await self._release_adsb()
        await self.broadcast({"type": "aircraft", **self.aircraft_view()})
        await self._publish()

    async def _terminate(self, proc):
        """Stop a process group and make sure it is gone: ask politely, then force it if it is still alive."""
        await terminate_process(proc, self._kill)

    async def _release_adsb(self):
        proc, self._air_proc = self._air_proc, None
        if proc is not None:
            await self._terminate(proc)
            if self.settle:
                await asyncio.sleep(self.settle)

    async def _aircraft_loop(self):
        diagnostic, stderr_task = bytearray(), None
        last = 0.0
        try:
            proc = await self._spawn_adsb()
            self._air_proc = proc
            stderr_task = asyncio.create_task(collect_stderr(proc, diagnostic))
            while True:
                try:
                    line = await asyncio.wait_for(proc.stdout.readline(), 1.0)
                except asyncio.TimeoutError:
                    line = None
                if line == b"":  # the process ended: dongle busy, unplugged...
                    await asyncio.wait({stderr_task}, timeout=1)
                    self.error = friendly_error(diagnostic.decode("utf-8", "replace")) or "Aircraft receiver stopped."
                    self.aircraft_enabled = False
                    return
                if line:
                    self.table.feed(line.decode("ascii", "ignore"))
                now = time.time()
                if now - last >= 1.0:
                    last = now
                    await self.broadcast({"type": "aircraft", **self.aircraft_view()})
        except Exception as exc:
            self.aircraft_enabled = False
            self.error = friendly_error(str(exc)) or "Aircraft receiver stopped."
        finally:
            await self._release_adsb()
            if stderr_task:
                stderr_task.cancel()
                await asyncio.gather(stderr_task, return_exceptions=True)
            await self._publish()
            await self.broadcast({"type": "aircraft", **self.aircraft_view()})


    # ---- decoder job (ISM-band sensors) ---------------------------------------------------------
    def job_view(self):
        base = {"enabled": self.job_enabled, "kind": self.job.kind if self.job else None, "mhz": self.job_mhz,
                "error": self.job_error, "presets": {k: v for k, v in decoders.PRESETS.items()}}
        return {**base, **(self.job.view() if self.job else {})}

    async def _push_job(self):
        await self.broadcast({"type": "job", "job": self.job_view()})

    @serialized
    async def start_job(self, kind, mhz=None):
        cls = decoders.JOBS.get(kind)
        if cls is None:
            raise ValueError("unknown decoder")
        mhz = mhz if mhz is not None else cls.default_mhz
        if mhz != decoders.AUTO:
            mhz = valid_mhz(mhz)
            if mhz is None:
                raise ValueError("frequency out of range")
        missing = self.capability_error(kind)
        if missing:
            self.job_error = missing
            await self._push_job()
            return
        await self.stop()
        await self.close_audio()
        await self.stop_aircraft()
        await self.stop_job(keep=False)
        self.job, self.job_mhz, self.job_error = cls(), mhz, None
        self.job_enabled = True
        wrap = orphan_guard if self._spawn_job is _spawn_job else None
        self._job_task = asyncio.create_task(self._job_loop(self.job.command(mhz, wrap=wrap)))
        await self._publish()
        await self._push_job()

    @serialized
    async def stop_job(self, keep=True):
        if not self.job_enabled and self._job_task is None:
            if not keep:
                self.job = None
            return
        self.job_enabled = False
        t, self._job_task = self._job_task, None
        if t:
            t.cancel()
            await asyncio.gather(t, return_exceptions=True)
        await self._release_job()
        if not keep:
            self.job = None
        await self._publish()
        await self._push_job()

    async def _release_job(self):
        proc, self._job_proc = self._job_proc, None
        if proc is not None:
            await self._terminate(proc)
            if self.settle:
                await asyncio.sleep(self.settle)

    async def _job_loop(self, command):
        diagnostic, stderr_task = bytearray(), None
        last, dirty = 0.0, False
        try:
            proc = await self._spawn_job(command)
            self._job_proc = proc
            stderr_task = asyncio.create_task(collect_stderr(proc, diagnostic))
            while True:
                try:
                    line = await asyncio.wait_for(proc.stdout.readline(), 1.0)
                except asyncio.TimeoutError:
                    line = None
                if line == b"":  # decoder ended: dongle busy, unplugged, or a bad tool
                    await asyncio.wait({stderr_task}, timeout=1)
                    self.job_error = friendly_error(diagnostic.decode("utf-8", "replace")) or "The decoder stopped."
                    self.job_enabled = False
                    return
                if line:
                    dirty = self.job.feed(line.decode("utf-8", "replace")) or dirty
                now = time.time()
                if dirty and now - last >= 1.0:
                    last, dirty = now, False
                    await self._push_job()
        except Exception as exc:
            self.job_enabled = False
            self.job_error = friendly_error(str(exc)) or "The decoder stopped."
        finally:
            await self._release_job()
            if stderr_task:
                stderr_task.cancel()
                await asyncio.gather(stderr_task, return_exceptions=True)
            await self._publish()
            await self._push_job()

    # ---- listening ----------------------------------------------------------------------
    @serialized
    async def open_audio(self, mhz, squelch=0):
        """Start streaming `mhz`. Surveys pause (the dongle is exclusive); returns the ffmpeg process."""
        mhz = valid_mhz(mhz)
        if mhz is None:
            raise ValueError("frequency out of range")
        missing = self.capability_error("audio")
        if missing:
            self.error = missing
            await self._publish()
            raise RuntimeError(missing)
        await self.close_audio()
        await self.stop_aircraft()
        await self.stop_job()
        self._paused.clear()
        await self._preempt_survey()  # kill a scan in progress: the dongle must be free before rtl_fm opens it
        try:
            self._audio = await self._spawn_audio(mhz, squelch)
        except BaseException as exc:
            self.listening = None
            self._paused.set()
            self.error = friendly_error(str(exc)) or "Audio receiver did not start."
            await self._publish()
            raise
        self.listening, self.error = mhz, None
        self._audio_diagnostic = bytearray()
        self._audio_stderr = asyncio.create_task(collect_stderr(self._audio, self._audio_diagnostic))
        await self._publish()
        return self._audio

    async def _preempt_survey(self):
        t = self._survey_task
        if t is not None and not t.done():
            self._preempted = True
            t.cancel()
            await asyncio.gather(t, return_exceptions=True)

    @serialized
    async def audio_ended(self, expected_proc):
        """Report unexpected audio EOF only if this request still owns the active stream."""
        if self._audio is not expected_proc:
            return
        if self._audio_stderr:
            await asyncio.wait({self._audio_stderr}, timeout=1)
        self.error = friendly_error(self._audio_diagnostic.decode("utf-8", "replace")) or "Audio receiver stopped."
        await self.close_audio(expected_proc)

    @serialized
    async def close_audio(self, expected_proc=None):
        if expected_proc is not None and self._audio is not expected_proc:
            return
        proc, self._audio = self._audio, None
        if proc is not None:
            await self._terminate(proc)
        if self._audio_stderr:
            self._audio_stderr.cancel()
            await asyncio.gather(self._audio_stderr, return_exceptions=True)
            self._audio_stderr = None
        was = self.listening
        self.listening = None
        if was is not None and self.settle:
            await asyncio.sleep(self.settle)  # let USB release the dongle before a survey can grab it again
        self._paused.set()
        if was is not None:
            await self._publish()

    @serialized
    async def shutdown(self):
        await self.stop()
        await self.close_audio()
        await self.stop_aircraft()
        await self.stop_job()
