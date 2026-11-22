"""Slurm panel: the queue from `squeue`, plus a one-line node summary from `sinfo`.

Config (panel.slurm): scope "me" (default, `squeue --me`) or "all"; "user" to use
`-u <user>` instead of --me (Slurm older than 20.02 has no --me); "ssh" to run both
tools on a login node; "max_rows".

Not verified against a live cluster: see docs/ONBOARDING.md, section Slurm.
"""
import re
import shlex
from collections import Counter

from . import Panel, Table

# The job name is last so that a "|" inside a name cannot shift the other fields.
# %b is the per-node gres request (tres-per-node), e.g. "gres/gpu:2" or "N/A".
SQUEUE_FORMAT = "%i|%u|%T|%M|%l|%D|%P|%b|%R|%j"
SQUEUE_FIELDS = ("id", "user", "state", "elapsed", "limit", "nodes", "partition", "tres", "where", "name")
# %F is allocated/idle/other/total node counts for the partition.
SINFO_FORMAT = "%P|%a|%F"

STATE_STYLE = {
    "RUNNING": "ok",
    "PENDING": "warn", "SUSPENDED": "warn", "PREEMPTED": "warn", "CANCELLED": "warn",
    "FAILED": "bad", "TIMEOUT": "bad", "NODE_FAIL": "bad", "OUT_OF_MEMORY": "bad",
    "BOOT_FAIL": "bad", "DEADLINE": "bad",
    "COMPLETING": "dim", "COMPLETED": "dim", "CONFIGURING": "dim",
}
_RANK = {"bad": 0, "ok": 1, "dim": 2, None: 2, "warn": 3}  # row order when max_rows truncates
DOC = "docs/ONBOARDING.md, section Slurm"


def parse_gpus(tres):
    """GPUs in a gres/tres string: 'gres/gpu:a100:2' -> 2, 'gpu' -> 1, 'N/A' -> 0."""
    total = 0
    for item in re.sub(r"\([^)]*\)", "", tres or "").split(","):
        tokens = re.split(r"[:/=]", item.strip().lower())
        if "gpu" in tokens:
            total += int(tokens[-1]) if tokens[-1].isdigit() else 1
    return total


def parse_squeue(text):
    """`squeue -h -o SQUEUE_FORMAT` output -> list of job dicts. Malformed lines are skipped."""
    jobs = []
    for line in text.splitlines():
        parts = line.strip().split("|", len(SQUEUE_FIELDS) - 1)
        if len(parts) != len(SQUEUE_FIELDS):
            continue
        job = dict(zip(SQUEUE_FIELDS, [p.strip() for p in parts]))
        job["gpus"] = parse_gpus(job["tres"])
        jobs.append(job)
    return jobs


def parse_sinfo(text):
    """`sinfo -h -o SINFO_FORMAT` output -> one dict per partition (lines for the same partition are summed)."""
    parts, order = {}, []
    for line in text.splitlines():
        cols = [c.strip() for c in line.strip().split("|")]
        if len(cols) != 3:
            continue
        counts = cols[2].split("/")
        if len(counts) != 4 or not all(c.isdigit() for c in counts):
            continue
        name = cols[0].rstrip("*")
        if name not in parts:
            parts[name] = {"partition": name, "default": cols[0].endswith("*"), "avail": cols[1],
                           "alloc": 0, "idle": 0, "other": 0, "total": 0}
            order.append(name)
        for key, c in zip(("alloc", "idle", "other", "total"), counts):
            parts[name][key] += int(c)
    return [parts[n] for n in order]


def summarize_states(jobs):
    """'5 jobs: 3 running, 2 pending'"""
    if not jobs:
        return "no jobs"
    counts = Counter(j["state"] for j in jobs)
    return "%d job%s: %s" % (len(jobs), "" if len(jobs) == 1 else "s",
                             ", ".join("%d %s" % (n, s.lower()) for s, n in counts.most_common()))


def summarize_partitions(parts, limit=6):
    """'idle nodes: gpu 3/8, cpu* 0/20'"""
    if not parts:
        return ""
    bits = ["%s%s %d/%d%s" % (p["partition"], "*" if p["default"] else "", p["idle"], p["total"],
                              "" if p["avail"] == "up" else " (%s)" % p["avail"]) for p in parts[:limit]]
    if len(parts) > limit:
        bits.append("+%d more" % (len(parts) - limit))
    return "idle nodes: " + ", ".join(bits)


def _clip(s, n):
    return s if len(s) <= n else s[:n - 1] + "~"


def job_rows(jobs, show_user=False, max_rows=30):
    """Job dicts -> (columns, table rows). Failures sort first, pending last."""
    ordered = sorted(jobs, key=lambda j: _RANK[STATE_STYLE.get(j["state"])])[:max_rows]
    cols = ["JOB"] + (["USER"] if show_user else []) + ["NAME", "STATE", "ELAPSED", "LIMIT", "NODES", "GPU/NODE",
                                                         "PARTITION", "NODELIST(REASON)"]
    rows = []
    for j in ordered:
        style = STATE_STYLE.get(j["state"])
        row = [(j["id"], "hot")] + ([j["user"]] if show_user else [])
        row += [_clip(j["name"], 28), (j["state"], style) if style else j["state"], j["elapsed"], j["limit"],
                j["nodes"], str(j["gpus"]) if j["gpus"] else ("-", "dim"), j["partition"], _clip(j["where"], 32)]
        rows.append(row)
    return cols, rows


class SlurmPanel(Panel):
    name = "slurm"
    title = "SLURM"
    interval = 20

    def argv(self, argv):
        # ssh joins its arguments into one remote shell command, so "|" and "%" need quoting there
        return [shlex.quote(a) for a in argv] if self.conf.get("ssh") else argv

    def _failure(self, tool, rc, err):
        host = self.conf.get("ssh", "")
        err = (err.strip().splitlines() or ["no error output"])[-1]
        if rc == 127:
            if host:
                return "%s not found on %s: set panel.slurm.ssh to a Slurm login node (%s)" % (tool, host, DOC)
            return ("%s not found on PATH: run pit on a Slurm login node, or set panel.slurm.ssh to one "
                    "in ~/.config/pit/config.json (%s)" % (tool, DOC))
        if host and rc == 255:
            return "ssh to %s failed: %s (needs key-based, non-interactive login; %s)" % (host, err, DOC)
        return "%s failed (exit %d): %s" % (tool, rc, err)

    def fetch(self):
        scope = self.conf.get("scope", "me")
        cmd = ["squeue", "-h", "-o", SQUEUE_FORMAT]
        if scope != "all":
            cmd += ["-u", self.conf["user"]] if self.conf.get("user") else ["--me"]
        rc, out, err = self.run(self.argv(cmd))
        if rc != 0:
            return Table.message(self._failure("squeue", rc, err))
        jobs = parse_squeue(out)
        if not jobs and out.strip():
            return Table.message("could not parse squeue output, first line: %r (%s)" % (out.strip().splitlines()[0], DOC))

        max_rows = int(self.conf.get("max_rows", 30))
        cols, rows = job_rows(jobs, show_user=(scope == "all"), max_rows=max_rows)
        note = [summarize_states(jobs) + (" (showing %d)" % len(rows) if len(rows) < len(jobs) else "")]
        rc, out, err = self.run(self.argv(["sinfo", "-h", "-o", SINFO_FORMAT]))
        if rc == 0:
            note.append(summarize_partitions(parse_sinfo(out)))
        return Table(cols, rows, note=" | ".join(n for n in note if n), ok=True)


PANEL = SlurmPanel
