#!/usr/bin/env bash
# Start SignalForge in the background and print the token URL.
#   ./start.sh             full console
#   ./start.sh --bridge    bridge mode (allows $ALLOW, default !02ecdcdc)
# Any other arguments go to tools/meshweb.py. See signalforge.sh for settings and status/logs/url.
exec "$(dirname "${BASH_SOURCE[0]}")/signalforge.sh" start "$@"
