#!/usr/bin/env bash
# Upgrade a Heltec V3 (ESP32-S3) to the firmware in firmware/<VERSION>/, keeping its config.
# Usage: tools/upgrade-heltec-v3.sh [-p PORT] [-v VERSION_DIR]
# Steps: port check -> config backup -> md5 verify -> flash app at 0x10000 -> confirm version.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PORT=/dev/ttyUSB0
VERSION_DIR=v2.7.26

while getopts "p:v:" opt; do
    case "$opt" in
        p) PORT=$OPTARG ;;
        v) VERSION_DIR=$OPTARG ;;
        *) echo "Usage: $0 [-p PORT] [-v VERSION_DIR]" >&2; exit 1 ;;
    esac
done

FW_DIR="$ROOT/firmware/$VERSION_DIR"
BIN=$(ls "$FW_DIR"/firmware-heltec-v3-*.bin | grep -v factory)
JSON=$(ls "$FW_DIR"/firmware-heltec-v3-*.mt.json)
BACKUP_DIR="$ROOT/backups"
mkdir -p "$BACKUP_DIR"

# 1. Port must exist and not be held by another process (browser Web Serial tabs are the usual culprit)
[ -e "$PORT" ] || { echo "No device at $PORT" >&2; exit 1; }
if holder=$(fuser "$PORT" 2>/dev/null) && [ -n "$holder" ]; then
    echo "$PORT is in use by PID(s): $holder ($(ps -o comm= -p ${holder%% *}))." >&2
    echo "Close any flasher/Meshtastic web page and try again." >&2
    exit 1
fi

# 2. Back up config (also proves we can talk to the device); refuse to continue on failure
NODE=$(meshtastic --port "$PORT" --info 2>/dev/null | sed -n 's/^Owner: .*(\(.*\))$/\1/p')
BACKUP="$BACKUP_DIR/${NODE:-unknown}-$(date +%Y%m%d-%H%M%S).yaml"
meshtastic --port "$PORT" --export-config > "$BACKUP"
if [ "$(wc -c < "$BACKUP")" -lt 500 ] || grep -q "OS Error" "$BACKUP"; then
    echo "Config backup looks bad ($BACKUP), aborting." >&2
    exit 1
fi
echo "Config backed up to $BACKUP"

# 3. Verify the image against the md5 in the release manifest
want=$(python -c "
import json,sys,os
for f in json.load(open('$JSON'))['files']:
    if f['name']==os.path.basename('$BIN'): print(f['md5'])")
have=$(md5sum "$BIN" | cut -d' ' -f1)
[ "$want" = "$have" ] || { echo "md5 mismatch for $BIN" >&2; exit 1; }

# 4. Flash the app partition only; NVS (config, keys) and the node DB are left intact
"$FW_DIR/device-update.sh" -p "$PORT" -f "$BIN"

# 5. Wait for reboot and confirm
echo "Waiting for device to reboot..."
sleep 12
meshtastic --port "$PORT" --info 2>/dev/null | grep -E '^Owner|firmwareVersion'
