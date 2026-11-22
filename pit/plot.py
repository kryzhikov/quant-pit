"""Terminal plots for `pit plot`: columns of a CSV or JSONL file as line charts.

Uses plotext 5.x (pure Python). Imported lazily so the rest of pit works without it.
Output is plain text when stdout is not a terminal, which is what lets the dashboard
command line and agents read a plot.
"""
import csv
import io
import json
import math
import re
import shutil
import sys

ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _float(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def read_records(text):
    """Parse CSV (with a header row) or JSONL (one object per line) into a list of dicts."""
    stripped = text.lstrip()
    if stripped.startswith("{"):
        out = []
        for line in stripped.splitlines():
            line = line.strip()
            if line:
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if isinstance(obj, dict):
                    out.append(obj)
        return out
    try:
        dialect = csv.Sniffer().sniff(stripped[:4096], delimiters=",\t;|")
    except csv.Error:
        dialect = csv.excel
    return list(csv.DictReader(io.StringIO(stripped), dialect=dialect))


def select_series(records, x=None, ys=None, tail=0, max_series=4):
    """Pick columns to plot. Returns (x_values, x_label, [(name, values)]).

    Without `ys`, every column that is numeric in most rows is used (first
    `max_series`). Rows where a series has no number are skipped for that series.
    """
    if tail:
        records = records[-tail:]
    if not records:
        raise ValueError("no rows to plot")
    columns = []
    for r in records:
        for k in r:
            if k not in columns:
                columns.append(k)
    if x and x not in columns:
        raise ValueError("no column '%s' (have: %s)" % (x, ", ".join(map(str, columns))))
    if ys:
        missing = [y for y in ys if y not in columns]
        if missing:
            raise ValueError("no column '%s' (have: %s)" % (missing[0], ", ".join(map(str, columns))))
    else:
        ys = [c for c in columns if c != x
              and sum(1 for r in records if _float(r.get(c)) is not None) >= max(1, len(records) // 2)][:max_series]
    if not ys:
        raise ValueError("no numeric columns found (have: %s)" % ", ".join(map(str, columns)))
    xs = [_float(r.get(x)) for r in records] if x else None
    if xs is not None and any(v is None for v in xs):
        raise ValueError("column '%s' is not numeric in every row: omit -x to plot against the row number" % x)
    series = []
    for y in ys:
        pts = [((xs[i] if xs else i), _float(r.get(y))) for i, r in enumerate(records)]
        pts = [p for p in pts if p[1] is not None]
        if pts:
            series.append((str(y), pts))
    if not series:
        raise ValueError("selected columns hold no numbers")
    return (x or "row"), series


def render(x_label, series, width=80, height=20, color=False, logy=False, title=""):
    import plotext as plt
    plt.clear_figure()
    plt.theme("dark" if color else "clear")
    plt.plotsize(width, height)
    if logy:
        plt.yscale("log")
    for name, pts in series:
        if logy:
            pts = [p for p in pts if p[1] > 0]
        plt.plot([p[0] for p in pts], [p[1] for p in pts], label=name)
    plt.xlabel(x_label)
    if title:
        plt.title(title)
    out = plt.build()
    return out if color else ANSI.sub("", out)


def main(path, x=None, ys=None, tail=0, logy=False, title="", size=None, out=None):
    out = out or sys.stdout
    text = sys.stdin.read() if path == "-" else open(path).read()
    x_label, series = select_series(read_records(text), x, ys, tail)
    tty = hasattr(out, "isatty") and out.isatty()
    if size:
        width, height = size
    elif tty:
        cols, rows = shutil.get_terminal_size((100, 30))
        width, height = cols, max(10, rows - 4)
    else:
        width, height = 78, 20
    out.write(render(x_label, series, width, height, color=tty, logy=logy, title=title or ("" if path == "-" else path)))
    last = "  ".join("%s=%.6g" % (n, pts[-1][1]) for n, pts in series)
    out.write("\nlast: %s  (%d rows)\n" % (last, max(len(p) for _, p in series)))
    return 0
