#!/usr/bin/env python3
"""PreCompact: snapshot the session so SessionStart(source=compact) can re-inject it.

PreCompact cannot add context itself (PLAN changelog 2). All it can do is leave
a copy of what the session knew where the next SessionStart will find it.
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
    from siftlib import session as session_mod, util

    state = session_mod.load(h.ctx, h.session_id)
    state["precompact_trigger"] = str(h.payload.get("trigger") or "")
    state["precompact_at"] = util.now_iso()
    util.write_json(h.ctx.precompact / (h.session_id + ".json"), state)
    session_mod.save(h.ctx, state)
    return None


if __name__ == "__main__":
    _common.run("pre_compact", main)
