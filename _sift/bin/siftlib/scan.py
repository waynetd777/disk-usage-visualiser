"""`scan` and `describe`.

`scan` regenerates `.cache/scan.json` (blob sha, size, token estimate, top-level
symbols), compacts `files.jsonl` after a union merge, and rewrites `covers:`
entries whose file was renamed. Blob shas come from `git ls-files -s`, so the
only files hashed in Python are the handful git reports as modified.

Ruling 16: the sift directory is never scanned. It is infrastructure, and counting it
as uncovered source would make `coverage` permanently wrong.
"""
from __future__ import annotations

import math
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import gitutil, ignore, symbols, util
from .config import Config
from .paths import Ctx

DESC_MAX = 160


def scannable_paths(ctx: Ctx, cfg: Config) -> List[str]:
    tracked = gitutil.ls_files(ctx.root)
    matcher = ignore.load(ctx.siftignore)
    return [p for p in tracked
            if not ctx.is_sift_path(p) and not matcher.ignored(p)]


def _binary(path: str, binary_exts: Sequence[str]) -> bool:
    ext = os.path.splitext(path)[1].lower()
    return ext in binary_exts


def run_scan(ctx: Ctx, cfg: Config, full: bool = False) -> Dict[str, Any]:
    """Rebuild the scan cache; return the summary `data` for the CLI."""
    head = gitutil.head(ctx.root)
    stage = gitutil.ls_files_stage(ctx.root)
    matcher = ignore.load(ctx.siftignore)
    modified = set(gitutil.modified_paths(ctx.root))
    binary_exts = [e.lower() for e in cfg.get("scan", "binary_extensions", default=[])]
    sym_min = int(cfg.get("scan", "symbol_min_tokens", default=500))
    sym_max_count = int(cfg.get("scan", "symbol_max_count", default=30))
    sym_max_bytes = int(cfg.get("scan", "symbol_max_bytes", default=262144))

    previous = util.read_json(ctx.scan_json, default={}) or {}
    prev_files: Dict[str, dict] = previous.get("files") or {}

    files: Dict[str, dict] = {}
    changed = 0
    for path in sorted(stage):
        if ctx.is_sift_path(path) or matcher.ignored(path):
            continue
        blob = stage[path]
        if path in modified:
            fresh = gitutil.hash_object(ctx.root, path)
            if fresh:
                blob = fresh
        blob16 = gitutil.short_blob(blob)
        prev = prev_files.get(path)
        if not full and prev and prev.get("blob") == blob16:
            files[path] = prev
            continue
        changed += 1
        ext = os.path.splitext(path)[1].lower()
        if _binary(path, binary_exts):
            files[path] = {"blob": blob16, "size": _size(ctx.root / path), "tokens": 0,
                           "binary": True, "symbols": []}
            continue
        size = _size(ctx.root / path)
        divisor = 3.5 if ext in util.CODE_EXTENSIONS else 4.0
        tokens = int(math.ceil(size / divisor))
        syms: List[dict] = []
        if tokens >= sym_min and size <= sym_max_bytes and symbols.supported(ext):
            text = util.read_text(ctx.root / path)
            tokens = util.estimate_tokens(text, ext)
            syms = symbols.extract(text, ext, sym_max_count, divisor)
        files[path] = {"blob": blob16, "size": size, "tokens": tokens,
                       "binary": False, "symbols": syms}

    since = cfg.get("hot", "since", default="6.months")
    churn = gitutil.churn(ctx.root, since)

    data = {
        "version": 1,
        "head": head,
        "scanned": util.now_iso(),
        "churn_since": since,
        "churn": {k: v for k, v in churn.items() if k in files},
        "files": files,
    }
    util.write_json(ctx.scan_json, data)

    compacted = compact_files_jsonl(ctx, files)
    pending = len(pending_descriptions(ctx, cfg, files))

    return {"files": len(files), "changed": changed, "pending_descriptions": pending,
            "compacted": compacted}


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


# ---------------------------------------------------------------------------
# files.jsonl
# ---------------------------------------------------------------------------

def load_descriptions(ctx: Ctx) -> Dict[str, dict]:
    """path -> record. Later duplicate lines win; `compact` decides properly."""
    out: Dict[str, dict] = {}
    for rec in util.read_jsonl(ctx.files_jsonl):
        path = rec.get("path")
        if isinstance(path, str) and path:
            out[path] = rec
    return out


def compact_files_jsonl(ctx: Ctx, files: Dict[str, dict]) -> int:
    """Resolve the duplicate lines a union merge leaves behind.

    Ruling 17: when two records describe the same path, keep the one whose
    `hash` matches the current blob; if neither does, keep the newest by `ts`
    and let lint W09 ask for a refresh. No extra field is written into
    files.jsonl -- BUILD-SPEC 7.3 says that file holds only what was authored.
    """
    rows = util.read_jsonl(ctx.files_jsonl)
    if not rows:
        return 0
    grouped: Dict[str, List[dict]] = {}
    for rec in rows:
        path = rec.get("path")
        if isinstance(path, str) and path:
            grouped.setdefault(path, []).append(rec)
    keep: List[dict] = []
    for path, records in grouped.items():
        if len(records) == 1:
            keep.append(records[0])
            continue
        current = (files.get(path) or {}).get("blob", "")
        match = [r for r in records if current and r.get("hash") == current]
        pool = match or records
        pool = sorted(pool, key=lambda r: str(r.get("ts", "")))
        keep.append(pool[-1])
    keep.sort(key=lambda r: r.get("path", ""))
    removed = len(rows) - len(keep)
    if removed or keep != rows:
        util.write_jsonl(ctx.files_jsonl, keep)
    return removed


def _relative(path: str) -> str:
    """Normalise a user-supplied path to a repo-relative one.

    Not `lstrip("./")`: that strips any leading run of `.` and `/` characters,
    not the prefix, so `.gitignore` became `gitignore` and no dotfile could
    ever be described. Found by an agent using the tool, which is the only way
    this class of bug gets found.
    """
    rel = path.replace("\\", "/")
    while rel.startswith("./"):
        rel = rel[2:]
    return rel


def set_description(ctx: Ctx, cfg: Config, path: str, desc: str) -> Tuple[Optional[dict], Optional[str]]:
    rel = _relative(path)
    tracked = set(gitutil.ls_files(ctx.root))
    if rel not in tracked:
        return None, "not a tracked file: " + rel
    desc = " ".join(desc.split())
    if not desc:
        return None, "description is empty"
    # Hash the worktree file rather than trusting the scan cache: a description
    # is about the content in front of the agent right now, and the cache may
    # predate an edit made moments ago.
    blob = gitutil.short_blob(gitutil.hash_object(ctx.root, rel))
    if not blob:
        blob = gitutil.short_blob(gitutil.ls_files_stage(ctx.root).get(rel, ""))
    rec = {"path": rel, "desc": desc, "hash": blob,
           "who": gitutil.user_email(ctx.root), "ts": util.now_iso()}
    existing = load_descriptions(ctx)
    existing[rel] = rec
    util.write_jsonl(ctx.files_jsonl, [existing[k] for k in sorted(existing)])
    return rec, None


def pending_descriptions(ctx: Ctx, cfg: Config,
                         files: Optional[Dict[str, dict]] = None) -> List[dict]:
    if files is None:
        scan = util.read_json(ctx.scan_json, default={}) or {}
        files = scan.get("files") or {}
    described = load_descriptions(ctx)
    churn = (util.read_json(ctx.scan_json, default={}) or {}).get("churn") or {}
    rows: List[dict] = []
    for path, rec in files.items():
        if rec.get("binary"):
            continue
        have = described.get(path)
        if have is None:
            reason = "new"
        elif have.get("hash") != rec.get("blob"):
            reason = "changed"
        else:
            continue
        rows.append({"path": path, "reason": reason,
                     "churn": churn.get(path, 0), "tokens": rec.get("tokens", 0)})
    rows.sort(key=lambda r: (-r["churn"], -r["tokens"], r["path"]))
    return rows


# ---------------------------------------------------------------------------
# Renames (BUILD-SPEC 17.4)
# ---------------------------------------------------------------------------

def _spec_would_match(path: str, specs: Sequence[str]) -> bool:
    """Cheap local approximation of git pathspec matching, used only to decide
    whether a rename needs reporting. `git ls-files` cannot answer for a path
    that no longer exists."""
    matcher = ignore.Matcher(list(specs))
    return matcher.ignored(path)


# ---------------------------------------------------------------------------
# map
# ---------------------------------------------------------------------------

def build_map(ctx: Ctx, cfg: Config, pathspec: str) -> List[dict]:
    spec = pathspec.rstrip("/")
    candidates = gitutil.match_pathspecs(ctx.root, [spec])
    if not candidates:
        candidates = gitutil.match_pathspecs(ctx.root, [spec + "/**"])
    if not candidates and spec in (".", ""):
        candidates = gitutil.ls_files(ctx.root)
    matcher = ignore.load(ctx.siftignore)
    candidates = [p for p in candidates if not ctx.is_sift_path(p) and not matcher.ignored(p)]
    scan = util.read_json(ctx.scan_json, default={}) or {}
    files = scan.get("files") or {}
    described = load_descriptions(ctx)
    rows: List[dict] = []
    for path in sorted(candidates):
        rec = files.get(path, {})
        rows.append({
            "path": path,
            "desc": (described.get(path) or {}).get("desc", ""),
            "tokens": rec.get("tokens", 0),
            "symbols": rec.get("symbols", []),
        })
    return rows
