#!/usr/bin/env python3
"""PreToolUse(Read): describe the file before it is read, and catch re-reads.

The point is not to stop reading; it is to make the read cheaper - a one-line
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
    from siftlib import gitutil, ledger, scan as scan_mod, session as session_mod
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

    # `ctx.contains`, not `startswith("..")`: `rel()` hands back the absolute
    # path for a file outside the root, so without this a Read of
    # `/tmp/scratch.md` was filed in `files_read` under its machine path and
    # logged an `index_miss` about a file this repo has never had. `post_read`
    # was fixed for exactly this in 0.4.0; this hook was missed.
    if not h.ctx.contains(rel):
        return None

    # A ranged read is already the behaviour we are trying to encourage.
    # `post_read` records it, not this hook: the saving is the whole file
    # minus what the range actually returned, and only one of those two
    # numbers is known before the read happens. Ranged means it returns less
    # than the file, judged by `is_whole_read` for both hooks: `offset: 0` and
    # a limit past the last line used to count as ranges here, which let a
    # whole-file read walk round the refusal below on its first attempt.
    if not _common.is_whole_read(tool_input, _line_count(h, raw_path, rel, tool_input)):
        return None

    scan = util.read_json(h.ctx.scan_json, default={}) or {}
    rec = dict((scan.get("files") or {}).get(rel) or {})
    # The scan is a steering cache, not proof that the worktree is unchanged.
    # Duplicate denial must compare the content about to be read with the
    # content previously seen, including edits made since the last scan.
    if rec:
        current_blob = gitutil.short_blob(gitutil.hash_object(h.ctx.root, rel))
        if current_blob:
            rec["blob"] = current_blob
    desc = (scan_mod.load_descriptions(h.ctx).get(rel) or {}).get("desc", "")
    tokens = int(rec.get("tokens", 0) or 0)

    if rec or desc:
        ledger.record(h.ctx, h.session_id, "index_hit", tokens)
    else:
        # A miss carries why, so a spike can be told apart from the transcript:
        # `absent` is a path the model guessed at that is not on disk, `stale`
        # is a real file the scan has not indexed (a new or never-scanned file,
        # the only kind that points at the index rather than the read). The
        # path rides along too; it is inside the repo (line 46 returned already
        # for anything outside), so it is repo-relative and safe to keep.
        try:
            exists = (h.ctx.root / rel).exists()
        except OSError:
            exists = False
        ledger.record(h.ctx, h.session_id, "index_miss", tokens,
                      path=rel, reason="stale" if exists else "absent")

    mode = str(h.cfg.get("hooks", "duplicate_read_mode", default="warn"))
    syms = rec.get("symbols") or []
    bits = ["{} - {}".format(rel, desc) if desc else rel]
    bits.append("(~{} tok).".format(tokens) if tokens else "(not indexed - `sift scan`).")
    if syms and rec.get("blob"):
        bits.append("Symbols: {} - prefer offset/limit.".format(sym_mod.format_hint(syms)))

    # A whole-file read of a big file, refused once and handed its ranges.
    # This is where the tokens went on the first real sessions: the Read tool
    # flooded five times as often as Bash, after this hook had offered the
    # ranges and been ignored. The refusal is honest where a rewrite would not
    # be -- the model knows it did not get the file -- and the second attempt
    # goes through, so a file that is genuinely needed whole costs one turn.
    big_mode = str(h.cfg.get("hooks", "big_read_mode", default="off"))
    big_threshold = int(h.cfg.get("hooks", "big_read_tokens", default=2000) or 2000)
    size_tokens = tokens or _size_tokens(h, raw_path, rel)
    big = (big_mode == "deny" and not h.subagent and size_tokens >= big_threshold)

    out: Dict[str, Any] = {"deny": False, "text": None, "event": None, "big": False}

    # One locked read-modify-write for everything that touches the session.
    # `load` then `save` was a lost update: Claude Code batches tool calls, so
    # a Read next to a Bash `cat` in the same block meant `post_bash`
    # registered its file under the lock and this hook wrote back its stale
    # copy over the top. Parallel Reads erased each other the same way. That is
    # what `mutate` is for (OpenWolf #83).
    def change(state: Dict[str, Any]) -> None:
        seen = (state.get("files_read") or {}).get(rel)
        # A compacted entry is not a duplicate in either direction: the content
        # was evicted, so "already read and has not changed" is false and the
        # warning used to say it anyway -- only the deny branch checked. The
        # read below clears the flag, so the one after it is caught again.
        duplicate = (bool(seen) and not seen.get("ranged", True)
                     and not seen.get("compacted")
                     and seen.get("blob") == rec.get("blob") and rec.get("blob"))
        if duplicate and not h.subagent and mode != "off":
            if mode == "deny" and tokens > 0 and not seen.get("denied_once"):
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
        # Kept apart from `files_read`: a refusal is not a read, and a
        # placeholder entry there would be counted as one by session_end and
        # compared as one by both duplicate checks.
        refused = state.setdefault("big_read_refused", {})
        if big and not refused.get(rel):
            refused[rel] = True
            out["deny"] = True
            out["big"] = True
            out["event"] = "big_read_denied"
            return
        _record_read(state, rel, rec, tokens)
        if not desc and not syms:
            return
        _offer(h, state, out, _common.PREFIX + " ".join(bits))

    session_mod.mutate(h.ctx, h.session_id, change)

    if out["event"]:
        # A refusal's size is recorded for the count and the JSON; the ledger
        # does not credit it to `tokens avoided`. What the refusal saves is
        # realised by the ranged reads that follow it, and `post_read` credits
        # those, so crediting the refusal as well booked the same file twice.
        ledger.record(h.ctx, h.session_id, out["event"],
                      size_tokens if out["big"] else tokens, path=rel,
                      at_call=h.tool_call_index())
    if out["big"]:
        if syms:
            how = "Symbols: {}. Read the part you need with offset/limit".format(
                sym_mod.format_hint(syms, 8))
        else:
            how = "Read it with offset/limit in windows of about 200 lines"
        return _denied(h, ledger, _common.PREFIX + (
            "{} is ~{} tok and this would read all of it into the conversation "
            "for the rest of the session. {}; if you need all of it, take it in "
            "a few windows.".format(rel, size_tokens, how)))
    if out["deny"]:
        return _denied(h, ledger, _common.PREFIX + (
            "{} is already in this conversation, unchanged (~{} tok). Scroll back rather "
            "than re-reading; if you need a specific part, read it with offset/limit."
            .format(rel, tokens)))
    if out["text"]:
        return _common.additional_context(EVENT, out["text"])
    return None


def _denied(h: "_common.HookCtx", ledger, reason: str) -> Dict[str, Any]:
    """A refusal, with its reason counted as context sift added.

    The reason reaches the model as the tool's result, so it is context this
    tool injected like any hint, and it went unrecorded. Recorded, not
    `charge`d: a refusal must never be silenced by the hint budget, and the
    budget must not be spent by it either.
    """
    from siftlib import util
    ledger.record(h.ctx, h.session_id, "injected", util.estimate_tokens(reason),
                  at_call=h.tool_call_index())
    return _common.deny(EVENT, reason)


def _line_count(h: "_common.HookCtx", raw_path: str, rel: str,
                tool_input: Dict[str, Any]) -> Optional[int]:
    """How many lines the file has, counted only when a `limit` was given --
    that is the one case `is_whole_read` needs it for -- so the ordinary read
    costs no extra disk access. None when it cannot be counted."""
    if tool_input.get("limit") is None:
        return None
    path = raw_path if os.path.isabs(raw_path) else str(h.ctx.root / rel)
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError:
        return None
    return data.count(b"\n") + (1 if data and not data.endswith(b"\n") else 0)


def _size_tokens(h: "_common.HookCtx", raw_path: str, rel: str) -> int:
    """A size for a file the scan has not measured: untracked, or not yet
    scanned. From the bytes on disk, with the scan's own per-extension divisor,
    so a big file is caught whether or not `sift scan` has run."""
    from siftlib import util
    path = raw_path if os.path.isabs(raw_path) else str(h.ctx.root / rel)
    try:
        size = os.path.getsize(path)
    except OSError:
        return 0
    return util.estimate_tokens_for_chars(size, os.path.splitext(rel)[1])


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
        "{} is ~{} tok. Read it in ranges and record as you go - `sift decide` "
        "or `sift bug add` per finding - rather than holding all of it at once."
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
    entry["ranged"] = False
    # The file is in the conversation again, so the compaction that evicted it
    # no longer excuses the next read. Nothing used to clear this, which
    # disarmed `deny` for that file for the rest of the session.
    entry["compacted"] = False
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
