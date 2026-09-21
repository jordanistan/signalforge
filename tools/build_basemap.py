#!/usr/bin/env python3
"""Build data/basemap.json for the TUI map.

Lines (coast, borders, highways, rivers): Natural Earth, public domain, naturalearthdata.com.
Cities: GeoNames cities5000 (population >= 5000), CC BY 4.0, geonames.org.
Downloads everything into data/raw/ (once),
crops them to a bounding box, simplifies the lines and writes one compact JSON file.
Usage: tools/build_basemap.py [--bbox S,N,W,E]   (default covers the continental US and its margins)
"""
import argparse
import io
import json
import os
import time
import urllib.request
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/"
GEONAMES = "https://download.geonames.org/export/dump/cities5000.zip"
def fetch(name, raw_dir):
    path = os.path.join(raw_dir, name + ".geojson")
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        print("downloading", name)
        urllib.request.urlretrieve(BASE + name + ".geojson", path)
    with open(path, encoding="utf-8") as f:
        return json.load(f)["features"]


def geonames_places(raw_dir, bbox, countries=("US", "CA", "MX"), min_pop=5000):
    """[name, state, lat, lon, population] for cities inside bbox, biggest first."""
    path = os.path.join(raw_dir, "cities5000.zip")
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        print("downloading cities5000")
        urllib.request.urlretrieve(GEONAMES, path)
    s, n, w, e = bbox
    names = {"CA": "Canada", "MX": "Mexico"}
    out = []
    with zipfile.ZipFile(path) as z, z.open("cities5000.txt") as f:
        for line in io.TextIOWrapper(f, encoding="utf-8"):
            c = line.rstrip("\n").split("\t")
            lat, lon, pop = float(c[4]), float(c[5]), int(c[14] or 0)
            if c[8] in countries and pop >= min_pop and s <= lat <= n and w <= lon <= e:
                out.append([c[2] or c[1], c[10] if c[8] == "US" else names[c[8]], round(lat, 3), round(lon, 3), pop])
    out.sort(key=lambda r: -r[4])
    return out


def rdp(pts, eps):
    """Ramer-Douglas-Peucker simplification (iterative). pts = [(lat, lon), ...]"""
    if len(pts) < 3:
        return pts
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        a, b = stack.pop()
        (ay, ax), (by, bx) = pts[a], pts[b]
        dx, dy = bx - ax, by - ay
        norm = (dx * dx + dy * dy) ** 0.5 or 1e-12
        far, far_i = 0.0, None
        for i in range(a + 1, b):
            py, px = pts[i]
            d = abs(dy * (px - ax) - dx * (py - ay)) / norm
            if d > far:
                far, far_i = d, i
        if far_i is not None and far > eps:
            keep[far_i] = True
            stack += [(a, far_i), (far_i, b)]
    return [p for p, k in zip(pts, keep) if k]


def lines_of(geom):
    if geom["type"] == "LineString":
        yield geom["coordinates"]
    elif geom["type"] == "MultiLineString":
        yield from geom["coordinates"]


def crop(coords, bbox):
    """Split a lon/lat polyline into runs of points inside bbox, returned as (lat, lon) lists."""
    s, n, w, e = bbox
    run, out = [], []
    for lon, lat, *_ in coords:
        if s <= lat <= n and w <= lon <= e:
            run.append((lat, lon))
        elif run:
            out.append(run)
            run = []
    if run:
        out.append(run)
    return [r for r in out if len(r) >= 2]


def pack(features, bbox, eps, keep=lambda p: True):
    out = []
    for f in features:
        if not keep(f["properties"]):
            continue
        for line in lines_of(f["geometry"]):
            for run in crop(line, bbox):
                run = rdp(run, eps)
                if len(run) >= 2:
                    out.append([round(v, 3) for p in run for v in p])  # flat [lat, lon, lat, lon, ...]
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bbox", default="17,53,-128,-64", help="south,north,west,east")
    ap.add_argument("--raw", default=os.path.join(ROOT, "data", "raw"))
    ap.add_argument("--out", default=os.path.join(ROOT, "data", "basemap.json"))
    a = ap.parse_args()
    bbox = tuple(float(v) for v in a.bbox.split(","))
    os.makedirs(a.raw, exist_ok=True)
    s, n, w, e = bbox

    na = {"USA", "CAN", "MEX"}  # state/province lines for these countries only
    data = {
        "coast": pack(fetch("ne_10m_coastline", a.raw), bbox, 0.004),
        "borders": pack(fetch("ne_10m_admin_1_states_provinces_lines", a.raw), bbox, 0.006,
                        lambda p: p.get("ADM0_A3") in na)
        + pack(fetch("ne_10m_admin_0_boundary_lines_land", a.raw), bbox, 0.006),
        "roads": pack(fetch("ne_10m_roads", a.raw), bbox, 0.006,
                      lambda p: p.get("type") == "Major Highway"),
        "rivers": pack(fetch("ne_10m_rivers_lake_centerlines", a.raw), bbox, 0.006,
                       lambda p: (p.get("scalerank") or 99) <= 6),
    }
    places = geonames_places(a.raw, bbox)
    data["places"] = places
    data["meta"] = {"lines": "Natural Earth (public domain)", "cities": "GeoNames, CC BY 4.0 (geonames.org)",
                    "bbox": bbox, "built": time.strftime("%Y-%m-%d")}
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(data, f, separators=(",", ":"))
    for k, v in data.items():
        if k != "meta":
            print(f"{k:8} {len(v):6} items" + (f", {sum(len(x) // 2 for x in v):7} points" if k != "places" else ""))
    print(f"wrote {a.out} ({os.path.getsize(a.out) / 1e6:.2f} MB)")


if __name__ == "__main__":
    main()
