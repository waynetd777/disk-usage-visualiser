"""Hook session state and the injection budget.

State lives in `.cache/sessions/<session_id>.json` (BUILD-SPEC 7.8) and is
always safe to delete. Every accessor tolerates a missing or corrupt file:
a hook that cannot read its own state must still exit 0 with empty stdout.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from . import ledger, util
from .config import Config
from .paths import Ctx

SESSION_TTL_DAYS = 7


def blank(session_id: str, source: str = "startup") -> Dict[str, Any]:
    return {
        "session_id": session_id,
        "started": util.now_iso(),
        "source": source,
        "files_read": {},
        "files_edited": [],
        "uncovered_edited": [],
        "commits": [],
        "nudged": {"commit": 0, "stop": 0, "pages": {}},
        "injected_tokens": 0,
        "stop_count": 0,
    }


def path_for(ctx: Ctx, session_id: str) -> "Any":
    safe = "".join(c if (c.isalnum() or c in "-_") else "_" for c in session_id)[:120] or "unknown"
    return ctx.sessions / (safe + ".json")


def load(ctx: Ctx, session_id: str, source: str = "startup") -> Dict[str, Any]:
    data = util.read_json(path_for(ctx, session_id), default=None)
    if not isinstance(data, dict) or "files_read" not in data:
        return blank(session_id, source)
    base = blank(session_id, data.get("source", source))
    base.update(data)
    base.setdefault("nudged", {"commit": 0, "stop": 0, "pages": {}})
    base["nudged"].setdefault("pages", {})
    return base


def save(ctx: Ctx, state: Dict[str, Any]) -> None:
    if util.readonly():
        return
    try:
        util.write_json(path_for(ctx, state.get("session_id", "unknown")), state)
    except OSError:
        pass


LOCK_BUDGET_MS = 250
LOCK_STALE_S = 30


def mutate(ctx: Ctx, session_id: str, change) -> Dict[str, Any]:
    """Read-modify-write the session under a lock, and return the new state.

    `load` then `save` is safe for a hook that fires once per turn, and wrong
    for one that fires per Bash call: several run in parallel, each reads the
    same state, and the last writer erases the others' registrations. That is
    OpenWolf's issue #83, and it is the reason the read tracking added for the
    Bash channel goes through here rather than through `save`.

    Lock-free on failure rather than blocking: if the lock cannot be taken
    inside the budget the change is applied unlocked, because a hook that
    stalls a session is worse than a registration that is occasionally lost.
    """
    import os
    import time

    lock = path_for(ctx, session_id).with_suffix(".lock")
    held = False
    if not util.readonly():
        deadline = time.time() + LOCK_BUDGET_MS / 1000.0
        while time.time() < deadline:
            try:
                lock.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.close(fd)
                held = True
                break
            except FileExistsError:
                try:
                    if time.time() - lock.stat().st_mtime > LOCK_STALE_S:
                        lock.unlink()  # a crashed hook must not wedge a session
                        continue
                except OSError:
                    pass
                time.sleep(0.01)
            except OSError:
                break
    try:
        state = load(ctx, session_id)
        change(state)
        save(ctx, state)
        return state
    finally:
        if held:
            try:
                lock.unlink()
            except OSError:
                pass


def sweep(ctx: Ctx) -> int:
    """Delete session files older than the TTL. Housekeeping at SessionEnd."""
    import time
    removed = 0
    if not ctx.sessions.is_dir():
        return 0
    cutoff = time.time() - SESSION_TTL_DAYS * 86400
    for path in ctx.sessions.glob("*.json"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError:
            pass
    return removed


# ---------------------------------------------------------------------------
# Injection budget
# ---------------------------------------------------------------------------

def budget_allows(state: Dict[str, Any], cfg: Config, text: str) -> bool:
    limit = int(cfg.get("hooks", "session_budget_tokens", default=2000))
    cost = util.estimate_tokens(text)
    return int(state.get("injected_tokens", 0)) + cost <= limit


def charge(ctx: Ctx, state: Dict[str, Any], text: str) -> None:
    cost = util.estimate_tokens(text)
    state["injected_tokens"] = int(state.get("injected_tokens", 0)) + cost
    ledger.record(ctx, state.get("session_id", ""), "injected", cost,
                  at_call=int(state.get("tool_calls", 0) or 0))


# ---------------------------------------------------------------------------
# Shared nudge text (pre_bash and stop say the same thing, phrased for when)
# ---------------------------------------------------------------------------
