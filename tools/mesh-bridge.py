#!/usr/bin/env python3
"""Headless mesh bridge with the web server's durable queue and link recovery.

Usage: .venv/bin/python tools/mesh-bridge.py [-p PORT] [-a !nodeid ...] [--state-dir DIR]
Reply with tools/mesh-reply.sh; for custom state set MESH_OUTBOX=DIR/outbox.
This prints allowed messages and follow events; it does not run a research worker.
"""
import argparse
import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VENV_PYTHON = os.path.join(ROOT, ".venv", "bin", "python")
if __name__ == "__main__" and sys.prefix == sys.base_prefix and os.path.exists(VENV_PYTHON):
    os.execv(VENV_PYTHON, [VENV_PYTHON, *sys.argv])

from meshcore import SerialBackend
from meshweb import Hub
from brand import PRODUCT_NAME


async def run(args):
    state = os.path.abspath(os.path.expanduser(args.state_dir))
    hub = Hub(lambda on_packet: SerialBackend(args.port, on_packet), allow=args.allow, bridge=True,
              log_path=os.path.join(state, "messages.jsonl"), nodes_json=os.path.join(state, "nodes.json"),
              follows_path=os.path.join(state, "follows.json"), outbox=os.path.join(state, "outbox"))
    try:
        await hub.start()
        await asyncio.Event().wait()
    finally:
        await hub.stop()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-p", "--port", default="/dev/ttyUSB0")
    parser.add_argument("-a", "--allow", nargs="+", default=["!02ecdcdc"])
    parser.add_argument("--state-dir", default=os.path.join(ROOT, "run"))
    args = parser.parse_args()
    if any(len(n) != 9 or n[0] != "!" or any(c not in "0123456789abcdef" for c in n[1:]) for n in args.allow):
        parser.error("--allow values must be ! followed by eight lowercase hexadecimal digits")
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        print(f"{PRODUCT_NAME} bridge stopped", flush=True)


if __name__ == "__main__":
    main()
