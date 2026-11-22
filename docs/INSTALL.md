# Installing pit on a box

Audience: a Claude Code session running on the target box (Rocky/CentOS 9, zsh, tmux).
Work through the steps in order and stop at the first failure. Do not use sudo: if
something needs it, report what is missing and stop.

This tool was developed on macOS with tmux 3.5a and has never run on this OS, on
tmux 3.2a, or under Windows Terminal. Part of your job is to find what breaks. Read
`AGENTS.md` first: it lists the constraints you must keep when fixing things.

## 1. Check prerequisites

```sh
cat /etc/os-release | head -2
tmux -V            # need 3.2 or newer (Rocky 9 ships 3.2a)
python3 -V         # need 3.9 or newer
zsh --version
command -v uv claude git
python3 -c "import curses, zoneinfo; zoneinfo.ZoneInfo('America/New_York'); print('ok')"
```

If `zoneinfo` fails, the `tzdata` package is missing: report it (needs an admin).

## 2. Put the code in place

The repo is a plain directory with no remote. Use whatever copy the user provided
(scp, rsync, or git clone if they have pushed it). Recommended location: `~/pit`.
Do not copy a `.venv` from another machine.

## 3. Install the one dependency

The dashboard needs `textual`. Everything else is stdlib.

```sh
cd ~/pit
uv sync                      # preferred; creates .venv on python >= 3.9
```

If uv is missing or PyPI is unreachable, try the internal mirror the box normally
uses, or:

```sh
python3 -m venv .venv && .venv/bin/pip install "textual>=0.80"
```

If neither works, pit still runs without the dashboard: `pit ls`, `pit new`,
`pit go`, `pit send` and the rest need no dependencies. Report it and continue.

## 4. Put `pit` on PATH

```sh
mkdir -p ~/.local/bin && ln -sf ~/pit/bin/pit ~/.local/bin/pit
command -v pit || echo 'add ~/.local/bin to PATH in ~/.zshenv'
```

## 5. Verify, using a throwaway tmux server

```sh
cd ~/pit
.venv/bin/python -m pytest -q                              # all tests must pass
/usr/bin/python3 -m py_compile pit/*.py pit/panels/*.py     # python 3.9 syntax check
export PIT_SOCKET=pit-test PIT_STATE_DIR=$(mktemp -d)
pit doctor                                                 # every line ok or warn
pit new t1 -p shell -d /tmp && sleep 1 && pit send t1 "sleep 3; echo hi"
sleep 1; pit ls            # t1 should be WORK
sleep 4; pit ls            # t1 should be DONE
pit explain t1; pit read t1 -n 5; pit kill t1
```

If `pit new` or `pit ls` fails here, the likely cause is a tmux 3.2a difference.
Run the failing tmux command by hand (`tmux -L pit-test ...`, see `pit/tmuxctl.py`),
fix it within the constraints in `AGENTS.md`, and add a note to "Known gaps".

Then check a real agent session (this spends a few tokens):

```sh
pit new c1 -p claude -d /tmp -m "Reply with the single word: pong"
sleep 8; pit ls; pit explain c1
```

Expect BLKD first if Claude Code shows its folder-trust prompt (answer it with
`pit keys c1 Down Enter` after reading the screen with `pit read c1`), then DONE with
a reason starting `hook:`. A reason starting `no hook signal` means the hooks did not
load: check `claude --version` supports `--settings`, and inspect
`$PIT_STATE_DIR/sessions/c1.settings.json`.

Clean up: `tmux -L pit-test kill-server; unset PIT_SOCKET PIT_STATE_DIR`.

## 6. Things only the human can check (ask them, do not guess)

The user connects from Windows Terminal on Windows 10 over ssh. Ask them to run
`pit`, then confirm each of these and tell you the result:

1. The dashboard draws without garbage, in amber on black, and the table lines up.
2. Up/down and Enter open a session; F12 returns to the dashboard; F10 and F9 work.
3. `q` detaches and `pit` re-attaches with sessions still running.
4. Colours look right. If not, compare `echo $TERM` outside and inside pit; the
   config assumes the outer TERM is `xterm-256color`.
5. A session finishing rings the bell. Windows Terminal only flashes the taskbar if
   the profile's `bellStyle` includes `taskbar`.
6. Resizing the window redraws cleanly.

Fix what they report, re-run the tests, and record anything unresolved in
`AGENTS.md` under "Known gaps".

## 7. Models and the gateway

Claude Code is the only harness; other models are reached through the gateway.
Ask the user for the gateway URL and how the token is provided, then have them
export `PIT_GATEWAY_URL` and `PIT_GATEWAY_TOKEN` in `~/.zshenv` (never write the
token into a file in this repo or into config). Test with:

```sh
pit new g1 -p gateway -M <a model id the gateway serves> -m "Reply: pong"
```

For models used often, add named profiles to `~/.config/pit/config.json`:

```json
{"profiles": {"kimi": {"kind": "agent", "agent": "claude", "cmd": "claude --model <id>",
  "env": {"ANTHROPIC_BASE_URL": "$PIT_GATEWAY_URL", "ANTHROPIC_AUTH_TOKEN": "$PIT_GATEWAY_TOKEN"}}}}
```

## 8. Integrations

Continue with `docs/ONBOARDING.md` for Slurm, GPUs, Jira and W&B.

## 9. Report back

Tell the user: what passed, what you changed and why, and what is still untested.
