#!/usr/bin/env python3
"""PreToolUse(Read): describe the file before it is read, and catch re-reads.

The point is not to stop reading; it is to make the read cheaper — a one-line
description plus symbol ranges usually turns a whole-file read into an
offset/limit read.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

import os
import sys

# `python3 -I` drops the script's own directory from sys.path, so the sibling
# `_common` import below has to be made possible by hand.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _common

EVENT = "PreToolUse"


def main(h: "_common.HookCtx") -> Optional[Dict[str, Any]]:
    from siftlib import ledger, scan as scan_mod, session as session_mod
    from siftlib import symbols as sym_mod, util

    tool_input = h.tool_input()
    raw_path = str(tool_input.get("file_path") or "")
    if not raw_path:
        return None
    rel = h.rel(raw_path)
    if h.ctx.is_sift_path(rel):
        # Describing this tool's own files to the agent reading them is noise,
        # with one exception: an import can leave a very large document in
        # `local/`, and the tool asks the agent to go and read it. Saying how
        # big it is, before it lands whole in the conversation, is the one
        # thing worth saying about a file in here.
        return _oversized_sift_file(h, rel, raw_path, tool_input)

    # A ranged read is already the behaviour we are trying to encourage.
    # `post_read` records it, not this hook: the saving is the whole file
    # minus what the range actually returned, and only one of those two
    # numbers is known before the read happens.
    if tool_input.get("offset") is not None or tool_input.get("limit") is not None:
        return None

    scan = util.read_json(h.ctx.scan_json, default={}) or {}
    rec = (scan.get("files") or {}).get(rel) or {}
    desc = (scan_mod.load_descriptions(h.ctx).get(rel) or {}).get("desc", "")
    tokens = int(rec.get("tokens", 0) or 0)

    ledger.record(h.ctx, h.session_id, "index_hit" if (rec or desc) else "index_miss", tokens)

    mode = str(h.cfg.get("hooks", "duplicate_read_mode", default="warn"))
    syms = rec.get("symbols") or []
    bits = ["{} — {}".format(rel, desc) if desc else rel]
    bits.append("(~{} tok).".format(tokens) if tokens else "(not indexed — `sift scan`).")
    if syms and rec.get("blob"):
        bits.append("Symbols: {} — prefer offset/limit.".format(sym_mod.format_hint(syms)))

    out: Dict[str, Any] = {"deny": False, "text": None, "event": None}

    # One locked read-modify-write for everything that touches the session.
    # `load` then `save` was a lost update: Claude Code batches tool calls, so
    # a Read next to a Bash `cat` in the same block meant `post_bash`
    # registered its file under the lock and this hook wrote back its stale
    # copy over the top. Parallel Reads erased each other the same way. That is
    # what `mutate` is for (OpenWolf #83).
    def change(state: Dict[str, Any]) -> None:
        seen = (state.get("files_read") or {}).get(rel)
        duplicate = bool(seen) and seen.get("blob") == rec.get("blob") and rec.get("blob")
        if duplicate and not h.subagent and mode != "off":
            if (mode == "deny" and tokens > 0 and not seen.get("denied_once")
                    and not seen.get("compacted")):
                seen["denied_once"] = True
                state["files_read"][rel] = seen
                out["deny"] = True
                out["event"] = "dup_denied"
                return
            out["event"] = "dup_warned"
            _record_read(state, rel, rec, tokens)
            _offer(h, state, out, _common.PREFIX + (
                "{} was already read in this session and has not changed "
                "(~{} tok).".format(rel, tokens)))
            return
        _record_read(state, rel, rec, tokens)
        if not desc and not syms:
            return
        _offer(h, state, out, _common.PREFIX + " ".join(bits))

    session_mod.mutate(h.ctx, h.session_id, change)

    if out["event"]:
        ledger.record(h.ctx, h.session_id, out["event"], tokens)
    if out["deny"]:
        return _common.deny(EVENT, _common.PREFIX + (
            "{} is already in this conversation, unchanged (~{} tok). Scroll back rather "
            "than re-reading; if you need a specific part, read it with offset/limit."
            .format(rel, tokens)))
    if out["text"]:
        return _common.additional_context(EVENT, out["text"])
    return None


def _oversized_sift_file(h: "_common.HookCtx", rel: str, raw_path: str,
                        tool_input: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    from siftlib import session as session_mod

    if tool_input.get("offset") is not None or tool_input.get("limit") is not None:
        return None
    try:
        size = os.path.getsize(raw_path)
    except OSError:
        return None
    limit = int(h.cfg.get("governance", "threshold_tokens", default=2000) or 2000) * 4
    if size < limit:
        return None
    text = _common.PREFIX + (
        "{} is ~{} tok. Read it in ranges and record as you go — `sift decide` "
        "or `sift bug add` per finding — rather than holding all of it at once."
        .format(rel, size // 4))
    out: Dict[str, Any] = {"text": None}
    session_mod.mutate(h.ctx, h.session_id,
                       lambda state: _offer(h, state, out, text))
    if out["text"]:
        return _common.additional_context(EVENT, out["text"])
    return None


def _record_read(state: Dict[str, Any], rel: str, rec: Dict[str, Any], tokens: int) -> None:
    entry = (state.setdefault("files_read", {})).get(rel) or {
        "count": 0, "tokens": tokens, "blob": rec.get("blob", ""),
        "denied_once": False, "compacted": False}
    entry["count"] = int(entry.get("count", 0)) + 1
    entry["tokens"] = tokens or entry.get("tokens", 0)
    entry["blob"] = rec.get("blob", entry.get("blob", ""))
    state["files_read"][rel] = entry


def _offer(h: "_common.HookCtx", state: Dict[str, Any], out: Dict[str, Any],
           text: str) -> None:
    """Charge the injection budget inside the lock, or say nothing."""
    from siftlib import session as session_mod
    if not session_mod.budget_allows(state, h.cfg, text):
        return
    session_mod.charge(h.ctx, state, text)
    out["text"] = text


if __name__ == "__main__":
    _common.run("pre_read", main)
