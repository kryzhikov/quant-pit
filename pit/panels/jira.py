"""Jira panel: issues matching a JQL query, from a self-hosted Jira (Server / Data Center).

Uses GET {base_url}/rest/api/2/search. Config (panel.jira): base_url, jql, max_rows,
auth "bearer" (personal access token) or "basic" (user + password/token), token_env and
user_env (NAMES of environment variables: secrets are never read from a file), and
optionally ca_file (PEM bundle for an internal CA) and summary_width.

Not verified against a live server: see docs/ONBOARDING.md, section Jira.
"""
import base64
import calendar
import json
import os
import re
import ssl
import time
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from . import Panel, Table

FIELDS = "summary,status,priority,assignee,updated"
PRIORITY_STYLE = {"blocker": "bad", "highest": "bad", "critical": "bad", "high": "warn", "major": "warn",
                  "low": "dim", "lowest": "dim", "minor": "dim", "trivial": "dim"}
CATEGORY_STYLE = {"indeterminate": "ok", "done": "dim"}  # statusCategory.key: new / indeterminate / done
DOC = "docs/ONBOARDING.md, section Jira"

_TS = re.compile(r"^(\d{4})-(\d\d)-(\d\d)[T ](\d\d):(\d\d):(\d\d)(?:\.\d+)?\s*(Z|[+-]\d\d:?\d\d)?$")


def parse_ts(s):
    """ISO-8601 timestamp ('2026-11-20T14:03:11.000+0000', '...Z', or naive = UTC) -> epoch seconds, or None."""
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
    """Compact relative age: 45s, 12m, 3h, 9d."""
    if seconds is None:
        return "?"
    seconds = max(0, int(seconds))
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return "%d%s" % (seconds // size, unit)
    return "%ds" % seconds


def build_url(base_url, jql, max_results):
    query = urlencode({"jql": jql, "fields": FIELDS, "maxResults": int(max_results)})
    return base_url.rstrip("/") + "/rest/api/2/search?" + query


def auth_header(auth, token, user=""):
    if auth == "basic":
        return "Basic " + base64.b64encode(("%s:%s" % (user, token)).encode("utf-8")).decode("ascii")
    return "Bearer " + token


def _name(obj):
    return obj.get("name", "") if isinstance(obj, dict) else ""


def parse_issues(data, now=None):
    """Decoded /rest/api/2/search response -> list of issue dicts. Raises ValueError on a wrong shape."""
    if not isinstance(data, dict) or not isinstance(data.get("issues"), list):
        raise ValueError("response has no 'issues' list")
    now = time.time() if now is None else now
    issues = []
    for it in data["issues"]:
        f = it.get("fields") or {}
        status = f.get("status") or {}
        updated = parse_ts(f.get("updated"))
        issues.append({
            "key": it.get("key", "?"),
            "summary": f.get("summary") or "",
            "status": _name(status),
            "category": (status.get("statusCategory") or {}).get("key", ""),
            "priority": _name(f.get("priority")),
            "assignee": (f.get("assignee") or {}).get("displayName", ""),
            "age": None if updated is None else now - updated,
        })
    return issues


def issue_rows(issues, width=60):
    rows = []
    for i in issues:
        summary = i["summary"] if len(i["summary"]) <= width else i["summary"][:width - 1] + "~"
        status = CATEGORY_STYLE.get(i["category"])
        prio = PRIORITY_STYLE.get(i["priority"].lower())
        rows.append([(i["key"], "hot"),
                     (i["status"], status) if status else i["status"],
                     ((i["priority"], prio) if prio else i["priority"]) if i["priority"] else ("-", "dim"),
                     fmt_age(i["age"]), summary])
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


def _api_errors(data):
    if not isinstance(data, dict):
        return ""
    msgs = list(data.get("errorMessages") or []) + ["%s: %s" % kv for kv in sorted((data.get("errors") or {}).items())]
    return "; ".join(str(m) for m in msgs)


class JiraPanel(Panel):
    name = "jira"
    title = "JIRA"
    interval = 120

    def fetch(self):
        c = self.conf
        base = c.get("base_url", "")
        if not base:
            return Table.message("Jira not configured: set panel.jira.base_url in ~/.config/pit/config.json (%s)" % DOC)
        if not re.match(r"^https?://", base):
            return Table.message("panel.jira.base_url must start with http:// or https://, got %r (%s)" % (base, DOC))
        auth = c.get("auth", "bearer")
        if auth not in ("bearer", "basic"):
            return Table.message("panel.jira.auth must be \"bearer\" or \"basic\", got %r (%s)" % (auth, DOC))
        token_env = c.get("token_env", "JIRA_TOKEN")
        token = os.environ.get(token_env, "")
        if not token:
            return Table.message("Jira token missing: export %s in the environment pit runs in (%s)" % (token_env, DOC))
        user = ""
        if auth == "basic":
            user_env = c.get("user_env", "JIRA_USER")
            user = os.environ.get(user_env, "")
            if not user:
                return Table.message("Jira basic auth needs a user: export %s, or set panel.jira.auth to "
                                     "\"bearer\" (%s)" % (user_env, DOC))

        max_rows = int(c.get("max_rows", 20))
        headers = {"Authorization": auth_header(auth, token, user), "Accept": "application/json"}
        status, data, err = http_json(build_url(base, c.get("jql", ""), max_rows), headers, ca_file=c.get("ca_file", ""))
        if status == 0:
            return Table.message("Jira unreachable at %s: %s (%s)" % (base, err, DOC))
        if status != 200:
            hint = {400: "bad JQL in panel.jira.jql", 401: "token rejected: check $%s and panel.jira.auth" % token_env,
                    403: "forbidden: token lacks permission, or the account needs a CAPTCHA login in a browser",
                    404: "wrong panel.jira.base_url (include any context path, e.g. /jira)"}.get(status, "")
            detail = "; ".join(x for x in (hint, _api_errors(data) or err) if x)
            return Table.message("Jira HTTP %d: %s (%s)" % (status, detail or "no detail", DOC))
        if data is None:
            return Table.message("Jira returned %s. An HTML page usually means an SSO or login redirect: "
                                 "check base_url and the token (%s)" % (err, DOC))
        try:
            issues = parse_issues(data)
        except ValueError as e:
            return Table.message("unexpected Jira response: %s (%s)" % (e, DOC))
        total = data.get("total")
        note = "%d issue%s" % (len(issues), "" if len(issues) == 1 else "s")
        if isinstance(total, int) and total > len(issues):
            note = "%d of %d issues" % (len(issues), total)
        return Table(["KEY", "STATUS", "PRIORITY", "UPDATED", "SUMMARY"],
                     issue_rows(issues[:max_rows], int(c.get("summary_width", 60))), note=note, ok=True)


PANEL = JiraPanel
