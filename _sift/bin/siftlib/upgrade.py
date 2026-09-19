"""Is the runtime in this repo the one the clone on this machine would install?

Every repo carries its own copy of the tool in `<sift-dir>/bin`, and the wrapper
forwards to that copy rather than to the clone it lives in, so a repo stays
pinned to the version it shipped and upgrading one never changes another. The
price of that is a repo with no way to notice it is behind. This is that way,
and there is no network call in it: if `sift` is on PATH then the clone is
already on this machine, so the comparison is two directory digests and cheap
enough for a session-start hook. The one network check lives in `doctor
--check-upstream` and stays opt-in.

The comparison is a digest, not the version string. `VERSION` is a release
marker a person bumps, and for the first months of this tool it never moved at
all -- a version check would have said "up to date" through every change the
tool has had. The digest is over exactly the files `init.install_bin` copies,
so it answers the only question worth asking: would running `sift update`
change anything here.

`bump_needed` is the other half of that discipline, and it only means anything
in the clone: shipped content that changed since `VERSION` last did is a
release someone forgot to number.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import util
from .paths import Ctx, resolve_dir_name

# What `init.install_bin` copies, and therefore what an upgrade would replace.
TOP_FILES = ("sift.py", "hook.sh", "VERSION")
PACKAGES = ("siftlib", "hooks")
SKIP_DIRS = {"__pycache__"}
SKIP_NAMES = {".DS_Store"}
SKIP_SUFFIXES = (".pyc", ".pyo")

# Everything the clone ships. A change to any of it is a change to what an
# install produces, which is what `VERSION` is supposed to number.
SHIPPED = ("src", "templates", "install.py", "bin/sift")

CURRENT, BEHIND, AHEAD, DIFFERS, SELF, UNKNOWN = (
    "current", "behind", "ahead", "differs", "self", "unknown")
# The two states worth interrupting a session for.
STALE = (BEHIND, DIFFERS)


def find_clone(env: Optional[Dict[str, str]] = None) -> Optional[Path]:
    """The sift clone on this machine, or None.

    `SIFT_CLONE` first -- for a clone that is not on PATH, and for tests, which
    must not depend on what the machine running them happens to have installed.
    Otherwise `sift` on PATH, resolved through its symlink and up two levels:
    the same arithmetic `bin/sift` does for itself.
    """
    env = os.environ if env is None else env
    candidates: List[Path] = []
    named = (env.get("SIFT_CLONE") or "").strip()
    if named:
        candidates.append(Path(named).expanduser())
    found = shutil.which("sift", path=env.get("PATH"))
    if found:
        candidates.append(Path(found).resolve().parent.parent)
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if (resolved / "install.py").is_file() and (resolved / "src" / "sift.py").is_file():
            return resolved
    return None


# Directories that never hold a repo of ours. `Library` is the one that
# matters: cloud-sync providers mount under `Library/CloudStorage`, and a walk
# through someone's synced notes is both slow and none of this tool's business.
DISCOVERY_SKIP = {"Library", "Applications", "node_modules", "venv", "vendor",
                  "__pycache__", "site-packages", "target", "dist", "build"}
# `~/a/b/c/d/e/repo` is already further down than anyone keeps working repos.
DISCOVERY_MAX_DEPTH = 6


def discover(root: Path, max_depth: int = DISCOVERY_MAX_DEPTH) -> List[Path]:
    """Every git repo under `root` with a sift runtime in it.

    Found by searching, not from a registry: OpenWolf kept one at
    `~/.openwolf/registry.json`, nothing ever removed a dead entry, and so its
    every run had to skip them. A search has nothing to go stale and answers
    about the disk as it is now. `--all` on any command that acts across repos
    -- `update`, `ledger` -- shares this one walk.

    The walk stops at each repo rather than descending into it: a repo inside a
    repo is rare, and walking every working tree in a home directory is what
    would make this too slow to run. Hidden directories are skipped, so `.git`
    is something to notice rather than somewhere to go.
    """
    found = []
    stack = [(root, 0)]
    while stack:
        current, depth = stack.pop()
        try:
            entries = list(os.scandir(str(current)))
        except OSError:
            continue
        if any(e.name == ".git" for e in entries):
            name = resolve_dir_name(current)
            if (current / name / "bin" / "sift.py").is_file():
                found.append(current)
            continue
        if depth >= max_depth:
            continue
        for entry in entries:
            if entry.name.startswith(".") or entry.name in DISCOVERY_SKIP:
                continue
            try:
                if entry.is_dir(follow_symlinks=False):
                    stack.append((Path(entry.path), depth + 1))
            except OSError:
                continue
    return sorted(found)


def status(ctx: Ctx, clone: Optional[Path] = None,
           env: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """{state, installed, clone, clone_version, same_bytes, detail}.

    `state` is UNKNOWN whenever there is nothing to compare against, which is
    the normal case in CI and on any machine without the clone. Nothing that
    reads this may treat "cannot tell" as "out of date".
    """
    bin_dir = ctx.dir / "bin"
    installed = util.read_text(bin_dir / "VERSION").strip() or "unknown"
    out: Dict[str, Any] = {
        "state": UNKNOWN, "installed": installed, "clone": None,
        "clone_version": "", "same_bytes": None, "detail": "",
    }
    if clone is None:
        clone = find_clone(env)
    if clone is None:
        out["detail"] = "no sift clone found - nothing to compare against"
        return out

    out["clone"] = str(clone)
    out["clone_version"] = util.read_text(clone / "src" / "VERSION").strip() or "unknown"
    if not bin_dir.is_dir():
        out["detail"] = "no runtime installed in " + ctx.rel(bin_dir)
        return out

    same = digest(installed_pairs(bin_dir)) == digest(clone_pairs(clone))
    out["same_bytes"] = same

    if _same_path(clone, ctx.root):
        # The clone is not behind itself. `bin/` lagging `src/` here is a real
        # thing to say and `sift version` says it, but it is this repo's own
        # business and never a session-start interruption.
        out["state"] = SELF
        out["detail"] = ("this repo is the clone; bin/ is what src/ would install"
                         if same else
                         "this repo is the clone; bin/ is not what src/ would install")
        return out

    order = _compare(installed, out["clone_version"])
    if order < 0:
        out["state"] = BEHIND
        out["detail"] = "{} installed, clone has {}".format(installed, out["clone_version"])
    elif order > 0:
        out["state"] = AHEAD
        out["detail"] = "{} installed, clone has the older {}".format(
            installed, out["clone_version"])
    elif same:
        out["state"] = CURRENT
        out["detail"] = "{} - the same runtime the clone would install".format(installed)
    else:
        out["state"] = DIFFERS
        out["detail"] = ("{} on both sides, but the files differ - the clone has "
                         "changed without a version bump".format(installed))
    return out


def bump_needed(clone: Path) -> Dict[str, Any]:
    """{needed, version, commits, dirty} - shipped content changed since VERSION did.

    Asked of git rather than of a recorded digest: the question is about
    history, and a second copy of the answer on disk is one more thing to keep
    in sync. `dirty` covers the uncommitted case, which is the one worth
    catching, because it is still fixable with an edit rather than a rewrite.
    """
    out: Dict[str, Any] = {"needed": False, "version": "", "commits": [],
                           "dirty": [], "pending": False}
    out["version"] = util.read_text(clone / "src" / "VERSION").strip()
    last = _git(clone, "log", "-1", "--format=%H", "--", "src/VERSION")
    if last is None:
        return out
    last = last.strip()
    if last:
        log = _git(clone, "log", "--format=%h %s", last + "..HEAD", "--", *SHIPPED)
        out["commits"] = [l for l in (log or "").splitlines() if l.strip()]
    porcelain = _git(clone, "status", "--porcelain", "--", *SHIPPED) or ""
    changed = [l[3:].strip() for l in porcelain.splitlines() if l.strip()]
    out["dirty"] = [c for c in changed if c]
    # A bump in the working tree is the bump, and it has not been committed
    # yet, so it clears both halves: the commits it is about to cover and the
    # edits it is shipping with.
    out["pending"] = any(c.endswith("src/VERSION") for c in changed)
    out["needed"] = bool(not out["pending"] and (out["commits"] or out["dirty"]))
    return out


# ---------------------------------------------------------------------------
# Digests
# ---------------------------------------------------------------------------

def installed_pairs(bin_dir: Path) -> List[Tuple[str, Path]]:
    return _pairs(bin_dir, bin_dir / "templates")


def clone_pairs(clone: Path) -> List[Tuple[str, Path]]:
    return _pairs(clone / "src", clone / "templates")


def digest(pairs: List[Tuple[str, Path]]) -> str:
    """Content of the runtime, keyed by the path it is installed to.

    Short on purpose: it is shown to people and compared to itself, never used
    as a security boundary.
    """
    h = hashlib.sha256()
    for logical, path in sorted(pairs):
        h.update(logical.encode("utf-8"))
        h.update(b"\0")
        try:
            h.update(hashlib.sha256(path.read_bytes()).digest())
        except OSError:
            h.update(b"?")
    return h.hexdigest()[:16]


def _pairs(root: Path, templates_dir: Optional[Path]) -> List[Tuple[str, Path]]:
    out: List[Tuple[str, Path]] = []
    for name in TOP_FILES:
        path = root / name
        if path.is_file():
            out.append((name, path))
    for pkg in PACKAGES:
        out.extend(_walk(root / pkg, pkg))
    if templates_dir is not None:
        out.extend(_walk(templates_dir, "templates"))
    return out


def _walk(root: Path, prefix: str) -> List[Tuple[str, Path]]:
    out: List[Tuple[str, Path]] = []
    if not root.is_dir():
        return out
    for dirpath, dirnames, filenames in os.walk(str(root)):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            if name in SKIP_NAMES or name.endswith(SKIP_SUFFIXES):
                continue
            path = Path(dirpath) / name
            out.append((prefix + "/" + str(path.relative_to(root)).replace(os.sep, "/"), path))
    return out


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _compare(installed: str, clone_version: str) -> int:
    """-1 behind, 0 same, 1 ahead. Unparseable versions only ever compare equal
    or behind: a string we cannot read is not evidence of being ahead."""
    if installed == clone_version:
        return 0
    left, right = _parts(installed), _parts(clone_version)
    if left is None or right is None:
        return -1
    return -1 if left < right else (1 if left > right else 0)


def _parts(version: str) -> Optional[Tuple[int, ...]]:
    try:
        return tuple(int(p) for p in version.split("."))
    except (ValueError, AttributeError):
        return None


def _same_path(left: Path, right: Path) -> bool:
    try:
        return left.resolve() == right.resolve()
    except OSError:
        return False


def _git(root: Path, *args: str) -> Optional[str]:
    try:
        proc = subprocess.run(["git", "-C", str(root)] + list(args),
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              check=False)
    except (OSError, ValueError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.decode("utf-8", "replace")
