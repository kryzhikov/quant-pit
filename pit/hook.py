#!/usr/bin/env python3
"""Claude Code hook and statusline entry point. Stdlib only, must stay fast and silent.

Claude Code runs this (wired up per session through `claude --settings <file>`, so
~/.claude/settings.json is never modified) with the event JSON on stdin:

    python3 hook.py event <pit-session>        # hook events -> @pit_state
    python3 hook.py statusline <pit-session>   # model / context / cost -> @pit_*

It must never print on the event path or exit non-zero: a hook that prints or fails
can change what Claude does.
"""
import json
import os
import subprocess
import sys
import time

# event -> state written to @pit_state ("" = record nothing)
EVENT_STATE = {
    "SessionStart": "idle",
    "UserPromptSubmit": "working",
    "PreToolUse": "working",
    "PostToolUse": "working",
    "PreCompact": "working",
    "Stop": "stop",
    "SessionEnd": "stop",
}
BLOCKING_NOTIFICATIONS = ("permission_prompt", "elicitation_dialog", "agent_needs_input")


def tmux_set(session, opts):
    args = ["tmux", "-L", os.environ.get("PIT_SOCKET", "pit")]
    # Prefer the pane we run in: it stays correct if the session is renamed.
    target = os.environ.get("TMUX_PANE") or ("=" + session + ":")
    first = True
    for k, v in opts.items():
        if not first:
            args.append(";")
        first = False
        args += ["set-option", "-t", target, "@pit_" + k, str(v)]
    env = dict(os.environ)
    env.pop("TMUX", None)
    subprocess.run(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env, timeout=3)


def on_event(session, data):
    event = data.get("hook_event_name", "")
    state, src = EVENT_STATE.get(event, ""), event
    if event == "Notification":
        ntype = data.get("notification_type", "")
        src = "Notification(%s)" % ntype
        msg = (data.get("message") or "").lower()
        if ntype in BLOCKING_NOTIFICATIONS or (not ntype and "permission" in msg):
            state = "blocked"
    if event in ("PreToolUse", "PostToolUse"):
        src = "%s(%s)" % (event, data.get("tool_name", ""))
    if not state:
        return
    opts = {"state": state, "state_ts": int(time.time()), "src": src}
    if data.get("session_id"):
        opts["sid"] = data["session_id"]
    tmux_set(session, opts)


def on_statusline(session, data, raw):
    model = (data.get("model") or {}).get("display_name") or (data.get("model") or {}).get("id") or ""
    ctx = (data.get("context_window") or {}).get("used_percentage")
    cost = (data.get("cost") or {}).get("total_cost_usd")
    opts = {}
    if model:
        opts["model"] = model
    if ctx is not None:
        opts["ctx"] = "%d" % round(float(ctx))
    if cost is not None:
        opts["cost"] = "%.2f" % float(cost)
    if opts:
        tmux_set(session, opts)
    # Keep the user's own statusline if they have one: run it with the same input.
    user_cmd = os.environ.get("PIT_USER_STATUSLINE", "")
    if user_cmd:
        p = subprocess.run(user_cmd, shell=True, input=raw, stdout=subprocess.PIPE,
                           stderr=subprocess.DEVNULL, timeout=5)
        sys.stdout.write(p.stdout.decode("utf-8", "replace"))
    else:
        bits = [b for b in (model, "ctx %s%%" % opts["ctx"] if "ctx" in opts else "",
                            "$%s" % opts["cost"] if "cost" in opts else "") if b]
        sys.stdout.write("pit:%s  %s" % (session, "  ".join(bits)))


def main(argv):
    if len(argv) < 3:
        return 0
    mode, session = argv[1], argv[2]
    try:
        raw = sys.stdin.buffer.read()
        data = json.loads(raw.decode("utf-8", "replace") or "{}")
        if mode == "event":
            on_event(session, data)
        elif mode == "statusline":
            on_statusline(session, data, raw)
    except Exception:  # never let a pit bug disturb the agent
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
