#!/usr/bin/env python3
"""SessionEnd: one journal line about the session, then housekeeping."""
from __future__ import annotations

from typing import Any, Dict, Optional

import os
import sys

# `python3 -I` drops the script's own directory from sys.path, so the sibling
# `_common` import below has to be made possible by hand.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _common


def main(h: "_common.HookCtx") -> Optional[Dict[str, Any]]:
    from siftlib import journal, session as session_mod

    state = session_mod.load(h.ctx, h.session_id)
    reads = state.get("files_read") or {}
    edits = state.get("files_edited") or []

    if reads or edits:
        # The counts are the whole session; the file list is only what lives in
        # this repo. A read outside the root keeps its absolute path, and this
        # line is committed -- see `Ctx.contains`.
        inside = sorted({p for p in list(reads.keys()) + list(edits)
                         if h.ctx.contains(p)})
        journal.append(h.ctx, "session", "{} reads, {} edits".format(
            len(reads), len(edits)), files=inside[:50])

    session_mod.sweep(h.ctx)
    return None


if __name__ == "__main__":
    _common.run("session_end", main)
