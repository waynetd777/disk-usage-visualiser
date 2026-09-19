#!/usr/bin/env python3
"""PostToolUse(Bash): govern the output, and track what Bash read.

Two jobs, both about tokens, and neither can be done anywhere else.

**Governance.** Result replacement happens after the command runs: Claude Code
uses `updatedToolOutput`, while Codex uses its documented `continue: false`
and `stopReason` feedback. `PreToolUse` would have to rewrite the *command*,
which can auto-approve a call the user never saw. That is a permission bypass,
not an optimisation. On Claude, the response object is mirrored and only
`stdout` changes; `stderr` is never touched.

**Read tracking.** `pre_read` watches the Read tool, which is not where
duplicate reads happen: OpenWolf measured 140 of 144 of them arriving as
`cat`, `sed`, `head` or `tail` through Bash. Registering those here puts both
channels in one history, so a Bash `cat` followed by a Read of the same file is
caught, and so is the reverse. The write goes through `session.mutate`, under a
lock, because several Bash calls run in parallel and `load`-then-`save` loses
one of them (their issue #83).

"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

import os
import sys

# `python3 -I` drops the script's own directory from sys.path, so the sibling
# `_common` import below has to be made possible by hand.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _common

def main(h: "_common.HookCtx") -> Optional[Dict[str, Any]]:
    command = str(h.tool_input().get("command") or "")
    note = _track_read(h, command)
    governed, flood = _govern(h, command)
    # The flood note is two strings, not one: the model is asked to raise the
    # setting, and the person is told in a line of their own. Everything else
    # this hook says is for the model alone.
    flood_text, flood_user = flood if flood else (None, "")
    note = "\n".join(n for n in (note, flood_text) if n) or None
    if governed:
        if note:
            hook_out = governed.get("hookSpecificOutput")
            if isinstance(hook_out, dict):
                hook_out["additionalContext"] = note
            else:
                # Codex's shape carries no `additionalContext`: its condensed
                # text travels as `stopReason`, so a note left anywhere else
                # is dropped, and the duplicate-read warning was the one that
                # went missing whenever governance also fired.
                governed["stopReason"] = "\n\n".join(
                    p for p in (str(governed.get("stopReason") or ""), note) if p)
        if flood_user:
            # Top level, beside the rewrite rather than inside it: the person's
            # line is not part of what the model is handed back.
            governed["systemMessage"] = flood_user
        return governed
    if note:
        return _common.additional_context("PostToolUse", note, flood_user)
    return None


def _track_read(h: "_common.HookCtx", command: str) -> Optional[str]:
    """Register a Bash file read, and say so if the content is already here.

    A note rather than a block: the output exists by the time this runs, so
    the tokens are already spent. What it buys is the next read -- the entry
    lands in the same history `pre_read` checks, which can still deny.
    """
    from siftlib import bashread, gitutil, session as session_mod

    read = bashread.parse(command)
    if read is None:
        return None
    response = h.payload.get("tool_response")
    stdout = _stdout(response)
    if not isinstance(stdout, str) or not stdout:
        return None

    try:
        target = Path(read.path)
        if not target.is_absolute():
            target = h.ctx.root / target
        target = target.resolve()
        rel = str(target.relative_to(h.ctx.root.resolve()))
    except (OSError, ValueError):
        # Outside the repo. `cat ../other-project/secrets.ts` has no business
        # in this project's read history, which is how OpenWolf found the same
        # hole in its own bash channel.
        return None
    if h.ctx.is_sift_path(rel) or not target.is_file():
        return None

    # `short_blob`, not the raw sha: `pre_read` compares this against the
    # scan's blob, and the scan stores the truncated form.
    blob = gitutil.short_blob(gitutil.hash_object(h.ctx.root, rel))
    tokens = _estimate(stdout)
    seen = {}

    def change(state: Dict[str, Any]) -> None:
        seen.update((state.get("files_read") or {}).get(rel) or {})
        entry = dict(seen) or {}
        entry.setdefault("count", 0)
        entry["count"] = int(entry.get("count", 0)) + 1
        entry["blob"] = blob
        entry["via_bash"] = True
        # A window into a file is not the file: recording tokens for a ranged
        # read would let the next full read be refused as a duplicate of
        # something the model never saw whole.
        if read.full:
            entry["tokens"] = tokens
            entry["ranged"] = False
            # Printed in full again, so the compaction that evicted it is spent
            # and the next `cat` of it is a real duplicate once more.
            entry["compacted"] = False
        else:
            entry.setdefault("tokens", 0)
            # A window after a full read must not downgrade the entry: doing so
            # threw away the knowledge that the whole file is already in the
            # conversation, and the next `cat` of it went unremarked.
            entry["ranged"] = bool(entry.get("ranged", True))
        state.setdefault("files_read", {})[rel] = entry

    session_mod.mutate(h.ctx, h.session_id, change)

    # `compacted`: compaction evicted the output, so "already printed in full"
    # is a false statement about the conversation. This channel ignored the
    # flag entirely and made that claim after every compaction.
    unchanged = bool(seen) and seen.get("blob") == blob and blob
    if (read.full and unchanged and not seen.get("ranged", True)
            and not seen.get("compacted")
            and int(seen.get("tokens", 0) or 0) > 0):
        return (_common.PREFIX + "{} was already printed in full this session "
                "(~{} tok) and has not changed since.".format(
                    rel, seen.get("tokens")))
    return None


# How many floods in one session before the model is asked to raise it. One is
# noise -- a single `git show` of a big commit is not a pattern. Three in a
# session is a habit, and by then the tokens lost are worth a question.
FLOODS_BEFORE_ASKING = 3


def _estimate(text: str) -> int:
    from siftlib import govern
    return govern.estimate_tokens(text)


def _stdout(response: Any) -> Optional[str]:
    """Bash output across Claude Code and Codex hook payloads."""
    if isinstance(response, str):
        return response
    if isinstance(response, dict):
        for key in ("stdout", "output", "content"):
            value = response.get(key)
            if isinstance(value, str):
                return value
    return None


def _govern(h: "_common.HookCtx", command: str):
    """Returns `(rewrite, note)`, either of which may be None. The note is the
    `(model text, person line)` pair `_flood_note` builds.

    The note exists because a default-off feature nobody is told about is a
    deleted feature that still costs maintenance. When `advise` is on and
    `enabled` is off, floods are counted and the model is asked -- once per
    session -- to put the choice to the person, who otherwise has no reason to
    know the setting exists.
    """
    from siftlib import govern, ledger, session as session_mod

    gcfg = h.cfg.governance
    advise, enabled = gcfg.get("advise"), gcfg.get("enabled")
    if not (advise or enabled) or not command:
        return None, None
    response = h.payload.get("tool_response")
    stdout = _stdout(response)
    if not stdout:
        return None, None

    # Cheap checks before the file write: most commands are neither a
    # replaceable family nor over the threshold, and preserving their output
    # would litter the cache with logs nothing will ever point at.
    family = govern.classify(command)
    if family not in govern.REPLACE_FAMILIES:
        return None, None
    threshold = int(gcfg.get("threshold_tokens")
                    or govern.DEFAULTS["threshold_tokens"])
    original = govern.estimate_tokens(stdout)
    if original < threshold:
        return None, None

    # A flood happened. Record that much even when nothing may be rewritten:
    # this count is the only evidence anyone gets about whether turning
    # `enabled` on would pay, and it cannot be gathered while it is off.
    note = None
    if advise:
        ledger.record(h.ctx, h.session_id, "flood_seen", original,
                      at_call=h.tool_call_index(),
                      family=family, condensed=bool(enabled))
        if not enabled:
            note = _flood_note(h, session_mod, original)
    if not enabled:
        return None, note

    cache = h.ctx.cache / "bash"
    log = govern.preserve(cache, stdout,
                          int(gcfg.get("max_log_bytes") or
                              govern.DEFAULTS["max_log_bytes"]),
                          int(gcfg.get("cache_budget_bytes") or
                              govern.DEFAULTS["cache_budget_bytes"]))
    result = govern.condense(command, stdout, gcfg, log)
    if result is None:
        return None, note

    ledger.record(h.ctx, h.session_id, "governed",
                  result.original_tokens - result.entered_tokens,
                  at_call=h.tool_call_index(),
                  family=result.family, original_tokens=result.original_tokens,
                  entered_tokens=result.entered_tokens, preserved=bool(log))
    # Mirror the object; change stdout and nothing else.
    if _is_codex(h):
        # Codex has no updatedToolOutput field. Its documented way to replace
        # a completed tool result with hook feedback is continue:false plus
        # stopReason; the model then continues from the condensed text.
        return ({"continue": False, "stopReason": result.text}, note)
    # Claude Code's `tool_response` is sometimes the output string itself, not
    # an object with a `stdout` key. `dict(response)` raised TypeError on that
    # shape, the fail-open wrapper swallowed it, and the ledger recorded a
    # `governed` saving the model never got: it saw the full output.
    if isinstance(response, str):
        updated: Any = result.text
    else:
        updated = dict(response)
        updated["stdout"] = result.text
    return ({"hookSpecificOutput": {"hookEventName": "PostToolUse",
                                    "updatedToolOutput": updated}}, note)


def _is_codex(h: "_common.HookCtx") -> bool:
    """Codex, on the evidence rather than on the absence of Claude's fields.

    `CLAUDE_PROJECT_DIR` settles it when it is set: the Claude hook command
    this tool installs is written in terms of that variable, so a Claude hook
    cannot have reached this process without it. Without that guard, the day
    Claude Code adds a `model` field to a tool payload -- other events already
    carry model metadata -- every Claude session would silently be handed
    Codex's `continue`/`stopReason` shape, which Claude Code does not read,
    and governance would stop replacing output with nothing said about it.
    """
    if os.environ.get("CLAUDE_PROJECT_DIR"):
        return False
    return bool(h.payload.get("turn_id") or h.payload.get("model"))


def _flood_note(h: "_common.HookCtx", session_mod,
                tokens: int) -> Optional["tuple[str, str]"]:
    """Ask the model to put the choice to the person, once per session.

    `(model text, person line)`. The whole mechanism rests on this nudge, and
    it used to be `additionalContext` only -- a channel the person never sees,
    put to an agent that waits to be asked. So the person is told too, in one
    line, through `systemMessage`. `post_read` does the same for `big_read_mode`.
    """
    seen = {"count": 0, "asked": False}

    def change(state: Dict[str, Any]) -> None:
        state["floods_seen"] = int(state.get("floods_seen", 0)) + 1
        state["flood_tokens"] = int(state.get("flood_tokens", 0)) + tokens
        seen["count"] = state["floods_seen"]
        seen["tokens"] = state["flood_tokens"]
        seen["asked"] = bool(state.get("flood_asked"))
        if seen["count"] >= FLOODS_BEFORE_ASKING and not seen["asked"]:
            state["flood_asked"] = True

    session_mod.mutate(h.ctx, h.session_id, change)
    if seen["asked"] or seen["count"] < FLOODS_BEFORE_ASKING:
        return None
    text = (_common.PREFIX + "{} commands this session have returned more than "
            "the condensation threshold (~{:,} tokens in total), and every line "
            "of that stays in context for the rest of the session. Output "
            "governance would condense these and keep the full text on disk. It "
            "is on by default but has been turned off in this repo. Ask the "
            "user whether to turn it back on -- `governance.enabled: true` in "
            "{}/config.json -- and do not turn it on yourself.".format(
                seen["count"], seen.get("tokens", 0), h.ctx.dir_name))
    user = ("sift: {} commands over the threshold this session (~{:,} tokens). "
            "governance is off in this repo; `governance.enabled: true` in "
            "{}/config.json would condense them. Say so if you want it back on.".format(
                seen["count"], seen.get("tokens", 0), h.ctx.dir_name))
    return (text, user)


if __name__ == "__main__":
    _common.run("post_bash", main)
