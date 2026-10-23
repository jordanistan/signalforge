#!/usr/bin/env python3
"""Aircraft table built from ADS-B (Mode S extended squitter, 1090 MHz) frames from `rtl_adsb`.

ADS-B is an open, unencrypted broadcast that aircraft transmit for air traffic control and other aircraft, and
receiving it is legal. `rtl_adsb` prints raw frames like `*8D40621D58C382D690C8AC2863A7;`; pyModeS decodes them.
Positions are decoded from an even+odd frame pair (unambiguous worldwide); afterwards single frames are decoded
relative to the aircraft's own last fix. The receiver's location is NEVER used as a decoding reference, because that
makes aircraft farther than ~180 NM appear at a wrong, plausible-looking spot. Frames that fail their checksum are
dropped, positions far outside the receiver's plausible range are ignored, and a fix that implies an impossible
speed from the previous one is rejected.
"""
import math
import time

try:
    import pyModeS as pms
    from pyModeS.position import airborne_position_pair, airborne_position_with_ref
except ImportError:
    pms = None
    airborne_position_pair = airborne_position_with_ref = None

NM_PER_KM = 0.539957


def distance_nm(a, b):
    """Great-circle distance in nautical miles between (lat, lon) points."""
    r = math.radians
    h = math.sin((r(b[0]) - r(a[0])) / 2) ** 2 + math.cos(r(a[0])) * math.cos(r(b[0])) * math.sin((r(b[1]) - r(a[1])) / 2) ** 2
    return 12742 * math.asin(math.sqrt(max(0.0, min(1.0, h)))) * NM_PER_KM


def bearing_deg(a, b):
    r = math.radians
    y = math.sin(r(b[1] - a[1])) * math.cos(r(b[0]))
    x = math.cos(r(a[0])) * math.sin(r(b[0])) - math.sin(r(a[0])) * math.cos(r(b[0])) * math.cos(r(b[1] - a[1]))
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def jump_ok(prev, new, dt, max_kt=1200.0):
    """True if moving from `prev` to `new` (lat, lon) in `dt` seconds is physically plausible."""
    if dt <= 0:
        return distance_nm(prev, new) < 0.5
    return distance_nm(prev, new) / (dt / 3600.0) <= max_kt


CRC_POLY = 0x1FFF409  # Mode S CRC-24 generator polynomial


def crc_remainder(msg):
    """Mode S CRC-24 remainder of a hex frame (parity bits included). 0 means an ADS-B (DF17/18) frame is intact."""
    bits = len(msg) * 4
    n = int(msg, 16)
    for i in range(bits - 1, 23, -1):
        if n >> i & 1:
            n ^= CRC_POLY << (i - 24)
    return n & 0xFFFFFF


# Syndrome of a single flipped bit in a 112-bit frame -> that bit's position. Bits 0-4 are the downlink format and
# are never "repaired": changing them would turn some other kind of frame into a plausible-looking ADS-B one.
_SINGLE_BIT = {crc_remainder(f"{1 << (111 - i):028X}"): i for i in range(5, 112)}


def repair_single_bit(msg):
    """A 28-hex-digit frame with exactly one wrong bit, corrected (the same repair dump1090 does), or None."""
    bit = _SINGLE_BIT.get(crc_remainder(msg)) if len(msg) == 28 else None
    if bit is None:
        return None
    return f"{int(msg, 16) ^ (1 << (111 - bit)):028X}"


def clean_frame(raw):
    """'*8D4840D6...;' or '8d4840d6...' -> upper-case hex, or None if it cannot be a Mode S frame."""
    s = (raw or "").strip().lstrip("*").rstrip(";").upper()
    if len(s) not in (14, 28) or any(c not in "0123456789ABCDEF" for c in s):
        return None
    return s


class AircraftTable:
    PAIR_WINDOW = 10.0  # seconds within which an even and odd frame may be combined
    LOCAL_WINDOW = 60.0  # a fix this recent is a valid reference for decoding single frames

    def __init__(self, reference, ttl=120.0, max_range_nm=250.0, clock=time.time):
        if pms is None or not callable(getattr(pms, "decode", None)):
            raise RuntimeError("Compatible pyModeS is not installed; install the aircraft dependencies.")
        self.reference, self.ttl, self.max_range_nm, self.clock = tuple(reference), ttl, max_range_nm, clock
        self.aircraft = {}
        self.frames = self.accepted = self.rejected = 0
        # Why frames were not used, so a receiver hearing only noise can be told apart from a decoding problem:
        # malformed = not a hex Mode S frame; short = 56-bit replies (no ADS-B content); other_df = long frames that
        # are not ADS-B (Comm-B replies, or noise whose first bits are random); crc_failed = ADS-B frames damaged
        # beyond a one-bit repair (or repairable but from an aircraft not heard intact recently);
        # corrected = ADS-B frames accepted after a one-bit repair.
        self.malformed = self.short = self.other_df = self.crc_failed = self.corrected = 0

    def feed(self, raw, now=None):
        """Decode one frame and merge it into the table. Returns True if it was a valid ADS-B message."""
        now = self.clock() if now is None else now
        msg = clean_frame(raw)
        self.frames += 1
        if msg is None:
            self.malformed += 1
            self.rejected += 1
            return False
        if len(msg) == 14:
            self.short += 1
            self.rejected += 1
            return False
        if int(msg[:2], 16) >> 3 not in (17, 18):
            self.other_df += 1
            self.rejected += 1
            return False
        repaired = False
        if crc_remainder(msg):
            fixed = repair_single_bit(msg)
            # A repair is trusted only for an aircraft already heard intact recently: across hours of noise a
            # random frame will occasionally look one bit away from valid, and must not invent an aircraft.
            known = self.aircraft.get(fixed[2:8]) if fixed else None
            if fixed is None or not known or now - known["_clean"] > self.ttl:
                self.crc_failed += 1
                self.rejected += 1
                return False
            msg, repaired = fixed, True
        try:
            d = pms.decode(msg)  # no reference: raw CPR fields are combined below, never against the receiver
        except Exception:  # bad length/hex/unknown format from a noisy frame
            self.crc_failed += 1
            self.rejected += 1
            return False
        # Only self-identified ADS-B (DF17/18) with a good checksum: everything else may be a corrupted frame.
        if d.get("df") not in (17, 18) or not d.get("crc_valid") or not d.get("icao"):
            self.crc_failed += 1
            self.rejected += 1
            return False
        self.accepted += 1
        self.corrected += repaired
        ac = self.aircraft.setdefault(d["icao"], {"icao": d["icao"], "callsign": None, "alt_ft": None, "gs_kt": None,
                                                    "track": None, "vrate": None, "lat": None, "lon": None,
                                                    "pos_time": None, "first_seen": now, "msgs": 0, "_cpr": {},
                                                    "_clean": now})
        if not repaired:
            ac["_clean"] = now
        ac["last_seen"], ac["msgs"] = now, ac["msgs"] + 1
        cs = (d.get("callsign") or "").strip().strip("_")
        if cs:
            ac["callsign"] = cs
        if d.get("altitude") is not None:
            ac["alt_ft"] = d["altitude"]
        if d.get("groundspeed") is not None:
            ac["gs_kt"] = round(d["groundspeed"])
        if d.get("track") is not None:
            ac["track"] = round(d["track"], 1)
        if d.get("vertical_rate") is not None:
            ac["vrate"] = d["vertical_rate"]
        # Surface CPR uses a different grid and requires its own reference procedure.
        # Feeding it to an airborne decoder creates plausible but incorrect positions.
        if d.get("typecode") in (*range(9, 19), *range(20, 23)) and d.get("cpr_format") in (0, 1) and d.get("cpr_lat") is not None and d.get("cpr_lon") is not None:
            self._update_position(ac, d["cpr_format"], d["cpr_lat"], d["cpr_lon"], now)
        return True

    def stats(self):
        """Frame counters for the UI: frames = everything the receiver printed; the rest say where they went."""
        return {"frames": self.frames, "accepted": self.accepted, "rejected": self.rejected,
                "corrected": self.corrected, "crc_failed": self.crc_failed, "other_df": self.other_df,
                "short": self.short, "malformed": self.malformed}

    def _update_position(self, ac, fmt, lat_raw, lon_raw, now):
        cpr = ac["_cpr"]
        cpr[fmt] = (lat_raw, lon_raw, now)
        pos = None
        if ac["lat"] is not None and now - ac["pos_time"] <= self.LOCAL_WINDOW:
            try:  # local decode relative to this aircraft's own recent fix
                pos = airborne_position_with_ref(fmt, lat_raw, lon_raw, ac["lat"], ac["lon"])
            except Exception:
                pos = None
        elif 0 in cpr and 1 in cpr and abs(cpr[0][2] - cpr[1][2]) <= self.PAIR_WINDOW:
            try:  # global decode from an even+odd pair: unambiguous
                pos = airborne_position_pair(cpr[0][0], cpr[0][1], cpr[1][0], cpr[1][1], even_is_newer=cpr[0][2] >= cpr[1][2])
            except Exception:
                pos = None
        if not pos or len(pos) != 2 or not all(math.isfinite(v) for v in pos):
            return
        if not (-90 <= pos[0] <= 90 and -180 <= pos[1] <= 180):
            return
        if distance_nm(self.reference, pos) > self.max_range_nm:
            return  # far beyond what this receiver can hear: treat as corrupt
        if ac["lat"] is not None and not jump_ok((ac["lat"], ac["lon"]), pos, now - ac["pos_time"]):
            return  # would need an impossible speed
        ac["lat"], ac["lon"], ac["pos_time"] = round(pos[0], 5), round(pos[1], 5), now

    def snapshot(self, now=None):
        """Aircraft heard within `ttl` seconds, nearest first (those without a position last). Purges stale ones."""
        now = self.clock() if now is None else now
        for icao in [k for k, a in self.aircraft.items() if now - a["last_seen"] > self.ttl]:
            del self.aircraft[icao]
        out = []
        for a in self.aircraft.values():
            row = {k: v for k, v in a.items() if not k.startswith("_")}
            row["age_s"] = round(now - a["last_seen"], 1)
            if a["lat"] is not None and now - (a["pos_time"] or 0) <= self.ttl:
                p = (a["lat"], a["lon"])
                row["dist_nm"] = round(distance_nm(self.reference, p), 1)
                row["bearing"] = round(bearing_deg(self.reference, p))
            else:
                row["lat"] = row["lon"] = None
                row["dist_nm"] = row["bearing"] = None
            out.append(row)
        out.sort(key=lambda r: (r["dist_nm"] is None, r["dist_nm"] if r["dist_nm"] is not None else 0, -r["last_seen"]))
        return out
