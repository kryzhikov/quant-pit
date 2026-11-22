"""Session state: one function turns raw tmux facts into a state plus the reason for it.

States (also the attention order, most urgent first):
  blocked   an agent is waiting on a permission prompt or a question
  done      work finished while nobody was looking (unacknowledged)
  working   agent is running a turn / shell has a foreground command
  starting  agent launched, no signal from it yet
  idle      nothing happening, and you have seen it
  exited    the agent process ended; the pane is back at a shell

Sources, in order of trust:
  1. hook    Claude Code hooks write @pit_state (see pit/hook.py)
  2. screen  regex rules over the bottom of the pane (rules/agents.json)
  3. process the pane's foreground command (shell sessions)
"""
import re
import time

from . import tmuxctl

ORDER = ["blocked", "done", "working", "starting", "idle", "exited"]
CODE = {"blocked": "BLKD", "done": "DONE", "working": "WORK", "starting": "INIT",
        "idle": "IDLE", "exited": "EXIT"}
STARTUP_GRACE = 20  # seconds an agent may stay silent before we trust the screen instead
WORKING_TRUST = 15  # seconds a "working" hook is believed before the screen must agree


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def is_shell(command):
    return command.lstrip("-") in tmuxctl.SHELLS


def screen_state(text, agent_rules):
    """Apply screen rules to pane text. Returns (state, reason)."""
    tail = "\n".join(text.splitlines()[-int(agent_rules.get("tail_lines", 30)):])
    for rule in agent_rules.get("rules", []):
        for pat in rule.get("any", []):
            try:
                if re.search(pat, tail, re.M):
                    return rule["state"], "screen rule /%s/" % pat
            except re.error as e:
                return agent_rules.get("default", "idle"), "bad regex /%s/: %s" % (pat, e)
    return agent_rules.get("default", "idle"), "screen: no rule matched"


def derive(row, rules, capture=None, now=None):
    """Return (state, reason, since_ts) for one row from tmuxctl.list_sessions().

    `capture(name) -> str` is called only when the screen must be consulted, so a
    poll over many healthy hook-driven sessions costs no extra tmux calls.
    """
    now = now or time.time()
    capture = capture or tmuxctl.capture
    hook, hook_ts, ack = row["hook_state"], _num(row["hook_ts"]), _num(row["ack_ts"])
    created = _num(row["created"])
    shell_fg = is_shell(row["command"])

    if row["kind"] == "shell":
        if not shell_fg:
            return "working", "process: foreground command '%s'" % row["command"], hook_ts or created
        if hook == "stop" and hook_ts > ack:
            return "done", "process: command finished, not yet seen", hook_ts
        return "idle", "process: at shell prompt", max(hook_ts, ack, created)

    # agent sessions
    if shell_fg:
        if not hook and now - created < STARTUP_GRACE:
            return "starting", "process: agent not up yet", created
        return "exited", "process: agent ended, pane is at '%s'" % row["command"], hook_ts or created

    agent_rules = rules.get(row["agent"]) or rules.get("generic", {})
    if hook == "working":
        # An interrupted turn (Esc) fires no Stop hook, so an old "working" needs the
        # screen to agree. Verified on Claude Code 2.1: the footer shows "esc to interrupt".
        if now - hook_ts < WORKING_TRUST:
            return "working", "hook: " + row["hook_src"], hook_ts
        s, why = screen_state(capture(row["name"]), agent_rules)
        if s == "working":
            return "working", "hook: " + row["hook_src"], hook_ts
        if s == "blocked":
            return "blocked", "hook said working; " + why, hook_ts
        if hook_ts > ack:
            return "done", "hook said working but the turn ended without a Stop (interrupted?); not yet seen", hook_ts
        return "idle", "hook said working but the turn ended without a Stop (interrupted?)", max(hook_ts, ack)
    if hook == "blocked":
        # A prompt dismissed with Esc fires no hook, so confirm against the screen.
        s, why = screen_state(capture(row["name"]), agent_rules)
        if s == "blocked" or now - hook_ts < 3:
            return "blocked", "hook: " + row["hook_src"], hook_ts
        return s, "hook said blocked but prompt is gone; " + why, hook_ts
    if hook == "stop":
        if hook_ts > ack:
            return "done", "hook: %s, not yet seen" % row["hook_src"], hook_ts
        return "idle", "hook: %s, seen" % row["hook_src"], max(hook_ts, ack)
    if hook == "idle":
        return "idle", "hook: " + row["hook_src"], hook_ts
    # No hook signal: the screen is all we have. A first-run trust prompt lands here.
    s, why = screen_state(capture(row["name"]), agent_rules)
    if s != "blocked" and not hook and now - created < STARTUP_GRACE:
        return "starting", "no hook signal yet", created
    return s, "no hook signal; " + why, created


def snapshot(rules, reconcile=True, now=None):
    """All managed sessions with derived state, sorted as an attention queue.

    With reconcile=True this also writes two small facts back to tmux:
    acknowledging sessions you are currently attached to, and stamping the moment
    a shell command finished. That is what makes `done` work without a daemon.
    """
    now = now or time.time()
    out = []
    for row in tmuxctl.list_sessions():
        if row["kind"] not in ("agent", "shell"):
            continue
        if reconcile:
            _reconcile(row, now)
        st, why, since = derive(row, rules, now=now)
        row.update(state=st, reason=why, since=since)
        out.append(row)
    out.sort(key=lambda r: (ORDER.index(r["state"]), r["name"]))
    return out


def _reconcile(row, now):
    name, attached = row["name"], row["attached"] not in ("", "0")
    updates = {}
    if row["kind"] == "shell":
        running = not is_shell(row["command"])
        if running and row["run"] != "1":
            updates.update(run=1, state="working", state_ts=int(now))
        elif not running and row["run"] == "1":
            updates.update(run=0, state="stop", state_ts=int(now))
            row["hook_state"], row["hook_ts"] = "stop", str(int(now))
    if attached and _num(row["hook_ts"]) >= _num(row["ack_ts"]):
        updates["ack"] = int(now) + 1
        row["ack_ts"] = str(int(now) + 1)
    if updates:
        tmuxctl.set_opts(name, **updates)


def fmt_age(seconds):
    s = int(max(0, seconds))
    if s < 60:
        return "%ds" % s
    if s < 3600:
        return "%dm" % (s // 60)
    if s < 86400:
        return "%dh%02d" % (s // 3600, s % 3600 // 60)
    return "%dd%02dh" % (s // 86400, s % 86400 // 3600)
