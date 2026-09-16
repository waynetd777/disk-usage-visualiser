"""Every git call in the tool. Nothing else shells out.

Rules from BUILD-SPEC 4: no `shell=True`, always `-z`/`--porcelain` where an
option exists, and `check=False` so a git failure is data rather than an
exception — the tool has to keep working in a shallow CI clone.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

GIT = "git"
NULL = "\0"


def run(root: Optional[Path], args: Sequence[str], timeout: int = 30) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.setdefault("GIT_OPTIONAL_LOCKS", "0")
    try:
        return subprocess.run(
            [GIT] + list(args),
            capture_output=True, text=True, check=False, timeout=timeout,
            cwd=str(root) if root else None, env=env,
        )
    except (OSError, subprocess.TimeoutExpired):
        return subprocess.CompletedProcess([GIT] + list(args), 128, "", "git unavailable")


def run_input(root: Optional[Path], args: Sequence[str], stdin: str,
              timeout: int = 30) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.setdefault("GIT_OPTIONAL_LOCKS", "0")
    try:
        return subprocess.run(
            [GIT] + list(args), input=stdin,
            capture_output=True, text=True, check=False, timeout=timeout,
            cwd=str(root) if root else None, env=env,
        )
    except (OSError, subprocess.TimeoutExpired):
        return subprocess.CompletedProcess([GIT] + list(args), 128, "", "git unavailable")


def available() -> bool:
    return run(None, ["--version"]).returncode == 0


def toplevel(cwd: Path) -> Optional[Path]:
    """Git toplevel containing `cwd`, or None. Resolved so symlinked /tmp on
    macOS does not produce two spellings of the same repo."""
    if not cwd.exists():
        cwd = Path.cwd()
    proc = run(cwd, ["rev-parse", "--show-toplevel"])
    if proc.returncode != 0:
        return None
    out = proc.stdout.strip()
    return Path(out).resolve() if out else None


def head(root: Path) -> str:
    """8-char HEAD sha (Ruling 6), or '' in a repo with no commits."""
    proc = run(root, ["rev-parse", "--short=8", "HEAD"])
    return proc.stdout.strip() if proc.returncode == 0 else ""


def rev_parse(root: Path, ref: str) -> str:
    proc = run(root, ["rev-parse", "--short=8", ref])
    return proc.stdout.strip() if proc.returncode == 0 else ""


def is_reachable(root: Path, sha: str) -> bool:
    """True when `sha` names a commit object in this clone. False after a
    squash-merge, a rebase, or in a shallow CI clone — hence the fallbacks."""
    if not sha:
        return False
    proc = run(root, ["cat-file", "-e", sha + "^{commit}"])
    return proc.returncode == 0


def _split_z(text: str) -> List[str]:
    return [p for p in text.split(NULL) if p]


def ls_files(root: Path) -> List[str]:
    proc = run(root, ["ls-files", "-z"])
    return _split_z(proc.stdout) if proc.returncode == 0 else []


def ls_files_stage(root: Path) -> Dict[str, str]:
    """path -> blob sha. Free hashing: git already knows the index blobs."""
    proc = run(root, ["ls-files", "-z", "-s"])
    out: Dict[str, str] = {}
    if proc.returncode != 0:
        return out
    for rec in _split_z(proc.stdout):
        # "<mode> <sha> <stage>\t<path>"
        meta, tab, path = rec.partition("\t")
        if not tab:
            continue
        bits = meta.split()
        if len(bits) >= 2:
            out[path] = bits[1]
    return out


def modified_paths(root: Path) -> List[str]:
    """Worktree paths whose content differs from the index (so their staged
    blob sha is a lie and we must hash the file ourselves)."""
    proc = run(root, ["status", "--porcelain=v1", "-z", "--untracked-files=no"])
    if proc.returncode != 0:
        return []
    out: List[str] = []
    fields = proc.stdout.split(NULL)
    i = 0
    while i < len(fields):
        rec = fields[i]
        i += 1
        if len(rec) < 4:
            continue
        status, path = rec[:2], rec[3:]
        if "R" in status or "C" in status:
            i += 1  # rename/copy records are followed by the source path
        out.append(path)
    return out


BLOB_CHARS = 16


def short_blob(sha: str) -> str:
    """The form every stored blob takes: the first `BLOB_CHARS` of the sha.

    It has to be one function because the comparison spans two producers. The
    scan truncated `hash_object`, the Bash read hook did not, and `pre_read`
    compared the two -- so a 40-char sha never equalled a 16-char one and
    cross-channel duplicate detection was dead in both directions, `deny` mode
    included. Truncate here or the same mismatch comes back.
    """
    return (sha or "")[:BLOB_CHARS]


def hash_object(root: Path, path: str) -> str:
    """The full sha git reports. Store it through `short_blob`."""
    proc = run(root, ["hash-object", "--", path])
    return proc.stdout.strip() if proc.returncode == 0 else ""


def globspec(pathspec: str) -> str:
    """`covers:` entries are stored bare; git needs the magic prefix for `**`."""
    if pathspec.startswith(":("):
        return pathspec
    return ":(glob)" + pathspec


def match_pathspecs(root: Path, pathspecs: Sequence[str]) -> List[str]:
    if not pathspecs:
        return []
    proc = run(root, ["ls-files", "-z", "--"] + [globspec(p) for p in pathspecs])
    return _split_z(proc.stdout) if proc.returncode == 0 else []


def diff_names(root: Path, base: str, ref: str = "HEAD",
               pathspecs: Sequence[str] = ()) -> List[str]:
    args = ["diff", "--name-only", "-z", base + ".." + ref]
    if pathspecs:
        args += ["--"] + [globspec(p) for p in pathspecs]
    proc = run(root, args)
    return _split_z(proc.stdout) if proc.returncode == 0 else []


def log_names_since(root: Path, since: str, pathspecs: Sequence[str] = ()) -> List[str]:
    """Fallback for an unreachable stamp: what changed since a timestamp."""
    args = ["log", "--since=" + since, "--format=", "--name-only", "-z"]
    if pathspecs:
        args += ["--"] + [globspec(p) for p in pathspecs]
    proc = run(root, args)
    if proc.returncode != 0:
        return []
    seen: List[str] = []
    for path in _split_z(proc.stdout):
        if path not in seen:
            seen.append(path)
    return seen


def commit_count_since(root: Path, since: str) -> int:
    proc = run(root, ["log", "--since=" + since, "--format=%H"])
    return len([l for l in proc.stdout.splitlines() if l.strip()]) if proc.returncode == 0 else 0


def commits_between(root: Path, base: str, ref: str = "HEAD") -> int:
    proc = run(root, ["rev-list", "--count", base + ".." + ref])
    try:
        return int(proc.stdout.strip())
    except ValueError:
        return 0


def churn(root: Path, since: str) -> Dict[str, int]:
    """path -> number of commits touching it since `since` (BUILD-SPEC 17.3)."""
    proc = run(root, ["log", "--since=" + since, "--format=%x00commit", "--name-only"])
    counts: Dict[str, int] = {}
    if proc.returncode != 0:
        return counts
    for chunk in proc.stdout.split("\0commit"):
        for line in {l.strip() for l in chunk.splitlines() if l.strip()}:
            counts[line] = counts.get(line, 0) + 1
    return counts


def name_status(root: Path, base: str, ref: str = "HEAD",
                find_renames: str = "-M50%") -> List[Tuple[str, str, str]]:
    """[(status, old_path, new_path)]; old == new for non-renames."""
    proc = run(root, ["diff", find_renames, "--name-status", "-z", base + ".." + ref])
    if proc.returncode != 0:
        return []
    fields = proc.stdout.split(NULL)
    out: List[Tuple[str, str, str]] = []
    i = 0
    while i < len(fields):
        status = fields[i]
        if not status:
            i += 1
            continue
        if status[0] in ("R", "C"):
            if i + 2 >= len(fields):
                break
            out.append((status, fields[i + 1], fields[i + 2]))
            i += 3
        else:
            if i + 1 >= len(fields):
                break
            out.append((status, fields[i + 1], fields[i + 1]))
            i += 2
    return out


def staged_files(root: Path) -> List[str]:
    proc = run(root, ["diff", "--cached", "--name-only", "-z"])
    return _split_z(proc.stdout) if proc.returncode == 0 else []


def commit_contents(root: Path, include_unstaged: bool = False) -> List[str]:
    """Paths this commit will carry, asked of git rather than inferred.

    Session tracking only sees edits made through the editing tools; anything
    written by a shell command, an external editor, or a teammate's earlier
    branch is invisible to it. At `git commit` time git already knows, so ask.
    """
    paths = staged_files(root)
    if include_unstaged or not paths:
        proc = run(root, ["diff", "--name-only", "-z"])
        if proc.returncode == 0:
            for path in _split_z(proc.stdout):
                if path not in paths:
                    paths.append(path)
    return paths


def subject_lines(root: Path, pathspecs: Sequence[str], limit: int = 30) -> List[str]:
    args = ["log", "--format=%s", "-n", str(limit)]
    if pathspecs:
        args += ["--"] + [globspec(p) for p in pathspecs]
    proc = run(root, args)
    return proc.stdout.splitlines() if proc.returncode == 0 else []


def commit_touches(root: Path, sha: str, paths: Sequence[str]) -> List[str]:
    proc = run(root, ["show", "--name-only", "--format=", "-z", sha])
    if proc.returncode != 0:
        return []
    touched = set(_split_z(proc.stdout))
    return [p for p in paths if p in touched]


def user_email(root: Path) -> str:
    """Ruling 15: email, then name, then `unknown`. Never the OS username."""
    for key in ("user.email", "user.name"):
        proc = run(root, ["config", "--get", key])
        value = proc.stdout.strip()
        if value:
            return value
    return "unknown"


def config_get(root: Path, key: str) -> str:
    return run(root, ["config", "--get", key]).stdout.strip()


def config_set(root: Path, key: str, value: str) -> bool:
    return run(root, ["config", key, value]).returncode == 0


def grep(root: Path, pattern: str, exclude: Sequence[str] = (),
         ignore_case: bool = True) -> List["Tuple[str, int, str]"]:
    """[(path, line number, text)] across tracked files. `git grep` rather than
    walking the tree: it already knows what is tracked and it is fast.

    Case-insensitive by default: the first version of this was not, and it
    silently missed every line that spelled a product name the way prose
    actually spells it.
    """
    args = ["grep", "-nIE"] + (["-i"] if ignore_case else [])
    args += ["--no-color", "-e", pattern, "--"]
    args += [":(exclude)" + spec for spec in exclude]
    proc = run(root, args)
    if proc.returncode not in (0, 1):
        return []
    out: List[Tuple[str, int, str]] = []
    for line in proc.stdout.splitlines():
        path, _, rest = line.partition(":")
        number, _, text = rest.partition(":")
        try:
            out.append((path, int(number), text.strip()[:120]))
        except ValueError:
            continue
    return out


def check_ignore(root: Path, paths: Sequence[str]) -> List[str]:
    """Which of `paths` git would ignore.

    `-z` is only legal alongside `--stdin`; passing it with arguments makes git
    exit 128 and print nothing, which this returned as "nothing is ignored" --
    the most dangerous possible wrong answer for a privacy check.
    """
    if not paths:
        return []
    proc = run_input(root, ["check-ignore", "-z", "--stdin"],
                     "\0".join(paths) + "\0")
    return _split_z(proc.stdout) if proc.returncode in (0, 1) else []
