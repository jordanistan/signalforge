#!/usr/bin/env python3
"""Read-only setup diagnostics: never opens a radio, starts an SDR, or contacts the network.

Everything here comes from the Python import system, $PATH, /sys and /proc: installed programs, which receiver
modes they enable, whether a Meshtastic serial radio and an RTL-SDR dongle are plugged in, and which process (if
any) is currently holding each of them.
"""
import argparse
import grp
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from radiowatch import ADSB_PROGRAMS  # noqa: E402  (the same list the Air page accepts)

PACMAN = "sudo pacman -S --needed"
PY_HINT = ".venv/bin/python -m pip install -r requirements-optional.txt"

# Browser/Python capabilities: mode -> (python packages, programs, install hint)
PACKAGES = {
    "web": ("aiohttp",), "mesh": ("meshtastic",), "terminal": ("textual",),
    "satellites": ("skyfield",), "aircraft": ("pyModeS",),
    "tests": ("pytest", "pytest_asyncio", "playwright"),
}
# SDR receiver modes, as offered by the Radio/Air pages. `any_of` means one of those programs is enough.
SDR_MODES = [
    {"mode": "survey", "label": "Band survey (activity scan)",
     "freq": "144-148, 151-163, 440-450, 460-470, 851-860, 929-932 MHz",
     "requires": ["rtl_power"], "hint": f"{PACMAN} rtl-sdr"},
    {"mode": "audio", "label": "Listen: FM audio (NOAA Weather Radio 162.40-162.55, 2 m, MURS, GMRS...)",
     "freq": "any 24-1766 MHz", "requires": ["rtl_fm", "ffmpeg"], "hint": f"{PACMAN} rtl-sdr ffmpeg"},
    {"mode": "aircraft", "label": "Aircraft: ADS-B (Mode S extended squitter)", "freq": "1090 MHz",
     "requires": [], "any_of": list(ADSB_PROGRAMS), "python": ["pyModeS"],
     "hint": f"{PACMAN} rtl-sdr; better reception: readsb or dump1090-fa from the AUR; pyModeS: {PY_HINT}"},
    {"mode": "sensors", "label": "Environmental sensors (weather stations, thermometers)",
     "freq": "auto-hop 433.92 / 315 / 915 MHz, or 433.92, 315, 868.3, 915 MHz", "requires": ["rtl_433"], "hint": f"{PACMAN} rtl_433"},
]
OTHER_COMMANDS = {  # not SDR modes of the web console, but part of the toolset
    "gqrx_scanner": (["gqrx"], "tools/sdrscan.py drives gqrx over its remote-control port", f"{PACMAN} gqrx"),
    "syntax_checks": (["node", "bash"], "make check", f"{PACMAN} nodejs"),
    "line_buffering": (["stdbuf"], "keeps ADS-B frames flowing through a pipe", f"{PACMAN} coreutils"),
}
NOT_INTEGRATED = {  # installed-or-not information only; the console does not drive these
    "direwolf": "APRS packet (144.39 MHz)", "dsd-fme": "digital voice (P25/DMR/NXDN)",
    "acarsdec": "aircraft ACARS (129-131 MHz)", "dumpvdl2": "aircraft VDL2 (136 MHz)",
    "AIS-catcher": "ship AIS (161.975/162.025 MHz)", "satdump": "weather-satellite images",
}
RTL_SDR_IDS = {("0bda", "2838"), ("0bda", "2832")}
DVB_MODULE = "dvb_usb_rtl28xxu"
# Legacy view kept for --json consumers: mode -> programs that must all be installed.
COMMANDS = {m["mode"]: tuple(m["requires"]) for m in SDR_MODES}
COMMANDS.update({"syntax_checks": ("node", "bash")})


def sdr_mode_status(spec, which=shutil.which, find_spec=importlib.util.find_spec):
    """Programs/packages missing for one SDR mode, using the same acceptance rules as radiowatch."""
    missing = [p for p in spec["requires"] if not which(p)]
    chosen = None
    if spec.get("any_of"):
        chosen = next((p for p in spec["any_of"] if which(p)), None)
        if chosen is None:
            missing.append(" or ".join(spec["any_of"]))
    missing += [p for p in spec.get("python", ()) if find_spec(p) is None]
    return {"available": not missing, "missing": missing, "program": chosen, "label": spec["label"],
            "freq": spec["freq"], "hint": None if not missing else spec["hint"]}


def _holders(path):
    """PIDs (and command names) of processes with `path` open. Only our own processes are visible; read-only."""
    try:
        target = os.path.realpath(path)
    except OSError:
        return []
    found = []
    for pid in filter(str.isdigit, os.listdir("/proc")):
        fd_dir = f"/proc/{pid}/fd"
        try:
            fds = os.listdir(fd_dir)
        except OSError:
            continue
        for fd in fds:
            try:
                if os.readlink(f"{fd_dir}/{fd}") == target:
                    with open(f"/proc/{pid}/cmdline", "rb") as f:
                        cmd = f.read().replace(b"\0", b" ").decode("utf-8", "replace").strip()[:120]
                    found.append({"pid": int(pid), "cmd": cmd})
                    break
            except OSError:
                continue
    return found


def usb_devices(sysfs="/sys/bus/usb/devices"):
    out = []
    try:
        entries = sorted(os.listdir(sysfs))
    except OSError:
        return out
    for name in entries:
        d = os.path.join(sysfs, name)
        try:
            vid = Path(d, "idVendor").read_text().strip()
            pid = Path(d, "idProduct").read_text().strip()
        except OSError:
            continue
        rec = {"vid": vid, "pid": pid, "product": "", "dev": None}
        try:
            rec["product"] = Path(d, "product").read_text().strip()
        except OSError:
            pass
        try:
            rec["dev"] = "/dev/bus/usb/%03d/%03d" % (int(Path(d, "busnum").read_text()), int(Path(d, "devnum").read_text()))
        except (OSError, ValueError):
            pass
        out.append(rec)
    return out


def serial_status(port):
    exists = Path(port).exists()
    info = {"path": port, "exists": exists, "accessible": exists and os.access(port, os.R_OK | os.W_OK),
            "holders": _holders(port) if exists else [], "hint": None}
    if not exists:
        info["hint"] = "Radio not plugged in (or on another port: pass --port). Use a data-capable USB cable."
    elif not info["accessible"]:
        try:
            group = grp.getgrgid(os.stat(port).st_gid).gr_name
        except (KeyError, OSError):
            group = "uucp"
        info["hint"] = f"No permission: sudo usermod -aG {group} $USER, then log out and back in."
    elif info["holders"]:
        info["hint"] = "In use by the process(es) listed; only one program can own the radio."
    return info


def dongle_status(devices=None, modules="/sys/module"):
    devices = usb_devices() if devices is None else devices
    dongles = [d for d in devices if (d["vid"], d["pid"]) in RTL_SDR_IDS]
    dvb = os.path.isdir(os.path.join(modules, DVB_MODULE))
    info = {"present": bool(dongles), "devices": dongles, "dvb_driver_loaded": dvb, "holders": [], "hint": None}
    for d in dongles:
        if d["dev"] and os.path.exists(d["dev"]):
            d["accessible"] = os.access(d["dev"], os.R_OK | os.W_OK)
            info["holders"] += _holders(d["dev"])
    if not dongles:
        info["hint"] = "No RTL-SDR dongle detected on USB."
    elif dvb:
        info["hint"] = (f"The TV driver {DVB_MODULE} is loaded and will claim the dongle: "
                        f"sudo modprobe -r {DVB_MODULE} (the rtl-sdr package blacklists it after a reboot).")
    elif any(d.get("accessible") is False for d in dongles):
        info["hint"] = "No permission for the dongle: install rtl-sdr (its udev rules), then replug it."
    elif info["holders"]:
        info["hint"] = "In use by the process(es) listed; only one SDR mode/program can own the dongle."
    return info


def inspect(port="/dev/ttyUSB0", state_dir=None):
    modes = {}
    for mode in dict.fromkeys([*PACKAGES, *COMMANDS]):
        missing = [p for p in PACKAGES.get(mode, ()) if importlib.util.find_spec(p) is None]
        missing += [p for p in COMMANDS.get(mode, ()) if shutil.which(p) is None]
        modes[mode] = {"available": not missing, "missing": missing}
    sdr = {spec["mode"]: sdr_mode_status(spec) for spec in SDR_MODES}
    for mode, status in sdr.items():  # the SDR view is authoritative (aircraft accepts several receivers)
        modes[mode] = {"available": status["available"], "missing": status["missing"]}
    other = {name: {"available": all(shutil.which(p) for p in progs), "missing": [p for p in progs if not shutil.which(p)],
                    "purpose": purpose, "hint": hint}
             for name, (progs, purpose, hint) in OTHER_COMMANDS.items()}
    extra = {name: {"installed": bool(shutil.which(name)), "signal": what} for name, what in NOT_INTEGRATED.items()}
    state = Path(state_dir) if state_dir else ROOT / "run"
    state_mode = None
    try:
        state_mode = oct(state.stat().st_mode & 0o777)
    except OSError:
        pass
    return {"python": sys.version.split()[0], "executable": sys.executable, "capabilities": modes,
            "sdr_modes": sdr, "tools": other, "not_integrated": extra,
            "serial": serial_status(port), "sdr_dongle": dongle_status(),
            "basemap": (ROOT / "data" / "basemap.json").is_file(),
            "browser": os.environ.get("CHROMIUM") or shutil.which("chromium") or shutil.which("chromium-browser"),
            "state_dir": str(state), "state_dir_mode": state_mode,
            "token_file_present": (state / "web-url.txt").is_file(),
            "note": "Dependencies only; hardware access, reception, and network feeds have not been tested."}


def render(report):
    lines = [f"Python {report['python']} · {report['executable']}", "", "Core"]
    for mode in ("web", "mesh", "terminal", "satellites", "tests"):
        cap = report["capabilities"][mode]
        lines.append(f"  {mode:12} {'ready' if cap['available'] else 'missing: ' + ', '.join(cap['missing'])}")
    lines += ["", "SDR modes (receive-only)"]
    for mode, s in report["sdr_modes"].items():
        state = f"ready ({s['program']})" if s["available"] and s["program"] else "ready" if s["available"] else "MISSING"
        lines.append(f"  {mode:9} {state:18} {s['freq']}")
        lines.append(f"            {s['label']}")
        if not s["available"]:
            lines.append(f"            missing: {', '.join(s['missing'])}")
            lines.append(f"            install: {s['hint']}")
        if mode == "aircraft" and s["available"] and s["program"] == "rtl_adsb":
            lines.append("            note: rtl_adsb is a weak demodulator; readsb or dump1090-fa (AUR) hear far more aircraft.")
    lines += ["", "Other tools"]
    for name, t in report["tools"].items():
        lines.append(f"  {name:15} {'ready' if t['available'] else 'missing: ' + ', '.join(t['missing']) + ' - ' + t['hint']}"
                     f"  ({t['purpose']})")
    have = [f"{n} ({v['signal']})" for n, v in report["not_integrated"].items() if v["installed"]]
    if have:
        lines.append(f"  installed but not driven by the console: {'; '.join(have)}")
    lines += ["", "Hardware (read-only checks; nothing was opened)"]
    serial = report["serial"]
    state = "present" + (", accessible" if serial["accessible"] else ", NOT accessible") if serial["exists"] else "not present"
    lines.append(f"  Serial radio {serial['path']}: {state}")
    for h in serial["holders"]:
        lines.append(f"    held by PID {h['pid']}: {h['cmd']}")
    if serial["hint"]:
        lines.append(f"    {serial['hint']}")
    dongle = report["sdr_dongle"]
    names = ", ".join(f"{d['vid']}:{d['pid']} {d['product']}".strip() for d in dongle["devices"])
    lines.append(f"  RTL-SDR dongle: {names or 'not detected'}")
    for h in dongle["holders"]:
        lines.append(f"    held by PID {h['pid']}: {h['cmd']}")
    if dongle["hint"]:
        lines.append(f"    {dongle['hint']}")
    lines.append(f"  Offline map    {'present' if report['basemap'] else 'missing: run tools/build_basemap.py'}")
    lines.append(f"  Browser        {report['browser'] or 'no system Chromium found'}")
    mode = report["state_dir_mode"]
    private = mode is None or mode == "0o700"
    lines.append(f"  State dir      {report['state_dir']} ({mode or 'not created yet'})"
                 + ("" if private else " - tightened to 0700 on next server start"))
    lines.append(report["note"])
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--port", default="/dev/ttyUSB0")
    parser.add_argument("--state-dir")
    args = parser.parse_args()
    report = inspect(args.port, args.state_dir)
    print(json.dumps(report, indent=2) if args.json else render(report))
    return 0 if report["capabilities"]["web"]["available"] else 1


if __name__ == "__main__":
    sys.exit(main())
