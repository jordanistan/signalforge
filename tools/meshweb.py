#!/usr/bin/env python3
"""SignalForge browser console for a serial-attached Meshtastic node, with an optional bridge.

One process owns the serial port and serves a browser UI (map, nodes, per-node message threads) over
http://127.0.0.1:PORT. It listens on this computer only and requires a token (printed at start; the browser
keeps it in a cookie), because the page can transmit on your radio.

With --bridge it also does what tools/mesh-bridge.py does, so a Claude session can chat with a remote node
while the GUI is open: text from --allow nodes is printed as `MSG from ...` lines, ignored senders as
`IGNORED ...`, and files dropped in run/outbox/ (tools/mesh-reply.sh) are sent to the first allowed node.
Follow mode: an allowed node can text `follow <topic>`, `unfollow <id|topic|all>` or `following`; the server keeps the
list (tools/meshfollow.py), answers over the radio, and prints `FOLLOW ...` events for the Claude session, which
does the periodic web checks and texts only when there is news.

Usage: tools/meshweb.py [-p PORT] [--http-port 8765] [--bridge] [--allow !nodeid ...] [--home LAT,LON] [--demo]
"""
import argparse
import asyncio
import hmac
import json
import os
import secrets
import sys
import time
import uuid
from collections import deque
from urllib.parse import urlsplit

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VENV_PYTHON = os.path.join(ROOT, ".venv", "bin", "python")
if __name__ == "__main__" and sys.prefix == sys.base_prefix and os.path.exists(VENV_PYTHON):
    os.execv(VENV_PYTHON, [VENV_PYTHON, *sys.argv])

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from aiohttp import WSCloseCode, WSMsgType, web  # noqa: E402

from meshcore import (BROADCAST, SerialBackend, is_broadcast, nearest_place, node_class,  # noqa: E402
                      place_text, position, parse_home, split_for_radio, truncate_utf8,
                      MAX_BYTES, BRIDGE_BYTES)
from radiowatch import DEFAULT_REFERENCE, RadioWatch  # noqa: E402
from weather import WeatherService  # noqa: E402
from meshfollow import (INTERVAL_MIN, MAX_FOLLOWS, TTL_SECS, FollowStore, parse_follow_command,  # noqa: E402
                        PATH as FOLLOWS_PATH)
from brand import PRODUCT_NAME, TAGLINE  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB_DIR = os.path.join(ROOT, "web")
LOG_PATH = os.path.join(ROOT, "run", "messages.jsonl")
OUTBOX = os.path.join(ROOT, "run", "outbox")
NODES_JSON = os.path.join(ROOT, "run", "nodes.json")
BASEMAP_PATH = os.path.join(ROOT, "data", "basemap.json")
CHUNK = BRIDGE_BYTES
LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]"}
SEND_LIMIT = (20, 60.0)  # at most 20 browser sends per 60 s: the radio is a shared airtime resource


class HubError(Exception):
    """A user-facing problem (bad input, radio not connected); the text is shown in the browser."""


def node_view(nid, n, now, places=None, near_cache=None):
    """Compact JSON-safe description of one node for the browser."""
    user = n.get("user") or {}
    dm = n.get("deviceMetrics") or {}
    p = position(n)
    near = ""
    if p and places:
        key = (round(p[0], 3), round(p[1], 3))
        if near_cache is not None and key in near_cache:
            near = near_cache[key]
        else:
            near = place_text(nearest_place(p[0], p[1], places))
            if near_cache is not None:
                near_cache[key] = near
    return {
        "id": nid, "short": user.get("shortName") or "?", "long": user.get("longName") or "",
        "hw": user.get("hwModel") or "", "lat": p[0] if p else None, "lon": p[1] if p else None,
        "snr": n.get("snr"), "hops": n.get("hopsAway"), "heard": n.get("lastHeard") or 0,
        "batt": dm.get("batteryLevel"), "volt": dm.get("voltage"), "chutil": dm.get("channelUtilization"),
        "airtx": dm.get("airUtilTx"), "uptime": dm.get("uptimeSeconds"), "cls": node_class(n, now), "near": near,
    }


class Hub:
    """Owns the radio, the message log and the connected browsers."""

    def __init__(self, backend_factory, log_path=LOG_PATH, allow=(), bridge=False, outbox=OUTBOX,
                 nodes_json=NODES_JSON, places=None, home=None, out=None, follows_path=None, demo=False):
        self.backend_factory = backend_factory
        self.log_path, self.outbox, self.nodes_json = log_path, outbox, nodes_json
        self.allow, self.bridge, self.places, self.home = list(allow), bridge, places or [], home
        self.out = out or (lambda line: print(line, flush=True))
        self.backend, self.my_id, self.error = None, None, None
        self.clients = set()
        self.msgs = deque(maxlen=5000)
        self.nodes = []  # latest node_view list
        self.hist = {}  # node id -> deque of recent SNR values
        self._last_heard = {}
        self._sends = deque()
        self._near_cache = {}
        self._tasks = []
        self._events = set()
        self._stopping = False
        self.loop = None
        self.radio = None  # optional RadioWatch (SDR panel), attached by main()
        self.weather = None  # optional WeatherService, attached by main()
        self.sats = None  # optional SatTracker, attached by main()
        self.demo = demo
        self.capability_errors = {}
        self._weather_asked = 0.0
        self.follows = FollowStore(follows_path or FOLLOWS_PATH)
        self._last_follow_view = None
        self.follow_check_secs = 60  # how often expired follows are swept
        self.reconnect_secs = 3  # how often to retry when the radio is missing
        self._reconnecting = False
        self.poll_secs = 3  # node table refresh
        self.chunk_delay = 3  # pause between bridge chunks so they arrive in order

    # ---- lifecycle -------------------------------------------------------------------------
    async def start(self):
        self.loop = asyncio.get_running_loop()
        self._load_history()
        try:
            self.backend, self.my_id = await self.loop.run_in_executor(None, self._connect_backend)
            self._hook_backend()
        except Exception as e:  # port busy, no device...
            self.error = f"Could not open radio: {e}"
            self.out(f"ERROR {self.error}")
            self._begin_reconnect()  # keep trying: the radio may just not be plugged in yet
        self._poll_nodes_once()
        self._tasks.append(asyncio.create_task(self._poll_nodes()))
        if self.weather:
            await self.weather.start()
        if self.bridge:
            self._tasks.append(asyncio.create_task(self._poll_outbox()))
            self._tasks.append(asyncio.create_task(self._expire_follows()))
            self.out(f"{PRODUCT_NAME} bridge up · listening for {', '.join(self.allow) or 'nobody (no --allow)'}")

    def _connect_backend(self):
        """A failed node-info handshake must release the port before reconnecting."""
        backend = self.backend_factory(self._on_packet_thread)
        try:
            return backend, backend.my_id
        except Exception:
            try:
                backend.close()
            except Exception:
                pass
            raise

    async def stop(self):
        self._stopping = True
        tasks = [*self._tasks, *self._events]
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self.weather:
            await self.weather.stop()
        if self.radio:
            await self.radio.shutdown()  # let go of the SDR dongle
        if self.backend:
            await self.loop.run_in_executor(None, self.backend.close)

    # ---- radio link: lost / reconnect --------------------------------------------------------
    def _hook_backend(self):
        if hasattr(self.backend, "on_lost"):
            self.backend.on_lost = self._on_lost_thread

    def _on_lost_thread(self):  # runs on meshtastic's thread
        self._from_thread(self._radio_lost, "link lost")

    def _from_thread(self, fn, *args):
        if self.loop and not self._stopping and not self.loop.is_closed():
            self.loop.call_soon_threadsafe(self._dispatch, fn, *args)

    def _dispatch(self, fn, *args):
        if self._stopping:
            return
        task = asyncio.create_task(fn(*args))
        self._events.add(task)
        def done(t):
            self._events.discard(t)
            if not t.cancelled() and t.exception():
                self.out(f"ERROR background event: {t.exception()}")
        task.add_done_callback(done)

    async def _radio_lost(self, why):
        """The serial link dropped (unplugged, hub re-enumerated...). Say so, stop pretending, and reconnect."""
        if self._reconnecting or self.backend is None:
            return
        old, self.backend = self.backend, None
        self.error = f"Radio disconnected ({why}); reconnecting…"
        self.out(f"ERROR {self.error}")
        try:
            await self.loop.run_in_executor(None, old.close)
        except Exception:
            pass
        await self.broadcast(self.state())
        self._begin_reconnect()

    def _begin_reconnect(self):
        if not self._reconnecting:
            self._reconnecting = True
            self._tasks.append(asyncio.create_task(self._reconnect()))

    async def _reconnect(self):
        while True:
            await asyncio.sleep(self.reconnect_secs)
            try:
                backend, my_id = await self.loop.run_in_executor(None, self._connect_backend)
            except Exception:
                continue  # still missing or busy; the banner already says so
            self.backend, self.my_id, self.error = backend, my_id, None
            self._hook_backend()
            self._reconnecting = False
            self._poll_nodes_once()
            self.out("BRIDGE radio reconnected")
            await self.broadcast(self.state())
            return

    def location(self):
        """Where to look up weather and satellite passes: the radio's GPS fix, else --home, else downtown Austin."""
        me = next((n for n in self.nodes if n.get("id") == self.my_id), None)
        if me and me.get("lat") is not None and me.get("lon") is not None:
            return (me["lat"], me["lon"])
        return tuple(self.home) if self.home else DEFAULT_REFERENCE

    def location_view(self):
        lat, lon = self.location()
        me = next((n for n in self.nodes if n.get("id") == self.my_id), None)
        source = "radio" if me and me.get("lat") is not None and me.get("lon") is not None else "home" if self.home else "assumed"
        return {"lat": lat, "lon": lon, "source": source}

    def on_weather_alert(self, a):
        if self.bridge:
            self.out(f"WEATHER alert {a['severity']}: {a['event']} - {a['headline'][:140]}")

    # ---- state -----------------------------------------------------------------------------
    def state(self):
        return {"type": "state", "me": self.my_id, "error": self.error, "nodes": self.nodes,
                "demo": self.demo, "location": self.location_view(),
                "capabilities": {"weather": self.weather is not None, "sats": self.sats is not None},
                "capability_errors": self.capability_errors,
                "messages": list(self.msgs)[-500:], "home": self.home, "allow": self.allow, "now": time.time(),
                "follows": self.follow_view(), "radio": self.radio.view() if self.radio else None,
                "aircraft": self.radio.aircraft_view() if self.radio else None,
                "job": self.radio.job_view() if self.radio else None,
                "weather": self.weather.view() if self.weather else None}

    def follow_view(self):
        return [{"id": f["id"], "topic": f["topic"], "node": f["node"], "expires": f["expires"],
                 "last_check": f.get("last_check"), "last_report": f.get("last_report", "")}
                for f in self.follows.list()]

    async def broadcast(self, obj):
        for ws in list(self.clients):
            try:
                await ws.send_json(obj)
            except Exception:
                self.clients.discard(ws)

    def _load_history(self):
        try:
            with open(self.log_path, encoding="utf-8") as f:
                for line in f:
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue  # a torn write must not hide subsequent messages
                    if not isinstance(rec, dict):
                        continue
                    if rec.get("type") == "delivery" and rec.get("delivery") in {"sent", "delivered", "failed", "pending", "simulated"}:
                        for stored in reversed(self.msgs):
                            if stored.get("id") == rec.get("id"):
                                stored.update(delivery=rec["delivery"], reason=rec.get("reason"))
                                break
                    elif (rec.get("dir") in ("in", "out") and isinstance(rec.get("text"), str)
                          and "from" in rec and "to" in rec and isinstance(rec.get("ts"), (float, int))):
                        self.msgs.append(rec)
        except (OSError, ValueError):
            pass

    def _store(self, rec):
        self.msgs.append(rec)
        self._append_log(rec)

    def _append_log(self, rec):
        os.makedirs(os.path.dirname(self.log_path), exist_ok=True)
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")

    async def _delivery(self, rec, status, reason=None):
        rec.update(delivery=status, reason=reason)
        self._append_log({"type": "delivery", "id": rec["id"], "delivery": status, "reason": reason})
        await self.broadcast({"type": "ack", "id": rec["id"], "ok": status == "delivered", "reason": reason})

    # ---- nodes -----------------------------------------------------------------------------
    def _poll_nodes_once(self):
        if not self.backend:
            return None
        raw = self.backend.nodes()
        now = time.time()
        for nid, n in raw.items():
            heard, snr = n.get("lastHeard") or 0, n.get("snr")
            if snr is not None and heard != self._last_heard.get(nid):
                self.hist.setdefault(nid, deque(maxlen=40)).append(snr)
            self._last_heard[nid] = heard
        views = []
        for nid, n in raw.items():
            v = node_view(nid, n, now, self.places, self._near_cache)
            v["me"] = nid == self.my_id
            v["hist"] = list(self.hist.get(nid, ()))
            views.append(v)
        views.sort(key=lambda v: -v["heard"])
        changed = json.dumps(views, sort_keys=True) != json.dumps(self.nodes, sort_keys=True)
        self.nodes = views
        try:  # snapshot for other tools/sessions (same format the old bridge wrote)
            os.makedirs(os.path.dirname(self.nodes_json), exist_ok=True)
            tmp = self.nodes_json + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"written": now, "nodes": raw}, f, default=str)
            os.replace(tmp, self.nodes_json)
        except OSError:
            pass
        return changed

    async def _poll_nodes(self):
        while True:
            await asyncio.sleep(self.poll_secs)
            try:
                if self.backend is not None and hasattr(self.backend, "alive") and not self.backend.alive():
                    await self._radio_lost("reader stopped")  # backstop if the library's lost-link event never came
                if self._poll_nodes_once():
                    await self.broadcast({"type": "nodes", "nodes": self.nodes, "now": time.time()})
                fv = self.follow_view()  # the periodic check job writes reports to the follow file
                if fv != self._last_follow_view:
                    self._last_follow_view = fv
                    await self.broadcast({"type": "follows", "follows": fv})
            except Exception as e:
                self.out(f"ERROR node poll: {e}")

    # ---- messages --------------------------------------------------------------------------
    def _on_packet_thread(self, packet):  # runs on meshtastic's reader thread
        self._from_thread(self._incoming, packet)

    async def _incoming(self, packet):
        # Stamp with this computer's clock: a radio without GPS/RTC has a clock that jumps by minutes, which
        # would misorder messages and confuse unread tracking. The radio's own time is kept for reference.
        rec = {"ts": time.time(), "from": packet.get("fromId"), "to": packet.get("toId"),
               "text": (packet.get("decoded") or {}).get("text", ""), "dir": "in", "id": uuid.uuid4().hex[:12]}
        if packet.get("rxTime"):
            rec["radio_ts"] = packet["rxTime"]
        self._store(rec)
        await self.broadcast({"type": "message", "msg": rec})
        if self.bridge:  # text from strangers is logged, never forwarded to the Claude session
            if rec["from"] in self.allow:
                cmd = parse_follow_command(rec["text"])
                if cmd:
                    await self._follow_command(cmd[0], cmd[1], rec["from"])
                else:
                    self.out(f"MSG from {rec['from']}: {rec['text']}")
            else:
                self.out(f"IGNORED message from {rec['from']} ({len(rec['text'])} chars)")

    async def _follow_command(self, cmd, arg, sender):
        """follow / unfollow / following, from an allowed node only. Answered here, over the radio."""
        def short(t, n=32):
            return t if len(t) <= n else t[:n - 1] + "…"

        hours = TTL_SECS // 3600
        if cmd == "follow":
            status, f = self.follows.add(arg, sender)
            if status == "bad":
                reply = "Follow what? Example: follow austin traffic"
            elif status == "limit":
                reply = f"Already following {MAX_FOLLOWS} topics. Text 'following' to list, 'unfollow <id>' to free one."
            elif status == "exists":
                reply = f"Already following '{short(f['topic'])}' ({f['id']}); renewed for {hours}h."
                self.out(f"FOLLOW renew {f['id']} topic={f['topic']!r}")
            else:
                reply = (f"Following '{short(f['topic'])}' ({f['id']}). I check ~every {INTERVAL_MIN} min and text only "
                         f"when there's news. 'unfollow {f['id']}' stops; ends in {hours}h.")
                self.out(f"FOLLOW start {f['id']} topic={f['topic']!r}")
        elif cmd == "unfollow":
            gone = self.follows.remove(arg, node=sender)
            for f in gone:
                self.out(f"FOLLOW stop {f['id']} topic={f['topic']!r}")
            reply = ("Stopped: " + "; ".join(f"{f['id']} {short(f['topic'], 24)}" for f in gone)) if gone \
                else "Not following that. Text 'following' to see your list."
        else:  # list
            mine = [f for f in self.follows.list() if f["node"] == sender]
            reply = ("Following: " + "; ".join(f"{f['id']} {short(f['topic'], 24)}" for f in mine)) if mine \
                else "Not following anything. Text 'follow <topic>' to start."
        try:
            await self.send(truncate_utf8(reply, CHUNK), sender, src="follow")
        except HubError as e:
            self.out(f"ERROR follow reply not sent: {e}")
        await self.broadcast({"type": "follows", "follows": self.follow_view()})

    async def _expire_follows(self):
        while True:
            await asyncio.sleep(self.follow_check_secs)
            try:
                gone = self.follows.expire()
                for f in gone:
                    self.out(f"FOLLOW expired {f['id']} topic={f['topic']!r}")
                    t = f["topic"] if len(f["topic"]) <= 32 else f["topic"][:31] + "…"
                    await self.send(truncate_utf8(f"Stopped following '{t}' (time limit). Text 'follow {t}' to renew."), f["node"], src="follow")
                if gone:
                    await self.broadcast({"type": "follows", "follows": self.follow_view()})
            except Exception as e:
                self.out(f"ERROR follow sweep: {e}")

    async def send(self, text, dest, src="web"):
        if not isinstance(text, str) or not text.strip():
            raise HubError("Empty message.")
        text = text.strip()
        if len(text.encode()) > MAX_BYTES:
            raise HubError(f"Too long ({len(text.encode())} bytes, max {MAX_BYTES}).")
        if not (dest == BROADCAST or (isinstance(dest, str) and len(dest) == 9 and dest[0] == "!"
                                      and all(c in "0123456789abcdef" for c in dest[1:]))):
            raise HubError("Bad destination.")
        if not self.backend:
            raise HubError(self.error or "Radio not connected.")
        if src == "web":
            now = time.time()
            while self._sends and now - self._sends[0] > SEND_LIMIT[1]:
                self._sends.popleft()
            if len(self._sends) >= SEND_LIMIT[0]:
                raise HubError(f"Slow down: at most {SEND_LIMIT[0]} messages per minute from the browser.")
            self._sends.append(now)
        rec = {"ts": time.time(), "from": self.my_id, "to": dest, "text": text, "dir": "out",
               "id": uuid.uuid4().hex[:12], "delivery": "simulated" if self.demo else "pending"}
        if self.demo:
            rec["demo"] = True
        if src != "web":
            rec["src"] = src
        self._store(rec)
        await self.broadcast({"type": "message", "msg": rec})

        def on_ack(packet):  # meshtastic thread
            reason = ((packet.get("decoded") or {}).get("routing") or {}).get("errorReason", "NONE")
            self._from_thread(self._delivery, rec, "delivered" if reason == "NONE" else "failed", reason)

        try:
            await self.loop.run_in_executor(None, self.backend.send_text, text, dest, on_ack)
        except Exception as e:
            await self._delivery(rec, "failed", f"send failed: {e}")
            raise HubError(f"Send failed: {e}")
        if dest == BROADCAST and not self.demo:
            rec["delivery"] = "sent"
            self._append_log({"type": "delivery", "id": rec["id"], "delivery": "sent"})
        return rec

    async def _poll_outbox(self):
        """Claude bridge: send anything dropped into run/outbox/ to the first allowed node."""
        os.makedirs(self.outbox, mode=0o700, exist_ok=True)
        while True:
            await self._drain_outbox_once()
            await asyncio.sleep(1)

    async def _drain_outbox_once(self):
        """Persist the unsent remainder after each accepted chunk; retry after a link loss."""
        if not self.allow or self.backend is None:
            if not self.allow and any(not n.startswith(".") and not n.endswith(".tmp") for n in os.listdir(self.outbox)):
                if not getattr(self, "_outbox_warned", False):
                    self.out("ERROR replies retained: no --allow node to send to")
                    self._outbox_warned = True
            return
        self._outbox_warned = False
        for name in sorted(os.listdir(self.outbox)):
            path = os.path.join(self.outbox, name)
            if name.startswith(".") or name.endswith(".tmp") or os.path.islink(path) or not os.path.isfile(path):
                continue
            try:
                with open(path, encoding="utf-8") as f:
                    parts = split_for_radio(f.read().strip())
                if not parts:
                    os.remove(path)
                    continue
                for i, part in enumerate(parts):
                    await self.send(part, self.allow[0], src="claude")
                    if i + 1 == len(parts):
                        os.remove(path)
                    else:
                        fd = os.open(path + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                        with os.fdopen(fd, "w", encoding="utf-8") as f:
                            f.write("\n".join(parts[i + 1:]))
                        os.replace(path + ".tmp", path)
                    self.out(f"SENT to {self.allow[0]}: {part}")
                    await asyncio.sleep(self.chunk_delay)
            except (OSError, UnicodeError, HubError) as e:
                self.out(f"ERROR reply retained in {name}: {e}")
                return  # preserve ordering and retry on the next poll


# ---- HTTP -----------------------------------------------------------------------------------------
COOKIE = "meshtoken"


@web.middleware
async def guard(request, handler):
    """Reject anything that is not this browser talking to this server: DNS-rebinding (bad Host),
    cross-site requests (bad Origin) and requests without the token."""
    hostport = request.headers.get("Host", "")
    try:
        parsed = urlsplit("http://" + hostport)
        port = parsed.port
        valid = parsed.hostname in {"127.0.0.1", "localhost", "::1"} and not parsed.username and not parsed.password
        valid = valid and not parsed.path and not parsed.query and not parsed.fragment
    except ValueError:
        valid = False
    if not valid:
        raise web.HTTPForbidden(text="bad Host header")
    origin = request.headers.get("Origin")
    if origin and origin != "http://" + hostport:
        raise web.HTTPForbidden(text="cross-origin request refused")
    token = request.app[TOKEN]
    supplied = request.query.get("token")
    if supplied is not None:
        if not hmac.compare_digest(supplied.encode(), token.encode()):
            raise web.HTTPUnauthorized(text="bad token")
        resp = web.HTTPFound(request.path)  # drop the token from the address bar
        resp.set_cookie(COOKIE, token, httponly=True, samesite="Strict", path="/")
        raise resp
    if not hmac.compare_digest(request.cookies.get(COOKIE, "").encode(), token.encode()):
        raise web.HTTPUnauthorized(text=f"Open the http://127.0.0.1:PORT/?token=... URL printed by {PRODUCT_NAME}")
    return await handler(request)


CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
       "img-src 'self' data: https://tile.openstreetmap.org https://radar.weather.gov; "
       "connect-src 'self' ws://127.0.0.1:* ws://localhost:*; "
       "frame-ancestors 'none'; base-uri 'none'; form-action 'self'")


async def add_security_headers(request, response):
    """Defence in depth: even if a hostile node name slipped past the page's escaping, scripts from
    anywhere but this server cannot run, and the page cannot be framed."""
    response.headers["Content-Security-Policy"] = CSP
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    if request.path.startswith("/api/") or request.path == "/":
        response.headers["Cache-Control"] = "no-store"


async def index(request):
    return web.FileResponse(os.path.join(WEB_DIR, "index.html"))


async def api_state(request):
    return web.json_response(request.app[HUB].state())


async def api_basemap(request):
    if not os.path.exists(BASEMAP_PATH):
        raise web.HTTPNotFound(text="run tools/build_basemap.py")
    return web.FileResponse(BASEMAP_PATH)


async def radio_audio(request):
    """Stream one channel's audio (MP3) to the browser. The dongle is released when the browser disconnects."""
    hub = request.app[HUB]
    if not hub.radio:
        raise web.HTTPServiceUnavailable(text="SDR not available")
    if error := hub.radio.capability_error("audio"):
        raise web.HTTPServiceUnavailable(text=error)
    try:
        squelch = max(0, min(int(request.query.get("squelch", "0")), 5000))
    except ValueError:
        raise web.HTTPBadRequest(text="bad squelch")
    try:
        proc = await hub.radio.open_audio(request.query.get("mhz"), squelch)
    except ValueError:
        raise web.HTTPBadRequest(text="bad frequency")
    except (RuntimeError, OSError) as e:
        raise web.HTTPServiceUnavailable(text=str(e))
    resp = web.StreamResponse(headers={"Content-Type": "audio/mpeg", "Cache-Control": "no-store"})
    await resp.prepare(request)
    try:
        while True:
            chunk = await proc.stdout.read(4096)
            if not chunk:
                await hub.radio.audio_ended(proc)
                break
            await resp.write(chunk)
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    finally:
        await hub.radio.close_audio(proc)
    return resp


async def ws_handler(request):
    hub = request.app[HUB]
    ws = web.WebSocketResponse(heartbeat=20, max_msg_size=64 * 1024)
    await ws.prepare(request)
    hub.clients.add(ws)
    await ws.send_json(hub.state())
    try:
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            try:
                data = json.loads(msg.data)
            except ValueError:
                continue
            if not isinstance(data, dict):
                continue
            if data.get("type") == "radio" and not hub.radio:
                await ws.send_json({"type": "error", "message": "SDR is disabled in this session."})
                continue
            if data.get("type") == "weather" and not hub.weather:
                await ws.send_json({"type": "weather", "error": hub.capability_errors.get("weather", "Weather is disabled in this session."), "data": None})
                continue
            if data.get("type") == "sats" and not hub.sats:
                await ws.send_json({"type": "sats", "error": hub.capability_errors.get("sats", "Satellite tracking is disabled in this session."), "sats": [], "passes": []})
                continue
            if data.get("type") == "radio" and hub.radio:
                cmd = data.get("cmd")  # only the browser (token holder) can drive the SDR, never a radio message
                if cmd == "start":
                    await hub.radio.start()
                elif cmd == "stop":
                    await hub.radio.stop()
                elif cmd == "stop_audio":
                    await hub.radio.close_audio()
                elif cmd == "aircraft_start":
                    await hub.radio.start_aircraft()
                elif cmd == "aircraft_stop":
                    await hub.radio.stop_aircraft()
                elif cmd == "job_start":
                    try:
                        await hub.radio.start_job(str(data.get("kind")), data.get("mhz"))
                    except ValueError as e:
                        await ws.send_json({"type": "error", "message": str(e)})
                elif cmd == "job_stop":
                    await hub.radio.stop_job()
                continue
            if isinstance(data, dict) and data.get("type") == "weather" and hub.weather:
                if time.time() - hub._weather_asked > 30:  # the page asks on open; do not hammer the NWS
                    hub._weather_asked = time.time()
                    await hub.weather.refresh()
                continue
            if isinstance(data, dict) and data.get("type") == "sats" and hub.sats:
                try:
                    await ws.send_json(await hub.sats.snapshot())
                except Exception as e:  # a bad TLE or no internet on first use must not kill the socket
                    await ws.send_json({"type": "sats", "error": f"Satellite data unavailable ({type(e).__name__}).", "sats": [], "passes": []})
                continue
            if isinstance(data, dict) and data.get("type") == "send":
                try:
                    await hub.send(data.get("text"), data.get("to"))
                except HubError as e:
                    await ws.send_json({"type": "error", "message": str(e)})
    finally:
        hub.clients.discard(ws)
    return ws


HUB, TOKEN = web.AppKey("hub", Hub), web.AppKey("token", str)


def make_app(hub, token):
    app = web.Application(middlewares=[guard])
    app[HUB], app[TOKEN] = hub, token
    app.router.add_get("/", index)
    app.router.add_get("/api/state", api_state)
    app.router.add_get("/api/basemap", api_basemap)
    app.router.add_get("/ws", ws_handler)
    app.router.add_get("/api/radio/audio", radio_audio)
    app.router.add_static("/assets", WEB_DIR, follow_symlinks=False)

    async def on_start(app):
        await hub.start()

    async def on_stop(app):
        await hub.stop()

    async def on_shutdown(app):
        # aiohttp would otherwise wait (up to a minute) for open browser sockets before exiting
        for ws in list(hub.clients):
            await ws.close(code=WSCloseCode.GOING_AWAY, message=b"server shutting down")

    app.on_shutdown.append(on_shutdown)
    app.on_response_prepare.append(add_security_headers)
    app.on_startup.append(on_start)
    app.on_cleanup.append(on_stop)
    return app


class DemoBackend:
    """Offline radio snapshot, with synthetic nodes when no usable snapshot exists."""

    def __init__(self, on_packet, snapshot=NODES_JSON, home=None):
        self.on_packet = on_packet
        try:
            with open(snapshot, encoding="utf-8") as f:
                self._nodes = json.load(f)["nodes"]
            if not isinstance(self._nodes, dict) or not self._nodes or not all(isinstance(n, dict) for n in self._nodes.values()):
                raise ValueError("invalid snapshot")
        except (OSError, ValueError, KeyError, TypeError):
            lat, lon = home or DEFAULT_REFERENCE
            now = time.time()
            self._nodes = {
                "!00000001": {"user": {"shortName": "DEMO", "longName": "Demo station"}, "lastHeard": now,
                              "position": {"latitude": lat, "longitude": lon}},
                "!00000002": {"user": {"shortName": "HILL", "longName": "Demo hill relay"}, "lastHeard": now - 60,
                              "hopsAway": 0, "snr": 7.0},
                "!00000003": {"user": {"shortName": "TRAIL", "longName": "Demo trail node"}, "lastHeard": now - 600,
                              "hopsAway": 1, "position": {"latitude": max(-90, min(90, lat + 0.02)), "longitude": lon}},
            }
        self.my_id = next(iter(self._nodes))

    def nodes(self):
        return dict(self._nodes)

    def send_text(self, text, dest, on_ack=None):
        pass  # simulated sends are labelled by the Hub and never report a radio delivery

    def close(self):
        pass


def load_or_create_token(path, rotate=False):
    """Reuse the token saved by the last run so an open browser tab reconnects after a restart;
    --new-token rotates it. The file is private (0600)."""
    if not rotate:
        try:
            with open(path, encoding="utf-8") as f:
                old = f.read().strip().split("?token=", 1)[1]
            if len(old) >= 20 and old.replace("-", "").replace("_", "").isalnum():
                return old
        except (OSError, IndexError):
            pass
    return secrets.token_urlsafe(24)


def load_places():
    try:
        with open(BASEMAP_PATH, encoding="utf-8") as f:
            return json.load(f).get("places", [])
    except (OSError, ValueError):
        return []


def argument_parser():
    ap = argparse.ArgumentParser()
    ap.add_argument("-p", "--port", default="/dev/ttyUSB0", help="serial port of the radio")
    ap.add_argument("--http-port", type=int, default=8765)
    ap.add_argument("--bridge", action="store_true", help="also act as the Claude bridge (stdout events + run/outbox)")
    ap.add_argument("--allow", nargs="*", default=["!02ecdcdc"], help="node IDs the bridge accepts messages from")
    ap.add_argument("--home", metavar="LAT,LON", help="your location, used to centre the map")
    ap.add_argument("--demo", action="store_true", help="use a cached node snapshot or sample nodes; no hardware or external feeds")
    ap.add_argument("--no-radio", action="store_true", help="do not offer the SDR panel")
    ap.add_argument("--no-weather", action="store_true", help="disable NWS requests")
    ap.add_argument("--no-sats", action="store_true", help="disable satellite calculations and TLE requests")
    ap.add_argument("--state-dir", default=os.path.join(ROOT, "run"), help="runtime state directory (demo uses its demo/ subdirectory)")
    ap.add_argument("--new-token", action="store_true", help="rotate the access token instead of reusing the saved one")
    return ap


def build_hub(a):
    home = parse_home(a.home) if a.home else None
    state_dir = os.path.abspath(os.path.expanduser(a.state_dir))
    snapshot = os.path.join(state_dir, "nodes.json")
    if a.demo:
        state_dir = os.path.join(state_dir, "demo")
    factory = (lambda on_packet: DemoBackend(on_packet, snapshot=snapshot, home=home)) if a.demo else (lambda on_packet: SerialBackend(a.port, on_packet))
    hub = Hub(factory, allow=a.allow, bridge=a.bridge, places=load_places(), home=home, demo=a.demo,
              log_path=os.path.join(state_dir, "messages.jsonl"), nodes_json=os.path.join(state_dir, "nodes.json"),
              follows_path=os.path.join(state_dir, "follows.json"), outbox=os.path.join(state_dir, "outbox"))
    if not a.no_radio and not a.demo:
        hub.radio = RadioWatch(hub.broadcast, reference=home)
    if not a.demo and not a.no_weather:
        hub.weather = WeatherService(hub.broadcast, hub.location, on_alert=hub.on_weather_alert)
    else:
        hub.capability_errors["weather"] = "Weather is disabled in demo mode." if a.demo else "Weather disabled with --no-weather."
    if not a.demo and not a.no_sats:
        try:
            from satellites import SatTracker
            hub.sats = SatTracker(hub.location)
        except ImportError:
            hub.capability_errors["sats"] = "Satellite tracking requires requirements-optional.txt (Skyfield)."
    else:
        hub.capability_errors["sats"] = "Satellite tracking is disabled in demo mode." if a.demo else "Satellite tracking disabled with --no-sats."
    return hub, state_dir


def main():
    ap = argument_parser()
    a = ap.parse_args()
    if not 1 <= a.http_port <= 65535:
        ap.error("--http-port must be between 1 and 65535")
    if any(len(n) != 9 or n[0] != "!" or any(c not in "0123456789abcdef" for c in n[1:]) for n in a.allow):
        ap.error("--allow values must be ! followed by eight lowercase hexadecimal digits")
    try:
        hub, state_dir = build_hub(a)
    except ValueError as e:
        ap.error(str(e))
    url_file = os.path.join(state_dir, "web-url.txt")
    token = load_or_create_token(url_file, rotate=a.new_token)
    url = f"http://127.0.0.1:{a.http_port}/?token={token}"
    os.makedirs(state_dir, mode=0o700, exist_ok=True)
    fd = os.open(url_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(url + "\n")
    print(f"{PRODUCT_NAME} · {TAGLINE}\nOpen {url}", flush=True)
    web.run_app(make_app(hub, token), host="127.0.0.1", port=a.http_port, print=None)


if __name__ == "__main__":
    main()
