#!/usr/bin/env python3
"""PreToolUse(Bash): suggest a cap on a flood; never rewrite the command.

This rides `governance.advise`, not `governance.enabled`. It changes nothing
-- it offers a form and the model decides -- so it has no reason to wait
behind the flag that guards rewriting output.

**The suggestion** covers the families governance refuses to rewrite: a
truncated test failure or build error is worse than an expensive one, so those
get a note offering the capped form and the model decides. Once per family per
session, because a note on every test run costs more context than it saves.

What this hook deliberately does *not* do is rewrite the command. Returning
`updatedInput` rides `hookSpecificOutput` and can auto-approve the call, so the
user would approve one command and a different one would run. Governance
happens in `post_bash`, on the real output, where the harness's documented
post-tool replacement shape changes what the model sees without touching the
permission gate.

The commit nudge that used to live here went with the pages in
There is nothing left for a commit to have made stale.
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
    """Offer the capped form for a family governance must not rewrite."""
    from siftlib import govern, session as session_mod

    command = str(h.tool_input().get("command") or "")
    if not h.cfg.governance.get("advise") or not command or h.subagent:
        return None
    text = govern.suggestion(command)
    if not text:
        return None
    family = govern.classify(command)
    said = {"before": False}

    def change(state: Dict[str, Any]) -> None:
        told = state.setdefault("cap_suggested", {})
        said["before"] = bool(told.get(family))
        told[family] = 1

    session_mod.mutate(h.ctx, h.session_id, change)
    if said["before"]:
        return None
    return _common.additional_context("PreToolUse", text)


if __name__ == "__main__":
    _common.run("pre_bash", main)
