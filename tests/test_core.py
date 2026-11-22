"""Core logic tests. No tmux needed: state.derive takes plain dict rows."""
import json
from datetime import datetime, timezone

import pytest

from pit import clocks, config, hook, sessions, state
from pit.panels import Table, load_panels

NOW = 1_000_000.0
RULES = config.load_rules()


def row(**kw):
    base = {"name": "s", "kind": "agent", "agent": "claude", "hook_state": "", "hook_ts": "", "hook_src": "",
            "ack_ts": "0", "run": "", "created": str(NOW - 600), "command": "claude", "attached": "0"}
    base.update(kw)
    return base


def derive(r, screen=""):
    return state.derive(r, RULES, capture=lambda name: screen, now=NOW)[0]


def test_hook_states():
    assert derive(row(hook_state="working", hook_ts=str(NOW - 5))) == "working"
    assert derive(row(hook_state="stop", hook_ts=str(NOW - 5), ack_ts=str(NOW - 60))) == "done"
    assert derive(row(hook_state="stop", hook_ts=str(NOW - 60), ack_ts=str(NOW - 5))) == "idle"


def test_blocked_is_confirmed_against_screen():
    r = row(hook_state="blocked", hook_ts=str(NOW - 30), hook_src="Notification(permission_prompt)")
    assert derive(r, screen="Do you want to proceed?\n❯ 1. Yes") == "blocked"
    assert derive(r, screen="❯ \n") == "idle"          # prompt dismissed with Esc: no hook fires
    assert derive(dict(r, hook_ts=str(NOW - 1)), screen="") == "blocked"  # too fresh to second-guess


def test_no_hook_signal_uses_screen():
    young = row(created=str(NOW - 3))
    assert derive(young, screen="") == "starting"
    assert derive(young, screen="Is this a project you created or one you trust?") == "blocked"
    assert derive(row(), screen="✻ Thinking (12s · esc to interrupt)") == "working"
    assert derive(row(), screen="nothing") == "idle"


def test_agent_exit_and_shell_sessions():
    assert derive(row(command="zsh", hook_state="stop", hook_ts=str(NOW - 5))) == "exited"
    assert derive(row(command="zsh", created=str(NOW - 2))) == "starting"
    sh = row(kind="shell", agent="", command="zsh")
    assert derive(sh) == "idle"
    assert derive(dict(sh, command="python3")) == "working"
    assert derive(dict(sh, hook_state="stop", hook_ts=str(NOW - 5))) == "done"
    assert derive(dict(sh, command="-zsh")) == "idle"


def test_bad_regex_does_not_raise():
    s, why = state.screen_state("x", {"rules": [{"state": "blocked", "any": ["("]}], "default": "idle"})
    assert s == "idle" and "bad regex" in why


def test_fmt_age():
    assert [state.fmt_age(x) for x in (5, 65, 3700, 90000)] == ["5s", "1m", "1h01", "1d01h"]


def test_resolve():
    rows = [{"name": "alpha"}, {"name": "alpine"}, {"name": "beta"}]
    assert sessions.resolve(rows, "beta") == "beta"
    assert sessions.resolve(rows, "2") == "alpine"
    assert sessions.resolve(rows, "b") == "beta"
    with pytest.raises(sessions.PitError):
        sessions.resolve(rows, "al")
    with pytest.raises(sessions.PitError):
        sessions.resolve(rows, "zzz")


def test_expand_env(monkeypatch):
    monkeypatch.setenv("PIT_T_TOKEN", "secret")
    assert sessions.expand_env({"A": "$PIT_T_TOKEN", "B": "plain"}) == {"A": "secret", "B": "plain"}
    with pytest.raises(sessions.PitError):
        sessions.expand_env({"A": "$PIT_T_UNSET_VAR"})


def test_claude_settings_and_command(tmp_path, monkeypatch):
    monkeypatch.setenv("PIT_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("SHELL", "/bin/zsh")
    cfg = config.load()
    cmd = sessions.build_command("x", cfg["profiles"]["claude"], cfg, prompt="it's a test", model="kimi-k2")
    assert cmd.startswith("/bin/zsh -lic ") and "--model kimi-k2" in cmd and "--settings" in cmd
    data = json.loads((tmp_path / "sessions" / "x.settings.json").read_text())
    assert set(data["hooks"]) == set(sessions.HOOK_EVENTS)
    assert "hook.py" in data["hooks"]["Stop"][0]["hooks"][0]["command"]
    assert sessions.build_command("y", cfg["profiles"]["shell"], cfg) == ""


def test_hook_event_mapping(monkeypatch):
    calls = []
    monkeypatch.setattr(hook, "tmux_set", lambda session, opts: calls.append(opts))
    hook.on_event("s", {"hook_event_name": "Notification", "notification_type": "permission_prompt"})
    hook.on_event("s", {"hook_event_name": "Notification", "notification_type": "idle_prompt"})
    hook.on_event("s", {"hook_event_name": "Stop", "session_id": "abc"})
    hook.on_event("s", {"hook_event_name": "PreToolUse", "tool_name": "Bash"})
    assert [c["state"] for c in calls] == ["blocked", "stop", "working"]
    assert calls[1]["sid"] == "abc" and calls[2]["src"] == "PreToolUse(Bash)"


def utc(*a):
    return datetime(*a, tzinfo=timezone.utc)


def venue(code):
    return next(v for v in config.load()["clocks"]["venues"] if v["code"] == code)


def test_clocks_nyse_sessions_and_dst():
    # Wed 2026-07-15: EDT (UTC-4). 13:00 UTC = 09:00 local -> pre-market, 30 min to the open.
    s = clocks.status(venue("NYSE"), utc(2026, 7, 15, 13, 0))
    assert (s["label"], int(s["seconds"]), s["next_label"]) == ("pre", 1800, "open")
    # Wed 2026-01-14: EST (UTC-5). 15:00 UTC = 10:00 local -> open, 6h to the close.
    s = clocks.status(venue("NYSE"), utc(2026, 1, 14, 15, 0))
    assert (s["label"], int(s["seconds"]), s["next_label"]) == ("open", 6 * 3600, "post")
    # Saturday: closed until Monday 04:00 local pre-market.
    s = clocks.status(venue("NYSE"), utc(2026, 7, 18, 12, 0))
    assert s["label"] == "closed" and s["next_label"] == "pre"
    assert int(s["seconds"]) == int((utc(2026, 7, 20, 8, 0) - utc(2026, 7, 18, 12, 0)).total_seconds())


def test_clocks_overnight_and_24h_and_holiday():
    # CME: Sunday 17:00 Chicago open runs to Monday 16:00. Mon 2026-07-13 03:00 UTC = Sun 22:00 CDT.
    s = clocks.status(venue("CME"), utc(2026, 7, 13, 3, 0))
    assert s["label"] == "open" and int(s["seconds"]) == 18 * 3600
    # ...and the daily maintenance hour is closed: Mon 16:30 CDT = 21:30 UTC.
    assert clocks.status(venue("CME"), utc(2026, 7, 13, 21, 30))["label"] == "closed"
    c = clocks.status(venue("CRYPTO"), utc(2026, 7, 13, 3, 0))
    assert c["label"] == "open" and c["seconds"] is None
    v = dict(venue("LSE"), holidays=["2026-07-15"])
    assert clocks.status(v, utc(2026, 7, 15, 10, 0))["label"] == "closed"
    # SHFE night session opens Monday evening Shanghai time (13:30 UTC = 21:30 CST).
    assert clocks.status(venue("SHFE"), utc(2026, 7, 13, 13, 30))["label"] == "night"


def test_panel_loader_isolates_failures():
    cfg = dict(config.load(), panels=["clocks", "does_not_exist"])
    good, bad = load_panels(cfg)
    assert good.safe_fetch().rows and "failed to load" in bad.safe_fetch().note
    assert "A" in Table(["A"], [[("x", "ok")]], note="n").to_text()


def test_config_deep_merge():
    assert config.deep_merge({"a": {"b": 1, "c": 2}, "d": 1}, {"a": {"b": 9}}) == {"a": {"b": 9, "c": 2}, "d": 1}


def test_stale_working_hook_is_checked_against_screen():
    # Esc interrupts a turn without a Stop hook: an old "working" must agree with the screen.
    r = row(hook_state="working", hook_ts=str(NOW - 120), hook_src="PreToolUse(Bash)", ack_ts=str(NOW - 300))
    assert derive(r, screen="auto mode on · esc to interrupt") == "working"
    assert derive(r, screen="❯ \n  auto mode on") == "done"
    assert derive(dict(r, ack_ts=str(NOW - 5)), screen="❯ ") == "idle"
    assert derive(r, screen="Do you want to proceed?") == "blocked"
    assert derive(dict(r, hook_ts=str(NOW - 2)), screen="❯ ") == "working"   # fresh hooks are trusted


def test_holiday_removes_overnight_session_by_trade_date():
    v = dict(venue("CME"), holidays=["2026-12-25"])
    # Fri 2026-12-25 10:00 Chicago = 16:00 UTC: the session that opened Thursday evening is Friday's.
    assert clocks.status(v, utc(2026, 12, 25, 16, 0))["label"] == "closed"
    assert clocks.status(venue("CME"), utc(2026, 12, 25, 16, 0))["label"] == "open"
    # Thursday's own session (opened Wednesday evening) is untouched.
    assert clocks.status(v, utc(2026, 12, 24, 16, 0))["label"] == "open"
