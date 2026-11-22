# quant-pit

The command is `pit`.

A minimal terminal for herding Claude Code and shell sessions on a research box:
one dashboard that shows which session needs you, a live preview, market clocks, and
panels for Slurm, GPUs, Jira and W&B, plus terminal plots. tmux does the terminal
work; pit is a small Python layer around it.

```
pit                                  open the dashboard
pit new lob -p opus -d ~/research/lob -m "profile the queue model"
pit new sweep -p shell -d ~/runs
pit new x -p gateway -M <model-id>   Claude Code against your gateway, any model
pit ls                               sessions, most urgent first
pit send lob "run the tests"         type into a session
pit wait lob --until done,blocked    block until it needs you
pit read lob -n 40                   read its screen
pit explain lob                      why pit thinks it is in that state
pit panel slurm                      print a panel as text
pit plot run.csv -x step -y loss     plot CSV/JSONL columns in the terminal
pit hack                             open an agent inside pit's own source
```

Inside a session: F12 returns to the dashboard, F10 jumps to the next session that
needs attention, F9 to the previous one. The tmux prefix inside pit is C-g.

States: BLKD (waiting on your answer), DONE (finished, not yet seen), WORK, INIT,
IDLE, EXIT. Opening a session marks it seen.

- Install on a box: `docs/INSTALL.md` (written for a Claude Code session to follow).
- Wire Slurm, Jira, W&B: `docs/ONBOARDING.md`.
- Change pit itself: `AGENTS.md`.

Requires tmux 3.2+ and Python 3.9+. `uv sync` installs the two optional
dependencies: `textual` (dashboard) and `plotext` (plots).
