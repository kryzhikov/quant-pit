"""Plot input handling, sparklines and the box panel's pure parts."""
import io

import pytest

from pit import config, plot
from pit.panels import load_panels, spark
from pit.panels.box import level, parse_meminfo

CSV = "step,loss,tag\n0,2.0,a\n1,1.0,b\n2,nan,c\n3,0.5,d\n"


def test_read_csv_and_pick_numeric_columns():
    x, series = plot.select_series(plot.read_records(CSV), x="step")
    assert x == "step" and [n for n, _ in series] == ["loss"]
    assert series[0][1] == [(0.0, 2.0), (1.0, 1.0), (3.0, 0.5)]   # the nan row is skipped


def test_jsonl_tail_and_row_index():
    recs = plot.read_records('{"a": 1}\nnot json\n{"a": 2, "b": "x"}\n{"a": 3}\n')
    x, series = plot.select_series(recs, tail=2)
    assert x == "row" and series == [("a", [(0, 2.0), (1, 3.0)])]


def test_tab_separated():
    assert plot.read_records("t\tv\n1\t2\n")[0] == {"t": "1", "v": "2"}


@pytest.mark.parametrize("kw", [{"x": "nope"}, {"ys": ["nope"]}, {"ys": ["tag"]}, {"x": "tag"}])
def test_bad_columns_raise_value_error(kw):
    with pytest.raises(ValueError):
        plot.select_series(plot.read_records(CSV), **kw)


def test_empty_input_raises():
    with pytest.raises(ValueError):
        plot.select_series(plot.read_records(""))


def test_render_is_plain_text_when_not_a_terminal(tmp_path):
    f = tmp_path / "run.csv"
    f.write_text(CSV)
    out = io.StringIO()
    assert plot.main(str(f), x="step", size=(60, 12), out=out) == 0
    text = out.getvalue()
    assert "\x1b" not in text and "loss" in text and "last: loss=0.5" in text
    assert max(len(line) for line in text.splitlines()) <= 62


def test_spark():
    assert spark([]) == ""
    assert spark([0, 50, 100], lo=0, hi=100) == "▁▅█"
    assert spark([5, 5, 5]) == "▁▁▁"          # flat data does not divide by zero
    assert spark([0, None, 100], lo=0, hi=100)[1] == " "
    assert spark([250], lo=0, hi=100) == "█"              # clamps out-of-range values


def test_meminfo_and_levels():
    text = "MemTotal:       263856000 kB\nMemFree:  1000 kB\nMemAvailable:   131928000 kB\n"
    assert parse_meminfo(text) == (263856000 * 1024, 131928000 * 1024)
    assert parse_meminfo("MemTotal: 5 kB\n") is None
    assert [level(p) for p in (10, 85, 95)] == ["ok", "warn", "bad"]


def test_box_panel_runs_here():
    cfg = dict(config.load(), panels=["box"])
    cfg["panel"] = dict(cfg["panel"], box={"paths": ["/", "/definitely/not/here"]})
    t = load_panels(cfg)[0].safe_fetch()
    kinds = [r[0][0] for r in t.rows]
    assert kinds[:2] == ["LOAD", "MEM"] and kinds.count("DISK") == 2
    assert t.rows[-1][2] == ("missing", "bad")
