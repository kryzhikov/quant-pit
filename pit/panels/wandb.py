"""Weights & Biases panel: the most recent runs of one project on a self-hosted server.

Talks GraphQL directly (no wandb SDK): POST {base_url}/graphql with HTTP basic auth,
user "api" and the API key as password.

UNVERIFIED: QUERY below was written from memory of the query the wandb SDK's public API
sends and has never been run against a server. Field names, argument names and the
"-created_at" order value may differ on your server version. If the panel shows a GraphQL
error, fix QUERY (and parse_runs if the shape changed): docs/ONBOARDING.md, section W&B,
shows how to capture the query the installed SDK really sends.

Config (panel.wandb): base_url, entity, project, api_key_env (NAME of the environment
variable holding the key), max_rows, metrics (summary metric names to show, max 3; empty
means the first numeric ones found), and optionally ca_file (PEM bundle for an internal CA).
"""
import base64
import calendar
import json
import os
import re
import ssl
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from . import Panel, Table

QUERY = """
query PitRecentRuns($entity: String!, $project: String!, $first: Int!, $order: String) {
  project(name: $project, entityName: $entity) {
    runs(first: $first, order: $order) {
      edges {
        node {
          name
          displayName
          state
          createdAt
          heartbeatAt
          summaryMetrics
          user { username }
        }
      }
    }
  }
}
"""
ORDER = "-created_at"
MAX_METRICS = 3
STALE_HEARTBEAT = 300  # seconds without a heartbeat before a "running" run is flagged
STATE_STYLE = {"running": "ok", "finished": "dim", "failed": "bad", "crashed": "bad",
               "killed": "warn", "preempted": "warn", "preempting": "warn", "pending": "warn"}
DOC = "docs/ONBOARDING.md, section W&B"

_TS = re.compile(r"^(\d{4})-(\d\d)-(\d\d)[T ](\d\d):(\d\d):(\d\d)(?:\.\d+)?\s*(Z|[+-]\d\d:?\d\d)?$")


def parse_ts(s):
    """ISO-8601 timestamp (naive is taken as UTC) -> epoch seconds, or None."""
    m = _TS.match(s.strip()) if isinstance(s, str) else None
    if not m:
        return None
    epoch = calendar.timegm(tuple(int(x) for x in m.groups()[:6]) + (0, 0, 0))
    tz = m.group(7)
    if tz and tz != "Z":
        digits = tz[1:].replace(":", "")
        offset = int(digits[:2]) * 3600 + int(digits[2:]) * 60
        epoch -= offset if tz[0] == "+" else -offset
    return epoch


def fmt_age(seconds):
    if seconds is None:
        return "?"
    seconds = max(0, int(seconds))
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return "%d%s" % (seconds // size, unit)
    return "%ds" % seconds


def build_body(entity, project, first):
    variables = {"entity": entity, "project": project, "first": int(first), "order": ORDER}
    return json.dumps({"query": QUERY, "variables": variables}).encode("utf-8")


def _is_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _summary(raw):
    """summaryMetrics arrives as a JSON-encoded string (or possibly an object): always return a dict."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return {}
    return raw if isinstance(raw, dict) else {}


def parse_runs(data):
    """Decoded GraphQL response -> list of run dicts, newest first as the server returned them.

    Raises ValueError with a readable message on GraphQL errors or an unexpected shape.
    """
    if not isinstance(data, dict):
        raise ValueError("response is not a JSON object")
    if data.get("errors"):
        msgs = [e.get("message", str(e)) if isinstance(e, dict) else str(e) for e in data["errors"]]
        raise ValueError("GraphQL error: " + "; ".join(msgs))
    payload = data.get("data")
    if not isinstance(payload, dict) or "project" not in payload:
        raise ValueError("response has no data.project")
    if payload["project"] is None:
        raise ValueError("project not found or not visible to this API key")
    try:
        edges = payload["project"]["runs"]["edges"]
        nodes = [e["node"] for e in edges]
    except (KeyError, TypeError) as e:
        raise ValueError("response has no data.project.runs.edges[].node (%s: %s)" % (type(e).__name__, e))
    runs = []
    for n in nodes:
        runs.append({
            "name": n.get("displayName") or n.get("name") or "?",
            "id": n.get("name") or "",
            "state": n.get("state") or "",
            "user": (n.get("user") or {}).get("username") or "",
            "created": parse_ts(n.get("createdAt")),
            "heartbeat": parse_ts(n.get("heartbeatAt")),
            "summary": _summary(n.get("summaryMetrics")),
        })
    return runs


def lookup(summary, name):
    """A summary value by exact key, else by dotted path ('val.loss' -> summary['val']['loss'])."""
    if name in summary:
        return summary[name]
    node = summary
    for part in name.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def pick_metrics(runs, wanted=(), limit=MAX_METRICS):
    """The metric columns: the configured names, else the first numeric summary keys seen (not _step etc)."""
    if wanted:
        return list(wanted)[:limit]
    picked = []
    for r in runs:
        for key, value in r["summary"].items():
            if _is_num(value) and not key.startswith("_") and key not in picked:
                picked.append(key)
                if len(picked) == limit:
                    return picked
    return picked


def run_rows(runs, metrics, now=None):
    now = time.time() if now is None else now
    rows = []
    for r in runs:
        style = STATE_STYLE.get(r["state"])
        beat_age = None if r["heartbeat"] is None else now - r["heartbeat"]
        beat = fmt_age(beat_age)
        if r["state"] == "running" and beat_age is not None and beat_age > STALE_HEARTBEAT:
            beat = (beat, "warn")
        row = [(r["name"] if len(r["name"]) <= 32 else r["name"][:31] + "~", "hot"),
               (r["state"], style) if style else r["state"], r["user"],
               fmt_age(None if r["created"] is None else now - r["created"]), beat]
        for m in metrics:
            v = lookup(r["summary"], m)
            row.append("%.4g" % v if _is_num(v) else ("-", "dim"))
        rows.append(row)
    return rows


def http_json(url, headers, body=None, timeout=10, ca_file=""):
    """GET (or POST when body is given) and decode JSON. Returns (status, data_or_None, error_text)."""
    req = Request(url, data=body, headers=headers)
    ctx = ssl.create_default_context(cafile=ca_file) if ca_file else None
    try:
        with urlopen(req, timeout=timeout, context=ctx) as resp:
            status, raw = resp.getcode(), resp.read()
    except HTTPError as e:
        status, raw = e.code, e.read()
    except (OSError, ValueError) as e:  # URLError, timeouts, TLS and DNS failures are all OSError
        return 0, None, str(getattr(e, "reason", "") or e)
    try:
        return status, json.loads(raw.decode("utf-8", "replace")), ""
    except ValueError:
        return status, None, "not JSON: %r" % raw[:80].decode("utf-8", "replace")


class WandbPanel(Panel):
    name = "wandb"
    title = "W&B"
    interval = 60

    def fetch(self):
        c = self.conf
        base = c.get("base_url", "")
        missing = [k for k in ("base_url", "entity", "project") if not c.get(k)]
        if missing:
            return Table.message("W&B not configured: set panel.wandb.%s in ~/.config/pit/config.json (%s)"
                                 % (", panel.wandb.".join(missing), DOC))
        if not re.match(r"^https?://", base):
            return Table.message("panel.wandb.base_url must start with http:// or https://, got %r (%s)" % (base, DOC))
        key_env = c.get("api_key_env", "WANDB_API_KEY")
        key = os.environ.get(key_env, "")
        if not key:
            return Table.message("W&B API key missing: export %s in the environment pit runs in (%s)" % (key_env, DOC))

        max_rows = int(c.get("max_rows", 20))
        headers = {"Authorization": "Basic " + base64.b64encode(("api:" + key).encode("utf-8")).decode("ascii"),
                   "Content-Type": "application/json", "Accept": "application/json"}
        status, data, err = http_json(base.rstrip("/") + "/graphql", headers,
                                      body=build_body(c["entity"], c["project"], max_rows), ca_file=c.get("ca_file", ""))
        if status == 0:
            return Table.message("W&B unreachable at %s: %s (%s)" % (base, err, DOC))
        if data is None:
            return Table.message("W&B HTTP %d, %s. Check panel.wandb.base_url (%s)" % (status, err, DOC))
        try:
            runs = parse_runs(data)  # a GraphQL schema error comes back as JSON, often with HTTP 400
        except ValueError as e:
            hint = "API key rejected: check $%s. " % key_env if status in (401, 403) else ""
            return Table.message("W&B HTTP %d: %s%s. The query is unverified: see QUERY in pit/panels/wandb.py (%s)"
                                 % (status, hint, e, DOC))
        if status != 200:
            return Table.message("W&B HTTP %d with no GraphQL error (%s)" % (status, DOC))
        runs = runs[:max_rows]
        metrics = pick_metrics(runs, c.get("metrics") or ())
        note = "%s/%s: %d run%s, %d running" % (c["entity"], c["project"], len(runs), "" if len(runs) == 1 else "s",
                                                sum(1 for r in runs if r["state"] == "running"))
        return Table(["RUN", "STATE", "USER", "AGE", "HEARTBEAT"] + [m.upper() for m in metrics],
                     run_rows(runs, metrics), note=note, ok=True)


PANEL = WandbPanel
