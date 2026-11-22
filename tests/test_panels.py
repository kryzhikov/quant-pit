"""Panel tests: parsing of canned tool output / API JSON, and the not-available paths.

No network and no real squeue / nvidia-smi: I/O is replaced with canned data.
"""
import io
import json
import os
import sys
from urllib.error import HTTPError, URLError

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pit.panels as panels_pkg  # noqa: E402
from pit.panels import Table, gpu, jira, slurm, wandb  # noqa: E402

NOW = 1795348800  # 2026-11-22T12:00:00Z


def cfg(name, **over):
    base = {
        "slurm": {"interval": 20, "scope": "me", "ssh": "", "max_rows": 30},
        "gpu": {"interval": 10, "ssh": ""},
        "jira": {"interval": 120, "base_url": "", "token_env": "PIT_TEST_JIRA_TOKEN", "user_env": "PIT_TEST_JIRA_USER",
                 "auth": "bearer", "jql": "assignee = currentUser() ORDER BY updated DESC", "max_rows": 20},
        "wandb": {"interval": 60, "base_url": "", "api_key_env": "PIT_TEST_WANDB_KEY", "entity": "", "project": "",
                  "max_rows": 20, "metrics": []},
    }[name]
    base.update(over)
    return {"panel": {name: base}}


def text(cell):
    return cell[0] if isinstance(cell, tuple) else cell


def style(cell):
    return cell[1] if isinstance(cell, tuple) else None


def assert_message(table, *needles):
    assert isinstance(table, Table)
    assert table.ok is False and table.rows == [] and table.columns == []
    for n in needles:
        assert n in table.note, "%r not in %r" % (n, table.note)
    assert "Traceback" not in table.note


@pytest.fixture
def no_tools(monkeypatch):
    monkeypatch.setattr(panels_pkg.shutil, "which", lambda name: None)


class FakeResponse(io.BytesIO):
    def __init__(self, payload, code=200):
        io.BytesIO.__init__(self, payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8"))
        self.code = code

    def getcode(self):
        return self.code


def fake_urlopen(payload, code=200, seen=None):
    def urlopen(req, timeout=None, context=None):
        if seen is not None:
            seen.append(req)
        if code >= 400:
            body = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
            raise HTTPError(req.full_url, code, "error", {}, io.BytesIO(body))
        return FakeResponse(payload, code)
    return urlopen


# ---------------------------------------------------------------- slurm

SQUEUE = """\
8812301|alice|RUNNING|1-02:13:44|2-00:00:00|1|gpu|gres/gpu:a100:4|gpu-n07|train_tft_v3
8812344|alice|PENDING|0:00|12:00:00|2|gpu|gres/gpu:2|(Resources)|sweep|lr=3e-4
8812350_[4-99%8]|alice|PENDING|0:00|4:00:00|1|cpu|N/A|(JobArrayTaskLimit)|backtest_array
8812299|bob|COMPLETING|3:10:02|UNLIMITED|4|cpu|N/A|cpu-n[01-04]|etl
8812200|bob|OUT_OF_MEMORY|0:42|1:00:00|1|highmem|N/A|hm-n02|feature_build
this line is not squeue output
"""

SINFO = """\
gpu|up|6/2/0/8
cpu*|up|20/0/1/21
highmem|down|0/0/2/2
"""


def test_slurm_parse_squeue_fields():
    jobs = slurm.parse_squeue(SQUEUE)
    assert len(jobs) == 5  # the junk line is skipped
    first = jobs[0]
    assert first == {"id": "8812301", "user": "alice", "state": "RUNNING", "elapsed": "1-02:13:44",
                     "limit": "2-00:00:00", "nodes": "1", "partition": "gpu", "tres": "gres/gpu:a100:4",
                     "where": "gpu-n07", "name": "train_tft_v3", "gpus": 4}
    assert jobs[1]["name"] == "sweep|lr=3e-4"  # a pipe in the job name must not shift fields
    assert jobs[1]["where"] == "(Resources)" and jobs[1]["gpus"] == 2
    assert jobs[2]["id"] == "8812350_[4-99%8]" and jobs[2]["gpus"] == 0


@pytest.mark.parametrize("tres,expected", [
    ("N/A", 0), ("", 0), ("gres/gpu:2", 2), ("gpu:1", 1), ("gres/gpu:a100:4", 4), ("gpu:v100:2(S:0-1)", 2),
    ("gres/gpu", 1), ("gres/gpu=8", 8), ("gres:gpu:3", 3), ("gres/gpu:1,gres/nvme:2", 1), ("gres/license:5", 0),
])
def test_slurm_parse_gpus(tres, expected):
    assert slurm.parse_gpus(tres) == expected


def test_slurm_rows_styles_order_and_truncation():
    jobs = slurm.parse_squeue(SQUEUE)
    cols, rows = slurm.job_rows(jobs, show_user=False, max_rows=30)
    assert cols[:4] == ["JOB", "NAME", "STATE", "ELAPSED"] and len(rows[0]) == len(cols)
    states = [(text(r[2]), style(r[2])) for r in rows]
    assert states == [("OUT_OF_MEMORY", "bad"), ("RUNNING", "ok"), ("COMPLETING", "dim"),
                      ("PENDING", "warn"), ("PENDING", "warn")]
    gpu_col = cols.index("GPU/NODE")
    assert text(rows[1][gpu_col]) == "4" and text(rows[2][gpu_col]) == "-"

    cols_all, rows_all = slurm.job_rows(jobs, show_user=True, max_rows=2)
    assert cols_all[1] == "USER" and len(rows_all) == 2 and rows_all[0][1] == "bob"


def test_slurm_summaries():
    jobs = slurm.parse_squeue(SQUEUE)
    assert slurm.summarize_states(jobs) == "5 jobs: 2 pending, 1 running, 1 completing, 1 out_of_memory"
    assert slurm.summarize_states([]) == "no jobs"
    parts = slurm.parse_sinfo(SINFO)
    assert parts[1] == {"partition": "cpu", "default": True, "avail": "up", "alloc": 20, "idle": 0, "other": 1, "total": 21}
    assert slurm.summarize_partitions(parts) == "idle nodes: gpu 2/8, cpu* 0/21, highmem 0/2 (down)"
    assert slurm.parse_sinfo("garbage\ngpu|up|x/y\n") == []
    assert slurm.summarize_partitions([]) == ""


def test_slurm_sinfo_sums_repeated_partition_lines():
    parts = slurm.parse_sinfo("gpu|up|4/1/0/5\ngpu|up|2/1/0/3\n")
    assert len(parts) == 1 and parts[0]["idle"] == 2 and parts[0]["total"] == 8


def test_slurm_fetch_with_canned_tools(monkeypatch):
    p = slurm.PANEL(cfg("slurm", max_rows=3))
    calls = []

    def run(argv, timeout=10):
        calls.append(argv)
        return (0, SQUEUE, "") if argv[0] == "squeue" else (0, SINFO, "")
    monkeypatch.setattr(p, "run", run)
    t = p.safe_fetch()
    assert t.ok and len(t.rows) == 3
    assert t.note.startswith("5 jobs: 2 pending") and "(showing 3)" in t.note and "idle nodes: gpu 2/8" in t.note
    assert calls[0] == ["squeue", "-h", "-o", slurm.SQUEUE_FORMAT, "--me"]
    assert "8812301" in t.to_text()


def test_slurm_scope_all_and_user_and_sinfo_failure(monkeypatch):
    p = slurm.PANEL(cfg("slurm", scope="all"))
    calls = []

    def run(argv, timeout=10):
        calls.append(argv)
        return (0, SQUEUE, "") if argv[0] == "squeue" else (1, "", "sinfo: error")
    monkeypatch.setattr(p, "run", run)
    t = p.safe_fetch()
    assert t.ok and "USER" in t.columns and "idle nodes" not in t.note  # sinfo failing is not fatal
    assert "--me" not in calls[0] and "-u" not in calls[0]

    p2 = slurm.PANEL(cfg("slurm", user="alice"))
    monkeypatch.setattr(p2, "run", run)
    p2.safe_fetch()
    assert calls[-2][-2:] == ["-u", "alice"]


def test_slurm_empty_queue_is_ok(monkeypatch):
    p = slurm.PANEL(cfg("slurm"))
    monkeypatch.setattr(p, "run", lambda argv, timeout=10: (0, "", ""))
    t = p.safe_fetch()
    assert t.ok and t.rows == [] and t.note == "no jobs"


def test_slurm_quotes_arguments_only_over_ssh():
    local = slurm.PANEL(cfg("slurm"))
    assert local.argv(["squeue", "-o", "%i|%j"]) == ["squeue", "-o", "%i|%j"]
    remote = slurm.PANEL(cfg("slurm", ssh="login1"))
    assert remote.argv(["squeue", "-o", "%i|%j"]) == ["squeue", "-o", "'%i|%j'"]


def test_slurm_missing_tool_and_errors(no_tools, monkeypatch):
    assert_message(slurm.PANEL(cfg("slurm")).safe_fetch(), "squeue not found on PATH", "panel.slurm.ssh", "docs/ONBOARDING.md")
    p = slurm.PANEL(cfg("slurm", ssh="login1"))
    monkeypatch.setattr(p, "run", lambda argv, timeout=10: (255, "", "ssh: connect to host login1: timed out"))
    assert_message(p.safe_fetch(), "ssh to login1 failed", "timed out")
    monkeypatch.setattr(p, "run", lambda argv, timeout=10: (1, "", "squeue: error: Invalid user: --me"))
    assert_message(p.safe_fetch(), "squeue failed (exit 1)", "Invalid user")
    monkeypatch.setattr(p, "run", lambda argv, timeout=10: (0, "JOBID NAME\n1 x\n", ""))
    assert_message(p.safe_fetch(), "could not parse squeue output")


# ---------------------------------------------------------------- gpu

NVIDIA_SMI = """\
0, NVIDIA A100-SXM4-80GB, 97, 71234, 81920, 68, 285.31
1, NVIDIA A100-SXM4-80GB, 0, 3, 81920, 31, 52.10
2, Tesla T4, 42, 14000, 15360, 86, [N/A]
3, NVIDIA RTX A6000, [N/A], [N/A], [N/A], [N/A], [Not Supported]
"""


def test_gpu_parse():
    gpus = gpu.parse_gpus(NVIDIA_SMI + "garbage line\n")
    assert [g["index"] for g in gpus] == [0, 1, 2, 3]
    assert gpus[0]["name"] == "A100-SXM4-80GB" and gpus[2]["name"] == "Tesla T4"
    assert gpus[0]["util"] == 97.0 and gpus[0]["mem_used"] == 71234.0 and gpus[0]["power"] == 285.31
    assert abs(gpus[0]["mem_frac"] - 71234.0 / 81920) < 1e-9
    assert [g["busy"] for g in gpus] == [True, False, True, False]
    assert gpus[2]["power"] is None
    assert gpus[3]["util"] is None and gpus[3]["mem_frac"] is None


def test_gpu_rows_and_summary():
    gpus = gpu.parse_gpus(NVIDIA_SMI)
    rows = gpu.gpu_rows(gpus)
    assert rows[0][2] == ("97%", "hot") and rows[1][2] == ("0%", "dim") and rows[2][2] == ("42%", "ok")
    assert rows[0][3] == ("69.6/80.0G 86%", "ok") and rows[1][3] == ("0.0/80.0G 0%", "dim")
    assert rows[2][3] == ("13.7/15.0G 91%", "warn")
    assert rows[0][4] == ("68C", "ok") and rows[2][4] == ("86C", "bad")
    assert rows[0][5] == "285W" and rows[2][5] == ("n/a", "dim")
    assert [text(c) for c in rows[3][2:]] == ["n/a", "n/a", "n/a", "n/a"]
    assert gpu.summarize(gpus) == "4 GPUs, 2 busy (util > 5% or memory > 5%)"
    assert gpu.summarize(gpus[:1]).startswith("1 GPU, 1 busy")


def test_gpu_fetch_with_canned_tool(monkeypatch):
    p = gpu.PANEL(cfg("gpu"))
    calls = []
    monkeypatch.setattr(p, "run", lambda argv, timeout=10: calls.append(argv) or (0, NVIDIA_SMI, ""))
    t = p.safe_fetch()
    assert t.ok and len(t.rows) == 4 and t.columns[0] == "GPU" and t.note.startswith("4 GPUs, 2 busy")
    assert calls[0][0] == "nvidia-smi" and calls[0][1].startswith("--query-gpu=index,name,utilization.gpu,")
    assert calls[0][2] == "--format=csv,noheader,nounits"


def test_gpu_missing_tool_and_errors(no_tools, monkeypatch):
    assert_message(gpu.PANEL(cfg("gpu")).safe_fetch(), "nvidia-smi not found on PATH", "panel.gpu.ssh", "docs/ONBOARDING.md")
    p = gpu.PANEL(cfg("gpu"))
    monkeypatch.setattr(p, "run", lambda argv, timeout=10: (
        9, "NVIDIA-SMI has failed because it couldn't communicate with the NVIDIA driver.\n", ""))
    assert_message(p.safe_fetch(), "nvidia-smi failed (exit 9)", "couldn't communicate")
    monkeypatch.setattr(p, "run", lambda argv, timeout=10: (0, "No devices were found\n", ""))
    assert_message(p.safe_fetch(), "no GPUs", "No devices were found")


# ---------------------------------------------------------------- jira

JIRA = {
    "expand": "schema,names", "startAt": 0, "maxResults": 20, "total": 37,
    "issues": [
        {"id": "10412", "key": "QR-812", "self": "https://jira.example.internal/rest/api/2/issue/10412", "fields": {
            "summary": "Intraday vol forecast: residuals autocorrelated after the 14:30 auction, refit HAR with overnight term",
            "status": {"name": "In Progress", "statusCategory": {"key": "indeterminate", "name": "In Progress"}},
            "priority": {"name": "High", "id": "2"},
            "assignee": {"name": "alice", "displayName": "Alice Example"},
            "updated": "2026-11-22T09:30:00.000+0000"}},
        {"id": "10398", "key": "QR-799", "fields": {
            "summary": "Backfill 2019 tick data",
            "status": {"name": "To Do", "statusCategory": {"key": "new"}},
            "priority": None, "assignee": None,
            "updated": "2026-11-19T20:00:00.000+0800"}},
        {"id": "10001", "key": "INFRA-7", "fields": {
            "summary": "Rotate cluster keys",
            "status": {"name": "Done", "statusCategory": {"key": "done"}},
            "priority": {"name": "Blocker"},
            "updated": "not a date"}},
    ],
}


@pytest.mark.parametrize("stamp,expected", [
    ("2026-11-22T12:00:00.000+0000", NOW), ("2026-11-22T12:00:00Z", NOW), ("2026-11-22T12:00:00", NOW),
    ("2026-11-22T20:00:00.123+0800", NOW), ("2026-11-22T07:00:00-05:00", NOW), ("2026-11-22 12:00:00", NOW),
    ("yesterday", None), (None, None), ("", None),
])
def test_parse_ts(stamp, expected):
    assert jira.parse_ts(stamp) == expected
    assert wandb.parse_ts(stamp) == expected


def test_fmt_age():
    assert [jira.fmt_age(s) for s in (None, -5, 0, 59, 60, 3599, 3600, 86399, 86400, 9 * 86400)] == \
        ["?", "0s", "0s", "59s", "1m", "59m", "1h", "23h", "1d", "9d"]


def test_jira_build_url_and_auth():
    url = jira.build_url("https://jira.example.internal/jira/", "assignee = currentUser() ORDER BY updated DESC", 5)
    assert url.startswith("https://jira.example.internal/jira/rest/api/2/search?jql=assignee+%3D+currentUser%28%29+ORDER")
    assert "fields=summary%2Cstatus%2Cpriority%2Cassignee%2Cupdated" in url and url.endswith("maxResults=5")
    assert jira.auth_header("bearer", "tok") == "Bearer tok"
    assert jira.auth_header("basic", "tok", "alice") == "Basic YWxpY2U6dG9r"


def test_jira_parse_issues():
    issues = jira.parse_issues(JIRA, now=NOW)
    assert [i["key"] for i in issues] == ["QR-812", "QR-799", "INFRA-7"]
    assert issues[0]["status"] == "In Progress" and issues[0]["category"] == "indeterminate"
    assert issues[0]["priority"] == "High" and issues[0]["assignee"] == "Alice Example"
    assert issues[0]["age"] == 2.5 * 3600
    assert issues[1]["priority"] == "" and issues[1]["assignee"] == "" and issues[1]["age"] == 3 * 86400
    assert issues[2]["age"] is None
    for bad in ({}, {"issues": None}, [], "<html>", {"errorMessages": ["x"]}):
        with pytest.raises(ValueError):
            jira.parse_issues(bad)
    assert jira.parse_issues({"issues": []}) == []


def test_jira_rows():
    rows = jira.issue_rows(jira.parse_issues(JIRA, now=NOW), width=40)
    assert rows[0][0] == ("QR-812", "hot") and rows[0][1] == ("In Progress", "ok") and rows[0][2] == ("High", "warn")
    assert rows[0][3] == "2h" and len(rows[0][4]) == 40 and rows[0][4].endswith("~")
    assert rows[1][1] == "To Do" and rows[1][2] == ("-", "dim") and rows[1][3] == "3d"
    assert rows[1][4] == "Backfill 2019 tick data"
    assert rows[2][1] == ("Done", "dim") and rows[2][2] == ("Blocker", "bad") and rows[2][3] == "?"


def test_jira_not_configured(monkeypatch):
    monkeypatch.delenv("PIT_TEST_JIRA_TOKEN", raising=False)
    monkeypatch.delenv("PIT_TEST_JIRA_USER", raising=False)
    monkeypatch.setattr(jira, "urlopen", lambda *a, **k: pytest.fail("network call attempted"))
    assert_message(jira.PANEL(cfg("jira")).safe_fetch(), "panel.jira.base_url", "docs/ONBOARDING.md")
    assert_message(jira.PANEL({}).safe_fetch(), "panel.jira.base_url")
    assert_message(jira.PANEL(cfg("jira", base_url="jira.example.internal")).safe_fetch(), "http://")
    url = "https://jira.example.internal"
    assert_message(jira.PANEL(cfg("jira", base_url=url)).safe_fetch(), "export PIT_TEST_JIRA_TOKEN")
    monkeypatch.setenv("PIT_TEST_JIRA_TOKEN", "s3cret")
    assert_message(jira.PANEL(cfg("jira", base_url=url, auth="basic")).safe_fetch(), "export PIT_TEST_JIRA_USER")
    assert_message(jira.PANEL(cfg("jira", base_url=url, auth="oauth")).safe_fetch(), "panel.jira.auth")


def test_jira_fetch_with_canned_response(monkeypatch):
    monkeypatch.setenv("PIT_TEST_JIRA_TOKEN", "s3cret")
    seen = []
    monkeypatch.setattr(jira, "urlopen", fake_urlopen(JIRA, seen=seen))
    t = jira.PANEL(cfg("jira", base_url="https://jira.example.internal", max_rows=3)).safe_fetch()
    assert t.ok and t.columns == ["KEY", "STATUS", "PRIORITY", "UPDATED", "SUMMARY"] and len(t.rows) == 3
    assert t.note == "3 of 37 issues"
    req = seen[0]
    assert req.get_method() == "GET" and req.get_header("Authorization") == "Bearer s3cret"
    assert req.full_url.startswith("https://jira.example.internal/rest/api/2/search?jql=") and "maxResults=3" in req.full_url
    assert "s3cret" not in t.to_text()


def test_jira_http_and_network_errors(monkeypatch):
    monkeypatch.setenv("PIT_TEST_JIRA_TOKEN", "s3cret")
    p = jira.PANEL(cfg("jira", base_url="https://jira.example.internal"))

    monkeypatch.setattr(jira, "urlopen", fake_urlopen({"errorMessages": ["Field 'asignee' does not exist."], "errors": {}}, 400))
    assert_message(p.safe_fetch(), "HTTP 400", "bad JQL", "Field 'asignee' does not exist.")
    monkeypatch.setattr(jira, "urlopen", fake_urlopen(b"<html>Unauthorized</html>", 401))
    assert_message(p.safe_fetch(), "HTTP 401", "token rejected", "$PIT_TEST_JIRA_TOKEN")
    monkeypatch.setattr(jira, "urlopen", fake_urlopen(b"<!DOCTYPE html><html><title>Log in</title>"))
    assert_message(p.safe_fetch(), "not JSON", "SSO")
    monkeypatch.setattr(jira, "urlopen", fake_urlopen({"unexpected": True}))
    assert_message(p.safe_fetch(), "unexpected Jira response")

    def refuse(req, timeout=None, context=None):
        raise URLError("[Errno 61] Connection refused")
    monkeypatch.setattr(jira, "urlopen", refuse)
    assert_message(p.safe_fetch(), "Jira unreachable", "Connection refused")

    def slow(req, timeout=None, context=None):
        raise OSError("timed out")  # socket.timeout is an OSError on 3.9
    monkeypatch.setattr(jira, "urlopen", slow)
    assert_message(p.safe_fetch(), "Jira unreachable", "timed out")
    for t in (p.safe_fetch(),):
        assert "s3cret" not in t.note


# ---------------------------------------------------------------- wandb

def wandb_node(name, display, state, created, beat, user, summary):
    return {"node": {"name": name, "displayName": display, "state": state, "createdAt": created, "heartbeatAt": beat,
                     "summaryMetrics": summary, "user": {"username": user} if user else None}}


WANDB = {"data": {"project": {"runs": {"edges": [
    wandb_node("a1b2c3d4", "tft-lr3e-4", "running", "2026-11-22T10:00:00", "2026-11-22T11:59:30", "alice",
               json.dumps({"_step": 4200, "_runtime": 7170.2, "_timestamp": 1795348770, "val/loss": 0.03182734,
                           "train/loss": 0.0271, "sharpe": 1.8349, "epoch": 12, "best": True, "tag": "x",
                           "_wandb": {"runtime": 7170}})),
    wandb_node("e5f6a7b8", "tft-lr1e-3", "running", "2026-11-22T08:00:00Z", "2026-11-22T11:40:00Z", "bob",
               json.dumps({"val/loss": 0.0412, "train/loss": 0.0390, "ic": {"mean": 0.041}})),
    wandb_node("c9d0e1f2", "", "crashed", "2026-11-20T12:00:00", "2026-11-20T13:00:00", "alice", "{}"),
    wandb_node("0a1b2c3d", "baseline-ridge", "finished", "2026-11-12T12:00:00", None, None, "not json"),
]}}}}


def test_wandb_query_and_body():
    for field in ("displayName", "state", "createdAt", "heartbeatAt", "summaryMetrics", "username"):
        assert field in wandb.QUERY
    body = json.loads(wandb.build_body("quant", "tft", 7).decode("utf-8"))
    assert body["query"] == wandb.QUERY
    assert body["variables"] == {"entity": "quant", "project": "tft", "first": 7, "order": "-created_at"}


def test_wandb_parse_runs():
    runs = wandb.parse_runs(WANDB)
    assert [r["name"] for r in runs] == ["tft-lr3e-4", "tft-lr1e-3", "c9d0e1f2", "baseline-ridge"]
    assert runs[0]["id"] == "a1b2c3d4" and runs[0]["state"] == "running" and runs[0]["user"] == "alice"
    assert runs[0]["created"] == NOW - 7200 and runs[0]["heartbeat"] == NOW - 30
    assert runs[0]["summary"]["val/loss"] == 0.03182734
    assert runs[1]["created"] == NOW - 4 * 3600
    assert runs[2]["summary"] == {} and runs[3]["summary"] == {}
    assert runs[3]["user"] == "" and runs[3]["heartbeat"] is None


def test_wandb_parse_runs_accepts_object_summary():
    data = {"data": {"project": {"runs": {"edges": [wandb_node("r", "r", "running", None, None, "a", {"loss": 1.5})]}}}}
    assert wandb.parse_runs(data)[0]["summary"] == {"loss": 1.5}


@pytest.mark.parametrize("data,needle", [
    ({"errors": [{"message": 'Cannot query field "heartbeatAt" on type "Run".'}]}, 'Cannot query field "heartbeatAt"'),
    ({"errors": ["plain string error"], "data": None}, "plain string error"),
    ({"data": {"project": None}}, "project not found"),
    ({"data": {}}, "no data.project"),
    ({"data": {"project": {"runs": None}}}, "runs.edges"),
    ({"data": {"project": {"runs": {"edges": [{"cursor": "x"}]}}}}, "runs.edges"),
    ([], "not a JSON object"),
])
def test_wandb_parse_runs_errors(data, needle):
    with pytest.raises(ValueError) as e:
        wandb.parse_runs(data)
    assert needle in str(e.value)


def test_wandb_pick_metrics_and_lookup():
    runs = wandb.parse_runs(WANDB)
    # default: first numeric, non-underscore keys in order seen; bools and strings are not metrics
    assert wandb.pick_metrics(runs) == ["val/loss", "train/loss", "sharpe"]
    assert wandb.pick_metrics(runs, ["sharpe", "ic.mean", "a", "b"]) == ["sharpe", "ic.mean", "a"]
    assert wandb.pick_metrics([]) == []
    assert wandb.lookup(runs[1]["summary"], "ic.mean") == 0.041
    assert wandb.lookup(runs[1]["summary"], "ic.std") is None
    assert wandb.lookup({"a.b": 1, "a": {"b": 2}}, "a.b") == 1


def test_wandb_rows():
    runs = wandb.parse_runs(WANDB)
    rows = wandb.run_rows(runs, ["val/loss", "sharpe", "ic.mean"], now=NOW)
    assert rows[0] == [("tft-lr3e-4", "hot"), ("running", "ok"), "alice", "2h", "30s", "0.03183", "1.835", ("-", "dim")]
    # running but no heartbeat for 20 minutes: flagged
    assert rows[1][1] == ("running", "ok") and rows[1][4] == ("20m", "warn") and rows[1][7] == "0.041"
    assert rows[2][1] == ("crashed", "bad") and rows[2][3] == "2d" and rows[2][4] == "1d"
    assert rows[3][1] == ("finished", "dim") and rows[3][2] == "" and rows[3][3] == "10d" and rows[3][4] == "?"


def test_wandb_not_configured(monkeypatch):
    monkeypatch.delenv("PIT_TEST_WANDB_KEY", raising=False)
    monkeypatch.setattr(wandb, "urlopen", lambda *a, **k: pytest.fail("network call attempted"))
    assert_message(wandb.PANEL(cfg("wandb")).safe_fetch(), "panel.wandb.base_url", "panel.wandb.entity",
                   "panel.wandb.project", "docs/ONBOARDING.md")
    assert_message(wandb.PANEL({}).safe_fetch(), "panel.wandb.base_url")
    t = wandb.PANEL(cfg("wandb", base_url="https://wandb.example.internal", entity="quant")).safe_fetch()
    assert_message(t, "panel.wandb.project")
    assert "base_url" not in t.note
    full = dict(base_url="https://wandb.example.internal", entity="quant", project="tft")
    assert_message(wandb.PANEL(cfg("wandb", **full)).safe_fetch(), "export PIT_TEST_WANDB_KEY")
    assert_message(wandb.PANEL(cfg("wandb", **dict(full, base_url="wandb.example.internal"))).safe_fetch(), "http://")


def test_wandb_fetch_with_canned_response(monkeypatch):
    monkeypatch.setenv("PIT_TEST_WANDB_KEY", "local-0123456789abcdef")
    seen = []
    monkeypatch.setattr(wandb, "urlopen", fake_urlopen(WANDB, seen=seen))
    p = wandb.PANEL(cfg("wandb", base_url="https://wandb.example.internal/", entity="quant", project="tft",
                        metrics=["sharpe"]))
    t = p.safe_fetch()
    assert t.ok and t.columns == ["RUN", "STATE", "USER", "AGE", "HEARTBEAT", "SHARPE"] and len(t.rows) == 4
    assert t.note == "quant/tft: 4 runs, 2 running"
    req = seen[0]
    assert req.full_url == "https://wandb.example.internal/graphql" and req.get_method() == "POST"
    assert req.get_header("Authorization") == "Basic YXBpOmxvY2FsLTAxMjM0NTY3ODlhYmNkZWY="  # api:<key>
    assert json.loads(req.data.decode("utf-8"))["variables"]["project"] == "tft"
    assert "local-0123456789abcdef" not in t.to_text()

    p2 = wandb.PANEL(cfg("wandb", base_url="https://wandb.example.internal", entity="quant", project="tft", max_rows=2))
    t2 = p2.safe_fetch()
    assert len(t2.rows) == 2 and t2.columns[5:] == ["VAL/LOSS", "TRAIN/LOSS", "SHARPE"]


def test_wandb_schema_http_and_network_errors(monkeypatch):
    monkeypatch.setenv("PIT_TEST_WANDB_KEY", "local-0123456789abcdef")
    p = wandb.PANEL(cfg("wandb", base_url="https://wandb.example.internal", entity="quant", project="tft"))

    schema_err = {"errors": [{"message": 'Cannot query field "heartbeatAt" on type "Run".'}]}
    for code in (200, 400):  # servers differ on the status used for GraphQL validation errors
        monkeypatch.setattr(wandb, "urlopen", fake_urlopen(schema_err, code))
        assert_message(p.safe_fetch(), "HTTP %d" % code, 'Cannot query field "heartbeatAt"', "QUERY in pit/panels/wandb.py")
    monkeypatch.setattr(wandb, "urlopen", fake_urlopen({"errors": [{"message": "permission denied"}]}, 401))
    assert_message(p.safe_fetch(), "API key rejected", "$PIT_TEST_WANDB_KEY", "permission denied")
    monkeypatch.setattr(wandb, "urlopen", fake_urlopen({"data": {"project": None}}))
    assert_message(p.safe_fetch(), "project not found")
    monkeypatch.setattr(wandb, "urlopen", fake_urlopen(b"<html>404 page not found</html>", 404))
    assert_message(p.safe_fetch(), "HTTP 404", "not JSON", "panel.wandb.base_url")

    def refuse(req, timeout=None, context=None):
        raise URLError("[Errno -2] Name or service not known")
    monkeypatch.setattr(wandb, "urlopen", refuse)
    t = p.safe_fetch()
    assert_message(t, "W&B unreachable", "Name or service not known")
    assert "local-0123456789abcdef" not in t.note


# ---------------------------------------------------------------- all panels

@pytest.mark.parametrize("mod,name", [(slurm, "slurm"), (gpu, "gpu"), (jira, "jira"), (wandb, "wandb")])
def test_panel_contract_and_clean_message_with_empty_config(mod, name, no_tools, monkeypatch):
    for var in ("JIRA_TOKEN", "JIRA_USER", "WANDB_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    assert mod.PANEL.name == name and mod.PANEL.title
    p = mod.PANEL({})
    assert p.interval > 0
    t = p.safe_fetch()
    assert_message(t, "docs/ONBOARDING.md")
    assert t.to_text().startswith("! ")
