"""Which file a Bash command read, when that can be known for certain.

The duplicate-read check in `pre_read` only ever sees the Read tool, and that
is the wrong channel: OpenWolf measured 140 of 144 real duplicate reads arriving
as `cat`, `sed`, `head` or `tail` through Bash, where no read hook was looking.
This module is what makes that channel visible.

Shell parsing is a tarpit and a wrong answer here is worse than no answer -- it
would attribute a read to a file that was never opened, and then tell the model
it already has content it has never seen. So this is a strict allowlist of
simple forms and returns None for everything else: globs, redirections,
command substitution, multiple files, or any pipe except a trailing filter that
cannot change which file was read.
"""
from __future__ import annotations

import re
from typing import NamedTuple, Optional

# Anything that could make the target ambiguous, or write rather than read.
_DISALLOWED = re.compile(r"[*?{}$`\\]|<\(|>>|>\s|\btee\b|\bxargs\b")
# A trailing filter that narrows what is shown but not which file was read.
_SAFE_FILTER = re.compile(r"^\s*(head|tail|wc|nl|sort|uniq|grep\s+-c)\b[^|]*$")
# `sed -n '1,60p'` and friends: an argument that is a range, not a path.
_RANGE_ARG = re.compile(r"^['\"]?\d+(,\d+)?[pdq]?['\"]?$")
# A sed script that selects lines. Matched against the *arguments*, not the
# whole command: anchored against the command it could never fire, because the
# command starts with `sed`, so `sed -n 5p f` was recorded as a full read and
# the next `cat` of the file was told it had already been printed whole.
_RANGED_SED = re.compile(r"^['\"]?\d+\s*(?:,\s*\d+\s*)?[pdq]['\"]?$")


class BashRead(NamedTuple):
    path: str
    full: bool  # the whole file was printed, not a window into it


def _file_args(args) -> list:
    return [a for a in args
            if not a.startswith("-") and not _RANGE_ARG.match(a)]


def parse(command: str) -> Optional[BashRead]:
    """The single file this command read, or None when that is not certain."""
    cmd = (command or "").strip()
    if not cmd or _DISALLOWED.search(cmd):
        return None
    # A `cd x && cat y` is two commands and the path is relative to the first;
    # not worth guessing.
    if "&&" in cmd or ";" in cmd or "||" in cmd:
        return None

    parts = cmd.split("|")
    if len(parts) > 2:
        return None
    piped = len(parts) == 2
    if piped and not _SAFE_FILTER.match(parts[1]):
        return None
    tokens = parts[0].split()
    if not tokens:
        return None

    binary = tokens[0]
    args = tokens[1:]
    if binary == "cat":
        files = _file_args(args)
        # `cat` with no path reads stdin; two paths is a concatenation, and
        # attributing the output to either one would be a lie.
        if len(files) != 1:
            return None
        return BashRead(files[0], not piped)
    if binary in ("head", "tail"):
        files = _file_args(args)
        if len(files) != 1:
            return None
        return BashRead(files[0], False)
    if binary in ("sed", "nl", "bat"):
        files = _file_args(args)
        if len(files) != 1:
            return None
        full = binary != "sed" or not any(_RANGED_SED.match(a) for a in args)
        return BashRead(files[0], full and not piped)
    return None
