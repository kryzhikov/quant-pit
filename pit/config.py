"""Config loading. Defaults live in conf/defaults.json; the user file overrides them.

User file: ~/.config/pit/config.json (or $PIT_CONFIG_DIR/config.json).
Stdlib only: this module is imported by the Claude Code hook, which must start fast.
"""
import json
import os
from pathlib import Path

PIT_HOME = Path(__file__).resolve().parent.parent


def config_dir():
    return Path(os.environ.get("PIT_CONFIG_DIR", "~/.config/pit")).expanduser()


def state_dir():
    d = Path(os.environ.get("PIT_STATE_DIR", "~/.local/state/pit")).expanduser()
    d.mkdir(parents=True, exist_ok=True)
    return d


def deep_merge(base, over):
    """Dicts merge recursively; any other value in `over` replaces the one in `base`."""
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load():
    cfg = json.loads((PIT_HOME / "conf" / "defaults.json").read_text())
    user = config_dir() / "config.json"
    if user.exists():
        try:
            cfg = deep_merge(cfg, json.loads(user.read_text()))
        except ValueError as e:
            cfg["_error"] = "config.json ignored: %s" % e
    return cfg


def load_rules():
    user = config_dir() / "agents.json"
    path = user if user.exists() else PIT_HOME / "rules" / "agents.json"
    return json.loads(path.read_text())
