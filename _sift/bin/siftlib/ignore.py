"""A small gitignore-syntax matcher for `.siftignore`.

`git ls-files` has already applied `.gitignore`; this layers the sift directory's own
exclusions on top. Only the subset of gitignore syntax that appears in the
shipped defaults is supported: anchoring, `*`, `?`, `**`, a trailing slash for
directories, and `!` negation.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import List, Sequence, Tuple


def _translate(pattern: str) -> str:
    out: List[str] = []
    i = 0
    n = len(pattern)
    while i < n:
        c = pattern[i]
        if c == "*":
            if pattern[i:i + 3] == "**/":
                out.append("(?:.*/)?")
                i += 3
                continue
            if pattern[i:i + 2] == "**":
                out.append(".*")
                i += 2
                continue
            out.append("[^/]*")
            i += 1
            continue
        if c == "?":
            out.append("[^/]")
            i += 1
            continue
        if c == "[":
            j = pattern.find("]", i + 1)
            if j < 0:
                out.append(re.escape(c))
                i += 1
                continue
            out.append(pattern[i:j + 1])
            i = j + 1
            continue
        out.append(re.escape(c))
        i += 1
    return "".join(out)


class Matcher:
    def __init__(self, patterns: Sequence[str]) -> None:
        self.rules: List[Tuple["re.Pattern[str]", bool, bool]] = []
        for raw in patterns:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            negate = line.startswith("!")
            if negate:
                line = line[1:].strip()
            if not line:
                continue
            dir_only = line.endswith("/")
            line = line.rstrip("/")
            anchored = "/" in line
            if line.startswith("/"):
                line = line[1:]
                anchored = True
            body = _translate(line)
            prefix = "" if anchored else "(?:.*/)?"
            # A directory pattern matches what is under the directory, and not
            # a file of the same name: both arms of this were once identical,
            # so `build/` in the shipped defaults also dropped a top-level
            # `build` script from the index.
            regex = "^" + prefix + body + ("/.*$" if dir_only else "(?:/.*)?$")
            self.rules.append((re.compile(regex), negate, dir_only))

    def ignored(self, path: str, is_dir: bool = False) -> bool:
        """True when `path` is excluded. Callers pass file paths; `is_dir=True`
        asks about the directory itself, which a `dir/` pattern matches."""
        probe = path.rstrip("/") + "/" if is_dir else path
        verdict = False
        for regex, negate, _dir_only in self.rules:
            if regex.match(probe):
                verdict = not negate
        return verdict

    def filter(self, paths: Sequence[str]) -> List[str]:
        return [p for p in paths if not self.ignored(p)]


def load(path: Path) -> Matcher:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        lines = []
    return Matcher(lines)
