# Onboarding the data panels (for the agent finishing the wiring)

You are on the real box. The panels `slurm`, `gpu`, `jira`, `wandb` were written on a Mac
with none of those systems available: parsing is unit-tested against canned output, the
live calls are not tested at all. Your job is to make each one print real data, fixing the
code where an assumption below turns out wrong.

## Ground rules

- Local config goes in `~/.config/pit/config.json`. It is deep-merged over
  `conf/defaults.json`, so write only the keys you change. Do not edit `conf/defaults.json`
  for site settings. Lists are replaced, not merged (relevant for `"panels"`).
- Secrets live in environment variables only. Config holds the variable NAME
  (`token_env`, `api_key_env`), never the value. Do not write, echo, log or commit a secret
  value; do not put one in a command line that lands in shell history. Check presence with
  `[ -n "$JIRA_TOKEN" ] && echo set`. If a variable is missing, ask the human how secrets
  are provisioned on this box and let them set it.
- Verify with `bin/pit panel <name>`. It prints the panel as text. A line starting with `!`
  is a failure message and says what to fix. No `!` plus sensible rows means done.
- The box is Rocky Linux 9 with Python 3.9. Panel code is stdlib only: no 3.10+ syntax
  (`match`, `X | Y` types), no third-party imports, no `tomllib`.
- After any code change: `.venv/bin/python -m pytest tests/test_panels.py -q` and
  `/usr/bin/python3 -m py_compile pit/panels/*.py`. When real output differs from the
  canned samples in `tests/test_panels.py`, replace the sample with the real output
  (scrub hostnames, users, tokens) and make the parser pass.
- If `bin/pit panel X` works in your shell but the dashboard shows "token missing" for the
  same panel, the dashboard process was started without the variable. A tmux server keeps
  the environment it was started with: `tmux show-environment -g | grep -c JIRA_TOKEN`
  prints 0 when it is absent. Restart pit from a shell that has the variable.
- A panel you cannot make work on this box: remove it from `"panels"` in the user config
  rather than leaving a permanent error tab, and tell the human why.

## Slurm

Code: `pit/panels/slurm.py`. Runs `squeue -h -o '%i|%u|%T|%M|%l|%D|%P|%b|%R|%j'` (plus
`--me` unless scope is "all") and `sinfo -h -o '%P|%a|%F'`.

Check first:

```
command -v squeue sinfo && squeue --version
squeue -h -o '%i|%u|%T|%M|%l|%D|%P|%b|%R|%j' --me | head -5
squeue -h -o '%i|%u|%T|%M|%l|%D|%P|%b|%R|%j' | head -5
sinfo -h -o '%P|%a|%F'
```

Config keys (`panel.slurm`):

| key | meaning |
| --- | --- |
| `scope` | `"me"` (default) or `"all"`; "all" adds a USER column |
| `user` | optional; uses `-u <user>` instead of `--me` |
| `ssh` | host to run squeue/sinfo on when this box is not a login node |
| `max_rows` | row cap; failed jobs sort first, pending last |
| `interval` | refresh seconds |

No secrets. With `ssh`, login must be key-based and non-interactive
(`ssh -o BatchMode=yes <host> true` must succeed).

Verify: `bin/pit panel slurm`. Compare job count against `squeue --me -h | wc -l`.

Known untested:

- `--me` needs Slurm 20.02 or newer. On older Slurm squeue exits with an error: set
  `panel.slurm.user`.
- `%b` is assumed to print the per-node GRES request (`gres/gpu:2`, `gpu:a100:2`, `N/A`).
  The column is labelled GPU/NODE for that reason. Jobs submitted with `--gpus=N`
  (per-job TRES) may show `N/A` here. If the GPU column is wrong for this site, look at
  `squeue -O tres-per-job,tres-per-node,tres-alloc` and adjust `SQUEUE_FORMAT` and
  `parse_gpus`.
- `%F` is assumed to print `allocated/idle/other/total` once per partition. Repeated
  partition lines are summed, which double counts if sinfo splits one partition by
  something other than node state. Compare the note line with plain `sinfo`.
- Finished states (FAILED, TIMEOUT, OUT_OF_MEMORY) only appear in squeue for
  `MinJobAge` seconds after the job ends, so the "bad" styling is rarely visible. A
  history view would need `sacct`: not implemented.
- Over `ssh`, arguments are shell-quoted for the remote shell. Tested only as a string
  comparison, never against a real remote host.
- Both commands run with a 10 s timeout each. A slow slurmctld shows as
  "squeue timed out".

## GPU

Code: `pit/panels/gpu.py`. Runs
`nvidia-smi --query-gpu=index,name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw --format=csv,noheader,nounits`.

Check first:

```
command -v nvidia-smi
nvidia-smi --query-gpu=index,name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw --format=csv,noheader,nounits
```

Config keys (`panel.gpu`): `ssh` (host to query instead of this one), `interval`. No secrets.

This panel shows ONE machine. On a login node without GPUs either set `ssh` to a GPU host
you may log in to, or remove `"gpu"` from `"panels"`. Many clusters forbid ssh to compute
nodes unless you hold a job there: check before setting it.

Verify: `bin/pit panel gpu`. Row count must equal `nvidia-smi -L | wc -l`.

Known untested:

- Everything against a real driver. The parser expects exactly 7 comma-separated fields
  and treats `[N/A]` / `[Not Supported]` as missing.
- "Busy" in the note line means utilisation above 5% or memory above 5% (constants
  `BUSY_UTIL`, `BUSY_MEM`). It does not know about Slurm allocations: an allocated but
  idle GPU counts as free.
- No per-process or per-user view. MIG devices and non-NVIDIA GPUs are not handled.

## Jira

Code: `pit/panels/jira.py`. Calls
`GET {base_url}/rest/api/2/search?jql=...&fields=summary,status,priority,assignee,updated&maxResults=N`.

Check first (BASE is the URL you see in the browser up to and including any context path
such as `/jira`):

```
BASE=https://jira.example.internal
curl -sS "$BASE/rest/api/2/serverInfo"        # no auth needed: version and deploymentType
[ -n "$JIRA_TOKEN" ] && echo token set
curl -sS -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $JIRA_TOKEN" "$BASE/rest/api/2/myself"
```

200 on the last call means bearer auth works. 401 means a bad token, or a server that does
not accept personal access tokens (they need Jira Server / Data Center 8.14 or newer): then
try basic auth with `curl -u "$JIRA_USER:$JIRA_TOKEN"`. An HTML response means an SSO proxy
sits in front of the API: ask the human for the API-reachable URL.

Config keys (`panel.jira`):

| key | meaning |
| --- | --- |
| `base_url` | required, with scheme and context path, no trailing `/rest/...` |
| `auth` | `"bearer"` (personal access token, default) or `"basic"` (user + password or token) |
| `token_env` | name of the env var holding the token (default `JIRA_TOKEN`) |
| `user_env` | name of the env var holding the user name, basic auth only (default `JIRA_USER`) |
| `jql` | the query; default is your unresolved issues by last update |
| `max_rows` | sent as `maxResults` |
| `summary_width` | optional, summary truncation width (default 60) |
| `ca_file` | optional, PEM bundle when the server uses an internal CA |

Minimal config:

```json
{"panel": {"jira": {"base_url": "https://jira.example.internal"}}}
```

Verify: `bin/pit panel jira`. Cross-check the keys against the same JQL in the Jira UI.

Known untested:

- The whole HTTP path against a real server. Only the response shape documented for the
  v2 search API is tested, from a hand-written sample.
- Jira Cloud is out of scope: Atlassian removed `/rest/api/2/search` there in favour of
  `/rest/api/3/search/jql`. If this turns out to be Cloud, change `build_url` and use
  basic auth with email + API token.
- Status colour uses `fields.status.statusCategory.key` (`new`, `indeterminate`, `done`).
  Priority colour matches the default scheme names (Blocker, Highest, High, Low, ...):
  a site with custom priority names gets unstyled text, edit `PRIORITY_STYLE`.
- TLS: a certificate error shows as "Jira unreachable ... CERTIFICATE_VERIFY_FAILED".
  Set `ca_file` (on Rocky the system bundle is `/etc/pki/tls/certs/ca-bundle.crt`). There
  is deliberately no "skip verification" switch.
- Proxies: urllib honours `https_proxy` / `no_proxy`. An internal host routed to an
  outbound proxy shows as a timeout or 403: add the host to `no_proxy`.

## W&B

Code: `pit/panels/wandb.py`. Calls `POST {base_url}/graphql` with basic auth user `api`,
password = API key, sending the constant `QUERY`.

THE QUERY IS UNVERIFIED. It was written from memory of what the wandb SDK sends. Expect to
fix it. Schema errors do not crash the panel: they show as
`! W&B HTTP 400: GraphQL error: Cannot query field ...`.

Check first:

```
BASE=https://wandb.example.internal
curl -sS -o /dev/null -w '%{http_code}\n' "$BASE/healthz"   # path unverified: any 2xx/3xx from $BASE is enough
[ -n "$WANDB_API_KEY" ] && echo key set
curl -sS -u "api:$WANDB_API_KEY" -H 'Content-Type: application/json' \
  -d '{"query":"query { viewer { username entity } }"}' "$BASE/graphql"
```

The last call must return JSON with your username. It also tells you the default entity.
If it fails, the auth scheme or the `/graphql` path is wrong for this server: stop and
find the right one before touching the query (the SDK's `wandb login --host $BASE` flow
and `~/.netrc` show what the SDK itself uses).

Config keys (`panel.wandb`):

| key | meaning |
| --- | --- |
| `base_url` | required, server root, e.g. `https://wandb.example.internal` |
| `entity` | required, team or user that owns the project |
| `project` | required |
| `api_key_env` | name of the env var holding the API key (default `WANDB_API_KEY`) |
| `metrics` | up to 3 summary metric names to show; `[]` picks the first numeric ones; `a.b` reaches nested values |
| `max_rows` | number of runs requested |
| `ca_file` | optional, PEM bundle for an internal CA |

Verify: `bin/pit panel wandb`. Compare the top rows with the project's runs table sorted by
creation time.

When the query is rejected, get the real one instead of guessing:

1. If the wandb SDK is installed anywhere on the box, read the query it sends:
   `python -c "import wandb, os; print(os.path.dirname(wandb.__file__))"` then
   `grep -rn "query ProjectRuns" -A 30 <that dir>/apis/` and the `fragment RunFragment`
   next to it. Copy field and argument names from there into `QUERY`.
2. Or introspect the server:
   `curl -sS -u "api:$WANDB_API_KEY" -H 'Content-Type: application/json' -d '{"query":"{ __type(name: \"Run\") { fields { name } } }"}' "$BASE/graphql"`
   (same for `Project`, to see the arguments of `runs`).
3. Keep `QUERY`, `ORDER` and `parse_runs` in step, update the `WANDB` sample in
   `tests/test_panels.py` to a real (scrubbed) response, rerun the tests.

Known untested (all of it, specifically):

- Field names: `project(name:, entityName:)`, `runs(first:, order:)`, and on Run: `name`,
  `displayName`, `state`, `createdAt`, `heartbeatAt`, `summaryMetrics`, `user { username }`.
- The order value `"-created_at"` for newest first.
- `summaryMetrics` being a JSON-encoded string (an object is also accepted).
- Timestamps without a timezone being UTC. If ages are off by your UTC offset, this is why.
- State names used for colour: running, finished, failed, crashed, killed, preempted.
- Basic auth with user `api` being accepted by this server version, and whether GraphQL
  validation errors come back as HTTP 200 or 400 (both are handled).
- A "running" run whose heartbeat is older than 5 minutes is highlighted
  (`STALE_HEARTBEAT`); whether that threshold suits this server's heartbeat period is unknown.

## Adding a new panel

1. Create `pit/panels/<name>.py` (or `~/.config/pit/panels/<name>.py` for a site-local
   panel; a file there overrides a built-in of the same name). Contract and a template are
   in the docstring of `pit/panels/__init__.py`; `pit/panels/gpu.py` is the shortest real
   example. Use `from pit.panels import Panel, Table` (relative imports do not work from
   the user directory).
2. Split it the same way as the existing ones: a pure `parse_*(text_or_dict)` function, a
   pure `*_rows(...)` function that builds cells, and a `fetch()` that does the I/O and
   returns a `Table`. Cells are a string or `(string, style)` with style in
   `ok, warn, bad, dim, hot`.
3. `fetch()` must use timeouts and must return `Table.message("what to set or install")`
   for every expected failure (tool missing, not configured, service down). Use
   `self.run(argv)` for subprocesses: it handles the optional `ssh` setting, a missing
   tool (returncode 127) and timeouts (124). Shell-quote arguments yourself when
   `self.conf.get("ssh")` is set.
4. Read settings from `self.conf` (the `panel.<name>` section), secrets from an env var
   whose name is a config key. End the module with `PANEL = YourClass`.
5. Add `<name>` to `"panels"` and defaults under `"panel"` in the user config (or in
   `conf/defaults.json` if it ships for everyone).
6. Add tests to `tests/test_panels.py`: parsing against real captured output, and one
   test that the unavailable case returns a message instead of raising.
7. `bin/pit panel <name>` to check it.
