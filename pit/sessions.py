"""Creating sessions from profiles, and the small operations on them.

A profile is a named (command, env) pair from the config. `kind` is "agent" or
"shell"; for agents, `agent` names the screen-rule set in rules/agents.json and,
when it is "claude", pit injects its hooks through `claude --settings <file>`.
"""
import json
import os
import shlex
import sys
import time
from pathlib import Path

from . import tmuxctl
from .config import PIT_HOME, state_dir

HOOK_EVENTS = ["SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse",
               "Notification", "Stop", "PreCompact", "SessionEnd"]


class PitError(Exception):
    pass


def expand_env(env):
    """Resolve "$VAR" values from the environment; error on any that are unset."""
    out, missing = {}, []
    for k, v in env.items():
        if isinstance(v, str) and v.startswith("$"):
            if v[1:] not in os.environ:
                missing.append(v[1:])
            out[k] = os.environ.get(v[1:], "")
        else:
            out[k] = str(v)
    if missing:
        raise PitError("profile needs these environment variables set: " + ", ".join(missing))
    return out


def user_statusline():
    """The statusline command from the user's own Claude settings, if any."""
    try:
        data = json.loads((Path.home() / ".claude" / "settings.json").read_text())
        return (data.get("statusLine") or {}).get("command", "")
    except (OSError, ValueError):
        return ""


def claude_settings(name, cfg):
    """Write the per-session settings file that wires Claude Code hooks to pit."""
    hook = "python3 %s" % shlex.quote(str(PIT_HOME / "pit" / "hook.py"))
    settings = {"hooks": {ev: [{"hooks": [{"type": "command", "timeout": 5,
                                           "command": "%s event %s" % (hook, name)}]}]
                          for ev in HOOK_EVENTS}}
    if cfg.get("statusline", True):
        settings["statusLine"] = {"type": "command", "command": "%s statusline %s" % (hook, name)}
    d = state_dir() / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    path = d / (name + ".settings.json")
    path.write_text(json.dumps(settings, indent=1))
    return path


def build_command(name, profile, cfg, prompt="", extra=None, model=""):
    """The shell command line a session runs. Empty string means a plain login shell."""
    cmd = profile.get("cmd", "")
    if not cmd:
        return ""
    if profile.get("agent") == "claude":
        cmd += " --settings %s" % shlex.quote(str(claude_settings(name, cfg)))
    if model:
        cmd += " --model %s" % shlex.quote(model)
    for a in extra or []:
        cmd += " " + shlex.quote(a)
    if prompt:
        cmd += " " + shlex.quote(prompt)
    shell = os.environ.get("SHELL", "/bin/sh")
    # Run through an interactive login shell so PATH matches what you get by hand, and
    # fall back to a shell when the command ends so its output stays inspectable.
    return "%s -lic %s" % (shell, shlex.quote("%s; exec %s -l" % (cmd, shell)))


def create(cfg, name, profile_name=None, cwd=None, prompt="", extra=None, model=""):
    name = tmuxctl.sanitize(name)
    if not name or name == tmuxctl.HOME_SESSION:
        raise PitError("invalid session name")
    profile_name = profile_name or cfg.get("default_profile", "claude")
    profile = cfg.get("profiles", {}).get(profile_name)
    if not profile:
        raise PitError("unknown profile '%s' (have: %s)" % (profile_name, ", ".join(sorted(cfg.get("profiles", {})))))
    if tmuxctl.has_session(name):
        raise PitError("session '%s' already exists" % name)
    cwd = str(Path(cwd or os.getcwd()).expanduser())
    if not os.path.isdir(cwd):
        raise PitError("no such directory: %s" % cwd)
    env = expand_env(profile.get("env", {}))
    env.update(PIT_SESSION=name, PIT_SOCKET=tmuxctl.SOCKET, PIT_HOME=str(PIT_HOME))
    if profile.get("agent") == "claude":
        sl = user_statusline()
        if sl and "hook.py statusline" not in sl:
            env["PIT_USER_STATUSLINE"] = sl
    opts = {"kind": profile.get("kind", "shell"), "profile": profile_name,
            "agent": profile.get("agent", ""), "ack": int(time.time())}
    if model:
        if profile.get("kind") != "agent":
            raise PitError("--model only applies to agent profiles")
        opts["model"] = model
    err = tmuxctl.new_session(name, cwd, build_command(name, profile, cfg, prompt, extra, model), env=env, opts=opts)
    if err:
        raise PitError(err)
    return name


def resolve(rows, ref):
    """Find a session by exact name, 1-based index in the listing, or unique prefix."""
    names = [r["name"] for r in rows]
    if ref in names:
        return ref
    if ref.isdigit() and 1 <= int(ref) <= len(names):
        return names[int(ref) - 1]
    hits = [n for n in names if n.startswith(ref)]
    if len(hits) == 1:
        return hits[0]
    raise PitError("no session '%s'" % ref if not hits else "'%s' is ambiguous: %s" % (ref, ", ".join(hits)))


def ensure_home():
    """Make sure the dashboard session exists. Returns its name."""
    if not tmuxctl.has_session(tmuxctl.HOME_SESSION):
        pit = shlex.quote(str(PIT_HOME / "bin" / "pit"))
        # Exit code 75 = "restart me" (the dashboard's R key, used after editing pit's code).
        loop = ("while :; do %s ui; rc=$?; [ $rc -eq 75 ] && continue; "
                "echo; echo \"pit ui exited with code $rc. Press Enter to restart it.\"; read _; done" % pit)
        err = tmuxctl.new_session(tmuxctl.HOME_SESSION, str(Path.home()), "sh -c %s" % shlex.quote(loop),
                                  env={"PIT_HOME": str(PIT_HOME)}, opts={"kind": "home"})
        if err:
            raise PitError(err)
        tmuxctl.tmux("set-option", "-t", tmuxctl.target(tmuxctl.HOME_SESSION) + ":", "status", "off")
    return tmuxctl.HOME_SESSION
