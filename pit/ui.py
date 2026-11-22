"""The dashboard (Textual). The only module allowed to import third-party code.

Layout, top to bottom:
  header    host, market clock chips, UTC time
  main      sessions table (left) and a live preview of the selected session (right)
  panels    one tab per configured panel (clocks, slurm, gpu, jira, wandb, ...)
  command   a command line that accepts exactly the `pit` CLI grammar
  keys      key legend

It holds no state of its own: every poll re-reads tmux, so it can be killed and
restarted (key R, after editing this file) without affecting any session.
Keys avoid everything Windows Terminal intercepts (F11, alt+enter, ctrl+shift+*).
"""
import contextlib
import io
import os
import shlex
import socket
import time
from datetime import datetime, timezone

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import DataTable, Input, Static

from . import clocks, config, state, tmuxctl
from .panels import load_panels

AMBER, DIM, RED, GREEN, CYAN, WHITE = "#ffaf00", "#8a8a8a", "#ff5f5f", "#5fd75f", "#5fd7ff", "#e4e4e4"
STATE_STYLE = {"blocked": "bold #000000 on " + RED, "done": "bold #000000 on " + GREEN, "working": "bold " + AMBER,
               "starting": CYAN, "idle": DIM, "exited": DIM}
CELL_STYLE = {"ok": GREEN, "warn": AMBER, "bad": RED, "dim": DIM, "hot": "bold " + WHITE}
CLOCK_STYLE = {"open": GREEN, "night": GREEN, "pre": AMBER, "post": AMBER, "closed": DIM}

HELP = """\
KEYS
  up/down or j/k   select session          enter   open selected session
  1-9              open session by number  a       open the most urgent session
  n                new session             x       kill selected (asks to confirm)
  :  or  /         command line            esc     back to the session list
  tab or ] / [     next / previous panel   r       refresh now
  R                restart the dashboard (after editing pit's code)
  q                detach (sessions keep running)

INSIDE A SESSION
  F12  back to this dashboard    F10  next session needing attention    F9  previous session
  C-g  is the tmux prefix here (C-g d detaches), so C-b still belongs to your own tmux

COMMAND LINE: same grammar as the `pit` CLI, for example
  new lob-fill -p opus -d ~/research/lob -m "profile the queue model"
  new sweep -p shell -d ~/runs        send lob-fill run the tests
  explain lob-fill                    note lob-fill waiting on data
  panel slurm                         profiles        doctor

STATES  BLKD needs your answer   DONE finished, unseen   WORK busy   INIT starting   IDLE   EXIT agent ended
"""


class PitApp(App):
    CSS = """
    Screen { background: #000000; color: %(amber)s; }
    #header { height: 1; background: #1c1c1c; }
    #main { height: 1fr; }
    #sessions { width: 3fr; min-width: 50; }
    #side { width: 2fr; border-left: solid #3a3a3a; }
    #preview_title, #panel_tabs { height: 1; background: #1c1c1c; color: %(white)s; }
    #preview { height: 1fr; color: %(white)s; }
    #panelbox { height: 14; border-top: solid #3a3a3a; }
    #panel_note { height: auto; max-height: 4; color: %(dim)s; }
    #status { height: 1; color: %(white)s; }
    #cmd { height: 1; border: none; background: #121212; color: %(white)s; padding: 0 1; }
    #keys { height: 1; background: #1c1c1c; color: %(dim)s; }
    DataTable { background: #000000; color: %(amber)s; overflow-x: hidden; scrollbar-size-horizontal: 0; }
    DataTable > .datatable--header { background: #000000; color: %(dim)s; text-style: none; }
    DataTable > .datatable--cursor { background: #303030; color: %(white)s; }
    """ % {"amber": AMBER, "white": WHITE, "dim": DIM}

    def __init__(self, cfg):
        super().__init__()
        self.cfg = cfg
        self.rules = config.load_rules()
        self.rows = []            # last snapshot
        self.selected = None      # selected session name
        self.prev_states = {}     # name -> state at the previous poll, for the bell
        self.output = None        # text shown in place of the preview (command output, help)
        self.panels = load_panels(cfg)
        self.panel_idx = 0
        self.panel_cache = {}     # panel name -> (fetched_at, Table)
        self.panel_busy = set()

    # ---- layout -----------------------------------------------------------
    def compose(self) -> ComposeResult:
        yield Static(id="header")
        with Horizontal(id="main"):
            yield DataTable(id="sessions", cursor_type="row", zebra_stripes=False)
            with Vertical(id="side"):
                yield Static(id="preview_title")
                yield Static(id="preview")
        with Vertical(id="panelbox"):
            yield Static(id="panel_tabs")
            yield DataTable(id="panel", cursor_type="none", zebra_stripes=False)
            yield Static(id="panel_note")
        yield Static(id="status")
        yield Input(id="cmd", placeholder="command  (? for help)")
        yield Static(" enter open  n new  x kill  a next alert  : command  tab panels  ? help  R restart  q detach", id="keys")

    def on_mount(self):
        t = self.query_one("#sessions", DataTable)
        for label, width in (("#", 2), ("ST", 4), ("NAME", 18), ("PROFILE", 8), ("MODEL", 12),
                             ("AGE", 6), ("CTX", 4), ("COST", 7), ("WHERE / NOTE", 40)):
            t.add_column(label, width=width, key=label)
        t.focus()
        self.set_interval(float(self.cfg.get("ui", {}).get("poll_seconds", 1.0)), self.poll)
        self.poll()
        self.draw_panel_tabs()

    # ---- polling ----------------------------------------------------------
    @work(thread=True, exclusive=True, group="poll")
    def poll(self):
        rows = state.snapshot(self.rules)
        names = [r["name"] for r in rows]
        sel = self.selected if self.selected in names else (names[0] if names else None)
        lines = int(self.cfg.get("ui", {}).get("preview_lines", 80))
        shot = tmuxctl.capture(sel, lines=lines, ansi=True) if sel and self.output is None else ""
        self.call_from_thread(self.apply, rows, sel, shot)
        self.refresh_panel()

    def apply(self, rows, sel, shot):
        now = time.time()
        self.rows, self.selected = rows, sel
        t = self.query_one("#sessions", DataTable)
        home = os.path.expanduser("~")
        # Rebuilding the table moves the cursor, which fires RowHighlighted; without
        # prevent() the handler would take that for a user move and re-poll in a loop.
        with t.prevent(DataTable.RowHighlighted):
            self._fill(t, rows, sel, now, home)
        alert = [r["name"] for r in rows if r["state"] in ("blocked", "done") and self.prev_states.get(r["name"]) != r["state"]]
        if alert and self.prev_states and self.cfg.get("ui", {}).get("bell", True):
            self.bell()
        self.prev_states = dict((r["name"], r["state"]) for r in rows)
        self.draw_header(rows)
        self.draw_preview(shot)

    def _fill(self, t, rows, sel, now, home):
        t.clear()
        for i, r in enumerate(rows, 1):
            where = r["note"] or r["cwd"].replace(home, "~", 1)
            if len(where) > 40:
                where = ".." + where[-38:]
            t.add_row(str(i), Text(state.CODE[r["state"]], style=STATE_STYLE[r["state"]]), r["name"],
                      r["profile"], Text(r["model"], style=DIM), state.fmt_age(now - r["since"]),
                      (r["ctx"] + "%") if r["ctx"] else "", ("$" + r["cost"]) if r["cost"] else "",
                      Text(where, style=WHITE if r["note"] else DIM), key=r["name"])
        if sel:
            t.move_cursor(row=[r["name"] for r in rows].index(sel), animate=False)

    def draw_header(self, rows):
        head = Text()  # (a style passed to Text() would apply to the whole line)
        head.append(" PIT ", style="bold #000000 on " + AMBER)
        head.append(" %s " % socket.gethostname().split(".")[0], style="bold " + WHITE)
        counts = {}
        for r in rows:
            counts[r["state"]] = counts.get(r["state"], 0) + 1
        for st in state.ORDER:
            if counts.get(st):
                head.append(" %d %s " % (counts[st], state.CODE[st]), style=STATE_STYLE[st])
        head.append("  ")
        venues = dict((v["code"], v) for v in self.cfg.get("clocks", {}).get("venues", []))
        for code in self.cfg.get("clocks", {}).get("bar", []):
            if code in venues:
                s = clocks.status(venues[code])
                head.append(" %s " % code, style=WHITE)
                head.append("%s %s " % (s["label"].upper(), clocks.fmt_span(s["seconds"])), style=CLOCK_STYLE.get(s["label"], GREEN))
        head.append("  UTC " + datetime.now(timezone.utc).strftime("%H:%M:%S"), style=DIM)
        self.query_one("#header", Static).update(head)

    def draw_preview(self, shot):
        title, body = self.query_one("#preview_title", Static), self.query_one("#preview", Static)
        if self.output is not None:
            title.update(" OUTPUT  (esc to close)")
            body.update(Text(self.output, style=WHITE))
            return
        row = next((r for r in self.rows if r["name"] == self.selected), None)
        if not row:
            title.update(" NO SESSIONS")
            body.update(Text("Press n to start one, or ? for help.", style=DIM))
            return
        title.update(Text().append(" %s  " % row["name"], style="bold " + WHITE).append(row["reason"], style=DIM))
        lines = []
        for ln in shot.rstrip().splitlines():  # squeeze blank runs so the tail shows content
            if ln.strip() or (lines and lines[-1].strip()):
                lines.append(ln)
        height = max(1, body.size.height)
        text = Text.from_ansi("\n".join(lines[-height:]))
        text.no_wrap, text.overflow = True, "crop"
        body.update(text)

    # ---- panels -----------------------------------------------------------
    def draw_panel_tabs(self):
        tabs = Text(" ")
        for i, p in enumerate(self.panels):
            tabs.append(" %s " % p.title, style=("bold #000000 on " + AMBER) if i == self.panel_idx else DIM)
            tabs.append(" ")
        self.query_one("#panel_tabs", Static).update(tabs)

    def refresh_panel(self, force=False):
        """Called from the poll thread: fetch the visible panel when its data is stale."""
        if not self.panels:
            return
        p = self.panels[self.panel_idx]
        at = self.panel_cache.get(p.name, (0, None))[0]
        if p.name in self.panel_busy or (not force and time.time() - at < float(p.interval)):
            return
        self.panel_busy.add(p.name)
        try:
            table = p.safe_fetch()
        finally:
            self.panel_busy.discard(p.name)
        self.panel_cache[p.name] = (time.time(), table)
        self.call_from_thread(self.draw_panel)

    def draw_panel(self):
        if not self.panels:
            return
        p = self.panels[self.panel_idx]
        dt, note = self.query_one("#panel", DataTable), self.query_one("#panel_note", Static)
        table = self.panel_cache.get(p.name, (0, None))[1]
        dt.clear(columns=True)
        if table is None:
            note.update("loading %s ..." % p.title)
            return
        for c in table.columns:
            dt.add_column(c)
        for r in table.rows:
            dt.add_row(*[Text(c[0], style=CELL_STYLE.get(c[1], "")) if isinstance(c, (tuple, list)) else str(c) for c in r])
        note.update(Text(table.note, style=DIM if table.ok else AMBER))

    def switch_panel(self, step):
        if self.panels:
            self.panel_idx = (self.panel_idx + step) % len(self.panels)
            self.draw_panel_tabs()
            self.draw_panel()
            self.poll()

    # ---- actions ----------------------------------------------------------
    def say(self, msg, bad=False):
        self.query_one("#status", Static).update(Text(" " + msg, style=RED if bad else WHITE))

    def open_session(self, name):
        tmuxctl.set_opts(name, ack=int(time.time()) + 1)
        if tmuxctl.inside_pit():
            tmuxctl.goto(name)
        else:
            with self.suspend():
                tmuxctl.goto(name, replace_process=False)
        self.poll()

    def run_command(self, line):
        """Run one line of `pit` CLI grammar inside the dashboard."""
        from . import cli
        try:
            argv = shlex.split(line)
        except ValueError as e:
            return self.say(str(e), bad=True)
        if not argv:
            return
        if argv[0] in ("?", "help"):
            self.output = HELP
            return self.draw_preview("")
        if argv[0] in ("q", "quit"):
            return self.action_detach()
        if argv[0] in ("ui", "home"):
            return self.say("already here")
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                a = cli.build_parser().parse_args(argv)
                # Commands that block or replace the process must not run on the UI thread.
                if a.cmd == "wait":
                    return self.say("wait blocks: run `pit wait` from a shell session instead", bad=True)
                if a.cmd == "next":
                    return self.open_urgent()
                if a.cmd == "go":
                    name = cli._name(a.name)
                else:
                    go_after = a.cmd == "new" and a.go
                    if go_after:
                        a.go = False
                    name, _ = None, a.fn(a, self.cfg)
                    if go_after:
                        name = tmuxctl.sanitize(a.name)
        except SystemExit:
            return self.say((buf.getvalue().strip().splitlines() or ["bad command"])[-1], bad=True)
        except cli.PitError as e:
            return self.say(str(e), bad=True)
        if name:
            return self.open_session(name)
        out = buf.getvalue().rstrip()
        if out.count("\n") >= 1:
            self.output = out
            self.draw_preview("")
            self.say("")
        else:
            self.say(out or "ok")
        self.poll()

    def on_input_submitted(self, event):
        line = event.value.strip()
        event.input.value = ""
        self.query_one("#sessions", DataTable).focus()
        self.run_command(line)

    def on_data_table_row_highlighted(self, event):
        if event.data_table.id == "sessions" and event.row_key is not None and event.row_key.value != self.selected:
            self.selected = event.row_key.value
            self.poll()

    def on_data_table_row_selected(self, event):
        if event.data_table.id == "sessions" and event.row_key is not None:
            self.open_session(event.row_key.value)

    def open_urgent(self):
        urgent = [r["name"] for r in self.rows if r["state"] in ("blocked", "done")]
        self.open_session(urgent[0]) if urgent else self.say("nothing needs attention")

    def prefill(self, text):
        box = self.query_one("#cmd", Input)
        box.value = text
        box.focus()
        box.cursor_position = len(text)

    def action_detach(self):
        if tmuxctl.inside_pit():
            tmuxctl.goto_detach()
        else:
            self.exit(return_code=0)

    def on_key(self, event):
        cmd = self.query_one("#cmd", Input)
        if event.key == "escape":
            self.output = None
            cmd.value = ""
            self.query_one("#sessions", DataTable).focus()
            self.poll()
            return event.stop()
        if cmd.has_focus:
            return
        k, c = event.key, event.character or ""
        handled = True
        if c in (":", "/"):
            self.prefill("")
        elif c == "?":
            self.run_command("?")
        elif c == "n":
            self.prefill("new ")
        elif c == "x" and self.selected:
            self.prefill("kill %s" % self.selected)
        elif c == "a":
            self.open_urgent()
        elif c in ("j", "k"):
            t = self.query_one("#sessions", DataTable)
            t.action_cursor_down() if c == "j" else t.action_cursor_up()
        elif c.isdigit() and c != "0" and int(c) <= len(self.rows):
            self.open_session(self.rows[int(c) - 1]["name"])
        elif k == "tab" or c == "]":
            self.switch_panel(1)
        elif k == "shift+tab" or c == "[":
            self.switch_panel(-1)
        elif c == "r":
            self.panel_cache.clear()
            self.rules = config.load_rules()
            self.poll()
            self.say("refreshed")
        elif c == "R":
            self.exit(return_code=75)
        elif c == "q":
            self.action_detach()
        else:
            handled = False
        if handled:
            event.stop()
            event.prevent_default()


def run(cfg):
    app = PitApp(cfg)
    app.run()
    return app.return_code or 0
