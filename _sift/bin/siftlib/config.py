"""Config load, defaults and validation (BUILD-SPEC 6).

The file is user-owned: an upgrade never rewrites it, missing keys take
defaults, and unknown keys are surfaced by `doctor` rather than rejected.
"""
from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, Dict, List, Tuple

from . import util

DEFAULTS: Dict[str, Any] = {
    "version": 1,
    "dir_name": "_sift",
    # Instruction decay is within-session, so the countermeasure is cadence.
    "context": {"reinjection_interval": 25},
    # Bash output governance, split by risk rather than by hook.
    #
    # `advise` never changes anything: it offers a capped form before a command
    # that will flood, and counts the floods it sees afterwards. That counting
    # is the only way anyone finds out whether `enabled` is worth turning on --
    # with both halves behind one flag, as they once were, a
    # repo could flood every session and nothing would ever say so.
    #
    # `enabled` replaces what the model sees, which is not a thing to switch on
    # for someone without their say-so.
    "governance": {
        "advise": True,
        "enabled": False,
        "threshold_tokens": 2000,
        "max_log_bytes": 4194304,
        "cache_budget_bytes": 67108864,
        "head_lines": 80,
        "tail_lines": 30,
        "grep_per_file": 3,
        "abandon_ratio": 0.7,
    },
    "store": {
        "total_token_budget": 60000,
        "sentence_per_line": True,
    },
    "scan": {
        "symbol_min_tokens": 500,
        "symbol_max_count": 30,
        "symbol_max_bytes": 262144,
        "binary_extensions": [
            ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf", ".woff",
            ".woff2", ".ttf", ".otf", ".zip", ".gz", ".jar", ".class", ".pyc",
            ".so", ".dylib", ".dll", ".exe", ".bin", ".ipynb",
        ],
    },
    # How far back `scan` asks git for churn. Read by `scan` since it was
    # written, and missing from here, so the window was unsettable and a repo
    # that set it anyway failed `doctor`'s config-keys check.
    "hot": {"since": "6.months"},
    "hooks": {
        "enabled": True,
        "session_budget_tokens": 2000,
        "session_start": True,
        "pre_read": True,
        "post_read": True,
        "post_tool": True,
        "pre_bash": True,
        "post_bash": True,
        "pre_compact": True,
        "session_end": True,
        "duplicate_read_mode": "warn",
    },
    # Files that mention OpenWolf for a reason that survives the migration --
    # a still-live npm dependency, a guard that names it on purpose. Recorded
    # here rather than edited out of the file, so the decision is auditable.
    "openwolf": {"reviewed": []},
    # How many of the busiest files `doctor` expects to be described before it
    # stops calling the install unfinished. Coverage of everything is the wrong
    # target -- most files are never opened -- and any-at-all was too weak: one
    # repo reported itself finished at 49 descriptions out of 466.
    # `check_upgrade` compares this repo's runtime with the clone on this
    # machine, if there is one, once per session. No network, and nothing acts
    # on the answer by itself -- it is a sentence, not an upgrade.
    "setup": {"describe_top": 25, "check_upgrade": True},
    "lint": {"hard_fail": ["W15", "W16", "W19"], "stale_updated_days": 30},
    "ci": {"advisory": True},
}

ENUMS = {
    ("hooks", "duplicate_read_mode"): {"off", "warn", "deny"},
}


def _merge(base: Dict[str, Any], over: Dict[str, Any]) -> Dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


class Config:
    def __init__(self, data: Dict[str, Any]) -> None:
        self.data = data

    def get(self, *keys: str, default: Any = None) -> Any:
        node: Any = self.data
        for key in keys:
            if not isinstance(node, dict) or key not in node:
                return default
            node = node[key]
        return node

    @property
    def hooks(self) -> Dict[str, Any]:
        return self.data.get("hooks", {})

    @property
    def governance(self) -> Dict[str, Any]:
        """Bash output governance. Off unless a repo turns it on."""
        return self.data.get("governance", {})


def load(path: Path) -> Config:
    raw = util.read_json(path, default={})
    if not isinstance(raw, dict):
        raw = {}
    return Config(_merge(DEFAULTS, raw))


def validate(path: Path) -> Tuple[List[str], List[str]]:
    """Return (unknown key paths, invalid value messages)."""
    raw = util.read_json(path, default={})
    unknown: List[str] = []
    invalid: List[str] = []
    if not isinstance(raw, dict):
        return unknown, ["config.json is not a JSON object"]

    def walk(node: Dict[str, Any], ref: Dict[str, Any], prefix: str) -> None:
        for key, value in node.items():
            here = prefix + key
            if key not in ref:
                unknown.append(here)
                continue
            if isinstance(value, dict) and isinstance(ref[key], dict):
                walk(value, ref[key], here + ".")

    walk(raw, DEFAULTS, "")
    for (section, key), allowed in ENUMS.items():
        value = raw.get(section, {}).get(key) if isinstance(raw.get(section), dict) else None
        if value is not None and value not in allowed:
            invalid.append("{}.{} must be one of {}".format(section, key, ", ".join(sorted(allowed))))
    return unknown, invalid


def default_json() -> str:
    import json
    return json.dumps(DEFAULTS, indent=2, ensure_ascii=False) + "\n"
