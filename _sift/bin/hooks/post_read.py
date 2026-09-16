#!/usr/bin/env python3
"""PostToolUse(Read): record what the read actually returned.

Until this existed, every read token in the ledger was an estimate from the
scan -- file size divided by a constant, recorded before the read happened.
The duplicate check then compared a guess against a guess, and the ledger
reported sizes nobody had measured.

What makes this worth a hook of its own is the shape of the payload. The tool
result arrives in `tool_response`, whose form depends on the tool and the
harness version: a plain string, a list of content blocks, or an object with
`content` or `file.content`. OpenWolf's equivalent read a `tool_output` field
that never existed in Claude Code's payload, so its read-token tracking was
silently zero for every session -- which is the failure this one is written to
avoid. Every shape is handled, and an unrecognised one records nothing rather
than a zero that looks like a measurement.
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
    from siftlib import govern, ledger, session as session_mod, util

    path = str(h.tool_input().get("file_path") or h.tool_input().get("path") or "")
    if not path:
        return None
    text = extract(h.payload.get("tool_response"))
    if text is None:
        return None

    try:
        rel = h.ctx.rel(_abs(h, path))
    except (OSError, ValueError):
        return None
    # `ctx.contains`, not `startswith("..")`: `rel()` returns the absolute
    # path when the file is outside the root, so the old test was false for
    # every out-of-repo read and /tmp paths landed in `files_read`.
    if not h.ctx.contains(rel) or h.ctx.is_sift_path(rel):
        return None

    tokens = govern.estimate_tokens(text)
    ranged = bool(h.tool_input().get("offset") or h.tool_input().get("limit"))

    def change(state: Dict[str, Any]) -> None:
        entry = (state.get("files_read") or {}).get(rel) or {"count": 1}
        # The measured size replaces the estimate, except for a window: the
        # tokens of a ranged read are not the tokens of the file, and writing
        # them here would let the next full read be refused as a duplicate of
        # something the model never saw whole.
        if not ranged:
            entry["tokens"] = tokens
            entry["ranged"] = False
            entry["measured"] = True
        else:
            entry["ranged"] = bool(entry.get("ranged", True))
        state.setdefault("files_read", {})[rel] = entry

    session_mod.mutate(h.ctx, h.session_id, change)

    # What a ranged read saved, measured rather than assumed: the whole file
    # as the scan sized it, minus what the window actually returned. This used
    # to be a hardcoded 400 recorded by `pre_read`, which could not know
    # either number. A file the scan has never seen contributes nothing --
    # zero is honest where a constant was not.
    if ranged:
        scan = util.read_json(h.ctx.scan_json, default={}) or {}
        whole = int(((scan.get("files") or {}).get(rel) or {}).get("tokens", 0) or 0)
        ledger.record(h.ctx, h.session_id, "ranged_steered",
                      max(0, whole - tokens), at_call=h.tool_call_index(),
                      whole_tokens=whole, read_tokens=tokens)
    return None


def _abs(h: "_common.HookCtx", path: str) -> "Any":
    from pathlib import Path
    p = Path(path)
    return p if p.is_absolute() else (h.ctx.root / p)


def extract(response: Any) -> Optional[str]:
    """The text a Read returned, across every payload shape, or None."""
    if isinstance(response, str):
        return response
    if isinstance(response, list):
        parts = [b.get("text", "") for b in response
                 if isinstance(b, dict) and isinstance(b.get("text"), str)]
        return "".join(parts) if parts else None
    if isinstance(response, dict):
        for key in ("content", "text", "stdout"):
            value = response.get(key)
            if isinstance(value, str):
                return value
            if isinstance(value, list):
                nested = extract(value)
                if nested is not None:
                    return nested
        file_block = response.get("file")
        if isinstance(file_block, dict):
            content = file_block.get("content")
            if isinstance(content, str):
                return content
    return None


if __name__ == "__main__":
    _common.run("post_read", main)
