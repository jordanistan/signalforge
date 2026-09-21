import socket
import subprocess
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import sdrscan  # noqa: E402


# ---- channel lists ------------------------------------------------------------------------------
def test_parse_single_list_and_ranges():
    assert sdrscan.parse_channels("146.52") == [146_520_000]
    assert sdrscan.parse_channels("146.52, 146.94") == [146_520_000, 146_940_000]
    assert sdrscan.parse_channels("144.0-144.045:0.015") == [144_000_000, 144_015_000, 144_030_000, 144_045_000]
    assert sdrscan.parse_channels("462.5-462.55") == [462_500_000, 462_512_500, 462_525_000, 462_537_500, 462_550_000]  # default 12.5 kHz
    assert sdrscan.parse_channels("146.52,146.52,146.94,146.52") == [146_520_000, 146_940_000]  # unique, in order
    assert sdrscan.parse_channels(" , 146.52 ,, ") == [146_520_000]
    assert sdrscan.parse_channels("") == []


@pytest.mark.parametrize("bad", ["abc", "148-144", "144-148:0", "144-148:-1", "1.2.3"])
def test_parse_rejects_bad_specs(bad):
    with pytest.raises(ValueError):
        sdrscan.parse_channels(bad)


def test_presets_are_valid_and_sized_sensibly():
    for name in sdrscan.PRESETS:
        chans = sdrscan.parse_channels(sdrscan.PRESETS[name])
        assert chans and all(1_000_000 < c < 2_000_000_000 for c in chans), name
    assert len(sdrscan.parse_channels(sdrscan.PRESETS["noaa"])) == 7
    assert len(sdrscan.parse_channels(sdrscan.PRESETS["gmrs"])) == 22
    assert 250 <= len(sdrscan.parse_channels(sdrscan.PRESETS["2m"])) <= 280  # 15 kHz steps across 144-148 MHz
    combined = sdrscan.parse_channels(sdrscan.expand_presets("murs, noaa"))
    assert len(combined) == 5 + 7


# ---- scanner logic (simulated radio and clock) -----------------------------------------------------
class Sim:
    """Fake radio + clock. `busy` maps a frequency to a list of (start, end) times when it is transmitting."""

    def __init__(self, busy=None, noise=-60.0, loud=-30.0):
        self.t, self.f, self.noise, self.loud = 0.0, None, noise, loud
        self.busy = busy or {}
        self.tuned = []

    def clock(self):
        return self.t

    def sleep(self, s):
        self.t += s

    def set_freq(self, hz):
        self.f = hz
        self.tuned.append(hz)

    def strength(self):
        for a, b in self.busy.get(self.f, []):
            if a <= self.t < b:
                return self.loud
        return self.noise


def make_scanner(sim, channels, **kw):
    return sdrscan.Scanner(sim, channels, clock=sim.clock, sleep=sim.sleep, **kw)


CH = [146_520_000, 146_940_000, 147_000_000, 147_060_000]


def test_calibrate_finds_the_noise_floor_and_ranks_channels():
    sim = Sim(busy={146_940_000: [(0, 100)]})
    sc = make_scanner(sim, CH)
    ranked = sc.calibrate()
    assert sc.floor == -60.0 and sc.threshold == -52.0  # median ignores the one loud channel
    assert ranked[0] == (146_940_000, -30.0)


def test_quiet_band_produces_no_events():
    sim = Sim()
    events = []
    make_scanner(sim, CH).run(events.append, max_passes=3)
    assert events == []


def test_activity_is_reported_with_frequency_duration_and_peak():
    sim = Sim(busy={146_940_000: [(1.0, 6.0)]})
    sc = make_scanner(sim, CH, hang=1.0)
    events = []
    sc.calibrate()
    sim.t = 0.95  # so the first look (after the settle time) lands inside the transmission
    sc.run(events.append, max_passes=1)
    assert len(events) == 1
    ev = events[0]
    assert ev["freq_mhz"] == 146.94 and ev["peak_db"] == -30.0 and ev["floor_db"] == -60.0
    assert 4.5 <= ev["duration_s"] <= 5.5  # the transmission lasted ~5 s; the hang time is not counted


def test_short_gaps_do_not_split_one_conversation_but_long_ones_do():
    two_bursts_close = {146_940_000: [(1.0, 3.0), (3.6, 6.0)]}  # 0.6 s gap < 2 s hang: same event
    sim = Sim(busy=two_bursts_close)
    sc = make_scanner(sim, [146_940_000], hang=2.0)
    sc.calibrate()
    sim.t = 0.95  # so the first look (after the settle time) lands inside the transmission
    ev = []
    sc.run(ev.append, max_passes=1)
    assert len(ev) == 1 and ev[0]["duration_s"] >= 4.5

    two_bursts_far = {146_940_000: [(1.0, 3.0), (8.0, 10.0)]}  # 5 s gap: two events
    sim = Sim(busy=two_bursts_far)
    sc = make_scanner(sim, [146_940_000], hang=2.0)
    sc.calibrate()
    sim.t = 0.95  # so the first look (after the settle time) lands inside the transmission
    ev = []
    sc.run(ev.append, max_passes=150)
    assert len(ev) == 2 and ev[0]["ts"] < ev[1]["ts"]


def test_two_channels_active_in_turn_are_both_reported_in_scan_order():
    sim = Sim(busy={146_520_000: [(0.0, 4.0)], 147_000_000: [(0.0, 30.0)]})
    sc = make_scanner(sim, CH, hang=1.0)
    sc.floor = -60.0  # skip calibration (channels are already busy)
    ev = []
    sc.run(ev.append, max_passes=1)
    assert [e["freq_mhz"] for e in ev] == [146.52, 147.0]


def test_margin_sets_the_sensitivity():
    weak = Sim(busy={146_940_000: [(0, 100)]}, loud=-55.0)  # only 5 dB above the floor
    ev = []
    sc = make_scanner(weak, CH, margin=8.0)
    sc.floor = -60.0
    sc.run(ev.append, max_passes=1)
    assert ev == []
    ev = []
    sc = make_scanner(weak, CH, margin=4.0, hang=0.5)
    sc.floor = -60.0
    weak.busy = {146_940_000: [(0, 3)]}
    sc.run(ev.append, max_passes=1)
    assert len(ev) == 1


def test_stop_flag_ends_a_long_hold_and_the_scan():
    sim = Sim(busy={146_940_000: [(0, 10_000)]})
    sc = make_scanner(sim, CH)
    sc.floor = -60.0
    seen = []

    def on_event(e):
        seen.append(e)
        sc.stop = True

    original = sim.sleep

    def sleep_and_stop(s):
        original(s)
        if sim.t > 3:
            sc.stop = True

    sc.sleep = sleep_and_stop
    sc.run(on_event, max_passes=None)
    assert sim.t < 20  # it did not hold for 10,000 s


def test_floor_follows_slow_drift():
    sim = Sim(noise=-60.0)
    sc = make_scanner(sim, CH)
    sc.calibrate()
    sim.noise = -50.0  # interference raises the floor by 10 dB
    events = []
    sc.run(events.append, max_passes=6)
    assert sc.floor > -52.0  # the threshold has adapted, so the whole band is not reported as a signal
    assert len(events) == 2 and all(e["capped"] for e in events)  # two stuck channels were enough to notice
    assert sim.t < 300  # ...and it did not waste a minute on every channel to find out
    later = []
    sc.run(later.append, max_passes=6)
    assert later == []  # the new floor holds: the raised noise is no longer reported


def test_a_permanent_carrier_is_capped_locked_out_and_does_not_block_the_rest():
    sim = Sim(busy={146_520_000: [(0, 10_000_000)], 147_000_000: [(20.0, 25.0)]})  # first channel never stops
    sc = make_scanner(sim, CH, hang=1.0, max_hold=10.0, lockout=100.0)
    sc.floor = -60.0
    ev = []
    sc.run(ev.append, max_passes=200)
    assert ev[0]["freq_mhz"] == 146.52 and ev[0]["capped"] and 9.5 <= ev[0]["duration_s"] <= 11
    assert any(e["freq_mhz"] == 147.0 and not e["capped"] for e in ev)  # the real conversation was still found
    assert sum(1 for e in ev if e["freq_mhz"] == 146.52) == 1  # locked out on later passes


# ---- the gqrx network client -------------------------------------------------------------------------
class FakeGqrx:
    """A tiny TCP server speaking the subset of gqrx's remote protocol that sdrscan uses."""

    def __init__(self, strength="-42.5", reply_ok="RPRT 0"):
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(1)
        self.port = self.srv.getsockname()[1]
        self.received, self.strength, self.reply_ok = [], strength, reply_ok
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        conn, _ = self.srv.accept()
        f = conn.makefile("rw", newline="\n")
        for line in f:
            line = line.strip()
            self.received.append(line)
            if line == "l STRENGTH":
                f.write(self.strength + "\n")
            elif line == "drop":
                break
            else:
                f.write(self.reply_ok + "\n")
            f.flush()
        conn.close()

    def close(self):
        self.srv.close()


def test_client_speaks_gqrx_protocol():
    srv = FakeGqrx()
    g = sdrscan.Gqrx(port=srv.port)
    g.set_freq(146_940_000)
    g.set_mode("FM", 15000)
    g.set_squelch(-52.0)
    assert g.strength() == -42.5
    g.close()
    assert srv.received == ["F 146940000", "M FM 15000", "L SQL -52.0", "l STRENGTH"]
    srv.close()


def test_client_reports_rejected_commands_and_dropped_connections():
    srv = FakeGqrx(reply_ok="RPRT 1")
    g = sdrscan.Gqrx(port=srv.port)
    with pytest.raises(RuntimeError, match="rejected"):
        g.set_freq(1)
    with pytest.raises(ConnectionError):
        g._cmd("drop")  # the server hangs up without answering
    with pytest.raises(ConnectionError):
        g.strength()
    srv.close()


def test_cli_explains_how_to_enable_remote_control_when_gqrx_is_not_listening():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()  # a port that is certainly closed
    r = subprocess.run([str(ROOT / "tools" / "sdrscan.py"), "--preset", "noaa", "--port", str(port)],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode != 0 and "Remote control" in r.stderr
    r = subprocess.run([str(ROOT / "tools" / "sdrscan.py")], capture_output=True, text=True, timeout=30)
    assert r.returncode == 2 and "--preset" in r.stderr
