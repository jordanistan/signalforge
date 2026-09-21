"""Shared logic for the Meshtastic front ends (tools/meshtui.py terminal UI, tools/meshweb.py web GUI).

Pure helpers (distances, node freshness, conversation routing) plus SerialBackend, the thin wrapper around
meshtastic's SerialInterface. Nothing here imports a UI toolkit.
"""
import math
import os
import time
import copy

BROADCAST = "^all"
RECENT_SECS = 2 * 3600
MAX_BYTES = 200
BRIDGE_BYTES = 180


def truncate_utf8(text, limit=MAX_BYTES):
    """Fit a payload into its byte budget without splitting a UTF-8 character."""
    return text.encode("utf-8")[:limit].decode("utf-8", errors="ignore")


def split_for_radio(text, limit=BRIDGE_BYTES):
    """Split on words when possible, with a strict UTF-8 byte budget per packet."""
    if limit < 4:
        raise ValueError("packet budget must accommodate a UTF-8 character")
    parts, current = [], ""
    for word in text.split():
        while len(word.encode("utf-8")) > limit:
            if current:
                parts.append(current)
                current = ""
            chunk = truncate_utf8(word, limit)
            parts.append(chunk)
            word = word[len(chunk):]
        if not word:
            continue
        candidate = f"{current} {word}" if current else word
        if len(candidate.encode("utf-8")) <= limit:
            current = candidate
        else:
            parts.append(current)
            current = word
    if current:
        parts.append(current)
    return parts


def parse_home(value):
    """Validate LAT,LON for all command-line entrypoints."""
    try:
        lat, lon = (float(v) for v in value.split(","))
    except (ValueError, AttributeError):
        raise ValueError("home must be LAT,LON, for example 30.267,-97.743") from None
    if not math.isfinite(lat) or not math.isfinite(lon) or not -90 <= lat <= 90 or not -180 <= lon <= 180:
        raise ValueError("home latitude must be -90..90 and longitude -180..180")
    return (lat, lon)


def haversine_km(a, b):
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(max(0, min(1, h))))


def bearing(a, b):
    """Initial compass bearing a -> b in radians (0 = north, clockwise)."""
    p1, p2, dl = math.radians(a[0]), math.radians(b[0]), math.radians(b[1] - a[1])
    return math.atan2(math.sin(dl) * math.cos(p2), math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl))


def position(node):
    p = (node or {}).get("position") or {}
    if "latitude" in p and "longitude" in p:
        lat, lon = p["latitude"], p["longitude"]
        if (all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in (lat, lon))
                and -90 <= lat <= 90 and -180 <= lon <= 180):
            return (lat, lon)
    return None


def age(ts, now=None):
    if not ts:
        return "-"
    return dur(max(0, int((now or time.time()) - ts)))


def dur(s):
    s = int(s)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h"
    return f"{s // 86400}d"


def node_class(n, now=None):
    """live = heard within RECENT_SECS, day = within 24h, old = a stale entry the radio still remembers."""
    lh = n.get("lastHeard") or 0
    a = (now or time.time()) - lh if lh else 1e12
    return "live" if a < RECENT_SECS else "day" if a < 86400 else "old"


def nearest_place(lat, lon, places, max_km=400):
    """(name, state, km) of the closest known city, or None."""
    best = None
    for name, st, plat, plon, _pop in places:
        if abs(plat - lat) > 4 or abs(plon - lon) > 5:  # cheap reject before the trig
            continue
        d = haversine_km((lat, lon), (plat, plon))
        if best is None or d < best[2]:
            best = (name, st, d)
    return best if best and best[2] <= max_km else None


def place_text(near):
    if not near:
        return ""
    name, st, km = near
    where = f"{name}, {st}" if st else name
    return f"in {where}" if km < 3 else f"{km:.0f} km from {where}"


def is_broadcast(to):
    return to in (BROADCAST, None, 0xFFFFFFFF, "!ffffffff")


def conv_of(rec):
    """Which conversation a message belongs to: the peer node for DMs, BROADCAST for the channel."""
    if rec["dir"] != "in":
        return BROADCAST if is_broadcast(rec["to"]) else rec["to"]
    return BROADCAST if is_broadcast(rec["to"]) else rec["from"]


def resolve_port(port, exists=os.path.exists, find=None):
    """The serial path to open. If `port` is gone (a USB hub/adapter renumbered the device), fall back to the
    single Meshtastic-looking serial device that is present; refuse to guess between several."""
    if port and exists(port):
        return port
    if find is None:
        from meshtastic.util import findPorts
        find = findPorts
    found = find(True)
    if len(found) == 1:
        return found[0]
    raise OSError(f"{port or 'no port given'} is not present and {len(found)} candidate radios were found")


class SerialBackend:
    """Thin wrapper over meshtastic's SerialInterface; on_text(packet) runs on the library's thread.
    Set `on_lost` to be called (on a library thread) when the link to the radio drops."""

    def __init__(self, port, on_text):
        from pubsub import pub
        import meshtastic.serial_interface

        self._on_text = on_text
        self.on_lost = None
        port = resolve_port(port)
        pub.subscribe(self._receive, "meshtastic.receive.text")
        pub.subscribe(self._lost, "meshtastic.connection.lost")
        try:
            self.iface = meshtastic.serial_interface.SerialInterface(port)
        except Exception:
            self.close()
            raise

    def _receive(self, packet, interface):
        if interface is getattr(self, "iface", None):
            self._on_text(packet)

    def _lost(self, interface):
        if interface is getattr(self, "iface", None) and self.on_lost:
            self.on_lost()

    def alive(self):
        """False once the library's reader thread has stopped (the USB device went away)."""
        t = getattr(self.iface, "_rxThread", None)
        return True if t is None else t.is_alive()

    @property
    def my_id(self):
        return self.iface.getMyNodeInfo()["user"]["id"]

    def nodes(self):
        return copy.deepcopy(self.iface.nodes or {})

    def send_text(self, text, dest, on_ack=None):
        if dest == BROADCAST:
            self.iface.sendText(text)
        else:
            self.iface.sendText(text, destinationId=dest, wantAck=True, onResponse=on_ack)

    def close(self):
        from pubsub import pub

        for topic, fn in (("meshtastic.receive.text", self._receive), ("meshtastic.connection.lost", self._lost)):
            try:
                pub.unsubscribe(fn, topic)
            except Exception:
                pass
        try:
            self.iface.close()
        except Exception:
            pass  # already gone: that is why we are closing
