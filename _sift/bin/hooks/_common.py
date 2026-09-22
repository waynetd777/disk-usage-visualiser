"""Shared Claude Code and Codex hook plumbing.

Contract (BUILD-SPEC 10.3): a hook reads one JSON object on stdin, writes at
most one JSON object on stdout, and always exits 0. `run()` catches everything,
including SystemExit, because a traceback on stdout would corrupt the session.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

_HERE = Path(__file__).resolve().parent
_BIN = _HERE.parent
if str(_BIN) not in sys.path:
    sys.path.insert(0, str(_BIN))

PREFIX = "sift: "


def read_payload() -> Optional[Dict[str, Any]]:
    try:
        raw = sys.stdin.read()
    except Exception:  # noqa: BLE001
        return None
    if not raw.strip():
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def emit(obj: Optional[Dict[str, Any]]) -> None:
    if not obj:
        return
    sys.stdout.write(json.dumps(obj, ensure_ascii=False))


def additional_context(event: str, text: str,
                      user_message: str = "") -> Optional[Dict[str, Any]]:
    """`additionalContext` reaches the model; `systemMessage` reaches the person.

    Almost everything this tool injects is for the model alone and the person
    should not have to read it. The exception is anything that needs them to
    act or to ask for something, which they cannot do if the only channel is
    one they never see.
    """
    if not text:
        return None
    out: Dict[str, Any] = {
        "hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}
    if user_message:
        out["systemMessage"] = user_message
    return out


def deny(event: str, reason: str) -> Dict[str, Any]:
    return {"hookSpecificOutput": {
        "hookEventName": event,
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }}


def read_window(tool_input: Dict[str, Any]) -> "tuple[int, Optional[int]]":
    """`(offset, limit)` as the Read tool reads them: offset 1-based, with a
    missing, zero or negative offset meaning the first line; limit None when
    absent or unusable."""
    def _int(value: Any) -> Optional[int]:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
    offset = _int(tool_input.get("offset"))
    limit = _int(tool_input.get("limit"))
    if offset is None or offset < 1:
        offset = 1
    if limit is not None and limit < 0:
        limit = None
    return offset, limit


def is_whole_read(tool_input: Dict[str, Any], total_lines: Optional[int]) -> bool:
    """Whether this Read returns the whole file, judged the one way by both
    Read hooks.

    `offset`/`limit` present used to mean ranged in `pre_read` and `offset or
    limit` truthy meant ranged in `post_read`, so `offset: 0` slipped past the
    big-read refusal in one hook and was recorded as a whole read by the other,
    and `offset: 1, limit: 100000` was a "range" that returned the file. From
    the first line with no limit is the whole file; so is a limit that reaches
    its last line, when the line count is known. `total_lines` None means the
    caller could not count, and a limit is then taken at its word.
    """
    offset, limit = read_window(tool_input)
    if offset > 1:
        return False
    if limit is None:
        return True
    if total_lines is None:
        return False
    return limit >= total_lines


def is_subagent(payload: Dict[str, Any]) -> bool:
    return bool(str(payload.get("agent_id") or "").strip()
                or str(payload.get("agent_type") or "").strip())


class HookCtx:
    """Everything a hook needs, or `enabled = False` if it should do nothing."""

    def __init__(self, payload: Dict[str, Any], hook_name: str) -> None:
        from siftlib import config as config_mod, paths

        self.payload = payload
        self.hook_name = hook_name
        self.event = str(payload.get("hook_event_name") or "")
        self.session_id = str(payload.get("session_id") or "unknown")
        self.subagent = is_subagent(payload)
        self.enabled = False
        self.ctx = None
        self.cfg = None

        cwd = payload.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
        try:
            self.ctx = paths.resolve(Path(cwd))
        except Exception:  # noqa: BLE001
            return
        if not self.ctx.exists:
            return
        self.cfg = config_mod.load(self.ctx.config_path)
        if not self.cfg.get("hooks", "enabled", default=True):
            return
        if not self.cfg.get("hooks", hook_name, default=True):
            return
        self.enabled = True

    def tool_call_index(self) -> int:
        """How many tool calls this session has made, per `post_tool`'s count.

        Best effort: a session file that is missing or unreadable means the
        ledger loses the position of one event, which is worth less than a
        hook that fails.
        """
        try:
            from siftlib import session as session_mod
            return int(session_mod.load(self.ctx, self.session_id).get(
                "tool_calls", 0) or 0)
        except Exception:  # noqa: BLE001
            return 0

    def rel(self, file_path: str) -> str:
        if not file_path:
            return ""
        p = Path(file_path)
        if p.is_absolute():
            try:
                return p.resolve().relative_to(self.ctx.root).as_posix()
            except (ValueError, OSError):
                return p.as_posix()
        return p.as_posix()

    def tool_input(self) -> Dict[str, Any]:
        value = self.payload.get("tool_input")
        return value if isinstance(value, dict) else {}


def run(hook_name: str, main: "Callable[[HookCtx], Optional[Dict[str, Any]]]") -> None:
    """Fail-open wrapper. Nothing below this line may raise out of the process."""
    try:
        payload = read_payload()
        if payload is None:
            return
        hctx = HookCtx(payload, hook_name)
        if not hctx.enabled:
            return
        emit(main(hctx))
    except BaseException:  # noqa: BLE001 - a hook must never break a session
        try:
            if os.environ.get("SIFT_HOOK_DEBUG"):
                import traceback
                traceback.print_exc(file=sys.stderr)
        except Exception:  # noqa: BLE001
            pass
    finally:
        try:
            sys.stdout.flush()
        except Exception:  # noqa: BLE001
            pass
        os._exit(0)
