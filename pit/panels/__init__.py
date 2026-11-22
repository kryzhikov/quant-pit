"""Panels: small data sources shown in the dashboard's lower pane and via `pit panel`.

To add one, drop a file here (or in ~/.config/pit/panels/) that defines:

    from pit.panels import Panel, Table

    class MyPanel(Panel):
        name = "mine"          # id used in config "panels" and `pit panel mine`
        title = "MINE"         # tab label
        interval = 30          # seconds between refreshes

        def fetch(self):
            return Table(["COL A", "COL B"], [["x", ("y", "ok")]], note="2 rows")

    PANEL = MyPanel

Rules: fetch() runs in a worker thread, so it may block on a subprocess or HTTP call,
but must use timeouts and must never raise for an expected condition (tool missing,
not configured, service down): return Table.message("...") instead. Panel code is
stdlib only and Python 3.9 compatible. Cells are a string or (string, style) with
style one of: ok, warn, bad, dim, hot. A panel instance lives as long as the
dashboard, so it may keep a short history on self and draw it with spark().
"""
import importlib
import importlib.util
import shutil
import subprocess
import traceback

from ..config import config_dir

STYLES = ("ok", "warn", "bad", "dim", "hot")


SPARK = "\u2581\u2582\u2583\u2584\u2585\u2586\u2587\u2588"  # block elements: safe in Windows Terminal


def spark(values, lo=None, hi=None):
    """A one-line bar sparkline. `lo`/`hi` fix the scale; otherwise the data's own range."""
    vals = [v for v in values if v is not None]
    if not vals:
        return ""
    lo = min(vals) if lo is None else lo
    hi = max(vals) if hi is None else hi
    span = (hi - lo) or 1.0
    out = []
    for v in values:
        if v is None:
            out.append(" ")
        else:
            out.append(SPARK[max(0, min(len(SPARK) - 1, int((v - lo) / span * (len(SPARK) - 1) + 0.5)))])
    return "".join(out)


class Table(object):
    def __init__(self, columns, rows, note="", ok=True):
        self.columns, self.rows, self.note, self.ok = list(columns), list(rows), note, ok

    @classmethod
    def message(cls, text, ok=False):
        """A table with no rows: used for 'not configured', 'tool missing', errors."""
        return cls([], [], note=text, ok=ok)

    def to_text(self):
        """Plain-text rendering, for `pit panel <name>` and for agents."""
        rows = [[c[0] if isinstance(c, (tuple, list)) else str(c) for c in r] for r in self.rows]
        lines = []
        if self.columns:
            widths = [max([len(h)] + [len(r[i]) for r in rows if i < len(r)]) for i, h in enumerate(self.columns)]
            fmt = lambda r: "  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip()
            lines.append(fmt(self.columns))
            lines += [fmt(r) for r in rows]
        if self.note:
            lines.append(("" if self.ok else "! ") + self.note)
        return "\n".join(lines)


class Panel(object):
    name = ""
    title = ""
    interval = 30

    def __init__(self, cfg):
        self.cfg = cfg                                   # the whole pit config
        self.conf = cfg.get("panel", {}).get(self.name, {})  # this panel's own section
        self.interval = self.conf.get("interval", self.interval)

    def fetch(self):
        raise NotImplementedError

    def safe_fetch(self):
        try:
            return self.fetch()
        except Exception as e:  # a broken panel must not take the dashboard down
            return Table.message("%s: %s\n%s" % (type(e).__name__, e, traceback.format_exc(limit=3)))

    # helper shared by panels that shell out, optionally through `ssh <host>`
    def run(self, argv, timeout=10):
        """Run argv (optionally over ssh, per this panel's "ssh" setting).

        Returns (returncode, stdout, stderr); returncode 127 means the tool is missing.
        """
        host = self.conf.get("ssh", "")
        if host:
            argv = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", host] + argv
        elif not shutil.which(argv[0]):
            return 127, "", "%s not found on PATH" % argv[0]
        try:
            p = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
        except subprocess.TimeoutExpired:
            return 124, "", "%s timed out after %ss" % (argv[0], timeout)
        return p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")


def load_panels(cfg):
    """Instantiate the panels named in cfg["panels"], in that order."""
    out = []
    for name in cfg.get("panels", []):
        try:
            user = config_dir() / "panels" / (name + ".py")
            if user.exists():
                spec = importlib.util.spec_from_file_location("pit_user_panel_" + name, str(user))
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
            else:
                mod = importlib.import_module("pit.panels." + name)
            out.append(mod.PANEL(cfg))
        except Exception as e:
            out.append(_Broken(cfg, name, "%s: %s" % (type(e).__name__, e)))
    return out


class _Broken(Panel):
    def __init__(self, cfg, name, err):
        self.name, self.title, self.err = name, name.upper(), err
        Panel.__init__(self, cfg)

    def fetch(self):
        return Table.message("panel '%s' failed to load: %s" % (self.name, self.err))
