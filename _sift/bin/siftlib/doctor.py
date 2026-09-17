"""`sift doctor` — environment, wiring, and whether the hooks reach a session.

Every check returns {id, ok, detail, fix}. `--fix` repairs only what is safe to
repair without a judgement call: cache dirs, `core.hooksPath`, the generated
index. It never rewrites content a human or an agent wrote.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from . import gitutil, hookcheck, openwolf, templates, util
from .config import Config
from .paths import Ctx

# A complete registration, per agent, as `templates/claude/settings.hooks.json`
# and `templates/codex/hooks.json` ship it.
HOOK_ENTRIES = {"Claude": 8, "Codex": 6}

TIME_SENSITIVE = re.compile(
    r"(?i)\bas of\b|\bcurrently\b|\b(january|february|march|april|may|june|july|"
    r"august|september|october|november|december)\s+20\d\d\b")
SKILL_NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


def _check(checks: List[dict], cid: str, ok: bool, detail: str, fix: str = "") -> None:
    checks.append({"id": cid, "ok": bool(ok), "detail": detail, "fix": fix})


def run(ctx: Ctx, cfg: Config, fix: bool = False,
        check_upstream: bool = False) -> Dict[str, Any]:
    checks: List[dict] = []
    fixed: List[str] = []

    # --- environment -------------------------------------------------------
    ver = sys.version_info
    _check(checks, "python", ver >= (3, 9),
           "python {}.{}.{}".format(ver[0], ver[1], ver[2]),
           "install or expose python3 >= 3.9")
    _check(checks, "git", gitutil.available(), "git present", "install git")

    # --- layout ------------------------------------------------------------
    _check(checks, "sift-dir", ctx.exists, str(ctx.rel(ctx.dir)),
           "run `sift init`")
    for name, path in (("bin", ctx.dir / "bin" / "sift.py"),
                       ("hook.sh", ctx.dir / "bin" / "hook.sh"),
                       ("conventions", ctx.conventions),
                       ("decisions", ctx.decisions)):
        _check(checks, "file-" + name, path.exists(), ctx.rel(path), "run install.py again")

    for path in (ctx.cache, ctx.local):
        if not path.exists() and fix:
            path.mkdir(parents=True, exist_ok=True)
            fixed.append(ctx.rel(path))
        _check(checks, "dir-" + path.name.strip("."), path.exists(), ctx.rel(path),
               "mkdir " + ctx.rel(path))

    # --- config ------------------------------------------------------------
    unknown, invalid = None, None
    if ctx.config_path.exists():
        from . import config as config_mod
        unknown, invalid = config_mod.validate(ctx.config_path)
        _check(checks, "config-keys", not unknown,
               "unknown keys: " + ", ".join(unknown) if unknown else "no unknown keys",
               "remove them or check the spelling")
        _check(checks, "config-values", not invalid,
               "; ".join(invalid) if invalid else "values valid", "correct the values")

    # --- git wiring --------------------------------------------------------
    # Every append-only file needs the union rule, and the rule has to name the
    # file that exists today. `append-stanza-once` will not rewrite a stanza it
    # has already written, so a repo that upgrades across a rename keeps a rule
    # pointing at a file that is gone -- which looks installed and merges like
    # nothing (D-20260915-10, where decisions.md became decisions.jsonl).
    attrs = util.read_text(ctx.root / ".gitattributes")
    union = set()
    for line in attrs.splitlines():
        line = line.split("#", 1)[0].split()
        if len(line) > 1 and "merge=union" in line[1:]:
            union.add(line[0])
    want = [f.format(dir=ctx.dir_name) for f in templates.EMPTY_FILES]
    missing = [f for f in want if f not in union]
    stale = sorted(p for p in union
                   if p.startswith(ctx.dir_name + "/") and p not in want
                   and not (ctx.root / p).exists())
    detail = "merge=union on {} append-only file{}".format(
        len(want), "" if len(want) == 1 else "s")
    if missing:
        detail = "no merge=union rule for " + ", ".join(missing)
    elif stale:
        detail = "merge=union names {}, which no longer exists".format(", ".join(stale))
    _check(checks, "gitattributes", not missing and not stale, detail,
           "match .gitattributes to templates/stanza/gitattributes — install.py "
           "will not rewrite a stanza it has already written")

    ignored = util.read_text(ctx.root / ".gitignore")
    want_ignore = ctx.dir_name + "/.cache/"
    _check(checks, "gitignore", want_ignore in ignored,
           "cache ignored" if want_ignore in ignored else "cache not ignored",
           "append the stanza from templates/stanza/gitignore")

    # local/ is where anything private is allowed to live, which only holds
    # while it is genuinely untracked. Checked, not assumed.
    local_prefix = ctx.dir_name + "/local/"
    tracked_local = [p for p in gitutil.ls_files(ctx.root) if p.startswith(local_prefix)]
    _check(checks, "local-private", local_prefix in ignored and not tracked_local,
           "local/ is gitignored and untracked" if local_prefix in ignored and not tracked_local
           else ("{} file(s) under local/ are tracked".format(len(tracked_local))
                 if tracked_local else "local/ is not in .gitignore"),
           "git rm --cached the tracked files and restore the .gitignore stanza")

    hooks_path = gitutil.config_get(ctx.root, "core.hooksPath")
    if (ctx.root / ".githooks").is_dir():
        if not hooks_path and fix:
            gitutil.config_set(ctx.root, "core.hooksPath", ".githooks")
            hooks_path = ".githooks"
            fixed.append("core.hooksPath")
        _check(checks, "hooks-path", hooks_path == ".githooks",
               "core.hooksPath = " + (hooks_path or "(unset)"),
               "git config core.hooksPath .githooks")

        # Installed is not the same as reachable. Until `sift init` learned to
        # wrap an existing hook it appended to one, and a hook that ends in
        # `exit 0` swallows everything after it -- on the repo this was found
        # on, the secret check had been dead since the day it went in.
        hook = ctx.root / ".githooks" / "pre-commit"
        if hook.is_file():
            text = util.read_text(hook)
            marker = text.find("sift.py")
            before = text[:marker] if marker > 0 else ""
            dead = bool(before) and any(
                line.strip() in ("exit 0", "exit 1") and not line.startswith((" ", "\t"))
                for line in before.splitlines())
            _check(checks, "hooks-reachable", not dead,
                   "sift's block in .githooks/pre-commit is unreachable — an "
                   "`exit` above it ends the script" if dead
                   else "pre-commit reaches sift's check",
                   "move your own hook to .githooks/pre-commit.local and re-run "
                   "`sift install`, which wraps it instead of appending")

    # Each agent is judged on its own count. An `or` across the two read as
    # healthy whenever either side was complete, so a Claude registration that
    # had been wiped stayed invisible for as long as Codex's six entries lived.
    registered: Dict[str, int] = {}
    absent: List[str] = []
    for agent, settings in (("Claude", ctx.root / ".claude" / "settings.json"),
                            ("Codex", ctx.root / ".codex" / "hooks.json")):
        count = 0
        data = util.read_json(settings, default=None)
        if isinstance(data, dict):
            for entries in (data.get("hooks") or {}).values():
                for entry in entries or []:
                    if templates.hook_entry_is_ours(entry, ctx.dir_name):
                        count += 1
        registered[agent] = count
        # No file at all is an agent this repo does not run, which is nothing
        # to report. A file with too few entries is hooks that have gone.
        if not settings.is_file():
            absent.append(agent)
    short = [a for a in registered
             if a not in absent and registered[a] < HOOK_ENTRIES[a]]
    detail = ", ".join(
        "no {} hooks file".format(agent) if agent in absent
        else "{} of {} {} hook entries".format(
            registered[agent], HOOK_ENTRIES[agent], agent)
        for agent in registered)
    _check(checks, "hooks-registered",
           len(absent) < len(registered) and not short, detail,
           "run install.py to merge the hook block")

    # Registered is not the same as delivered. The transcript records every
    # hook invocation, and that record is the only evidence that is not this
    # tool's own account of itself -- which is exactly where this project has
    # been wrong before.
    session = hookcheck.latest_session(ctx.root, ctx.dir)
    delivery = hookcheck.verify(session, ctx.root) if session else {}
    if delivery.get("available"):
        _check(checks, "hooks-delivered", delivery["failed"] == 0,
               "{} invocations in the last session, {} failed".format(
                   delivery["fired"], delivery["failed"]),
               "check the failing hook's stderr in the transcript")
    else:
        _check(checks, "hooks-delivered", True,
               "not verifiable (no transcript, or its format has moved)")

    # --- caches ------------------------------------------------------------
    # Judged on what the scan keyed on, not on HEAD. Comparing HEAD turned this
    # red after every commit -- including commits of content the scan had
    # already described -- and left it green through an uncommitted edit, which
    # is the case that actually makes the cache wrong. A check that is red most
    # of the time trains people to ignore it. `scan.scan_inputs` is the single
    # producer of the keyed set; this side only digests it (D-20260917-01).
    from . import scan as scan_mod
    scan = util.read_json(ctx.scan_json, default=None)
    recorded = (scan or {}).get("inputs") if isinstance(scan, dict) else None
    shown = (scan or {}).get("head", "(absent)") if isinstance(scan, dict) else "(absent)"
    current = scan_mod.inputs_digest(scan_mod.scan_inputs(ctx))
    fresh = bool(recorded) and recorded == current
    if not isinstance(scan, dict):
        detail = "no scan cache"
    elif not recorded:
        # A cache written before the digest existed. Unjudgeable, so stale.
        detail = "scan cache at {} predates the content digest".format(shown)
    elif fresh:
        detail = "scan cache at {} matches the index and worktree".format(shown)
    else:
        detail = "scan cache at {} describes different content".format(shown)
    _check(checks, "cache-scan", fresh, detail, "run `sift scan`")

    # --- OpenWolf ----------------------------------------------------------
    found = openwolf.detect(ctx.root)
    migrated = any(e.get("kind") == "migrate" for e in util.read_jsonl(ctx.journal))
    _check(checks, "openwolf", not found,
           "no OpenWolf" if not found else "; ".join(f["path"] for f in found),
           # Telling someone who has already migrated to migrate again is how a
           # health check trains people to ignore it.
           _openwolf_fix(ctx.root, found)
           if migrated else "sift install --migrate-openwolf")
    # Only sweep the repo once a migration has happened: in a repo that never
    # ran OpenWolf every mention is somebody else's, and in this tool's own
    # repo the scan would match its entire migration module.
    if migrated:
        reviewed = list(cfg.get("openwolf", "reviewed", default=[]) or [])
        wide = openwolf.repo_remnants(ctx.root, ctx.dir_name, reviewed)
        # A reviewed path that no longer mentions it is a note that has gone
        # stale; say so, or the list quietly becomes a blanket suppression.
        still = {r["path"] for r in openwolf.repo_remnants(ctx.root, ctx.dir_name)}
        dead = sorted(p for p in reviewed if p not in still)
        detail = "no tracked file mentions OpenWolf" if not wide else \
            "{} files still mention it: {}".format(
                len(wide), ", ".join(w["path"] for w in wide[:5]))
        if reviewed and not wide:
            detail += "; {} kept after review".format(len(reviewed) - len(dead))
        if dead:
            detail += "; {} reviewed path(s) no longer mention it: {}".format(
                len(dead), ", ".join(dead[:3]))
        _check(checks, "openwolf-repo", not wide and not dead, detail,
               "sort each by hand, or record the ones you are keeping "
               "in config.json under openwolf.reviewed"
               if wide else "drop the stale entries from openwolf.reviewed")

    left = openwolf.remnants(ctx.root)
    from . import followup as followup_mod
    todo = followup_mod.pending(ctx, cfg)
    _check(checks, "setup-complete", not todo,
           "; ".join(item["what"] for item in todo) if todo
           else "nothing left to set up",
           "; ".join(item["how"] for item in todo))
    data_todo = todo

    _check(checks, "openwolf-remnants", not left,
           "none" if not left else "; ".join(
               "{} § {} ({} lines)".format(r["path"], r["heading"], r["lines"])
               for r in left[:4]),
           "delete each section or rewrite it for the sift directory — nothing here was "
           "written by this tool, so it is your call")

    # Local, always on: the clone on this machine, if there is one. UNKNOWN is
    # the normal answer in CI and on any machine without a clone, and it is not
    # a finding -- "cannot tell" must never read as "out of date".
    from . import upgrade as upgrade_mod
    runtime = upgrade_mod.status(ctx)
    _check(checks, "runtime", runtime["state"] not in upgrade_mod.STALE,
           runtime["detail"] or runtime["state"],
           "sift update — it replaces {}/bin, so give it its own commit".format(ctx.dir_name))

    if check_upstream:
        checks.append(_upstream_check(ctx))

    failed = [c for c in checks if not c["ok"]]
    return {"checks": checks, "failed": len(failed), "fixed": fixed,
            "todo": data_todo}


def _openwolf_fix(root: Path, found: List[Dict[str, str]]) -> str:
    """The removal advice, as a command that runs clean on this repo.

    A repo that stopped tracking `.wolf/` before it migrated has nothing in the
    index to remove, and a hardcoded `git rm --cached .wolf` then exits 128 with
    `fatal: pathspec '.wolf' did not match any files`. The delete after it still
    runs, so the advice works and reads as if it did not — which is the worst of
    the two. `--ignore-unmatch` is what `remove_openwolf` already passes.
    """
    dirs = [str(w.relative_to(root)) for w in openwolf.wolf_dirs(root)]
    if not dirs:
        return "remove what is left: " + ", ".join(f["path"] for f in found)
    return "remove what is left: " + "; ".join(
        "git rm -r -q --cached --ignore-unmatch {0}; rm -rf {0}".format(d)
        for d in dirs)


def _upstream_check(ctx: Ctx) -> dict:
    """The one network call in the tool, and only behind an explicit flag."""
    url = "https://raw.githubusercontent.com/SFT-Experiments/sift/main/src/VERSION"
    local = util.read_text(ctx.dir / "bin" / "VERSION").strip() or "unknown"
    try:
        import urllib.request
        with urllib.request.urlopen(url, timeout=5) as resp:  # nosec - opt-in
            remote = resp.read().decode("utf-8").strip()
    except Exception as exc:  # noqa: BLE001 - any network failure is just "unknown"
        return {"id": "upstream", "ok": True,
                "detail": "local {}; upstream unknown ({})".format(local, exc.__class__.__name__),
                "fix": ""}
    return {"id": "upstream", "ok": local == remote,
            "detail": "local {} / upstream {}".format(local, remote),
            "fix": "re-run install.py from a fresh clone"}
