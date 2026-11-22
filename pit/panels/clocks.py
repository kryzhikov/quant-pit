"""Market clocks panel: every configured venue with its session state."""
from .. import clocks
from . import Panel, Table

LABEL_STYLE = {"open": "ok", "pre": "warn", "post": "warn", "night": "ok", "closed": "dim"}


class ClocksPanel(Panel):
    name = "clocks"
    title = "CLOCKS"
    interval = 15

    def fetch(self):
        rows = []
        for s in clocks.all_status(self.cfg):
            style = LABEL_STYLE.get(s["label"], "ok")
            rows.append([(s["code"], "hot"), s["name"], s["local"], (s["label"].upper(), style),
                         clocks.fmt_span(s["seconds"]),
                         ("-" if s["seconds"] is None else s["next_label"].upper(), "dim")])
        return Table(["VENUE", "MARKET", "LOCAL", "STATE", "CHANGES IN", "THEN"], rows,
                     note="No holiday calendars built in: see clocks.venues[].holidays", ok=True)


PANEL = ClocksPanel
