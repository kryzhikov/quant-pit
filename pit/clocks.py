"""Market session clocks. Pure functions over the `clocks.venues` config; stdlib only.

No holiday calendar is built in: add dates to a venue's `holidays` list.
"""
from datetime import date, datetime, time, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - python < 3.9
    ZoneInfo = None


def _hm(s):
    h, m = s.split(":")
    return time(int(h), int(m))


def intervals(venue, around):
    """Concrete (start, end, label) sessions, tz-aware, for a window around `around`."""
    tz = ZoneInfo(venue["tz"])
    today = around.astimezone(tz).date()
    holidays = set(venue.get("holidays", []))
    out = []
    for offset in range(-2, 9):
        d = today + timedelta(days=offset)
        for s in venue["sessions"]:
            if d.weekday() not in s["days"]:
                continue
            o, c = _hm(s["open"]), _hm(s["close"])
            # A holiday removes the sessions of that trade date. An overnight session
            # belongs to the date it closes on (CME's Sunday evening is Monday's session).
            trade_date = d + timedelta(days=1) if c <= o else d
            if trade_date.isoformat() in holidays:
                continue
            start = datetime.combine(d, o, tzinfo=tz)
            end = datetime.combine(d + timedelta(days=1) if c <= o else d, c, tzinfo=tz)
            out.append((start, end, s.get("label", "open")))
    out.sort(key=lambda x: x[0])
    return out


def status(venue, now=None):
    """Where the venue is right now.

    Returns {code, name, label, seconds, next_label, local}: `label` is the running
    session's label or "closed"; `seconds` is the time until that changes (None if
    it never does); `next_label` is what it changes to.
    """
    now = now or datetime.now(timezone.utc)
    ivs = intervals(venue, now)
    local = now.astimezone(ZoneInfo(venue["tz"]))
    base = {"code": venue["code"], "name": venue.get("name", ""), "local": local.strftime("%a %H:%M")}
    cur = next((iv for iv in ivs if iv[0] <= now < iv[1]), None)
    if cur:
        # Back-to-back sessions with the same label (24h venues) read as one long session.
        end, label = cur[1], cur[2]
        while True:
            nxt = next((iv for iv in ivs if iv[0] == end and iv[2] == label), None)
            if not nxt:
                break
            end = nxt[1]
        always = end >= ivs[-1][1]
        follow = next((iv[2] for iv in ivs if iv[0] == end), "closed")
        base.update(label=label, seconds=None if always else (end - now).total_seconds(), next_label=follow)
        return base
    nxt = next((iv for iv in ivs if iv[0] > now), None)
    base.update(label="closed", seconds=(nxt[0] - now).total_seconds() if nxt else None,
                next_label=nxt[2] if nxt else "closed")
    return base


def fmt_span(seconds):
    if seconds is None:
        return "24h"
    s = int(seconds)
    if s < 3600:
        return "%dm" % (s // 60)
    if s < 86400:
        return "%dh%02d" % (s // 3600, s % 3600 // 60)
    return "%dd%02dh" % (s // 86400, s % 86400 // 3600)


def all_status(cfg, now=None):
    return [status(v, now) for v in cfg.get("clocks", {}).get("venues", [])]
