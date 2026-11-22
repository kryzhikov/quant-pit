"""Box panel: load, memory and free disk on this machine.

Config (panel.box): "paths" = directories whose filesystems to watch (default: home
and /tmp). Memory comes from /proc/meminfo, so it reads n/a outside Linux.
"""
import collections
import os
import shutil

from . import Panel, Table, spark


def parse_meminfo(text):
    """/proc/meminfo text -> (total_bytes, available_bytes), or None if fields are missing."""
    kb = {}
    for line in text.splitlines():
        parts = line.replace(":", " ").split()
        if len(parts) >= 2 and parts[1].isdigit():
            kb[parts[0]] = int(parts[1])
    if "MemTotal" not in kb or "MemAvailable" not in kb:
        return None
    return kb["MemTotal"] * 1024, kb["MemAvailable"] * 1024


def gib(n):
    return "%.1fG" % (n / 1024.0 ** 3)


def level(pct, warn=85, bad=95):
    return "bad" if pct >= bad else ("warn" if pct >= warn else "ok")


def disk_row(path, usage):
    pct = 100.0 * usage.used / usage.total if usage.total else 0.0
    return [("DISK", "hot"), path, ("%d%% used" % pct, level(pct)), "%s free of %s" % (gib(usage.free), gib(usage.total))]


class BoxPanel(Panel):
    name = "box"
    title = "BOX"
    interval = 10

    def __init__(self, cfg):
        Panel.__init__(self, cfg)
        self.load_hist = collections.deque(maxlen=30)

    def fetch(self):
        rows = []
        cpus = os.cpu_count() or 1
        try:
            l1, l5, l15 = os.getloadavg()
            self.load_hist.append(l1)
            rows.append([("LOAD", "hot"), "%d cpus" % cpus, ("%.2f %.2f %.2f" % (l1, l5, l15), level(100 * l1 / cpus, 80, 120)),
                         spark(self.load_hist, lo=0, hi=max(cpus, max(self.load_hist)))])
        except OSError:
            rows.append([("LOAD", "hot"), "%d cpus" % cpus, ("n/a", "dim"), ""])
        mem = None
        try:
            with open("/proc/meminfo") as f:
                mem = parse_meminfo(f.read())
        except OSError:
            pass
        if mem:
            pct = 100.0 * (mem[0] - mem[1]) / mem[0]
            rows.append([("MEM", "hot"), "", ("%d%% used" % pct, level(pct, 85, 95)), "%s available of %s" % (gib(mem[1]), gib(mem[0]))])
        else:
            rows.append([("MEM", "hot"), "", ("n/a", "dim"), "no /proc/meminfo on this OS"])
        seen = set()
        for p in self.conf.get("paths") or ["~", "/tmp"]:
            path = os.path.expanduser(p)
            try:
                dev = os.stat(path).st_dev
                if dev in seen:
                    continue
                seen.add(dev)
                rows.append(disk_row(p, shutil.disk_usage(path)))
            except OSError as e:
                rows.append([("DISK", "hot"), p, ("missing", "bad"), str(e)])
        return Table(["", "WHAT", "NOW", "DETAIL"], rows, note="load sparkline: last %d samples of the 1-minute average" % len(self.load_hist))


PANEL = BoxPanel
