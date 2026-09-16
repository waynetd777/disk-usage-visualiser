"""OpenWolf detection (BUILD-SPEC 15.1).

`sift init` hard-stops when it finds OpenWolf. The two tools both register
SessionStart and pre-read hooks, both maintain a file index, and both ask the
agent to log the same bug in a different place. There is no coexist flag
(Ruling 11): removing a knowledge base is the user's keystroke.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Sequence

from . import util

CLAUDE_MD_PATTERNS = [
    re.compile(r"@\.wolf/OPENWOLF\.md"),
    re.compile(r"This project uses OpenWolf"),
]
REMNANT_RE = re.compile(r"(?i)openwolf|\.wolf/|designqc|cerebrum")


def wolf_dirs(root: Path) -> List[Path]:
    """Every `.wolf/` in the repo, not just the one at the top.

    A workspace inside a repo gets its own: the repo this was found on had a
    tracked `ui/.wolf/memory.md` and a directory of screenshots that survived
    a migration untouched, because nothing had ever looked below the root.
    """
    out = []
    if (root / ".wolf").is_dir():
        out.append(root / ".wolf")
    for path in sorted(root.glob("*/.wolf")) + sorted(root.glob("*/*/.wolf")):
        if path.is_dir() and not any(
                part in ("node_modules", ".git", "dist", "build")
                for part in path.relative_to(root).parts):
            out.append(path)
    return out


def detect(root: Path) -> List[Dict[str, str]]:
    """[{path, what}] — empty when the repo is clean."""
    found: List[Dict[str, str]] = []

    for wolf in wolf_dirs(root):
        rel = str(wolf.relative_to(root)) + "/"
        found.append({"path": rel, "what": "index, cerebrum, buglog, hooks"})

    for name in (".claude/settings.json", ".claude/settings.local.json"):
        path = root / name
        data = util.read_json(path, default=None)
        if not isinstance(data, dict):
            continue
        count = 0
        for entries in (data.get("hooks") or {}).values():
            for entry in entries or []:
                for hook in entry.get("hooks", []) or []:
                    if ".wolf/hooks" in str(hook.get("command", "")):
                        count += 1
        if count:
            found.append({"path": name, "what": "{} hook entries -> .wolf/hooks/*.js".format(count)})

    for name in ("CLAUDE.md", "AGENTS.md"):
        path = root / name
        if not path.is_file():
            continue
        for i, line in enumerate(util.read_text(path).splitlines(), 1):
            if any(p.search(line) for p in CLAUDE_MD_PATTERNS):
                found.append({"path": "{} line {}".format(name, i), "what": line.strip()[:60]})
                break

    if (root / ".claude/rules/openwolf.md").is_file():
        found.append({"path": ".claude/rules/openwolf.md", "what": "OpenWolf rules file"})

    codex = root / ".codex/hooks.json"
    if codex.is_file() and ".wolf/" in util.read_text(codex):
        found.append({"path": ".codex/hooks.json", "what": "hook entries"})

    attrs = root / ".gitattributes"
    if attrs.is_file():
        n = sum(1 for l in util.read_text(attrs).splitlines() if l.strip().startswith(".wolf/"))
        if n:
            found.append({"path": ".gitattributes", "what": "{} .wolf/ merge rules".format(n)})

    ignore = root / ".gitignore"
    if ignore.is_file():
        for line in util.read_text(ignore).splitlines():
            s = line.strip()
            # Only an active rule counts. A surviving comment about OpenWolf is
            # a remnant to review, not evidence that OpenWolf is installed --
            # otherwise `doctor` would never go green after a migration.
            if s == ".wolf/":
                found.append({"path": ".gitignore", "what": s[:60]})
                break

    return found


# Read only, never written. It belongs to the other tool and lists projects
# beyond this repo; the most this tool may do is say it is still there.
REGISTRY = Path("~/.openwolf/registry.json").expanduser()


def registered(root: Path) -> bool:
    """True when this repo is still listed in OpenWolf's machine-level registry.

    Deleting `.wolf/` does not deregister the project, and the CLI exposes no
    command that would -- `unregisterProject` exists in its source but nothing
    calls it. The entry is inert rather than dangerous: `openwolf update` asks
    for registered projects with `validateExists`, which skips any whose
    `.wolf/` is gone, and deliberately does not persist that prune so an
    unmounted volume is not deregistered by a read-only listing.

    So this is reported once, in the migration's closing instructions, as
    tidying rather than as a risk. It is never edited: the file lists the
    user's other projects.
    """
    try:
        raw = util.read_json(REGISTRY, default=None)
    except OSError:
        return False
    if raw is None:
        return False
    target = str(root.resolve())
    return target in json.dumps(raw)


def report(found: List[Dict[str, str]], dir_name: str,
           command: str = "sift install") -> str:
    if not found:
        return ""
    width = max(len(f["path"]) for f in found)
    lines = ["OpenWolf detected in this repo:"]
    for f in found:
        lines.append("  {}  ({})".format(f["path"].ljust(width), f["what"]))
    lines += [
        "",
        "sift does not run alongside OpenWolf. Either:",
        "  {} --migrate-openwolf".format(command),
        "      import its content, remove its hooks, guide you through removing .wolf/",
        "  remove OpenWolf yourself, then run `{}` again".format(command),
    ]
    return "\n".join(lines)


def repo_remnants(root: Path, dir_name: str,
                  reviewed: Sequence[str] = ()) -> List[Dict[str, Any]]:
    """Every tracked file still referring to OpenWolf, outside the sift directory.

    Scoped to the whole repo because the pieces that matter are bespoke: a
    pre-commit hook guarding paths under `.wolf/`, a README setup note, a
    skill that writes to `cerebrum.md`. None of those are in the places
    `detect` knows to look, and all of them break silently once `.wolf/` is
    gone -- the hook stops guarding anything, and nobody notices.

    The sift directory is excluded: `local/` holds the imported cerebrum on purpose,
    the journal holds imported bug entries, and a page may legitimately
    discuss the migration.
    """
    from . import gitutil

    # This tool's own source implements the migration, so every module, test
    # and template mentions OpenWolf. Scanning it would report the machinery
    # as a leftover of itself.
    if (root / "src" / "siftlib" / "migrate_openwolf.py").is_file():
        return []

    hits = gitutil.grep(
        root, r"openwolf|\.wolf/|cerebrum|designqc",
        # The sift directory holds imported notes on purpose; the skills we install
        # describe how to clean this very mess.
        exclude=[dir_name + "/"])
    # Some mentions are legitimate and permanent: the npm package may still be
    # a live dependency for something unrelated, or a guard may still name it
    # on purpose. A check that cannot reach green on an accurate repo gets
    # ignored, so the answer is a recorded decision, not a deleted sentence.
    keep = set(reviewed)
    grouped: Dict[str, Dict[str, Any]] = {}
    for path, number, text in hits:
        if path in keep:
            continue
        entry = grouped.setdefault(path, {"path": path, "lines": 0, "first": "",
                                          "first_line": number})
        entry["lines"] += 1
        entry["first"] = entry["first"] or text
    return [grouped[k] for k in sorted(grouped)]


def remnants(root: Path) -> List[Dict[str, Any]]:
    """Prose about OpenWolf left behind after a migration.

    Grouped by the heading it sits under, not reported line by line. "11 lines
    mention OpenWolf" reads like stray references and gets skipped; "two whole
    sections are about a tool you just removed" is a decision someone has to
    make. Never edited automatically -- some of it is a real rule worth keeping
    in a rewritten form, and none of it was written by this tool.
    """
    from . import gitutil

    out: List[Dict[str, Any]] = []
    tracked = set(gitutil.ls_files(root))
    for name in ("CLAUDE.md", "AGENTS.md"):
        path = root / name
        if not path.is_file():
            continue
        # An untracked or gitignored file cannot be read by a colleague and
        # cannot be leaked, so stale prose in it is not this tool's business.
        # In the first real migration, AGENTS.md was gitignored and regenerated
        # per clone by the OpenWolf CLI itself -- permanently red, and nothing
        # the user could do about it without hand-editing a generated file.
        if name not in tracked:
            continue
        heading, start, hits, first = "(top of file)", 1, 0, ""

        def flush() -> None:
            if hits:
                out.append({"path": name, "heading": heading, "line": start,
                            "lines": hits, "text": first})

        for i, line in enumerate(util.read_text(path).splitlines(), 1):
            if line.startswith("#"):
                flush()
                heading, start, hits, first = line.lstrip("# ").strip(), i, 0, ""
                if REMNANT_RE.search(line):
                    hits, first = 1, line.strip()[:100]
                continue
            if REMNANT_RE.search(line):
                hits += 1
                first = first or line.strip()[:100]
        flush()
    return out
