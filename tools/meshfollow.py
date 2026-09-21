#!/usr/bin/env python3
"""Follow mode bookkeeping: topics a remote node asked to be kept up to date on.

The web server (tools/meshweb.py) records `follow <topic>` requests from allowed nodes here and answers them over
the radio; the Claude session runs the periodic web check and uses this CLI to see what is due and to record what
it reported. State lives in run/follows.json (guarded by a lock file, written atomically).

CLI (used by the periodic check):
  meshfollow.py list [--json]          all active follows
  meshfollow.py due [--json]           follows due for a check now (interval elapsed, message budget left)
  meshfollow.py checked ID [--report TEXT] [--sent N]   record a check (and how many messages were sent)
  meshfollow.py stop KEY               stop by id, topic text or 'all'
"""
import argparse
import contextlib
import fcntl
import json
import os
import re
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATH = os.path.join(ROOT, "run", "follows.json")
MAX_FOLLOWS = 3
INTERVAL_MIN = 15  # how often a follow is re-checked
TTL_SECS = 6 * 3600  # follows end by themselves; the node can renew
MAX_MSGS_PER_HOUR = 8  # unsolicited messages per follow: radio airtime is shared
MAX_TOPIC = 80


def clean_topic(text):
    """Topic text comes from an unauthenticated radio: keep it to a short printable search phrase."""
    t = re.sub(r"[\x00-\x1f\x7f]", " ", str(text or ""))
    t = re.sub(r"\s+", " ", t).strip(" \t\"'`.,;:!?")
    return t[:MAX_TOPIC].strip()


class FollowStore:
    def __init__(self, path=PATH, clock=time.time):
        self.path, self.clock = path, clock

    @contextlib.contextmanager
    def _locked(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path + ".lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def _read(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d.get("follows"), list):
                return d
        except (OSError, ValueError, AttributeError):
            pass
        return {"follows": [], "next_id": 1}

    def _write(self, d):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=1)
        os.replace(tmp, self.path)

    def list(self):
        with self._locked():
            return self._read()["follows"]

    def add(self, topic, node, interval_min=INTERVAL_MIN):
        """-> (status, follow): status is 'added', 'exists' or 'limit'."""
        topic = clean_topic(topic)
        if len(topic) < 3:
            return "bad", None
        with self._locked():
            d = self._read()
            mine = [f for f in d["follows"] if f["node"] == node]
            for f in mine:
                if f["topic"].lower() == topic.lower():
                    f["expires"] = self.clock() + TTL_SECS  # asking again renews it
                    self._write(d)
                    return "exists", f
            if len(mine) >= MAX_FOLLOWS:
                return "limit", None
            now = self.clock()
            f = {"id": f"f{d.get('next_id', 1)}", "topic": topic, "node": node, "started": now,
                 "expires": now + TTL_SECS, "interval_min": interval_min, "last_check": None,
                 "last_report": "", "sent_log": []}
            d["next_id"] = d.get("next_id", 1) + 1
            d["follows"].append(f)
            self._write(d)
            return "added", f

    def remove(self, key, node=None):
        """Stop follows by id, topic substring, or 'all'. Returns the removed follows."""
        key = (key or "all").strip().lower()
        with self._locked():
            d = self._read()
            gone = [f for f in d["follows"] if (node is None or f["node"] == node)
                    and (key in ("all", "everything") or f["id"] == key or key in f["topic"].lower())]
            ids = {f["id"] for f in gone}
            d["follows"] = [f for f in d["follows"] if f["id"] not in ids]
            if gone:
                self._write(d)
            return gone

    def expire(self):
        with self._locked():
            d = self._read()
            now = self.clock()
            gone = [f for f in d["follows"] if f["expires"] <= now]
            if gone:
                d["follows"] = [f for f in d["follows"] if f["expires"] > now]
                self._write(d)
            return gone

    def due(self):
        now = self.clock()
        out = []
        for f in self.list():
            recent = [t for t in f.get("sent_log", []) if now - t < 3600]
            interval = f.get("interval_min", INTERVAL_MIN) * 60
            if f["expires"] > now and len(recent) < MAX_MSGS_PER_HOUR and (
                    f["last_check"] is None or now - f["last_check"] >= interval):
                out.append(f)
        return out

    def checked(self, fid, report=None, sent=0):
        with self._locked():
            d = self._read()
            now = self.clock()
            for f in d["follows"]:
                if f["id"] == fid:
                    f["last_check"] = now
                    if report is not None:
                        f["last_report"] = str(report)[:600]
                    f["sent_log"] = [t for t in f.get("sent_log", []) if now - t < 3600] + [now] * int(sent)
                    self._write(d)
                    return f
        return None


def parse_follow_command(text):
    """Recognise follow-mode commands. Returns (command, argument) or None."""
    t = (text or "").strip()
    if t.startswith("/"):  # `/follow topic` works the same as `follow topic`
        t = t[1:].lstrip()
    m = re.match(r"^(?:un-?follow|stop\s+following|stop\s+follow)\b[\s:]*(.*)$", t, re.I | re.S)
    if m:
        return "unfollow", m.group(1).strip() or "all"
    m = re.match(r"^follow\b[\s:]+(.+)$", t, re.I | re.S)
    if m:
        return "follow", m.group(1)
    if re.match(r"^(?:following|follows)\s*\??$", t, re.I):
        return "list", ""
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--path", default=PATH)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("list", "due"):
        sub.add_parser(name).add_argument("--json", action="store_true")
    c = sub.add_parser("checked")
    c.add_argument("id")
    c.add_argument("--report")
    c.add_argument("--sent", type=int, default=0)
    sub.add_parser("stop").add_argument("key")
    a = ap.parse_args()
    store = FollowStore(a.path)
    if a.cmd in ("list", "due"):
        rows = store.list() if a.cmd == "list" else store.due()
        if a.json:
            print(json.dumps(rows, indent=1))
        for f in ([] if a.json else rows):
            left = max(0, int((f["expires"] - time.time()) / 60))
            print(f"{f['id']}  node={f['node']}  every {f['interval_min']} min  {left} min left  topic={f['topic']!r}")
            if f.get("last_report"):
                print(f"    last reported: {f['last_report']}")
        if not rows and not a.json:
            print("(none)")
    elif a.cmd == "checked":
        f = store.checked(a.id, a.report, a.sent)
        print("recorded" if f else "no such follow", file=sys.stdout)
        sys.exit(0 if f else 1)
    elif a.cmd == "stop":
        gone = store.remove(a.key)
        print(f"stopped {len(gone)}: " + ", ".join(f['id'] for f in gone))


if __name__ == "__main__":
    main()
