#!/usr/bin/env python3
"""SessionStart: put a compaction snapshot back, and heal the git hooks.

It used to open every session with a status line - page count, staleness,
coverage. Removing the pages took with them the only thing that
line had to report; a session does not need to be told how many files are
described before it has been asked anything.

What remains is the half that was never about pages. PreCompact cannot inject
context (PLAN changelog 2), so it leaves a snapshot behind and this is where
that snapshot re-enters the conversation. Ruling 7 still applies: caches only,
because a fresh clone must not wait while five thousand files are scanned.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

import os
import sys

# `python3 -I` drops the script's own directory from sys.path, so the sibling
# `_common` import below has to be made possible by hand.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _common


def main(h: "_common.HookCtx") -> Optional[Dict[str, Any]]:
    from siftlib import gitutil, session as session_mod

    source = str(h.payload.get("source") or "startup")
    state = session_mod.load(h.ctx, h.session_id, source)
    state["source"] = source

    # A clone whose githooks are not wired up heals derived state on its own
    # once `core.hooksPath` points at them.
    if (h.ctx.root / ".githooks").is_dir() and not gitutil.config_get(h.ctx.root, "core.hooksPath"):
        gitutil.config_set(h.ctx.root, "core.hooksPath", ".githooks")

    if source != "compact":
        # An unfinished install -- and a runtime the clone has moved past --
        # are told to the one party that can do something about them. Once per
        # session, and each stops as soon as it is dealt with.
        text, for_user = _pending_text(h, state)
        if text:
            session_mod.charge(h.ctx, state, text)
            session_mod.save(h.ctx, state)
            return _common.additional_context("SessionStart", text, for_user)
        session_mod.save(h.ctx, state)
        return None

    for rec in (state.get("files_read") or {}).values():
        # Compaction evicted the contents, so a later re-read is not a
        # duplicate and must not be refused as one.
        rec["compacted"] = True
    digest = _precompact_digest(h, state)
    if not digest:
        session_mod.save(h.ctx, state)
        return None

    text = _common.PREFIX + digest
    if not session_mod.budget_allows(state, h.cfg, text):
        session_mod.save(h.ctx, state)
        return None
    session_mod.charge(h.ctx, state, text)
    session_mod.save(h.ctx, state)
    return _common.additional_context("SessionStart", text)


def _pending_text(h: "_common.HookCtx", state: Dict[str, Any]) -> "tuple[str, str]":
    """(what the agent is told, what the person is told).

    Two notices, one injection: an unfinished install and an available upgrade
    are independent, but a session should be interrupted once or not at all,
    and the budget is charged for what is actually sent.
    """
    from siftlib import followup as followup_mod

    try:
        items = followup_mod.pending(h.ctx, h.cfg)
        upgrade = followup_mod.upgrade_item(h.ctx, h.cfg)
    except Exception:  # noqa: BLE001 - a hook never fails the session
        return ("", "")
    parts = [followup_mod.agent_text(items), followup_mod.upgrade_agent_text(upgrade)]
    text = "\n\n".join(p for p in parts if p)
    if not text:
        return ("", "")
    text = _common.PREFIX + text
    if not session_budget_allows(h, state, text):
        return ("", "")
    said = [followup_mod.user_message(items), followup_mod.upgrade_user_message(upgrade)]
    return (text, " ".join(s for s in said if s))


def session_budget_allows(h: "_common.HookCtx", state: Dict[str, Any], text: str) -> bool:
    from siftlib import session as session_mod

    return session_mod.budget_allows(state, h.cfg, text)


def _precompact_digest(h: "_common.HookCtx", state: Dict[str, Any]) -> str:
    """PreCompact cannot inject; it snapshots. This puts the snapshot back."""
    from siftlib import util

    snapshot = util.read_json(h.ctx.precompact / (h.session_id + ".json"), default=None)
    source = snapshot if isinstance(snapshot, dict) else state
    edited = list(source.get("files_edited") or [])
    if not edited:
        return ""
    return "session in progress - files edited: {}".format(", ".join(edited[:8]))


if __name__ == "__main__":
    _common.run("session_start", main)
