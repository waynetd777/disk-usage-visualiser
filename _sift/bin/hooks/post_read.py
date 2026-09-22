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
import re
import sys

# `python3 -I` drops the script's own directory from sys.path, so the sibling
# `_common` import below has to be made possible by hand.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _common


def main(h: "_common.HookCtx") -> Optional[Dict[str, Any]]:
    from siftlib import ledger, session as session_mod, util

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

    # Measured in the scan's unit, so that it can be set against the scan's
    # size of the whole file: the line-number prefix the Read tool adds is
    # stripped, and the file's own per-extension divisor is used. Measured
    # raw at chars/3.5, the same 3,023-token file came back as 4,623, so the
    # floods were counted half again too large and a window could outweigh
    # the file it was cut from.
    tokens = util.estimate_tokens(strip_line_numbers(text), os.path.splitext(rel)[1])
    scan = util.read_json(h.ctx.scan_json, default={}) or {}
    whole = int(((scan.get("files") or {}).get(rel) or {}).get("tokens", 0) or 0)
    # Whole by what was asked for -- judged the one way `pre_read` judges it,
    # so `offset: 0` is a whole read in both hooks -- or by what came back:
    # a "range" that returned the file is the file.
    whole_read = (_common.is_whole_read(h.tool_input(), _line_count(h, path, rel))
                  or (whole > 0 and tokens >= whole))
    at_call = h.tool_call_index()
    credit = {"delta": 0, "record": False}

    def change(state: Dict[str, Any]) -> None:
        entry = (state.get("files_read") or {}).get(rel) or {"count": 1}
        # Compaction evicted whatever windows of this file were in context, so
        # the coverage they built starts again from nothing.
        covered = 0 if entry.get("compacted") else int(entry.get("window_tokens", 0) or 0)
        # What the ranged reads so far have been credited with, in total: the
        # file less the windows, and nothing at all until there is a window.
        # Each read records the change in that figure, so the ledger's sum for
        # the file is `whole - sum(windows)` however many windows it took --
        # not `whole - window` per window, which credited a file read in two
        # halves with the whole of itself while every line of it entered.
        before = max(0, whole - covered) if covered else 0
        whole_in_context = bool(entry.get("measured")) and not entry.get("ranged", True) \
            and not entry.get("compacted")
        if whole_read:
            # The measured size replaces the estimate. A whole read after the
            # windows were credited supersedes them: the file is in context
            # entire, so what they were credited with is taken back.
            entry["tokens"] = tokens
            entry["ranged"] = False
            entry["measured"] = True
            entry["compacted"] = False
            entry["window_tokens"] = 0
            if before:
                credit["delta"] = -before
                credit["record"] = True
        else:
            entry["ranged"] = bool(entry.get("ranged", True))
            entry["compacted"] = False
            if not whole_in_context:
                entry["window_tokens"] = covered + tokens
                after = max(0, whole - covered - tokens)
                credit["delta"] = after - before
            # A window of a file already in context whole saves nothing; it
            # is recorded so the count is honest, with a zero credit.
            credit["record"] = True
        state.setdefault("files_read", {})[rel] = entry

    session_mod.mutate(h.ctx, h.session_id, change)

    # What a ranged read saved, measured rather than assumed: the whole file
    # as the scan sized it, minus what the windows actually returned. This used
    # to be a hardcoded 400 recorded by `pre_read`, which could not know
    # either number. A file the scan has never seen contributes nothing --
    # zero is honest where a constant was not. The row carries the path so the
    # summary can fold a file's windows together, and `tokens` is the change
    # in the file's credit, which can be negative (see `change`).
    if credit["record"]:
        ledger.record(h.ctx, h.session_id, "ranged_steered", credit["delta"],
                      at_call=at_call, path=rel, whole_tokens=whole,
                      read_tokens=tokens, whole_read=whole_read)
    if not whole_read:
        return None

    # A whole-file read over the threshold is a flood by the same definition
    # governance uses for Bash, and it is counted the same way -- measured from
    # what came back, whether or not `big_read_mode` would have refused it.
    # This is the count that watches whether the default keeps paying off.
    threshold = int(h.cfg.get("hooks", "big_read_tokens", default=2000) or 2000)
    if tokens < threshold:
        return None
    deny_on = str(h.cfg.get("hooks", "big_read_mode", default="off")) == "deny"
    ledger.record(h.ctx, h.session_id, "read_flood", tokens,
                  at_call=at_call, path=rel, denied_first=deny_on)
    if deny_on or h.subagent:
        return None
    return _flood_note(h, session_mod, tokens)


# The same bar `post_bash` sets before asking about governance: one big read
# is a file somebody needed, three in a session is a habit worth a question.
FLOODS_BEFORE_ASKING = 3


def _flood_note(h: "_common.HookCtx", session_mod, tokens: int) -> Optional[Dict[str, Any]]:
    """Ask the model to put `big_read_mode` to the person, once per session.

    Reached only when the mode has been turned off (it is on by default), and
    the same shape as the governance note: the floods it would have caught
    should stay visible so someone can decide to turn it back on. One
    difference -- the person is told too, in a line, because `additionalContext`
    never reaches them and an agent waits to be asked.
    """
    seen = {"count": 0, "tokens": 0, "asked": False}

    def change(state: Dict[str, Any]) -> None:
        state["read_floods_seen"] = int(state.get("read_floods_seen", 0)) + 1
        state["read_flood_tokens"] = int(state.get("read_flood_tokens", 0)) + tokens
        seen["count"] = state["read_floods_seen"]
        seen["tokens"] = state["read_flood_tokens"]
        seen["asked"] = bool(state.get("read_flood_asked"))
        if seen["count"] >= FLOODS_BEFORE_ASKING and not seen["asked"]:
            state["read_flood_asked"] = True

    session_mod.mutate(h.ctx, h.session_id, change)
    if seen["asked"] or seen["count"] < FLOODS_BEFORE_ASKING:
        return None
    text = _common.PREFIX + (
        "{} whole-file reads this session came back over {:,} tokens (~{:,} "
        "tokens in total), and every line of that stays in context for the rest "
        "of the session. `hooks.big_read_mode: deny` in {}/config.json would "
        "refuse the first whole read of a file that size and hand back its "
        "symbol ranges instead; the second attempt goes through. It is on by "
        "default but has been turned off in this repo. Ask the user whether to "
        "turn it back on, and do not turn it on yourself.".format(
            seen["count"], int(h.cfg.get("hooks", "big_read_tokens", default=2000) or 2000),
            seen["tokens"], h.ctx.dir_name))
    user = ("sift: {} whole-file reads over the threshold this session (~{:,} tokens). "
            "big-read refusal is off in this repo; `hooks.big_read_mode: deny` in "
            "{}/config.json would turn those into ranged reads. Say so if you want it back on.".format(
                seen["count"], seen["tokens"], h.ctx.dir_name))
    return _common.additional_context("PostToolUse", text, user)


def _abs(h: "_common.HookCtx", path: str) -> "Any":
    from pathlib import Path
    p = Path(path)
    return p if p.is_absolute() else (h.ctx.root / p)


_LINE_NUMBER_RE = re.compile(r"^\s*\d+\t", re.M)


def strip_line_numbers(text: str) -> str:
    """The Read tool returns `cat -n` style lines, `     1\\tcontent`. The
    prefix is the tool's, not the file's, and the scan never saw it."""
    return _LINE_NUMBER_RE.sub("", text)


def _line_count(h: "_common.HookCtx", raw_path: str, rel: str) -> Optional[int]:
    """The file's line count, for `is_whole_read`'s limit check; counted only
    when a limit was given, so the ordinary read costs no extra disk access."""
    if h.tool_input().get("limit") is None:
        return None
    try:
        with open(str(_abs(h, raw_path)), "rb") as fh:
            data = fh.read()
    except OSError:
        return None
    return data.count(b"\n") + (1 if data and not data.endswith(b"\n") else 0)


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
