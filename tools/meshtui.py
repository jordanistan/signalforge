#!/usr/bin/env python3
"""Terminal dashboard + message board for a serial-attached Meshtastic node.

Dashboard tab: nodes in reach + detail/SNR sparkline, animated radar, this node's gauges, and an
inbox of conversations with unread counts. Messages tab: conversation list + the open thread.
Every node you DM gets its own thread, plus one for the primary channel; typing sends to the open thread.
Map tab: full-size clickable map (real lat/lon, or the signal radar) with a details panel. Click a node for
details; double-click it, press Enter, or use the Message button to DM it. On the map: + / - zoom, c centre on
the selected node, 0 reset, g switch radar/geo.
Keys: F1 dashboard  F2 node filter  F3 channel thread  F4 next theme  F5 messages  F6 map  Ctrl+P theme picker  Ctrl+Q quit
      Enter on a node or inbox row opens its conversation; Tab moves between panels.
Only one program can hold the serial port, so stop tools/mesh-bridge.py first.
Usage: tools/meshtui.py [-p PORT] [--home LAT,LON]
"""
import argparse
import json
import math
import os
import sys
import threading
import time
import zlib
from collections import deque

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VENV_PYTHON = os.path.join(ROOT, ".venv", "bin", "python")
if __name__ == "__main__" and sys.prefix == sys.base_prefix and os.path.exists(VENV_PYTHON):
    os.execv(VENV_PYTHON, [VENV_PYTHON, *sys.argv])  # run under the project venv even via `python3 meshtui.py`

from meshcore import (BROADCAST, RECENT_SECS, SerialBackend, age, bearing, conv_of, dur, haversine_km,  # noqa: E402,F401
                      is_broadcast, nearest_place, node_class, place_text, position, parse_home)
from rich.markup import escape  # noqa: E402
from brand import PRODUCT_NAME, TAGLINE  # noqa: E402
from rich.text import Text  # noqa: E402
from textual.app import App, ComposeResult  # noqa: E402
from textual.binding import Binding  # noqa: E402
from textual.containers import Horizontal, Vertical  # noqa: E402
from textual import on  # noqa: E402
from textual.message import Message  # noqa: E402
from textual.widget import Widget  # noqa: E402
from textual.widgets import (Button, DataTable, Footer, Header, Input, RichLog, Sparkline, Static,  # noqa: E402
                             TabbedContent, TabPane)

LOG_PATH = os.path.join(ROOT, "run", "messages.jsonl")
BASEMAP_PATH = os.path.join(ROOT, "data", "basemap.json")
MAX_BYTES = 200
FILTERS = ["all", "recent", "direct"]
THEMES = ["tokyo-night", "dracula", "nord", "gruvbox", "catppuccin-mocha", "rose-pine", "monokai"]
PALETTE = ["#7aa2f7", "#bb9af7", "#9ece6a", "#e0af68", "#f7768e", "#2ac3de", "#ff9e64", "#73daca"]
SNR_HISTORY = 40


# ---- pure helpers (unit-tested) ---------------------------------------------------------------
def node_color(node_id):
    return PALETTE[zlib.crc32(str(node_id).encode()) % len(PALETTE)]


def signal_bars(snr):
    """Four-step signal meter from SNR in dB."""
    if snr is None:
        return Text("····", style="dim")
    n = 4 if snr >= 5 else 3 if snr >= 0 else 2 if snr >= -5 else 1 if snr >= -10 else 0
    color = "green" if n >= 3 else "yellow" if n == 2 else "red"
    return Text("▂▄▆█"[:n] + "·" * (4 - n), style=color)


def age_text(ts, now=None):
    a = (now or time.time()) - (ts or 0)
    style = "green" if ts and a < 300 else "yellow" if ts and a < 1800 else "dim"
    return Text(age(ts, now), style=style)


def gauge(pct, width=12, invert=False):
    """Block-bar gauge; invert=True means high values are bad (utilisation)."""
    pct = max(0.0, min(100.0, pct))
    filled = round(pct / 100 * width)
    good = pct < 50 if invert else pct > 50
    mid = pct < 80 if invert else pct > 20
    style = "green" if good else "yellow" if mid else "red"
    t = Text("█" * filled, style=style)
    t.append("░" * (width - filled), style="dim")
    t.append(f" {pct:.0f}%")
    return t


def signal_radius(node):
    """Radar ring (0..1) for a node without a known position: strong signal = inner ring."""
    hops, snr = node.get("hopsAway"), node.get("snr")
    if hops and hops > 0:
        return min(0.95, 0.7 + 0.08 * hops)
    if snr is None:
        return 0.7
    return 0.25 + 0.5 * max(0.0, min(1.0, (10 - snr) / 30))


class Basemap:
    """Coastline, borders, highways, rivers and cities (from data/basemap.json, see tools/build_basemap.py),
    rasterised with braille characters (each cell is a 2x4 dot grid) so lines look smooth in a terminal."""

    LAYERS = (("coast", 1, "cyan"), ("rivers", 2, "blue"), ("borders", 3, "grey50"), ("roads", 4, "gold3"))
    _BITS = ((0x01, 0x08), (0x02, 0x10), (0x04, 0x20), (0x40, 0x80))  # [dot row][dot col]

    def __init__(self, data):
        self.layers = {}
        for name, _, _ in self.LAYERS:
            polys = []
            for flat in data.get(name, []):
                lats, lons = flat[0::2], flat[1::2]
                polys.append((min(lats), max(lats), min(lons), max(lons), list(zip(lats, lons))))
            self.layers[name] = polys
        self.places = data.get("places", [])
        self._cache = {}

    @classmethod
    def load(cls, path=BASEMAP_PATH):
        try:
            with open(path, encoding="utf-8") as f:
                return cls(json.load(f))
        except (OSError, ValueError):
            return None  # the map still works without it, just without geography

    def raster(self, width, height, center, km_per_row, to_cell):
        """{(col, row): (braille_char, style)} for the view; cached because panning/zooming is rare."""
        key = (width, height, round(center[0], 4), round(center[1], 4), round(km_per_row, 6))
        if key in self._cache:
            return self._cache[key]
        k_lat, k_lon = 110.57, 111.32 * math.cos(math.radians(center[0]))
        dlat = (height / 2 + 1) * km_per_row / k_lat
        dlon = (width / 4 + 1) * km_per_row / k_lon
        W, H = width * 2, height * 4
        cells = {}

        def dot(px, py, prio):
            if 0 <= px < W and 0 <= py < H:
                c = cells.setdefault((px >> 1, py >> 2), [0, 0])
                c[0] |= self._BITS[py & 3][px & 1]
                c[1] = max(c[1], prio)

        def project(p):
            xf, yf = to_cell(*p)
            return math.floor(2 * xf + 1), math.floor((yf + 0.5) * 4)

        for name, prio, _ in self.LAYERS:
            for lat0, lat1, lon0, lon1, pts in self.layers[name]:
                if lat1 < center[0] - dlat or lat0 > center[0] + dlat or lon1 < center[1] - dlon or lon0 > center[1] + dlon:
                    continue
                prev = project(pts[0])
                for p in pts[1:]:
                    cur = project(p)
                    if not ((prev[0] < 0 and cur[0] < 0) or (prev[0] >= W and cur[0] >= W)
                            or (prev[1] < 0 and cur[1] < 0) or (prev[1] >= H and cur[1] >= H)):
                        x0, y0, x1, y1 = prev + cur
                        dx, dy, sx, sy = abs(x1 - x0), -abs(y1 - y0), 1 if x0 < x1 else -1, 1 if y0 < y1 else -1
                        err = dx + dy
                        while True:  # Bresenham
                            dot(x0, y0, prio)
                            if x0 == x1 and y0 == y1:
                                break
                            e2 = 2 * err
                            if e2 >= dy:
                                err, x0 = err + dy, x0 + sx
                            if e2 <= dx:
                                err, y0 = err + dx, y0 + sy
                    prev = cur
        styles = {prio: style for _, prio, style in self.LAYERS}
        out = {k: (chr(0x2800 + bits), styles[prio]) for k, (bits, prio) in cells.items()}
        if len(self._cache) >= 3:
            self._cache.pop(next(iter(self._cache)))
        self._cache[key] = out
        return out


def _draw_places(grid, places, to_cell, occupied, max_labels, bounds):
    """City markers + names, biggest first, into whatever space the important nodes left free."""
    width, height = len(grid[0]), len(grid)
    (clat, clon), dlat, dlon = bounds
    shown = 0
    for name, _st, lat, lon, pop in places:
        if abs(lat - clat) > dlat or abs(lon - clon) > dlon:
            continue  # off the map: skip the projection
        xf, yf = to_cell(lat, lon)
        x, y = round(xf), round(yf)
        if not (1 <= x < width - 2 and 1 <= y < height - 2):
            continue
        label = name[:14]
        cells = {(x + i, y) for i in range(len(label) + 3)}
        if x + len(label) + 2 >= width or cells & occupied:
            continue
        occupied |= cells
        big = pop >= 500_000
        _put(grid, x, y, "◆" if big else "◇", "bold white" if big else "grey70")
        _put(grid, x + 2, y, label, "bold white" if big else "grey70")
        shown += 1
        if shown >= max_labels:
            return


def nice(x):
    """Round down to 1, 2 or 5 times a power of ten (for scale bars and grid spacing)."""
    e = 10 ** math.floor(math.log10(x))
    f = x / e
    return e * (1 if f < 2 else 2 if f < 5 else 5)


def _blank(width, height):
    return [[(" ", "") for _ in range(width)] for _ in range(height)]


def _to_text(grid):
    out = Text(no_wrap=True)
    for y, row in enumerate(grid):
        for ch, style in row:
            out.append(ch, style=style or None)
        if y < len(grid) - 1:
            out.append("\n")
    return out


def _put(grid, x, y, s, style=""):
    for i, ch in enumerate(s):
        if 0 <= y < len(grid) and 0 <= x + i < len(grid[0]):
            grid[y][x + i] = (ch, style)


def _draw_nodes(grid, placed, occupied, selected, unread, now, hits):
    """Draw nodes onto the grid. Live nodes are bold with a label, day-old ones plain, remembered
    (old) ones a dim dot with no label unless selected/unread. Colliding nodes are nudged apart,
    and every drawn footprint is recorded in `hits` as (node_id, x0, x1, y) for click handling."""
    width, height = len(grid[0]), len(grid)
    order = {"live": 0, "day": 1, "old": 2}
    for nid, n, x, y in sorted(placed, key=lambda p: (p[0] != selected, p[0] not in unread, order[node_class(p[1], now)])):
        cls = node_class(n, now)
        sel, hot = nid == selected, nid == selected or nid in unread
        label = ""
        if hot or cls != "old":
            label = ("*" if nid in unread else "") + ((n.get("user") or {}).get("shortName") or "?")[:6]
        span = len(label) + 2 if label else 1
        for yy in (y, y + 1, y - 1, y + 2, y - 2):  # nudge vertically if another node is in the way
            cells = {(x + i, yy) for i in range(span) if x + i < width}
            if 0 < yy < height and not cells & occupied:
                break
        else:
            continue  # nowhere to put it without hiding another node
        occupied |= cells
        color = node_color(nid)
        _put(grid, x, yy, "◉" if sel else "•" if cls == "old" and not hot else "●",
             "dim" if cls == "old" and not hot else f"bold {color}" if cls == "live" or hot else color)
        if label:
            _put(grid, x + 1, yy, label, f"bold reverse {color}" if sel else f"bold {color}" if cls == "live" else color)
        hits.append((nid, x, x + len(label), yy))


def render_radar(width, height, me_id, nodes, rows, sweep, selected=None, unread=(), hits=None, now=None):
    """Radar canvas as Rich Text. Real bearing/distance when both ends have a position, otherwise
    signal-strength rings at a stable pseudo-random angle per node. North is up."""
    if width < 24 or height < 11:
        return Text("radar needs a bigger window", style="dim")
    hits = [] if hits is None else hits
    now = now or time.time()
    cx, cy = (width - 1) / 2, (height - 1) / 2
    R = min(cy - 0.5, (cx - 1) / 2)  # radius in row units; columns are ~2x narrower than rows
    mine = position(nodes.get(me_id))
    polar = {}
    if mine:
        for nid, n in rows:
            p = position(n)
            if p:
                polar[nid] = (haversine_km(mine, p), bearing(mine, p))
    max_d = max([d for d, _ in polar.values()] + [1.0])
    placed = []
    for nid, n in rows:
        if nid in polar:
            d, theta = polar[nid]
            r = 0.2 + 0.75 * d / max_d
        else:
            theta = (zlib.crc32(nid.encode()) % 3600) / 3600 * 2 * math.pi
            r = signal_radius(n)
        x = min(width - 2, max(1, round(cx + 2 * R * r * math.sin(theta))))
        y = min(height - 1, max(0, round(cy - R * r * math.cos(theta))))
        placed.append((nid, n, x, y))

    grid = _blank(width, height)
    for y in range(height):
        for x in range(width):
            dx, dy = (x - cx) / 2, y - cy
            dist = math.hypot(dx, dy)
            if dist > R + 0.4:
                continue
            diff = (sweep - math.atan2(dx, -dy)) % (2 * math.pi)
            if diff < 0.12:
                grid[y][x] = ("▒", "bold green")
            elif diff < 0.5:
                grid[y][x] = ("░", "green")
            elif any(abs(dist - R * k / 3) < 0.4 for k in (1, 2, 3)):
                grid[y][x] = ("·", "grey50")
    grid[round(cy)][round(cx)] = ("✚", "bold white")
    grid[0][round(cx)] = ("N", "bold")
    occupied = {(round(cx), round(cy))} | {(x, 0) for x in range(width)}  # keep centre and the N row clear
    _draw_nodes(grid, placed, occupied, selected, unread, now, hits)
    return _to_text(grid)


def render_geo(width, height, me_id, nodes, rows, selected=None, unread=(), zoom=1.0, focus=None,
               hits=None, stats=None, now=None, basemap=None, home=None):
    """Geographic map (equirectangular, north up) of every row that has a position, centred on `focus`,
    else your own position, else the median of the plotted nodes. At zoom 1 the view fits ~90% of the
    plotted nodes; nodes outside the view are counted in stats['off'] rather than drawn."""
    hits = [] if hits is None else hits
    stats = {} if stats is None else stats
    now = now or time.time()
    if width < 30 or height < 12:
        return Text("map needs a bigger window", style="dim")
    pts = [(nid, n, position(n)) for nid, n in rows if position(n)]
    own = position(nodes.get(me_id))
    mine = own or home  # `home` stands in for your location when the connected node has no position
    stats.update(on_map=0, off=0, no_pos=len(rows) - len(pts))
    if focus:
        center, kind = focus, "focus"
    elif mine:
        center, kind = mine, "you" if own else "home"
    elif pts:
        lats, lons = sorted(p[0] for _, _, p in pts), sorted(p[1] for _, _, p in pts)
        center, kind = (lats[len(lats) // 2], lons[len(lons) // 2]), "median"
    else:
        stats["center"] = None
        return Text("No node has a position yet.\nPositions appear once nodes broadcast them.", style="dim")
    stats["center"] = kind
    k_lat, k_lon = 110.57, 111.32 * math.cos(math.radians(center[0]))

    def offset(p):
        return ((p[1] - center[1]) * k_lon, (p[0] - center[0]) * k_lat)  # km east, km north

    dists = sorted(math.hypot(*offset(p)) for _, _, p in pts)
    base = max(1.0, 1.1 * dists[math.ceil(0.9 * (len(dists) - 1))]) if dists else 1.0  # small sets fit fully
    cx, cy = (width - 1) / 2, (height - 1) / 2
    km_per_row = base / max(1.0, cy - 1) / zoom
    grid = _blank(width, height)

    def to_cell(lat, lon):
        dx, dy = offset((lat, lon))
        return cx + dx / km_per_row * 2, cy - dy / km_per_row

    stats.update(cx=cx, cy=cy, km_per_row=km_per_row, center_pos=center, k_lat=k_lat, k_lon=k_lon)
    step = nice(km_per_row * 10) / 2  # faint graticule, twice per scale-bar length
    col_step, row_step = step / km_per_row * 2, step / km_per_row
    if basemap:
        for (c, r), cell in basemap.raster(width, height, center, km_per_row, to_cell).items():
            grid[r][c] = cell
    elif row_step >= 2.5:
        for j in range(-int(cy / row_step) - 1, int(cy / row_step) + 2):
            for i in range(-int(cx / col_step) - 1, int(cx / col_step) + 2):
                x, y = round(cx + i * col_step), round(cy + j * row_step)
                if 0 < y < height - 1 and 0 <= x < width:
                    grid[y][x] = ("·", "grey27")
    bar_km = nice(km_per_row * 10)
    bar_cols = max(3, round(bar_km / km_per_row * 2))
    label = f"{bar_km:g} km" if bar_km >= 1 else f"{bar_km * 1000:.0f} m"
    _put(grid, 1, height - 1, "├" + "─" * (bar_cols - 2) + "┤ " + label, "bold")
    occupied = {(x, y) for y in (0, height - 2, height - 1) for x in range(width)}  # N row, tray, scale bar
    _put(grid, round(cx), 0, "N", "bold")

    placed = []
    for nid, n, p in pts:
        dx, dy = offset(p)
        x, y = round(cx + dx / km_per_row * 2), round(cy - dy / km_per_row)
        if 0 <= x < width - 1 and 1 <= y < height - 2:
            placed.append((nid, n, x, y))
        else:
            stats["off"] += 1
    stats["on_map"] = len(placed)
    if mine:
        mx, my = offset(mine)
        x, y = round(cx + mx / km_per_row * 2), round(cy - my / km_per_row)
        if 0 <= x < width and 1 <= y < height - 2:
            _put(grid, x, y, "✚", "bold white")
            occupied.add((x, y))
    others = [p for p in placed if p[0] != me_id]

    def important(p):
        return node_class(p[1], now) != "old" or p[0] == selected or p[0] in unread

    # Priority: live/selected/unread nodes, then city labels, then remembered dots fill whatever is left.
    _draw_nodes(grid, [p for p in others if important(p)], occupied, selected, unread, now, hits)
    if basemap:
        bounds = (center, (height / 2 + 1) * km_per_row / k_lat, (width / 4 + 1) * km_per_row / k_lon)
        _draw_places(grid, basemap.places, to_cell, occupied, max(5, width * height // 300), bounds)
    _draw_nodes(grid, [p for p in others if not important(p)], occupied, selected, unread, now, hits)
    _draw_tray(grid, [(nid, n) for nid, n in rows if not position(n) and node_class(n, now) != "old"],
               selected, unread, height - 2, hits)
    return _to_text(grid)


def _draw_tray(grid, items, selected, unread, y, hits):
    """Bottom strip listing recently heard nodes that have no position, so they are not invisible on a
    geographic map. Entries are clickable like the map dots."""
    if not items:
        return
    width = len(grid[0])
    _put(grid, 1, y, "no position:", "dim")
    x = 14
    for i, (nid, n) in enumerate(items):
        name = ("*" if nid in unread else "") + ((n.get("user") or {}).get("shortName") or "?")[:6]
        if x + len(name) + 1 + 6 > width:  # keep room for the "+N more" tail
            _put(grid, x, y, f"+{len(items) - i} more", "dim")
            return
        color = node_color(nid)
        _put(grid, x, y, "●", f"bold {color}")
        _put(grid, x + 1, y, name, f"bold reverse {color}" if nid == selected else f"bold {color}")
        hits.append((nid, x, x + len(name), y))
        x += len(name) + 3


def render_detail(nid, node, mine, places=None):
    if not node:
        return Text("Select a node with ↑/↓", style="dim")
    user = node.get("user") or {}
    t = Text()
    t.append(user.get("longName") or "?", style=f"bold {node_color(nid)}")
    t.append(f"  [{user.get('shortName') or '?'}]\n", style="dim")
    t.append(f"{nid}  {user.get('hwModel', '')}\n", style="dim")
    hops, snr = node.get("hopsAway"), node.get("snr")
    t.append(f"hops {'-' if hops is None else hops}   SNR ")
    t.append("-" if snr is None else f"{snr:.1f} dB ")
    t.append_text(signal_bars(snr))
    t.append(f"\nheard {age(node.get('lastHeard'))} ago\n")
    batt = (node.get("deviceMetrics") or {}).get("batteryLevel")
    t.append("power  ")
    t.append("-" if batt is None else "⚡ USB" if batt > 100 else f"{batt}%")
    p = position(node)
    t.append("\n")
    if p:
        t.append(f"{p[0]:.4f}, {p[1]:.4f}")
        if mine:
            t.append(f"  ({haversine_km(mine, p):.1f} km)")
        near = place_text(nearest_place(p[0], p[1], places)) if places else ""
        if near:
            t.append(f"\n{near}", style="italic")
    else:
        t.append("no position", style="dim")
    return t


def render_me(node, total, direct, n_in, n_out):
    user = (node or {}).get("user") or {}
    dm = (node or {}).get("deviceMetrics") or {}
    t = Text()
    t.append(f"◈ {user.get('longName') or 'this node'}\n", style="bold")
    t.append(f"{user.get('id', '')}  {user.get('hwModel', '')}\n\n", style="dim")
    batt = dm.get("batteryLevel")
    t.append("Battery ")
    if batt is None:
        t.append("-")
    elif batt > 100:
        t.append("⚡ USB powered", style="green")
    else:
        t.append_text(gauge(batt, 10))
    volt = dm.get("voltage")
    t.append(f"\nVoltage {'-' if volt is None else f'{volt:.2f} V'}\n")
    t.append("Ch.util ")
    t.append_text(gauge(dm.get("channelUtilization") or 0, 10, invert=True))
    t.append("\nAirtime ")
    t.append_text(gauge(dm.get("airUtilTx") or 0, 10, invert=True))
    up = dm.get("uptimeSeconds")
    t.append(f"\nUptime  {'-' if up is None else dur(up)}\n\n")
    t.append(f"Heard {total} nodes ({direct} direct)\n")
    t.append(f"Msgs  ↓{n_in}  ↑{n_out}")
    return t


# ---- widgets ------------------------------------------------------------------------------------
class Radar(Widget):
    """Clickable node map. mode 'radar': signal rings around you (animated sweep). mode 'geo': real
    lat/lon map with zoom. Click a node to select it; double-click (or Enter) to message it."""

    can_focus = True
    BINDINGS = [
        Binding("plus,equals_sign", "zoom_in", "Zoom in", show=False),
        Binding("minus", "zoom_out", "Zoom out", show=False),
        Binding("0", "zoom_reset", "Reset view", show=False),
        Binding("g", "toggle_mode", "Radar/Map", show=False),
        Binding("c", "center", "Center on node", show=False),
        Binding("enter", "message", "Message node", show=False),
    ]

    class NodePicked(Message):
        def __init__(self, node_id, open_dm=False):
            super().__init__()
            self.node_id = node_id
            self.open_dm = open_dm

    def __init__(self, mode="radar", **kw):
        super().__init__(**kw)
        self.mode = mode
        self.sweep = 0.0
        self.zoom = 1.0
        self.focus_pos = None
        self.me_id = None
        self.nodes = {}
        self.rows = []
        self.selected = None
        self.unread = set()
        self.hits = []
        self.stats = {}
        self.basemap = None
        self.home = None
        self._base_title = None

    def render(self):
        self.hits, self.stats = [], {}
        w, h = self.size.width, self.size.height
        sub = ""
        if self.mode == "geo":
            out = render_geo(w, h, self.me_id, self.nodes, self.rows, self.selected, self.unread, self.zoom,
                             self.focus_pos, self.hits, self.stats, basemap=self.basemap, home=self.home)
            s = self.stats
            if s.get("center") is not None:
                sub = (f"{s['on_map']} on map · {s['off']} off-screen · {s['no_pos']} no position · "
                       f"{self.zoom:.1f}x · centred on {s['center']}")
        else:
            out = render_radar(w, h, self.me_id, self.nodes, self.rows, self.sweep, self.selected,
                               self.unread, self.hits)
        if (self.border_subtitle or "") != sub:
            self.call_after_refresh(setattr, self, "border_subtitle", sub)  # not during render: avoids a loop
        return out

    def tick(self):
        if self.mode == "radar":
            self.sweep = (self.sweep + 0.15) % (2 * math.pi)
            self.refresh()

    def hit_test(self, x, y):
        """Node whose drawn footprint is under (x, y), else the nearest dot within ~1.5 rows."""
        for nid, x0, x1, yy in self.hits:
            if yy == y and x0 <= x <= x1:
                return nid
        best, best_d = None, 1.5
        for nid, x0, x1, yy in self.hits:
            d = math.hypot((x - min(max(x, x0), x1)) / 2, y - yy)
            if d < best_d:
                best, best_d = nid, d
        return best

    def on_mouse_move(self, event):
        """Geo mode: show the coordinates under the pointer and the nearest city in the title."""
        s = self.stats
        off = event.get_content_offset(self)
        if self.mode != "geo" or off is None or "km_per_row" not in s:
            return
        if self._base_title is None:
            self._base_title = self.border_title or ""
        dx = (off.x - s["cx"]) * s["km_per_row"] / 2  # km east / north of the map centre
        dy = (s["cy"] - off.y) * s["km_per_row"]
        lat, lon = s["center_pos"][0] + dy / s["k_lat"], s["center_pos"][1] + dx / s["k_lon"]
        near = place_text(nearest_place(lat, lon, self.basemap.places)) if self.basemap else ""
        self.border_title = f"{self._base_title}  ·  {lat:.3f}, {lon:.3f}" + (f"  ·  {near}" if near else "")

    def on_click(self, event):
        off = event.get_content_offset(self)
        if off is None:
            return
        self.focus()
        nid = self.hit_test(off.x, off.y)
        if nid:
            self.post_message(self.NodePicked(nid, open_dm=event.chain >= 2))

    def action_zoom_in(self):
        self.zoom = min(64.0, self.zoom * 1.5)
        self.refresh()

    def action_zoom_out(self):
        self.zoom = max(0.1, self.zoom / 1.5)
        self.refresh()

    def action_zoom_reset(self):
        self.zoom, self.focus_pos = 1.0, None
        self.refresh()

    def action_toggle_mode(self):
        self.mode = "geo" if self.mode == "radar" else "radar"
        self.refresh()

    def action_center(self):
        self.focus_pos = position(self.nodes.get(self.selected))
        if self.focus_pos is None:
            self.app.notify("The selected node has no position to centre on.", timeout=3)
        self.refresh()

    def action_message(self):
        if self.selected:
            self.post_message(self.NodePicked(self.selected, open_dm=True))


# ---- app ----------------------------------------------------------------------------------------
class MeshTUI(App):
    TITLE = f"◈ {PRODUCT_NAME.upper()}"
    CSS = """
    TabPane { padding: 0; }
    #left { width: 64; }
    #nodes { height: 1fr; border: round $primary; }
    #detailbox { height: 13; border: round $primary; padding: 0 1; }
    #detail { height: 8; }
    #snr { height: 3; }
    #top { height: 21; }
    #radar { width: 1fr; border: round $success; }
    #me { width: 34; border: round $primary; padding: 0 1; }
    #inbox { height: 1fr; border: round $warning; }
    #convs { width: 40; height: 1fr; border: round $primary; }
    #bigmap { width: 1fr; border: round $success; }
    #mapside { width: 46; border: round $primary; padding: 0 1; }
    #mapdetail { height: 1fr; }
    #mapsnr { height: 3; }
    #dm { width: 100%; margin: 1 0 0 0; }
    #maphelp { height: 7; color: $text-muted; }
    #thread { height: 1fr; border: round $primary; }
    """
    BINDINGS = [
        Binding("ctrl+q", "quit", "Quit"),
        Binding("f1", "dashboard", "Dashboard"),
        Binding("f2", "cycle_filter", "Filter"),
        Binding("f3", "broadcast", "Channel"),
        Binding("f4", "next_theme", "Theme"),
        Binding("f5", "messages", "Messages"),
        Binding("f6", "map", "Map"),
    ]

    def __init__(self, backend_factory, log_path=LOG_PATH, basemap="auto", home=None):
        super().__init__()
        self.basemap = Basemap.load() if basemap == "auto" else basemap
        self.home = home  # (lat, lon) stand-in for your position when the connected node has none
        self._backend_factory = backend_factory
        self.log_path = log_path
        self.backend = None
        self.filter = "all"
        self.my_id = None
        self.selected = None
        self.history = {}
        self.convs = {BROADCAST: []}  # conversation key -> messages; key is a node id or BROADCAST
        self.unread = {}
        self.current = BROADCAST
        self.n_in = self.n_out = 0
        self._theme_i = 0

    def compose(self) -> ComposeResult:
        yield Header()
        with TabbedContent(initial="dash"):
            with TabPane("Dashboard", id="dash"):
                with Horizontal():
                    with Vertical(id="left"):
                        yield DataTable(id="nodes", cursor_type="row", zebra_stripes=True)
                        with Vertical(id="detailbox"):
                            yield Static(id="detail")
                            yield Sparkline([0], id="snr")
                    with Vertical(id="right"):
                        with Horizontal(id="top"):
                            yield Radar(id="radar")
                            yield Static(id="me")
                        yield DataTable(id="inbox", cursor_type="row", zebra_stripes=True)
            with TabPane("Map", id="map"):
                with Horizontal():
                    yield Radar(mode="geo", id="bigmap")
                    with Vertical(id="mapside"):
                        yield Static(id="mapdetail")
                        yield Sparkline([0], id="mapsnr")
                        yield Button("✉ Message this node", id="dm", variant="primary", disabled=True)
                        yield Static("click: details · double-click / Enter: message\n+ / -: zoom · 0: reset · c: centre on node\n"
                                     "g: radar ⇄ map · F2: node filter\nhover the map for coordinates + nearest city", id="maphelp")
            with TabPane("Messages", id="messages"):
                with Horizontal():
                    yield DataTable(id="convs", cursor_type="row", zebra_stripes=True)
                    with Vertical():
                        yield RichLog(id="thread", wrap=True, markup=True)
                        yield Input(id="send", placeholder="Message (Enter to send)")
        yield Footer()

    def on_mount(self):
        self.theme = THEMES[0]
        for r in self._radars():
            r.basemap, r.home = self.basemap, self.home
        self.query_one("#nodes", DataTable).add_columns("Name", "ID", "Hop", "Sig", "Heard", "Batt", "Dist")
        self.query_one("#inbox", DataTable).add_columns("Conversation", "New", "Last message", "When")
        self.query_one("#convs", DataTable).add_columns("Conversation", "New")
        for wid, title in (("#mapside", "Selected node"), ("#bigmap", "Map  ·  ● heard lately  • remembered  ◆ ◇ cities  gold=highways"),
                           ("#detailbox", "Selected node"), ("#radar", "Radar  ·  inner ring = strong signal"),
                           ("#me", "This node"), ("#inbox", "Inbox  ·  Enter opens a conversation"),
                           ("#convs", "Conversations")):
            self.query_one(wid).border_title = title
        self._load_history()
        try:
            self.backend = self._backend_factory(self._incoming_from_thread)
            self.my_id = self.backend.my_id
        except Exception as e:  # port busy, no device...
            msg = f"Could not open radio: {e}"
            self._note(BROADCAST, msg)  # kept in the channel thread; the toast makes it visible on any tab
            self.notify(msg, title="No radio", severity="error", timeout=15)
            self._show_thread()
            return
        self.sub_title = f"{TAGLINE}  ·  you are {self.my_id}"
        for r in self._radars():
            r.me_id = self.my_id
        self.refresh_nodes()
        self._show_thread()
        self.set_interval(5, self.refresh_nodes)
        self.set_interval(0.1, self.query_one("#radar", Radar).tick)
        self.query_one("#nodes", DataTable).focus()

    def on_unmount(self):
        if self.backend:
            self.backend.close()

    # ---- nodes -----------------------------------------------------------------------------
    def name_of(self, node_id, nodes=None):
        if nodes is None:
            if not self.backend:
                return node_id
            nodes = self.backend.nodes()
        user = (nodes.get(node_id) or {}).get("user") or {}
        return user.get("shortName") or user.get("longName") or node_id

    def visible_nodes(self, nodes):
        now = time.time()
        rows = []
        for nid, n in nodes.items():
            if nid == self.my_id:
                continue
            heard = n.get("lastHeard") or 0
            if self.filter == "recent" and now - heard > RECENT_SECS:
                continue
            if self.filter == "direct" and n.get("hopsAway") != 0:
                continue
            rows.append((nid, n))
        rows.sort(key=lambda r: r[1].get("lastHeard") or 0, reverse=True)
        return rows

    def _radars(self):
        return [self.query_one("#radar", Radar), self.query_one("#bigmap", Radar)]

    def refresh_nodes(self):
        if not self.backend:
            return
        table = self.query_one("#nodes", DataTable)
        nodes = self.backend.nodes()
        mine = position(nodes.get(self.my_id))
        for nid, n in nodes.items():
            if n.get("snr") is not None:
                self.history.setdefault(nid, deque(maxlen=SNR_HISTORY)).append(n["snr"])
        keep = None
        if table.row_count:
            keep = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        table.clear()
        rows = self.visible_nodes(nodes)
        for nid, n in rows:
            user = n.get("user") or {}
            theirs = position(n)
            dist = f"{haversine_km(mine, theirs):.1f}km" if mine and theirs else "-"
            batt = (n.get("deviceMetrics") or {}).get("batteryLevel")
            hops = n.get("hopsAway")
            table.add_row(
                Text(user.get("shortName") or "?",
                     style="dim" if node_class(n) == "old" else f"bold {node_color(nid)}"), nid,
                "-" if hops is None else str(hops), signal_bars(n.get("snr")), age_text(n.get("lastHeard")),
                "-" if batt is None else "USB" if batt > 100 else f"{batt}%", dist, key=nid,
            )
        if keep:
            for i, (nid, _) in enumerate(rows):
                if nid == keep:
                    table.move_cursor(row=i)
        live = sum(1 for nid, n in nodes.items() if nid != self.my_id and node_class(n) == "live")
        self.query_one("#nodes").border_title = (
            f"Nodes: {self.filter} ({len(rows)} shown · {max(0, len(nodes) - 1)} known · {live} live)")
        for r in self._radars():
            r.nodes, r.rows = nodes, rows
        direct = sum(1 for n in nodes.values() if n.get("hopsAway") == 0)
        self.query_one("#me", Static).update(
            render_me(nodes.get(self.my_id), max(0, len(nodes) - 1), direct, self.n_in, self.n_out))
        self.update_detail()
        self._refresh_convs()  # names/colours may have changed

    def update_detail(self):
        if not self.backend:
            return
        nodes = self.backend.nodes()
        n = nodes.get(self.selected)
        card = render_detail(self.selected, n, position(nodes.get(self.my_id)) or self.home,
                             self.basemap.places if self.basemap else None)
        data = list(self.history.get(self.selected, [])) or [0]
        self.query_one("#detail", Static).update(card)
        self.query_one("#snr", Sparkline).data = data
        self.query_one("#mapdetail", Static).update(card)
        self.query_one("#mapsnr", Sparkline).data = data
        self.query_one("#dm", Button).disabled = n is None
        for r in self._radars():
            r.selected = self.selected
            r.refresh()

    def on_data_table_row_highlighted(self, event):
        if event.data_table.id == "nodes":
            self.selected = event.row_key.value if event.row_key else None
            self.update_detail()

    def select_node(self, nid):
        """Select a node everywhere (detail cards, map highlight, node-list cursor)."""
        self.selected = nid
        table = self.query_one("#nodes", DataTable)
        for i, row in enumerate(table.ordered_rows):
            if row.key.value == nid:
                table.move_cursor(row=i)
        self.update_detail()

    @on(Radar.NodePicked)
    def _node_picked(self, event):
        self.select_node(event.node_id)
        if event.open_dm:
            self.open_conv(event.node_id)

    @on(Button.Pressed, "#dm")
    def _dm_pressed(self):
        if self.selected:
            self.open_conv(self.selected)

    def action_map(self):
        self.query_one(TabbedContent).active = "map"
        self.query_one("#bigmap", Radar).focus()

    def action_cycle_filter(self):
        self.filter = FILTERS[(FILTERS.index(self.filter) + 1) % len(FILTERS)]
        self.refresh_nodes()

    def action_next_theme(self):
        self._theme_i = (self._theme_i + 1) % len(THEMES)
        self.theme = THEMES[self._theme_i]
        self.notify(f"Theme: {self.theme}", timeout=2)

    # ---- conversations ---------------------------------------------------------------------
    def conv_name(self, key):
        return "# Channel" if key == BROADCAST else self.name_of(key)

    def _messages_active(self):
        return self.query_one(TabbedContent).active == "messages"

    def _refresh_convs(self):
        """Rebuild the inbox (dashboard) and conversation list (messages tab); update unread markers."""
        def last_ts(key):
            return max((r["ts"] for r in self.convs[key]), default=0)

        keys = sorted(self.convs, key=last_ts, reverse=True)
        inbox, convs = self.query_one("#inbox", DataTable), self.query_one("#convs", DataTable)
        for table in (inbox, convs):
            table.clear()
        for key in keys:
            msgs = [r for r in self.convs[key] if r["dir"] != "note"]
            name = Text(self.conv_name(key), style="bold " + ("cyan" if key == BROADCAST else node_color(key)))
            n = self.unread.get(key, 0)
            badge = Text(f"● {n}", style="bold yellow") if n else Text("")
            if msgs:
                last = msgs[-1]
                preview = ("you: " if last["dir"] == "out" else "") + last["text"].replace("\n", " ")[:40]
                when = age_text(last["ts"])
            else:
                preview, when = Text("no messages yet", style="dim"), Text("")
            inbox.add_row(name, badge, preview, when, key=key)
            convs.add_row(name, badge, key=key)
        if self.current in keys:
            convs.move_cursor(row=keys.index(self.current))
        total = sum(self.unread.values())
        self.query_one(TabbedContent).get_tab("messages").label = f"Messages ({total})" if total else "Messages"
        for r in self._radars():
            r.unread = {k for k, v in self.unread.items() if v and k != BROADCAST}
        if self.my_id:
            self.sub_title = f"you are {self.my_id}" + (f"  ·  {total} unread" if total else "")

    def _show_thread(self):
        """Render the open conversation into the thread pane and mark it read."""
        thread = self.query_one("#thread", RichLog)
        thread.clear()
        for rec in self.convs.get(self.current, [])[-200:]:
            thread.write(self._line(rec))
        self.unread.pop(self.current, None)
        title = self.conv_name(self.current)
        if self.current != BROADCAST:
            title += f"  ({self.current})"
        thread.border_title = title
        self.query_one("#send", Input).placeholder = f"Message {self.conv_name(self.current)} (Enter to send)"
        self._refresh_convs()

    def open_conv(self, key):
        self.convs.setdefault(key, [])
        self.current = key
        self._show_thread()
        tabs = self.query_one(TabbedContent)
        if tabs.active != "messages":
            tabs.active = "messages"
        self.call_after_refresh(self.query_one("#send", Input).focus)

    def on_tabbed_content_tab_activated(self, event):
        if event.pane.id == "messages":
            self._show_thread()  # opening the tab reads the current thread
            self.query_one("#send", Input).focus()

    def on_data_table_row_selected(self, event):
        if event.row_key and event.row_key.value is not None:
            self.open_conv(event.row_key.value)  # a node row starts/opens that DM; inbox rows open their thread

    def action_broadcast(self):
        self.open_conv(BROADCAST)

    def action_messages(self):
        self.open_conv(self.current)

    def action_dashboard(self):
        self.query_one(TabbedContent).active = "dash"
        self.query_one("#nodes", DataTable).focus()

    # ---- messages --------------------------------------------------------------------------
    def _line(self, rec):
        stamp = time.strftime("%H:%M", time.localtime(rec["ts"]))
        text = escape(rec["text"])
        if rec["dir"] == "note":
            return f"[dim italic]{text}[/]"
        if rec["dir"] == "out":
            return f"[dim]{stamp}[/] [green]you[/]: {text}"
        who = escape(self.name_of(rec["from"]))
        return f"[dim]{stamp}[/] [bold {node_color(rec['from'])}]{who}[/]: {text}"

    def _load_history(self):
        try:
            with open(self.log_path, encoding="utf-8") as f:
                for line in f.readlines()[-300:]:
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    if (isinstance(rec, dict) and rec.get("dir") in ("in", "out") and isinstance(rec.get("text"), str)
                            and all(k in rec for k in ("from", "to", "ts"))):
                        self.convs.setdefault(conv_of(rec), []).append(rec)
        except (OSError, ValueError):
            pass

    def _incoming_from_thread(self, packet):
        rec = {
            "ts": time.time(),
            "from": packet.get("fromId"),
            "to": packet.get("toId"),
            "text": (packet.get("decoded") or {}).get("text", ""),
            "dir": "in",
        }
        self.call_from_thread(self.add_message, rec)

    def add_message(self, rec):
        if rec["dir"] != "note":  # notes (delivery receipts, warnings) are shown but not saved
            os.makedirs(os.path.dirname(self.log_path), exist_ok=True)
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec) + "\n")
        key = conv_of(rec)
        self.convs.setdefault(key, []).append(rec)
        viewing = key == self.current and self._messages_active()
        if viewing:
            self.query_one("#thread", RichLog).write(self._line(rec))
        if rec["dir"] == "in":
            self.n_in += 1
            if not viewing:
                self.unread[key] = self.unread.get(key, 0) + 1
                self.notify(rec["text"][:120], title=f"New message from {self.name_of(rec['from'])}", timeout=6)
                self.bell()
        elif rec["dir"] == "out":
            self.n_out += 1
        self._refresh_convs()

    def _note(self, key, text):
        self.add_message({"ts": time.time(), "from": None, "to": key, "text": text, "dir": "note"})

    def on_input_submitted(self, event):
        text = event.value.strip()
        event.input.value = ""
        if not text or not self.backend:
            return
        dest = self.current
        if len(text.encode()) > MAX_BYTES:
            self._note(dest, f"Too long ({len(text.encode())} bytes, max {MAX_BYTES}); not sent.")
            return
        self.add_message({"ts": time.time(), "from": self.my_id, "to": dest, "text": text, "dir": "out"})

        def on_ack(packet):
            reason = ((packet.get("decoded") or {}).get("routing") or {}).get("errorReason", "NONE")
            who = self.name_of(dest)
            self.call_from_thread(self._note, dest, f"✓ delivered to {who}" if reason == "NONE" else f"{who}: {reason}")

        def send():
            try:
                self.backend.send_text(text, dest, on_ack)
            except Exception as e:
                self.call_from_thread(self._note, dest, f"send failed: {e}")

        threading.Thread(target=send, daemon=True).start()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("-p", "--port", default="/dev/ttyUSB0")
    ap.add_argument("--home", metavar="LAT,LON", help="your location, used to centre the map when the radio has no GPS fix")
    a = ap.parse_args()
    try:
        home = parse_home(a.home) if a.home else None
    except ValueError as e:
        ap.error(str(e))
    MeshTUI(lambda on_text: SerialBackend(a.port, on_text), home=home).run()
