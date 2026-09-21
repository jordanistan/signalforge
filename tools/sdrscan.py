#!/usr/bin/env python3
"""Scan channels through gqrx's remote-control port and stop on the ones where someone is transmitting.

Receive-only. gqrx does the tuning, demodulation and audio (so you see the waterfall and hear each active
channel); this script only sets the frequency, reads the signal level and holds while a channel is active.
In gqrx enable Tools -> Remote control first (localhost, port 7356).

Examples:
  tools/sdrscan.py --preset 2m,murs,gmrs            # scan those channel lists until Ctrl-C
  tools/sdrscan.py --channels 146.52,146.94,144.0-148.0:0.015 --margin 10
  tools/sdrscan.py --preset 2m --calibrate          # just measure the noise floor and list the strongest channels
Every time a channel opens, one line is printed and appended to run/sdr-events.jsonl.
"""
import argparse
import json
import os
import socket
import statistics
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EVENTS = os.path.join(ROOT, "run", "sdr-events.jsonl")

PRESETS = {
    "2m": "144.0-148.0:0.015",  # US 2 m amateur band, 15 kHz FM channel spacing
    "70cm": "440.0-450.0:0.025",  # US 70 cm amateur repeater sub-band, 25 kHz steps
    "murs": "151.820,151.880,151.940,154.570,154.600",
    "gmrs": ("462.5625,462.5875,462.6125,462.6375,462.6625,462.6875,462.7125,"
             "467.5625,467.5875,467.6125,467.6375,467.6625,467.6875,467.7125,"
             "462.550,462.575,462.600,462.625,462.650,462.675,462.700,462.725"),
    "noaa": "162.400,162.425,162.450,162.475,162.500,162.525,162.550",
    "seen": "146.94,460.8008,461.377,461.5234,464.5508,463.7891",  # channels found active in the first survey
}


def parse_channels(spec):
    """'146.52,146.94,144.0-148.0:0.015' (MHz) -> unique frequencies in Hz, in the order given."""
    out, seen = [], set()
    for part in (p.strip() for p in spec.split(",")):
        if not part:
            continue
        if "-" in part:
            rng, _, step = part.partition(":")
            lo, hi = (float(x) for x in rng.split("-"))
            step = float(step) if step else 0.0125
            if step <= 0 or hi < lo:
                raise ValueError(f"bad range {part!r}")
            freqs = [lo + i * step for i in range(int(round((hi - lo) / step)) + 1)]
        else:
            freqs = [float(part)]
        for f in freqs:
            hz = round(f * 1e6)
            if hz not in seen:
                seen.add(hz)
                out.append(hz)
    return out


def expand_presets(names):
    return ",".join(PRESETS[n.strip()] for n in names.split(",") if n.strip())


class Gqrx:
    """Minimal client for gqrx's remote-control protocol (a rigctl subset over TCP)."""

    def __init__(self, host="127.0.0.1", port=7356, timeout=3.0):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.f = self.sock.makefile("rw", newline="\n")

    def _cmd(self, line):
        self.f.write(line + "\n")
        self.f.flush()
        reply = self.f.readline().strip()
        if not reply:
            raise ConnectionError("gqrx closed the connection")
        return reply

    def _ok(self, line):
        r = self._cmd(line)
        if r != "RPRT 0":
            raise RuntimeError(f"gqrx rejected {line!r}: {r}")

    def set_freq(self, hz):
        self._ok(f"F {hz}")

    def strength(self):
        return float(self._cmd("l STRENGTH"))

    def set_mode(self, mode="FM", passband=12500):
        self._ok(f"M {mode} {passband}")

    def set_squelch(self, db):
        self._ok(f"L SQL {db:.1f}")

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


class Scanner:
    """Sweep the channel list; when a channel rises `margin` dB above the noise floor, hold on it until it has
    been quiet for `hang` seconds, then report the event and carry on."""

    def __init__(self, radio, channels, margin=8.0, dwell=0.06, hang=2.0, max_hold=60.0, lockout=300.0,
                 clock=time.time, sleep=time.sleep):
        self.radio, self.channels = radio, list(channels)
        self.margin, self.dwell, self.hang = margin, dwell, hang
        self.max_hold, self.lockout = max_hold, lockout  # a channel that never goes quiet is skipped for a while
        self.clock, self.sleep = clock, sleep
        self.floor = None
        self.stop = False
        self.locked_until = {}

    def level(self, hz):
        self.radio.set_freq(hz)
        self.sleep(self.dwell)  # let the tuner and demodulator settle
        return self.radio.strength()

    def calibrate(self):
        """One pass over every channel. The noise floor is the median level; returns (hz, level) sorted loudest first."""
        levels = [(hz, self.level(hz)) for hz in self.channels]
        self.floor = statistics.median(l for _, l in levels)
        return sorted(levels, key=lambda x: -x[1])

    @property
    def threshold(self):
        return self.floor + self.margin

    def hold(self, hz, first_level):
        """Stay on an active channel until it has been quiet for `hang` seconds (or `max_hold` passes, so a
        permanent carrier cannot trap the scanner). Returns (duration_s, peak_db, capped)."""
        start, last_active, peak = self.clock(), self.clock(), first_level
        while not self.stop:
            self.sleep(0.2)
            lv = self.radio.strength()
            peak = max(peak, lv)
            if lv > self.threshold - 3:  # small hysteresis so a fading signal is not cut off
                last_active = self.clock()
            elif self.clock() - last_active >= self.hang:
                break
            if self.clock() - start >= self.max_hold:
                return self.clock() - start, peak, True
        return last_active - start, peak, False

    def run(self, on_event, max_passes=None):
        if self.floor is None:
            self.calibrate()
        passes = 0
        while not self.stop and (max_passes is None or passes < max_passes):
            seen, capped_now = [], 0
            for hz in self.channels:
                if self.stop:
                    break
                if self.locked_until.get(hz, 0) > self.clock():
                    continue  # recently a permanent carrier
                lv = self.level(hz)
                seen.append(lv)
                if lv > self.threshold:
                    started = self.clock()
                    dur, peak, capped = self.hold(hz, lv)
                    if capped:
                        self.locked_until[hz] = self.clock() + self.lockout
                        capped_now += 1
                    on_event({"ts": started, "freq_mhz": round(hz / 1e6, 4), "peak_db": round(peak, 1),
                              "floor_db": round(self.floor, 1), "duration_s": round(dur, 1), "capped": capped})
                    if capped_now >= 2:  # two channels that never go quiet: the whole band moved, not two signals
                        self.calibrate()
                        self.locked_until.clear()
                        seen = []
                        break
            if seen:  # follow slow band-wide changes in the noise floor (gain, temperature, interference)
                self.floor = 0.7 * self.floor + 0.3 * statistics.median(seen)
            else:
                self.sleep(1.0)  # nothing was scanned this pass (all locked out): wait instead of spinning
            passes += 1
        return passes


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--preset", help="comma list of: " + ", ".join(PRESETS))
    ap.add_argument("--channels", help="MHz: 146.52,146.94,144.0-148.0:0.015")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=7356)
    ap.add_argument("--mode", default="FM")
    ap.add_argument("--bandwidth", type=int, default=12500)
    ap.add_argument("--margin", type=float, default=8.0, help="dB above the noise floor that counts as active")
    ap.add_argument("--dwell", type=float, default=0.06, help="seconds to settle on each channel")
    ap.add_argument("--hang", type=float, default=2.0, help="seconds of quiet before leaving an active channel")
    ap.add_argument("--max-hold", type=float, default=60.0, help="longest stay on one channel; then it is skipped for 5 min")
    ap.add_argument("--calibrate", action="store_true", help="measure the noise floor, list the strongest, then exit")
    ap.add_argument("--passes", type=int, help="stop after this many sweeps")
    a = ap.parse_args()
    spec = ",".join(s for s in (expand_presets(a.preset) if a.preset else "", a.channels or "") if s)
    if not spec:
        ap.error("give --preset and/or --channels")
    channels = parse_channels(spec)
    try:
        radio = Gqrx(a.host, a.port)
    except OSError as e:
        sys.exit(f"Cannot reach gqrx on {a.host}:{a.port} ({e}).\nIn gqrx open Tools -> Remote control and tick it.")
    radio.set_mode(a.mode, a.bandwidth)
    sc = Scanner(radio, channels, a.margin, a.dwell, a.hang, a.max_hold)
    print(f"{len(channels)} channels; measuring the noise floor...", flush=True)
    ranked = sc.calibrate()
    print(f"noise floor {sc.floor:.1f} dBFS; active above {sc.threshold:.1f} dBFS", flush=True)
    if a.calibrate:
        for hz, lv in ranked[:10]:
            print(f"  {hz / 1e6:9.4f} MHz  {lv:6.1f} dBFS  ({lv - sc.floor:+.1f} dB)")
        return
    radio.set_squelch(sc.threshold - 2)  # mute the hiss between transmissions; opens on real signals

    def on_event(ev):
        os.makedirs(os.path.dirname(EVENTS), exist_ok=True)
        with open(EVENTS, "a", encoding="utf-8") as f:
            f.write(json.dumps(ev) + "\n")
        print(f"{time.strftime('%H:%M:%S', time.localtime(ev['ts']))}  {ev['freq_mhz']:9.4f} MHz  "
              f"{ev['duration_s']:5.1f}s  peak {ev['peak_db']} dBFS"
              + ("  (never went quiet: skipped for 5 min)" if ev["capped"] else ""), flush=True)

    print("scanning (Ctrl-C to stop)...", flush=True)
    try:
        sc.run(on_event, a.passes)
    except KeyboardInterrupt:
        pass
    finally:
        radio.close()


if __name__ == "__main__":
    main()
