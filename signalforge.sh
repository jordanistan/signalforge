#!/usr/bin/env bash
# Start/stop SignalForge (tools/meshweb.py) in the background.
#
#   ./signalforge.sh start [--bridge] [extra meshweb args]   start it, print the token URL
#   ./signalforge.sh stop                                     stop it and release the radio + SDR dongle
#   ./signalforge.sh restart [...]                            stop, then start with the given args
#   ./signalforge.sh status                                   is it running, who owns the radio
#   ./signalforge.sh url                                      print the private token URL
#   ./signalforge.sh logs                                     follow the log (Ctrl-C to quit)
#
# Settings come from the environment: SERIAL (/dev/ttyUSB0), HTTP_PORT (8765), LOCATION (30.267,-97.743),
# ALLOW (!02ecdcdc, used with --bridge). Only one program may own the radio: this refuses to start while
# another meshweb/meshtui/mesh-bridge or the signalforge systemd service holds it.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="$ROOT/.venv/bin/python"
RUN="$ROOT/run"
PIDFILE="$RUN/signalforge.pid"
LOGFILE="$RUN/signalforge.log"
URLFILE="$RUN/web-url.txt"
SERIAL="${SERIAL:-/dev/ttyUSB0}"
HTTP_PORT="${HTTP_PORT:-8765}"
LOCATION="${LOCATION:-30.267,-97.743}"
ALLOW="${ALLOW:-!02ecdcdc}"
STOP_TIMEOUT=20  # seconds, same as the systemd unit's TimeoutStopSec

die() { echo "error: $*" >&2; exit 1; }

# PID from the pidfile if that process is still our meshweb.py, else nothing (and a stale pidfile is removed).
our_pid() {
    [[ -f "$PIDFILE" ]] || return 0
    local pid
    pid="$(<"$PIDFILE")"
    if [[ "$pid" =~ ^[0-9]+$ ]] && tr '\0' ' ' <"/proc/$pid/cmdline" 2>/dev/null | grep -q 'tools/meshweb.py'; then
        echo "$pid"
    else
        rm -f "$PIDFILE"
    fi
}

# Other SignalForge programs that would fight over the radio, one "PID command" per line.
other_owners() {
    local mine="${1:-}"
    pgrep -af 'tools/(meshweb|meshtui|mesh-bridge)\.py' | grep -v -E "^(${mine:-x}|$$) " | grep -v -E '^[0-9]+ (/usr/bin/)?(ba|z)?sh ' || true
}

start() {
    [[ -x "$PYTHON" ]] || die "no venv at $PYTHON; run 'make setup' first"
    local pid
    pid="$(our_pid)"
    if [[ -n "$pid" ]]; then
        echo "SignalForge is already running (PID $pid)."
        show_url
        return 0
    fi
    if systemctl --user is-active --quiet signalforge 2>/dev/null; then
        die "the signalforge systemd service is running; use 'systemctl --user stop signalforge' first"
    fi
    local others
    others="$(other_owners)"
    [[ -z "$others" ]] || die "another SignalForge program already owns the radio:
$others
Stop it first (or run '$0 stop' if this script started it)."

    local args=(-u tools/meshweb.py -p "$SERIAL" --http-port "$HTTP_PORT" --home "$LOCATION")
    local a
    for a in "$@"; do
        if [[ "$a" == --bridge ]]; then
            args+=(--bridge --allow "$ALLOW")
        else
            args+=("$a")
        fi
    done

    mkdir -p "$RUN"
    chmod 700 "$RUN"
    (umask 077; : >"$LOGFILE")
    cd "$ROOT"
    # setsid: own process group, so stop can clean up any rtl_* children it leaves behind.
    setsid "$PYTHON" "${args[@]}" >>"$LOGFILE" 2>&1 </dev/null &
    pid=$!
    echo "$pid" >"$PIDFILE"

    # Wait for the "Open <url>" line, or an early exit (port in use, bad args, crash).
    local i
    for ((i = 0; i < 100; i++)); do
        if ! kill -0 "$pid" 2>/dev/null; then
            rm -f "$PIDFILE"
            echo "SignalForge exited during startup. Last log lines:" >&2
            tail -n 20 "$LOGFILE" >&2
            exit 1
        fi
        grep -q '^Open ' "$LOGFILE" && break
        sleep 0.1
    done
    echo "SignalForge started (PID $pid). Log: $LOGFILE"
    show_url
}

stop() {
    local pid
    pid="$(our_pid)"
    if [[ -z "$pid" ]]; then
        echo "SignalForge is not running (from this script)."
        local others
        others="$(other_owners)"
        [[ -z "$others" ]] || echo "Other SignalForge processes (not started here, left alone):
$others"
        return 0
    fi
    # SIGTERM to the server only, so it can stop its SDR receivers and close the radio itself.
    kill -TERM "$pid" 2>/dev/null || true
    local i
    for ((i = 0; i < STOP_TIMEOUT * 10; i++)); do
        kill -0 "$pid" 2>/dev/null || break
        sleep 0.1
    done
    if kill -0 "$pid" 2>/dev/null; then
        echo "Still running after ${STOP_TIMEOUT}s; killing it."
        kill -KILL "$pid" 2>/dev/null || true
    fi
    # Anything left in its process group (a stuck rtl_* child) would keep the dongle busy.
    kill -KILL -- "-$pid" 2>/dev/null || true
    rm -f "$PIDFILE"
    echo "SignalForge stopped."
}

status() {
    local pid
    pid="$(our_pid)"
    if [[ -n "$pid" ]]; then
        echo "SignalForge is running (PID $pid): $(tr '\0' ' ' <"/proc/$pid/cmdline")"
        show_url
    else
        echo "SignalForge is not running (from this script)."
    fi
    if systemctl --user is-active --quiet signalforge 2>/dev/null; then
        echo "The signalforge systemd service is active."
    fi
    local others
    others="$(other_owners "$pid")"
    [[ -z "$others" ]] || echo "Other SignalForge processes:
$others"
    if command -v fuser >/dev/null && [[ -e "$SERIAL" ]]; then
        local owner
        owner="$(fuser "$SERIAL" 2>/dev/null | xargs || true)"
        if [[ -n "$owner" ]]; then echo "$SERIAL: held by PID $owner"; else echo "$SERIAL: free"; fi
    fi
}

show_url() {
    # The log has the URL this run is serving (--demo and --state-dir keep web-url.txt elsewhere).
    local url=""
    [[ -z "$(our_pid)" || ! -f "$LOGFILE" ]] || url="$(sed -n 's/^Open //p' "$LOGFILE" | tail -n 1)"
    if [[ -n "$url" ]]; then
        echo "Open: $url"
    elif [[ -f "$URLFILE" ]]; then
        echo "Open: $(<"$URLFILE")"
    else
        echo "No token URL yet ($URLFILE)."
    fi
}

cmd="${1:-}"
shift || true
case "$cmd" in
    start) start "$@" ;;
    stop) stop ;;
    restart) stop; start "$@" ;;
    status) status ;;
    url) show_url ;;
    logs) touch "$LOGFILE"; exec tail -n 50 -f "$LOGFILE" ;;
    *) sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
