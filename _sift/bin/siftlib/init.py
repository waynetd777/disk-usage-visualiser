"""`sift init` — scaffold the sift directory and wire it into the repo.

Everything it writes comes from the manifest in `templates.py`, so "what gets
installed" is a table you can read rather than a sequence of writes you have to
follow.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from . import gitutil, openwolf, templates, util
from .config import Config
from .paths import Ctx


def scaffold(ctx: Ctx, cfg: Config, githooks: bool = True, skills: bool = True,
             force: bool = False, advisory: bool = True) -> Dict[str, Any]:
    troot = templates.templates_root()
    result: Dict[str, Any] = {"created": [], "merged": [], "skipped": [],
                              "openwolf": {}}
    if troot is None:
        result["error"] = "templates not found next to sift.py"
        return result

    subs = templates.substitutions(ctx.dir_name, ctx.root.name,
                                   head=gitutil.head(ctx.root), advisory=advisory)

    for dirname in templates.KEEP_DIRS:
        target = ctx.root / dirname.format(dir=ctx.dir_name)
        target.mkdir(parents=True, exist_ok=True)
        keep = target / ".gitkeep"
        if not keep.exists():
            util.atomic_write(keep, "")
            result["created"].append(ctx.rel(keep))
    for dirname in templates.PLAIN_DIRS:
        target = ctx.root / dirname.format(dir=ctx.dir_name)
        if not target.exists():
            target.mkdir(parents=True, exist_ok=True)
            result["created"].append(ctx.rel(target) + "/")

    # An escape hatch nobody knows about does not get used, and the whole
    # privacy rule rests on this one being used.
    local_readme = ctx.local / "README.md"
    if not local_readme.exists() and troot is not None:
        source = troot / "sift" / "local-README.md"
        if source.is_file():
            util.atomic_write(local_readme, templates.render(util.read_text(source), subs))
    for filename in templates.EMPTY_FILES:
        target = ctx.root / filename.format(dir=ctx.dir_name)
        if not target.exists():
            util.atomic_write(target, "")
            result["created"].append(ctx.rel(target))

    for source, dest_tpl, mode in templates.MANIFEST:
        if not skills and "/skills/" in dest_tpl:
            continue
        src = troot / source
        if not src.is_file():
            continue
        dest = ctx.root / dest_tpl.format(dir=ctx.dir_name)
        text = templates.render(util.read_text(src), subs)
        outcome = _apply(mode, dest, text, ctx.dir_name, force)
        if outcome == "skipped-absent":
            continue
        bucket = {"created": "created", "merged": "merged",
                  "skipped": "skipped"}[outcome]
        result[bucket].append(ctx.rel(dest))

    if githooks:
        result.update(install_githooks(ctx, troot, subs))

    return result


def _apply(mode: str, dest: Path, text: str, dir_name: str, force: bool) -> str:
    if mode == "copy-replace":
        return templates.apply_copy_replace(dest, text, force)
    if mode == "copy-if-absent":
        return templates.apply_copy_if_absent(dest, text, force)
    if mode == "marker-block":
        return templates.apply_marker_block(dest, text)
    if mode == "marker-block-if-present":
        if not dest.exists():
            return "skipped-absent"
        return templates.apply_marker_block(dest, text)
    if mode == "append-stanza-once":
        return templates.apply_append_stanza_once(dest, text, force)
    if mode == "merge-json-hooks":
        return templates.apply_merge_json_hooks(dest, text, dir_name, force)
    raise ValueError("unknown manifest mode: " + mode)


def _wrap(text: str, ctx: Ctx, kept: Path) -> str:
    """sift's hook, with the repo's own hook called first and its exit code kept."""
    body = [l for l in text.splitlines() if not l.startswith("#!")]
    return "\n".join([
        "#!/bin/sh",
        "# sift: runs " + ctx.rel(kept) + " first, then sift's own check.",
        "# Edit that file, not this one: `sift install` rewrites this one.",
        'own=$(dirname "$0")/' + kept.name,
        '[ -x "$own" ] && { "$own" "$@" || exit $?; }',
        "",
    ] + body) + "\n"


def install_githooks(ctx: Ctx, troot: Path, subs: Dict[str, str]) -> Dict[str, Any]:
    """Write `.githooks/*`, appending to an existing script rather than
    replacing it. Never steals `core.hooksPath` when it is already set."""
    out: Dict[str, Any] = {"githooks": [], "githooks_appended": []}
    hooks_dir = ctx.root / ".githooks"
    for source, dest_rel in templates.GITHOOKS:
        src = troot / source
        if not src.is_file():
            continue
        dest = ctx.root / dest_rel
        text = templates.render(util.read_text(src), subs)
        name = dest.name
        kept = dest.with_name(name + ".local")
        if dest.exists():
            current = util.read_text(dest)
            if "sift" in current:
                # Ours, so it is upgradeable: the wrapper's own header tells
                # people to edit `<hook>.local` and says this file is rewritten.
                # Skipping it meant a hook installed once kept whatever it said
                # on the day it went in -- including, for the pre-commit, a
                # comment about which lint codes actually block a commit.
                fresh = _wrap(text, ctx, kept) if kept.is_file() else text
                if fresh != current:
                    util.atomic_write(dest, fresh)
                    _chmod_x(dest)
                    out["githooks"].append(ctx.rel(dest) + " (updated)")
                else:
                    out["githooks"].append(ctx.rel(dest) + " (already current)")
                continue
            # Wrap, never concatenate. A hook that ends in `exit 0` — most of
            # them do — swallows anything appended after it, and the result
            # looks installed while never running once. The repo's own hook
            # moves aside untouched and runs first, keeping its exit code.
            util.atomic_write(kept, current)
            _chmod_x(kept)
            util.atomic_write(dest, _wrap(text, ctx, kept))
            out["githooks_appended"].append(
                "{} (wrapped; yours moved to {})".format(ctx.rel(dest), ctx.rel(kept)))
        else:
            # A `<hook>.local` with no wrapper above it is a hook that runs
            # nothing: either a previous wrap whose top half was deleted, or
            # one moved aside by hand. Either way it is meant to run.
            if kept.is_file():
                text = _wrap(text, ctx, kept)
                out["githooks_appended"].append(
                    "{} (wrapping the {} already there)".format(
                        ctx.rel(dest), kept.name))
            else:
                out["githooks"].append(ctx.rel(dest))
            util.atomic_write(dest, text)
        _chmod_x(dest)
    existing = gitutil.config_get(ctx.root, "core.hooksPath")
    if not existing and hooks_dir.is_dir():
        gitutil.config_set(ctx.root, "core.hooksPath", ".githooks")
        out["hooks_path_set"] = True
    elif existing and existing != ".githooks":
        out["hooks_path_other"] = existing
    return out


def _chmod_x(path: Path) -> None:
    try:
        mode = path.stat().st_mode
        path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    except OSError:
        pass


def install_bin(ctx: Ctx, src_root: Path) -> List[str]:
    """Copy the runtime (sift.py, hook.sh, VERSION, siftlib/, hooks/, templates/)
    into `<sift-dir>/bin`, replacing it wholesale. bin/ is ours; nothing else is."""
    import shutil
    bin_dir = ctx.dir / "bin"
    written: List[str] = []
    if bin_dir.exists():
        shutil.rmtree(str(bin_dir))
    bin_dir.mkdir(parents=True, exist_ok=True)
    for name in ("sift.py", "hook.sh", "VERSION"):
        source = src_root / name
        if source.is_file():
            shutil.copy2(str(source), str(bin_dir / name))
            written.append(name)
    for pkg in ("siftlib", "hooks"):
        source = src_root / pkg
        if source.is_dir():
            shutil.copytree(str(source), str(bin_dir / pkg),
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            written.append(pkg + "/")
    troot = templates.templates_root(src_root / "sift.py")
    if troot and troot.is_dir():
        shutil.copytree(str(troot), str(bin_dir / "templates"),
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        written.append("templates/")
    _chmod_x(bin_dir / "hook.sh")
    _chmod_x(bin_dir / "sift.py")
    return written
