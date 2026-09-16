#!/usr/bin/env python3
"""PostToolUse(any): re-state the rules that must not be forgotten mid-session.

Instruction compliance decays *within* a session — the only structural effect a
factorial study of the question found (arXiv 2605.10039, 1,650 sessions), at
roughly 5.6% lower odds per generated function, with file size showing no
effect. The remedy is therefore cadence rather than shorter rule files: bring
the few rules that matter back, periodically, as short factual statements.

This costs about a hundred tokens each firing and saves none, which is why it
was nearly left out. That was the wrong test. The rules it repeats are the
privacy ones, and they are the only content in this sift directory the eval could show a
model does not reconstruct on its own (`q5`, scoring zero of four without it).
Their failure mode is also the one failure here that cannot be undone: a
committed secret has to be rotated, a laptop path in a shared repo is a small
leak, and a sentence about a colleague published by mistake is a disclosure you
cannot take back. A wasted token is cheap; those are not.

Read from `conventions.md` rather than hardcoded, so a repo that rewrites its
own rules gets its own rules repeated back.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

import os
import sys

# `python3 -I` drops the script's own directory from sys.path, so the sibling
# `_common` import below has to be made possible by hand.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _common

DEFAULT_INTERVAL = 25
SECTION_RE = re.compile(r"^##+\s*What never goes in\s*$", re.M)
BULLET_RE = re.compile(r"^\s*[-*]\s+(.+?)\s*$", re.M)
MAX_RULES = 4


def main(h: "_common.HookCtx") -> Optional[Dict[str, Any]]:
    from siftlib import session as session_mod

    interval = int(h.cfg.get("context", "reinjection_interval",
                             default=DEFAULT_INTERVAL) or 0)
    if h.subagent:
        return None

    due = {"now": False}
    edited = _edited_paths(h)

    def change(state: Dict[str, Any]) -> None:
        if edited:
            files = state.setdefault("files_edited", [])
            for path in edited:
                if path not in files:
                    files.append(path)
        count = int(state.get("tool_calls", 0) or 0) + 1
        state["tool_calls"] = count
        last = int(state.get("rules_reinjected_at", 0) or 0)
        if interval > 0 and count - last >= interval:
            state["rules_reinjected_at"] = count
            due["now"] = True

    session_mod.mutate(h.ctx, h.session_id, change)
    if not due["now"]:
        return None

    rules = top_rules(h.ctx)
    if not rules:
        return None
    text = _common.PREFIX + "a reminder of what never goes in {}/: {}".format(
        h.ctx.dir_name, " ".join("{}.".format(r.rstrip(".")) for r in rules))
    # Deliberately not budget-checked. The budget exists to stop injection
    # crowding out a conversation, and this is the one injection whose absence
    # costs more than its presence.
    #
    # Charged under the lock, not with `save(state)`: that wrote back the
    # snapshot taken before the first lock was released, erasing whatever a
    # concurrent `post_bash` or `post_read` had landed in between.
    session_mod.mutate(h.ctx, h.session_id,
                       lambda s: session_mod.charge(h.ctx, s, text))
    return _common.additional_context("PostToolUse", text)


def _edited_paths(h: "_common.HookCtx") -> List[str]:
    """Repo-relative paths changed by Claude editing tools or Codex apply_patch."""
    tool_name = str(h.payload.get("tool_name") or "")
    if tool_name not in ("Edit", "Write", "NotebookEdit", "apply_patch"):
        return []
    tool_input = h.tool_input()
    raw_paths: List[str] = []
    direct = str(tool_input.get("file_path") or tool_input.get("path")
                 or tool_input.get("notebook_path") or "")
    if direct:
        raw_paths.append(direct)
    if tool_name == "apply_patch":
        command = str(tool_input.get("command") or "")
        raw_paths.extend(re.findall(
            r"^\*\*\* (?:Add|Update|Delete) File: (.+?)\s*$",
            command, re.MULTILINE))
        raw_paths.extend(re.findall(r"^\*\*\* Move to: (.+?)\s*$",
                                    command, re.MULTILINE))
    out: List[str] = []
    for raw in raw_paths:
        rel = h.rel(raw)
        if h.ctx.contains(rel) and not h.ctx.is_sift_path(rel) and rel not in out:
            out.append(rel)
    return out


def top_rules(ctx: "Any", limit: int = MAX_RULES) -> List[str]:
    """The bullets under *What never goes in* in this repo's conventions."""
    from siftlib import util

    text = util.read_text(ctx.dir / "conventions.md")
    if not text:
        return []
    match = SECTION_RE.search(text)
    if not match:
        return []
    rest = text[match.end():]
    nxt = re.search(r"^##+\s", rest, re.M)
    section = rest[:nxt.start()] if nxt else rest
    rules = [_plain(b) for b in BULLET_RE.findall(section)]
    return [r for r in rules if r][:limit]


def _plain(bullet: str) -> str:
    """Strip the markdown: this is injected as a sentence, not rendered."""
    out = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", bullet)
    out = out.replace("**", "").replace("`", "").replace("*", "")
    return " ".join(out.split())


if __name__ == "__main__":
    _common.run("post_tool", main)
