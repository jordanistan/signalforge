#!/usr/bin/env python3
"""Read-only setup diagnostics: never opens a radio, starts an SDR, or contacts the network."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
PACKAGES = {
    "web": ("aiohttp",), "mesh": ("meshtastic",), "terminal": ("textual",),
    "satellites": ("skyfield",), "aircraft": ("pyModeS",),
    "tests": ("pytest", "pytest_asyncio", "playwright"),
}
COMMANDS = {
    "survey": ("rtl_power",), "audio": ("rtl_fm", "ffmpeg"), "aircraft": ("rtl_adsb",),
    "pager": ("rtl_fm", "multimon-ng"), "sensors": ("rtl_433",), "syntax_checks": ("node", "bash"),
}


def inspect(port="/dev/ttyUSB0", state_dir=None):
    modes = {}
    for mode in dict.fromkeys([*PACKAGES, *COMMANDS]):
        missing = [p for p in PACKAGES.get(mode, ()) if importlib.util.find_spec(p) is None]
        missing += [p for p in COMMANDS.get(mode, ()) if shutil.which(p) is None]
        modes[mode] = {"available": not missing, "missing": missing}
    state = Path(state_dir) if state_dir else ROOT / "run"
    return {"python": sys.version.split()[0], "executable": sys.executable, "capabilities": modes,
            "serial": {"path": port, "exists": Path(port).exists(), "accessible": os.access(port, os.R_OK | os.W_OK)},
            "basemap": (ROOT / "data" / "basemap.json").is_file(),
            "browser": os.environ.get("CHROMIUM") or shutil.which("chromium") or shutil.which("chromium-browser"),
            "state_dir": str(state), "token_file_present": (state / "web-url.txt").is_file(),
            "note": "Dependencies only; hardware access, reception, and network feeds have not been tested."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--port", default="/dev/ttyUSB0")
    parser.add_argument("--state-dir")
    args = parser.parse_args()
    report = inspect(args.port, args.state_dir)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(f"Python {report['python']} · {report['executable']}")
        for mode, cap in report["capabilities"].items():
            print(f"{mode:14} {'ready' if cap['available'] else 'missing: ' + ', '.join(cap['missing'])}")
        serial = report["serial"]
        print(f"Serial         {serial['path']}: {'accessible' if serial['accessible'] else 'missing or not accessible'}")
        print(f"Offline map    {'present' if report['basemap'] else 'missing: run tools/build_basemap.py'}")
        print(f"Browser        {report['browser'] or 'no system Chromium found'}")
        print(report["note"])
    return 0 if report["capabilities"]["web"]["available"] else 1


if __name__ == "__main__":
    sys.exit(main())
