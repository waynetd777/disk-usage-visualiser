"""Did the hooks actually fire, and did anything they said arrive?

This project has already been burned once by believing its own accounting:
`tokens_avoided_est` was steered reads times a hardcoded 400, quoted for weeks
as though it were a measurement. OpenWolf reports the same class of error at up
to 20x between self-reports and reality. The only cure is a record neither side
wrote, and Claude Code keeps one: every hook invocation is appended to the
session transcript as an attachment carrying the hook name, event, command,
exit code and output.

That format is explicitly unstable -- the documentation says scripts parsing
transcripts directly can break on any release. So this probes the shape first
and returns `None` for "cannot tell" rather than a confident zero. A wrong
number here would be worse than no number, because the whole point of it is to
catch wrong numbers.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

TRANSCRIPTS = Path("~/.claude/projects").expanduser()
_HOOK_KINDS = ("hook_success", "hook_non_blocking_error", "hook_error")


def project_dir_name(root: Path) -> str:
    """The exact directory name Claude Code gives a project under `~/.claude/projects`.

    It mangles the absolute path by replacing every character outside
    `[A-Za-z0-9]` with `-`, so the leading `/` becomes a leading `-` and `/`,
    `.` and `_` all flatten to the same character:
    `/Users/x/.tools/wf_1` becomes `-Users-x--tools-wf-1`.

    Matching this exactly matters twice. A substring test on a partially
    mangled stem matched any sibling whose path merely starts with this one --
    `/p/repo` matched `/p/repo-eval`'s directory -- and it matched nothing at
    all for a path containing `_` or `.`, because the harness mangles those and
    a `/`-only replacement does not.
    """
    return re.sub(r"[^A-Za-z0-9]", "-", str(root))


def transcript_for(session_id: str, root: Optional[Path] = None) -> Optional[Path]:
    """The transcript file for a session, wherever the harness put it."""
    if not session_id or not re.match(r"^[\w-]{4,}$", session_id):
        return None
    base = TRANSCRIPTS
    if not base.is_dir():
        return None
    matches = sorted(base.glob("*/{}.jsonl".format(session_id)))
    if root:
        # Prefer the directory that *is* this repo's, so two sessions with the
        # same id in different projects cannot be confused.
        name = project_dir_name(root)
        exact = [m for m in matches if m.parent.name == name]
        if exact:
            return exact[0]
    # A session id is a UUID, so a file carrying it is this session's wherever
    # the harness filed it; the directory match above only breaks a tie.
    return matches[0] if matches else None


def _attachments(path: Path) -> Optional[List[dict]]:
    """Hook attachment records, or None when the format is not what we expect.

    "None" is load-bearing. An empty list means the hooks did not fire; None
    means this cannot be answered from the transcript any more, and the caller
    must say so rather than reporting that nothing fired.
    """
    found: List[dict] = []
    saw_any_line = False
    saw_attachment_key = False
    try:
        with open(path, encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                if not line.strip():
                    continue
                saw_any_line = True
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(row, dict):
                    continue
                if "attachment" in row or row.get("type") == "attachment":
                    saw_attachment_key = True
                att = row.get("attachment")
                if isinstance(att, dict) and str(att.get("type")) in _HOOK_KINDS:
                    found.append(att)
    except OSError:
        return None
    if not saw_any_line:
        return None
    if found:
        return found
    # Lines parsed, but nothing that looks like a hook attachment. Either the
    # hooks genuinely never fired, or the schema moved. `attachment` appearing
    # at all is what separates those two.
    return [] if saw_attachment_key else None


def verify(session_id: str, root: Optional[Path] = None) -> Dict[str, Any]:
    """{available, fired, failed, by_hook, transcript} for one session."""
    out: Dict[str, Any] = {"available": False, "fired": 0, "failed": 0,
                           "by_hook": {}, "transcript": None}
    path = transcript_for(session_id, root)
    if path is None:
        return out
    out["transcript"] = str(path)
    records = _attachments(path)
    if records is None:
        return out
    out["available"] = True
    for att in records:
        name = str(att.get("hookName") or att.get("hook_name") or "?")
        entry = out["by_hook"].setdefault(name, {"fired": 0, "failed": 0})
        entry["fired"] += 1
        out["fired"] += 1
        failed = (str(att.get("type")) != "hook_success"
                  or int(att.get("exitCode") or att.get("exit_code") or 0) != 0)
        if failed:
            entry["failed"] += 1
            out["failed"] += 1
    return out


def latest_session(root: Path, sift_dir: Optional[Path] = None) -> Optional[str]:
    """The session `doctor` should verify: the last one sift itself saw.

    Not simply the newest transcript. A session that starts and is left without
    a message writes no transcript at all, so the newest file on disk can
    belong to an entirely different session - and reporting "0 invocations in
    the last session" about someone else's session reads as "your hooks did not
    fire" when they did. When the recorded session has no transcript, say so by
    returning nothing rather than verifying the wrong one.
    """
    base = TRANSCRIPTS
    if not base.is_dir():
        return None
    name = project_dir_name(root)
    # Exact name only, no looser fallback: if the harness's mangling moves,
    # the answer is "not verifiable", which `doctor` says. A substring match
    # would instead verify a prefix sibling's session and call it ours.
    candidates = [p for p in base.glob("*/*.jsonl") if p.parent.name == name]
    if not candidates:
        return None
    recorded = _last_recorded_session(sift_dir)
    if recorded:
        for path in candidates:
            if path.stem == recorded:
                return recorded
        return None
    newest = max(candidates, key=lambda p: os.path.getmtime(p))
    return newest.stem


def _last_recorded_session(sift_dir: Optional[Path]) -> str:
    """The newest session file sift wrote - its own record of being called."""
    if sift_dir is None:
        return ""
    sessions = sift_dir / ".cache" / "sessions"
    files = sorted(sessions.glob("*.json"), key=lambda p: os.path.getmtime(p)) \
        if sessions.is_dir() else []
    return files[-1].stem if files else ""
