#!/usr/bin/env bash
# Queue a reply for the bridge (meshweb.py --bridge or mesh-bridge.py) to send. Usage: tools/mesh-reply.sh "text"
# One radio message holds ~200 bytes; the bridge sends 180 UTF-8 bytes at most per message. Longer text is refused
# (shorten it, or send several calls) unless MESH_ALLOW_SPLIT=1, in which case the bridge splits it at word breaks.
set -euo pipefail
umask 077
text="$*"
export LC_ALL=C
if [[ -z "${text//[[:space:]]/}" ]]; then
    echo "mesh-reply: empty message (not sent)." >&2
    exit 2
fi
if [ "${#text}" -gt 180 ] && [ "${MESH_ALLOW_SPLIT:-0}" != 1 ]; then
    echo "mesh-reply: ${#text} bytes is over the 180 limit for one radio message; shorten it (not sent)." >&2
    exit 2
fi
OUTBOX="${MESH_OUTBOX:-$(cd "$(dirname "$0")/.." && pwd)/run/outbox}"
mkdir -p "$OUTBOX"
f=$(mktemp "$OUTBOX/$(date +%s)-XXXXXX.tmp")
trap 'rm -f -- "$f"' EXIT
printf '%s' "$text" > "$f"
mv "$f" "${f%.tmp}"  # atomic, so the bridge never reads a half-written file
