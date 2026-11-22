"""Command line. This is also pit's API: agents drive other sessions through it.

    pit                      open the dashboard
    pit ls [--json]          list sessions, most urgent first
    pit new NAME [-p PROFILE] [-M MODEL] [-d DIR] [-m PROMPT] [-- ARGS...]
    pit go NAME              show a session (NAME may be an index or unique prefix)
    pit send NAME TEXT       type TEXT + Enter (refuses if the agent is blocked)
    pit keys NAME KEY...     send raw tmux keys, e.g. `pit keys x Escape` or `pit keys x 1 Enter`
    pit read NAME [-n 50]    print the bottom of the session's screen
    pit wait NAME [--until done,blocked] [--timeout S]
    pit explain NAME         why pit thinks the session is in its state
    pit ack NAME | pit kill NAME | pit note NAME TEXT
    pit next                 jump to the most urgent session
    pit panel NAME           print a panel (clocks, box, slurm, gpu, jira, wandb) as text
    pit plot FILE [-x COL] [-y A,B] [--tail N] [--logy]   plot CSV/JSONL columns in the terminal
    pit profiles | pit doctor | pit hack
"""
import argparse
import json
import os
import shutil
import sys
import time

from . import __version__, config, sessions, state, tmuxctl
from .sessions import PitError


def _snap(reconcile=True):
    return state.snapshot(config.load_rules(), reconcile=reconcile)


def _public(r, now):
    return {"name": r["name"], "state": r["state"], "kind": r["kind"], "profile": r["profile"],
            "seconds_in_state": int(max(0, now - r["since"])), "cwd": r["cwd"], "command": r["command"],
            "attached": r["attached"] not in ("", "0"), "model": r["model"], "context_pct": r["ctx"],
            "cost_usd": r["cost"], "claude_session_id": r["claude_sid"], "note": r["note"], "reason": r["reason"]}


def cmd_ls(a, cfg):
    rows, now = _snap(), time.time()
    if a.json:
        print(json.dumps([_public(r, now) for r in rows], indent=1))
        return 0
    if not rows:
        print("no sessions. start one with: pit new NAME [-p PROFILE] [-d DIR]")
        return 0
    home = os.path.expanduser("~")
    print("%-3s %-4s %-20s %-8s %-6s %-4s %-7s %s" % ("#", "ST", "NAME", "PROFILE", "AGE", "CTX", "COST", "CWD"))
    for i, r in enumerate(rows, 1):
        print("%-3d %-4s %-20s %-8s %-6s %-4s %-7s %s" % (
            i, state.CODE[r["state"]], r["name"][:20], r["profile"][:8], state.fmt_age(now - r["since"]),
            (r["ctx"] + "%") if r["ctx"] else "-", ("$" + r["cost"]) if r["cost"] else "-",
            r["cwd"].replace(home, "~", 1)))
    return 0


def cmd_new(a, cfg):
    name = sessions.create(cfg, a.name, a.profile, a.dir, a.message or "", a.extra, a.model or "")
    print("started %s" % name)
    if a.go:
        tmuxctl.goto(name)
    return 0


def _name(ref):
    return sessions.resolve(_snap(reconcile=False), ref)


def cmd_go(a, cfg):
    name = _name(a.name)
    tmuxctl.set_opts(name, ack=int(time.time()) + 1)
    return tmuxctl.goto(name) or 0


def cmd_send(a, cfg):
    rows = _snap()
    name = sessions.resolve(rows, a.name)
    row = next(r for r in rows if r["name"] == name)
    if row["state"] == "blocked" and not a.force:
        raise PitError("%s is blocked on a prompt (%s). Answer it with `pit keys`, or pass --force."
                       % (name, row["reason"]))
    err = tmuxctl.send_text(name, " ".join(a.text), enter=not a.no_enter)
    if err:
        raise PitError(err)
    if row["kind"] == "agent" and not a.no_enter:
        # Mark the turn as started now, so `pit send x ... && pit wait x` cannot return on
        # the previous turn's state before the agent's own hook has fired.
        tmuxctl.set_opts(name, state="working", state_ts=int(time.time()), src="pit send")
    return 0


def cmd_keys(a, cfg):
    err = tmuxctl.send_keys(_name(a.name), a.keys)
    if err:
        raise PitError(err)
    return 0


def cmd_read(a, cfg):
    print(tmuxctl.capture(_name(a.name), lines=a.lines))
    return 0


def cmd_wait(a, cfg):
    want = set(a.until.split(","))
    bad = want - set(state.ORDER)
    if bad:
        raise PitError("unknown state(s): %s (have: %s)" % (", ".join(sorted(bad)), ", ".join(state.ORDER)))
    name, deadline = _name(a.name), time.time() + a.timeout if a.timeout else None
    while True:
        # reconcile=True: shell sessions only reach "done" through reconcile, and there
        # may be no dashboard running to do it.
        row = next((r for r in _snap(reconcile=True) if r["name"] == name), None)
        if row is None:
            raise PitError("session '%s' is gone" % name)
        if row["state"] in want:
            print(row["state"])
            return 0
        if deadline and time.time() > deadline:
            print("timeout (state: %s)" % row["state"])
            return 2
        time.sleep(1.0)


def cmd_explain(a, cfg):
    rows = _snap(reconcile=False)
    name = sessions.resolve(rows, a.name)
    r = next(x for x in rows if x["name"] == name)
    print("session   %s (%s, profile %s)" % (name, r["kind"], r["profile"]))
    print("state     %s for %s" % (r["state"], state.fmt_age(time.time() - r["since"])))
    print("because   %s" % r["reason"])
    print("raw       hook_state=%r hook_src=%r hook_ts=%s ack_ts=%s command=%r attached=%s"
          % (r["hook_state"], r["hook_src"], r["hook_ts"], r["ack_ts"], r["command"], r["attached"]))
    if r["kind"] == "agent":
        rules = config.load_rules()
        s, why = state.screen_state(tmuxctl.capture(name), rules.get(r["agent"]) or rules.get("generic", {}))
        print("screen    would say %s (%s)" % (s, why))
    return 0


def cmd_ack(a, cfg):
    tmuxctl.set_opts(_name(a.name), ack=int(time.time()) + 1)
    return 0


def cmd_note(a, cfg):
    tmuxctl.set_opts(_name(a.name), note=" ".join(a.text))
    return 0


def cmd_kill(a, cfg):
    name = _name(a.name)
    tmuxctl.kill_session(name)
    print("killed %s" % name)
    return 0


def cmd_next(a, cfg):
    rows = [r for r in _snap() if r["state"] in ("blocked", "done") and r["attached"] in ("", "0")]
    if not rows:
        tmuxctl.tmux("display-message", "pit: nothing needs attention")
        return 0
    tmuxctl.set_opts(rows[0]["name"], ack=int(time.time()) + 1)
    return tmuxctl.goto(rows[0]["name"]) or 0


def cmd_panel(a, cfg):
    from .panels import load_panels
    cfg = dict(cfg, panels=[a.name])
    print(load_panels(cfg)[0].safe_fetch().to_text())
    return 0


def cmd_plot(a, cfg):
    try:
        from . import plot
        size = tuple(int(v) for v in a.size.lower().split("x")) if a.size else None
        return plot.main(a.file, a.x, a.y.split(",") if a.y else None, a.tail, a.logy, a.title or "", size)
    except ImportError:
        raise PitError("plots need plotext: run `uv sync` in %s" % config.PIT_HOME)
    except (OSError, ValueError) as e:
        raise PitError(str(e))


def cmd_profiles(a, cfg):
    for name, p in sorted(cfg.get("profiles", {}).items()):
        env = " ".join("%s=%s" % (k, v) for k, v in p.get("env", {}).items())
        print("%-10s %-6s %-28s %s" % (name, p.get("kind", ""), p.get("cmd", "") or "(login shell)", env))
    return 0


def cmd_doctor(a, cfg):
    ok = True

    def check(label, good, detail, fatal=True):
        nonlocal ok
        ok = ok and (good or not fatal)
        print("%-4s %-22s %s" % ("ok" if good else ("FAIL" if fatal else "warn"), label, detail))

    v = tmuxctl.version()
    nums = "".join(c if c.isdigit() or c == "." else "" for c in v)
    check("tmux", bool(v) and float(nums or 0) >= 3.2, v or "not found (need 3.2+)")
    check("python", sys.version_info >= (3, 9), sys.version.split()[0])
    try:
        import textual
        check("textual (dashboard)", True, textual.__version__)
    except ImportError:
        check("textual (dashboard)", False, "missing: run `uv sync` in %s" % config.PIT_HOME)
    check("config", "_error" not in cfg, cfg.get("_error", str(config.config_dir() / "config.json")))
    try:
        config.load_rules()
        check("screen rules", True, "rules/agents.json parses")
    except ValueError as e:
        check("screen rules", False, str(e))
    check("claude on PATH", bool(shutil.which("claude")), shutil.which("claude") or "not found", fatal=False)
    check("TERM", True, "%s (inside pit: %s)" % (os.environ.get("TERM", "?"), tmuxctl.inside_pit()), fatal=False)
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo("America/New_York")
        check("tzdata", True, "zoneinfo works")
    except Exception as e:
        check("tzdata", False, "%s (install the tzdata package)" % e)
    print("sessions: %d" % len(_snap(reconcile=False)))
    return 0 if ok else 1


def cmd_hack(a, cfg):
    """Open an agent session inside pit's own source tree."""
    name = sessions.create(cfg, "pit-dev", a.profile, str(config.PIT_HOME),
                           a.message or "Read AGENTS.md, then wait for instructions about changing pit.")
    print("started %s in %s" % (name, config.PIT_HOME))
    return 0


def cmd_ui(a, cfg):
    try:
        from .ui import run
    except ImportError as e:
        print("dashboard needs textual: run `uv sync` in %s (%s)\nUse `pit ls` meanwhile." % (config.PIT_HOME, e))
        return 1
    return run(cfg)


def cmd_home(a, cfg):
    sessions.ensure_home()
    return tmuxctl.goto(tmuxctl.HOME_SESSION) or 0


def build_parser():
    p = argparse.ArgumentParser(prog="pit", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version="pit " + __version__)
    sub = p.add_subparsers(dest="cmd")

    def add(name, fn, *args):
        sp = sub.add_parser(name)
        sp.set_defaults(fn=fn)
        for flags, kw in args:
            sp.add_argument(*flags, **kw)
        return sp

    N = (["name"], {})
    add("ls", cmd_ls, (["--json"], {"action": "store_true"}))
    add("new", cmd_new, N, (["-p", "--profile"], {}), (["-d", "--dir"], {}), (["-m", "--message"], {}),
        (["-M", "--model"], {}), (["-g", "--go"], {"action": "store_true"}), (["extra"], {"nargs": "*"}))
    add("go", cmd_go, N)
    add("send", cmd_send, N, (["text"], {"nargs": "+"}), (["--force"], {"action": "store_true"}),
        (["--no-enter"], {"action": "store_true"}))
    add("keys", cmd_keys, N, (["keys"], {"nargs": "+"}))
    add("read", cmd_read, N, (["-n", "--lines"], {"type": int, "default": 50}))
    add("wait", cmd_wait, N, (["--until"], {"default": "done,blocked,idle,exited"}),
        (["--timeout"], {"type": float, "default": 0}))
    add("explain", cmd_explain, N)
    add("ack", cmd_ack, N)
    add("note", cmd_note, N, (["text"], {"nargs": "*"}))
    add("kill", cmd_kill, N)
    add("next", cmd_next)
    add("panel", cmd_panel, N)
    add("plot", cmd_plot, (["file"], {"help": "CSV or JSONL file, or - for stdin"}), (["-x"], {}),
        (["-y"], {"help": "comma-separated columns (default: numeric ones)"}), (["--tail"], {"type": int, "default": 0}),
        (["--logy"], {"action": "store_true"}), (["--title"], {}), (["--size"], {"help": "WxH in characters"}))
    add("profiles", cmd_profiles)
    add("doctor", cmd_doctor)
    add("hack", cmd_hack, (["-p", "--profile"], {}), (["-m", "--message"], {}))
    add("ui", cmd_ui)
    add("home", cmd_home)
    return p


def main(argv=None):
    a = build_parser().parse_args(argv)
    cfg = config.load()
    try:
        return (a.fn if a.cmd else cmd_home)(a, cfg)
    except PitError as e:
        sys.stderr.write("pit: %s\n" % e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
