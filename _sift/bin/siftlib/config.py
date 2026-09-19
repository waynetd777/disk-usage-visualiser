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
    # `advise` offers a capped form before a command whose family must never be
    # rewritten and counts every flood it sees. `enabled` replaces a flood with
    # a condensed form plus a pointer to the full text on disk.
    #
    # Both on by default (D-20260919-06). A backtest of 1,600 transcripts found
    # floods on 4.5% of Bash calls, ~2.5M tokens condensable, dominated by
    # `cat`/`sed` of a file rather than search. The full text is preserved, so
    # the cost is a later re-read (~25% of the trackable cases, usually a cheap
    # ranged one), not lost content.
    "governance": {
        "advise": True,
        "enabled": True,
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
        # A whole-file Read over `big_read_tokens` is where the tokens actually
        # go: the backtest behind D-20260919-06 found whole-file reads over the
        # threshold in 22% of every Read on disk, 10.7M tokens, over four times
        # the Bash floods. `deny` refuses the first whole read of such a file and
        # hands back the symbol ranges; the second attempt goes through, so a
        # model that really needs the whole file is delayed one turn, never
        # blocked, and nothing is ever dropped. On by default for that reason;
        # `read_flood` in the ledger counts the floods whichever way it is set.
        "big_read_mode": "deny",
        "big_read_tokens": 2000,
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
    ("hooks", "big_read_mode"): {"off", "deny"},
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
        """Bash output governance. On by default (D-20260919-06)."""
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


def lookup_default(dotted: str) -> Tuple[Any, Any]:
    """(path tuple, default value) for a dotted key, or (None, None) if it is
    not a known key. Only keys that exist in DEFAULTS can be set, so a typo is
    rejected rather than written -- the same stance `doctor` takes on read."""
    parts = dotted.split(".")
    node: Any = DEFAULTS
    for part in parts:
        if not isinstance(node, dict) or part not in node:
            return None, None
        node = node[part]
    return tuple(parts), node


def coerce_value(default: Any, raw: str) -> Tuple[Any, Any]:
    """Coerce a CLI string to the type of `default`. Returns (value, None) or
    (None, error). Structured values (dict/list) are not settable from the CLI:
    a list of extensions or hard-fail codes is edited in the file, not typed."""
    if isinstance(default, bool):
        low = raw.strip().lower()
        if low in ("true", "on", "yes", "1"):
            return True, None
        if low in ("false", "off", "no", "0"):
            return False, None
        return None, "expected true or false"
    if isinstance(default, int):
        try:
            return int(raw), None
        except ValueError:
            return None, "expected an integer"
    if isinstance(default, float):
        try:
            return float(raw), None
        except ValueError:
            return None, "expected a number"
    if isinstance(default, (dict, list)):
        return None, "structured value; edit config.json by hand"
    return raw, None


def enum_error(dotted: str, value: Any) -> Any:
    """An ENUM message if `value` is not allowed for `dotted`, else None."""
    parts = tuple(dotted.split("."))
    allowed = ENUMS.get(parts)
    if allowed is not None and value not in allowed:
        return "{} must be one of {}".format(dotted, ", ".join(sorted(allowed)))
    return None


def set_value(path: Path, dotted: str, value: Any) -> Any:
    """Write one key into config.json, preserving every other key and its
    order, and return an error string or None. The caller has already checked
    the key is known and the value is the right type and an allowed enum."""
    import json
    raw = util.read_json(path, default={})
    if not isinstance(raw, dict):
        raw = {}
    parts = dotted.split(".")
    node = raw
    for part in parts[:-1]:
        child = node.get(part)
        if not isinstance(child, dict):
            child = {}
            node[part] = child
        node = child
    node[parts[-1]] = value
    if not util.atomic_write(path, json.dumps(raw, indent=2, ensure_ascii=False) + "\n"):
        return "could not write config.json (readonly?)"
    return None
