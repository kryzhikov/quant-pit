"""GPU panel: one row per GPU from `nvidia-smi`.

Config (panel.gpu): "ssh" to query another host. Shows the GPUs of ONE machine (this one
or the ssh host), not of the cluster: on a Slurm login node without GPUs use "ssh".
"""
import collections
import shlex

from . import Panel, Table, spark

QUERY_FIELDS = ("index", "name", "utilization.gpu", "memory.used", "memory.total", "temperature.gpu", "power.draw")
ARGV = ["nvidia-smi", "--query-gpu=" + ",".join(QUERY_FIELDS), "--format=csv,noheader,nounits"]
BUSY_UTIL = 5      # percent utilisation above which a GPU counts as busy
BUSY_MEM = 0.05    # or this fraction of memory in use
DOC = "docs/ONBOARDING.md, section GPU"


def _num(s):
    """nvidia-smi prints '[N/A]' or '[Not Supported]' for fields a card lacks."""
    try:
        return float(s)
    except ValueError:
        return None


def parse_gpus(text):
    """`nvidia-smi --query-gpu=... --format=csv,noheader,nounits` output -> list of GPU dicts.

    Numeric fields are floats (memory in MiB, power in W) or None when not reported.
    """
    gpus = []
    for line in text.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != len(QUERY_FIELDS) or not parts[0].isdigit():
            continue
        name = parts[1][7:] if parts[1].startswith("NVIDIA ") else parts[1]
        util, used, total, temp, power = [_num(p) for p in parts[2:]]
        frac = used / total if used is not None and total else None
        gpus.append({"index": int(parts[0]), "name": name, "util": util, "mem_used": used, "mem_total": total,
                     "mem_frac": frac, "temp": temp, "power": power,
                     "busy": (util or 0) > BUSY_UTIL or (frac or 0) > BUSY_MEM})
    return gpus


def _level(value, warn, hot):
    if value is None or value <= BUSY_UTIL:
        return "dim"
    return "hot" if value >= hot else ("warn" if value >= warn else "ok")


def gpu_rows(gpus):
    rows = []
    for g in gpus:
        util = ("%d%%" % g["util"], _level(g["util"], 101, 90)) if g["util"] is not None else ("n/a", "dim")
        if g["mem_frac"] is not None:
            pct = g["mem_frac"] * 100
            mem = ("%.1f/%.1fG %d%%" % (g["mem_used"] / 1024.0, g["mem_total"] / 1024.0, pct), _level(pct, 90, 101))
        else:
            mem = ("n/a", "dim")
        if g["temp"] is None:
            temp = ("n/a", "dim")
        else:
            temp = ("%dC" % g["temp"], "bad" if g["temp"] >= 85 else ("warn" if g["temp"] >= 75 else "ok"))
        power = "%dW" % g["power"] if g["power"] is not None else ("n/a", "dim")
        rows.append([(str(g["index"]), "hot"), g["name"], util, mem, temp, power])
    return rows


def summarize(gpus):
    n = len(gpus)
    return "%d GPU%s, %d busy (util > %d%% or memory > %d%%)" % (
        n, "" if n == 1 else "s", sum(1 for g in gpus if g["busy"]), BUSY_UTIL, BUSY_MEM * 100)


class GpuPanel(Panel):
    name = "gpu"
    title = "GPU"
    interval = 10

    def __init__(self, cfg):
        Panel.__init__(self, cfg)
        self.hist = {}  # gpu index -> recent utilisation samples

    def fetch(self):
        host = self.conf.get("ssh", "")
        rc, out, err = self.run([shlex.quote(a) for a in ARGV] if host else ARGV)
        if rc != 0:
            err = ((err.strip() or out.strip()).splitlines() or ["no error output"])[0]
            if rc == 127 and host:
                return Table.message("nvidia-smi not found on %s: set panel.gpu.ssh to a GPU host (%s)" % (host, DOC))
            if rc == 127:
                return Table.message("nvidia-smi not found on PATH: no NVIDIA driver on this host. Set panel.gpu.ssh "
                                     "to a GPU host in ~/.config/pit/config.json, or drop \"gpu\" from \"panels\" (%s)" % DOC)
            if rc == 255 and host:
                return Table.message("ssh to %s failed: %s (needs key-based, non-interactive login; %s)" % (host, err, DOC))
            return Table.message("nvidia-smi failed (exit %d): %s" % (rc, err))
        gpus = parse_gpus(out)
        if not gpus:
            first = (out.strip().splitlines() or ["<empty>"])[0]
            return Table.message("nvidia-smi reported no GPUs, first line: %r (%s)" % (first, DOC))
        note = summarize(gpus) + (" on %s" % host if host else "")
        rows = gpu_rows(gpus)
        for g, row in zip(gpus, rows):
            h = self.hist.setdefault(g["index"], collections.deque(maxlen=30))
            h.append(g["util"])
            row.append(spark(h, lo=0, hi=100))
        return Table(["GPU", "MODEL", "UTIL", "MEMORY", "TEMP", "POWER", "UTIL HISTORY"], rows, note=note, ok=True)


PANEL = GpuPanel
