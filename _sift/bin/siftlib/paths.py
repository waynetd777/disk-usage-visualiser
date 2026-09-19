"""Root and sift-dir resolution (BUILD-SPEC 5).

Written so that monorepo support (Ruling 13) is a later addition rather than a
rewrite: everything downstream takes a `Ctx`, and a nearest-sift-dir walk-up
would only change how that `Ctx` is built.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import List, Optional

from . import gitutil

KNOWN_DIR_NAMES = ["_sift"]
DEFAULT_DIR_NAME = "_sift"


class NotARepo(Exception):
    pass


class Ctx:
    """Everything a command needs to find its files."""

    def __init__(self, root: Path, dir_name: str) -> None:
        self.root = root
        self.dir_name = dir_name
        self.dir = root / dir_name

    # --- committed content -------------------------------------------------




    @property
    def decisions(self) -> Path:
        return self.dir / "decisions.jsonl"

    @property
    def journal(self) -> Path:
        return self.dir / "journal.jsonl"

    @property
    def files_jsonl(self) -> Path:
        return self.dir / "files.jsonl"

    @property
    def conventions(self) -> Path:
        return self.dir / "conventions.md"

    @property
    def siftignore(self) -> Path:
        return self.dir / ".siftignore"

    @property
    def config_path(self) -> Path:
        return self.dir / "config.json"

    # --- derived -----------------------------------------------------------
    @property
    def cache(self) -> Path:
        return self.dir / ".cache"

    @property
    def local(self) -> Path:
        return self.dir / "local"

    @property
    def scan_json(self) -> Path:
        return self.cache / "scan.json"


    @property
    def search_json(self) -> Path:
        return self.cache / "search.json"

    @property
    def ledger(self) -> Path:
        return self.cache / "ledger.jsonl"

    @property
    def sessions(self) -> Path:
        return self.cache / "sessions"

    @property
    def precompact(self) -> Path:
        return self.cache / "precompact"

    @property
    def exists(self) -> bool:
        return self.dir.is_dir()

    def rel(self, path: Path) -> str:
        try:
            return path.resolve().relative_to(self.root).as_posix()
        except (ValueError, OSError):
            return path.as_posix()

    def contains(self, path: str) -> bool:
        """True when a path a hook recorded is genuinely inside the repo.

        `rel()` falls back to the absolute path when a file is outside the
        root, so an out-of-repo read is recognisable by still being absolute.
        Anything that writes a recorded path into a committed file has to ask
        this first: a scratchpad or a home-directory path in `journal.jsonl` is
        a machine path in a colleague's checkout, which is what W16 blocks at
        pre-commit.
        """
        norm = path.replace("\\", "/")
        return bool(norm) and not norm.startswith("/") and not norm.startswith("../") \
            and norm != ".." and ":" not in norm.split("/")[0]

    def is_sift_path(self, path: str) -> bool:
        """True for anything under the sift directory - infrastructure, not project."""
        norm = path.replace("\\", "/")
        return norm == self.dir_name or norm.startswith(self.dir_name + "/")


def resolve_dir_name(root: Path, flag: Optional[str] = None) -> str:
    if flag:
        return flag.strip("/")
    env = os.environ.get("SIFT_DIR", "").strip()
    if env:
        return env.strip("/")
    marker = root / ".siftdir"
    candidates = list(KNOWN_DIR_NAMES)
    if marker.is_file():
        try:
            named = marker.read_text(encoding="utf-8").strip().splitlines()
            if named and named[0].strip():
                candidates.insert(0, named[0].strip().strip("/"))
        except OSError:
            pass
    for name in candidates:
        if (root / name).is_dir():
            return name
    return DEFAULT_DIR_NAME


def resolve(cwd: Optional[Path] = None, dir_flag: Optional[str] = None) -> Ctx:
    cwd = Path(cwd) if cwd else Path.cwd()
    root = gitutil.toplevel(cwd)
    if root is None:
        raise NotARepo("not a git repository (or git is unavailable): " + str(cwd))
    return Ctx(root, resolve_dir_name(root, dir_flag))
