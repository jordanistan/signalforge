#!/usr/bin/env bash
# Stop the SignalForge started by start.sh / signalforge.sh, releasing the radio and SDR dongle.
exec "$(dirname "${BASH_SOURCE[0]}")/signalforge.sh" stop
