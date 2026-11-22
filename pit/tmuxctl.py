"""All tmux access goes through this module.

pit owns a private tmux server (socket name "pit", i.e. `tmux -L pit`), so it never
touches the user's own tmux sessions or options. Each managed session is one tmux
session; its metadata lives in tmux user options (@pit_*) on that session, which
makes tmux the single source of truth: there is no pit daemon and no database.

Target tmux 3.2a (Rocky/RHEL 9). Do not use anything newer: see AGENTS.md.
"""
import os
import re
import subprocess
import time

from .config import PIT_HOME

SOCKET = os.environ.get("PIT_SOCKET", "pit")
HOME_SESSION = "pit-home"
SEP = "|~|"
SHELLS = {"zsh", "bash", "sh", "fish", "dash", "ksh", "tcsh", "csh"}

# name -> tmux format. Order matters only for readability.
FIELDS = {
    "name": "#{session_name}",
    "kind": "#{@pit_kind}",
    "profile": "#{@pit_profile}",
    "hook_state": "#{@pit_state}",
    "hook_ts": "#{@pit_state_ts}",
    "hook_src": "#{@pit_src}",
    "ack_ts": "#{@pit_ack}",
    "run": "#{@pit_run}",
    "agent": "#{@pit_agent}",
    "claude_sid": "#{@pit_sid}",
    "model": "#{@pit_model}",
    "ctx": "#{@pit_ctx}",
    "cost": "#{@pit_cost}",
    "note": "#{@pit_note}",
    "attached": "#{session_attached}",
    "created": "#{session_created}",
    "activity": "#{session_activity}",
    "command": "#{pane_current_command}",
    "cwd": "#{pane_current_path}",
    "pane": "#{pane_id}",
    "pid": "#{pane_pid}",
    "dead": "#{pane_dead}",
}


def base_cmd():
    return ["tmux", "-L", SOCKET, "-f", str(PIT_HOME / "conf" / "tmux.conf")]


def tmux(*args, **kw):
    """Run one tmux command. Returns (returncode, stdout, stderr)."""
    env = dict(os.environ)
    env.pop("TMUX", None)  # we may be called from inside another tmux
    env["PIT_HOME"] = str(PIT_HOME)  # tmux.conf key bindings call $PIT_HOME/bin/pit
    try:
        p = subprocess.run(base_cmd() + list(args), stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, env=env,
                           timeout=kw.get("timeout", 5), input=kw.get("input"))
    except FileNotFoundError:
        return 127, "", "tmux not found"
    except subprocess.TimeoutExpired:
        return 124, "", "tmux timed out"
    return p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")


def version():
    try:
        out = subprocess.run(["tmux", "-V"], stdout=subprocess.PIPE).stdout.decode()
    except FileNotFoundError:
        return ""
    return out.strip().replace("tmux ", "")


def sanitize(name):
    """tmux treats '.' and ':' as target separators; keep names boring."""
    name = re.sub(r"[^A-Za-z0-9_-]+", "-", name.strip()).strip("-")
    return name[:40]


def target(name):
    return "=" + name  # '=' forces an exact session-name match


def list_sessions():
    """One tmux call for everything: a row per session (its active pane)."""
    fmt = SEP.join(FIELDS.values())
    rc, out, _ = tmux("list-panes", "-a", "-f", "#{&&:#{window_active},#{pane_active}}", "-F", fmt)
    if rc != 0:
        return []
    rows = []
    for line in out.splitlines():
        parts = line.split(SEP)
        if len(parts) != len(FIELDS):
            continue
        rows.append(dict(zip(FIELDS.keys(), parts)))
    return rows


def has_session(name):
    return tmux("has-session", "-t", target(name))[0] == 0


def set_opts(name, **opts):
    """Set several @pit_* options in a single tmux invocation."""
    args = []
    for k, v in opts.items():
        if args:
            args.append(";")
        args += ["set-option", "-t", target(name) + ":", "@pit_" + k, str(v)]  # needs the trailing ":"
    return tmux(*args)[0] == 0 if args else True


def new_session(name, cwd, command, env=None, opts=None, width=200, height=50):
    args = ["new-session", "-d", "-s", name, "-c", cwd, "-x", str(width), "-y", str(height)]
    for k, v in (env or {}).items():
        args += ["-e", "%s=%s" % (k, v)]
    if command:
        args.append(command)
    rc, _, err = tmux(*args)
    if rc != 0:
        return err.strip() or "tmux new-session failed"
    set_opts(name, **(opts or {}))
    return ""


def kill_session(name):
    return tmux("kill-session", "-t", target(name))[0] == 0


def capture(name, lines=40, ansi=False):
    """Last `lines` lines of the session's active pane."""
    args = ["capture-pane", "-p", "-J", "-t", target(name) + ":", "-S", "-%d" % lines]
    if ansi:
        args.insert(1, "-e")
    rc, out, _ = tmux(*args)
    if rc != 0:
        return ""
    return "\n".join(out.rstrip("\n").splitlines()[-lines:])


def send_text(name, text, enter=True):
    """Type literal text into the session, then (optionally) press Enter."""
    rc, _, err = tmux("send-keys", "-t", target(name) + ":", "-l", "--", text)
    if rc == 0 and enter:
        time.sleep(0.15)  # TUIs such as Claude Code treat a too-fast Enter as part of a paste
        rc, _, err = tmux("send-keys", "-t", target(name) + ":", "Enter")
    return err.strip() if rc != 0 else ""


def send_keys(name, keys):
    rc, _, err = tmux("send-keys", "-t", target(name) + ":", *keys)
    return err.strip() if rc != 0 else ""


def inside_pit():
    """True when this process runs in a pane of the pit tmux server."""
    t = os.environ.get("TMUX", "")
    return bool(t) and os.path.basename(t.split(",")[0]) == SOCKET


def goto(name, replace_process=True):
    """Show session `name` to the user: switch if already inside pit, else attach.

    Outside pit, attaching normally replaces this process; pass replace_process=False
    to attach as a child instead (the dashboard does this, then resumes on detach).
    """
    if inside_pit():
        return subprocess.call(base_cmd() + ["switch-client", "-t", target(name)])
    env = dict(os.environ)
    env.pop("TMUX", None)
    env["PIT_HOME"] = str(PIT_HOME)
    argv = base_cmd() + ["attach-session", "-t", target(name)]
    if not replace_process:
        return subprocess.call(argv, env=env)
    os.execvpe("tmux", argv, env)


def goto_detach():
    """Detach the client that is looking at pit (sessions keep running)."""
    return subprocess.call(base_cmd() + ["detach-client"])
