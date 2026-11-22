# pit: guide for agents changing this tool

pit is a small terminal for running many Claude Code and shell sessions on a research
box, with a dashboard and data panels (market clocks, Slurm, GPUs, Jira, W&B). It is
meant to be edited in place by agents. Keep it small: prefer deleting to adding.

## How it works (read this before editing)

- pit owns a private tmux server: `tmux -L pit`, config `conf/tmux.conf`. The user's
  own tmux is never touched. Every managed session is one tmux session.
- tmux is the only store. Session metadata lives in tmux user options (`@pit_kind`,
  `@pit_state`, `@pit_state_ts`, `@pit_ack`, `@pit_model`, ...). There is no daemon and
  no database. Killing the dashboard loses nothing.
- Claude Code tells pit its state through hooks. pit writes a per-session settings
  file and starts `claude --settings <file>`; each hook runs `pit/hook.py`, which sets
  `@pit_state`. The user's `~/.claude/settings.json` is never modified.
- `pit/state.py` turns raw facts into a state and a reason. Trust order: hook, then
  screen rules (`rules/agents.json`), then the pane's foreground process.
- The CLI (`pit/cli.py`) is the API. The dashboard's command line runs the same parser.

## Map

| File | Job |
|---|---|
| `bin/pit` | launcher: picks `.venv` python, runs `pit.cli` |
| `pit/tmuxctl.py` | every tmux call; one `list-panes` call reads all sessions |
| `pit/state.py` | state derivation, attention ordering, `done` vs `idle` |
| `pit/hook.py` | Claude Code hook + statusline entry point |
| `pit/sessions.py` | profiles to command lines, session creation |
| `pit/cli.py` | commands |
| `pit/ui.py` | Textual dashboard (only file allowed third-party imports) |
| `pit/clocks.py` | market session math |
| `pit/plot.py` | `pit plot`: CSV/JSONL columns as terminal charts (plotext 5.x, imported lazily) |
| `pit/panels/*.py` | one data panel per file; contract in `pit/panels/__init__.py` |
| `conf/defaults.json` | default config; user overrides in `~/.config/pit/config.json` |
| `rules/agents.json` | screen-detection regexes, as data |
| `docs/INSTALL.md`, `docs/ONBOARDING.md` | install on a box; wire Slurm/Jira/W&B |

## Hard constraints

1. Python 3.9 only (Rocky 9 system python). No `match`, no `X | Y` type syntax at
   runtime, no `tomllib`, no parenthesised multi-item `with`, no `zip(strict=)`.
2. Everything except `pit/ui.py` (textual) and `pit/plot.py` (plotext) is stdlib only. `pit/hook.py` runs on every Claude
   tool call: keep its imports minimal, never print on the event path, never exit non-zero.
3. tmux 3.2a only. Not available there: `display-popup -T/-b/-B/-e`, popup styles,
   `pane-border-indicators`, `allow-passthrough`, `remain-on-exit-format`,
   `pane_dead_time`, `pane_dead_signal`, `capture-pane -T`, `extended-keys-format`,
   hooks `client-active` and `window-resized`.
4. tmux targets: sessions are addressed as `=name` (exact match), but `set-option`
   and `capture-pane` need `=name:` with the trailing colon. Use `tmuxctl.target()`.
5. Keys must survive Windows Terminal, which intercepts: F11, alt+enter, alt+arrows,
   alt+shift+*, ctrl+shift+*, ctrl+tab, ctrl+alt+digits, ctrl+plus/minus/0, ctrl+v.
   Do not depend on the mouse, OSC 52 reads, or synchronized output.
6. Never run `tmux kill-server` on the pit socket, and never touch the default socket.
7. Secrets come from environment variables named in config. Never write one to a file.
8. Style: no emojis, no em dashes.

## Common changes

- New panel: add `pit/panels/<name>.py` (or `~/.config/pit/panels/<name>.py`) with a
  `Panel` subclass and `PANEL = ThatClass`, add the name to `"panels"` in config.
  Check with `bin/pit panel <name>`.
- New profile or model: add to `"profiles"` in `~/.config/pit/config.json`.
- State is wrong: run `bin/pit explain <session>`. It prints the state, the reason,
  the raw hook values and what the screen rules would say. Fix regexes in
  `rules/agents.json`; fix event mapping in `EVENT_STATE` in `pit/hook.py`.
- New market or changed hours: `clocks.venues` in config. Add a test with a fixed UTC time.

## Verify every change

```sh
uv run pytest -q                       # or: .venv/bin/python -m pytest -q
/usr/bin/python3 -m py_compile pit/*.py pit/panels/*.py   # must pass on python 3.9
PIT_SOCKET=pit-test bin/pit doctor     # PIT_SOCKET gives you a throwaway tmux server
```

Test against a throwaway server (`PIT_SOCKET=pit-test`, plus `PIT_STATE_DIR` and
`PIT_CONFIG_DIR` pointing at temp dirs), never the live `pit` socket. Clean up with
`tmux -L pit-test kill-server`. After editing `pit/ui.py`, press `R` in the dashboard
to restart it; sessions are unaffected.

## Known gaps

- Developed on macOS with tmux 3.5a. tmux 3.2a and Windows Terminal are untested.
- The Slurm, GPU, Jira and W&B panels are tested against canned output only.
- The `gateway` profile is an untested template.
- `done` for shell sessions is stamped when pit next polls, not when the command ended.
- The pit tmux server keeps the environment it started with. A token exported later
  (Jira, W&B, gateway) is invisible to the dashboard and to new sessions until that
  server is restarted: detach, end the sessions you can, then `tmux -L pit kill-server`
  only with the user's agreement.
- An agent turn interrupted with Esc fires no Stop hook; pit notices from the screen
  after 15 seconds (`WORKING_TRUST` in `pit/state.py`).
- Planned, not built: q/kdb+ support (a q REPL session kind, `pit q` for query to
  table or plot through the `q` binary, a panel of kdb processes).
