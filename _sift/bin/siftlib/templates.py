"""Template resolution, rendering, and the install manifest (BUILD-SPEC 3).

Ruling 19: the templates ship inside `bin/` in the target repo, so `sift init`
is self-sufficient in a clone that never saw `install.py`. The resolver finds
them next to `sift.py` (installed: `bin/templates/`) or two levels up (this
repo: `src/sift.py` -> `templates/`).

The manifest is data. Each entry is (source, dest, mode) and each mode has one
function; adding a file to the install is a table row.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from . import util

BEGIN = "<!-- sift:begin"
END = "<!-- sift:end -->"
STANZA_SENTINEL = "# sift:"

# (source relative to templates/, dest relative to root - {dir} expanded, mode)
# Every generated block says this in its opening marker, and every one behaves
# that way: `apply_marker_block` rewrites what is between the markers and
# leaves everything outside them alone. `test_templates` holds the templates to
# the promise. Keeping local notes below the end marker is the whole contract,
# and it is simpler than any conflict machinery that tried to guess.
OVERWRITE_BANNER = "overwritten on upgrade"

MANIFEST: List[Tuple[str, str, str]] = [
    ("sift/README.md", "{dir}/README.md", "marker-block"),
    ("sift/conventions.md", "{dir}/conventions.md", "marker-block"),
    ("sift/config.json", "{dir}/config.json", "copy-if-absent"),
    ("sift/.siftignore", "{dir}/.siftignore", "copy-if-absent"),
    ("sift/.gitignore", "{dir}/.gitignore", "copy-if-absent"),
    ("claude/settings.hooks.json", ".claude/settings.json", "merge-json-hooks"),
    ("codex/hooks.json", ".codex/hooks.json", "merge-json-hooks"),
    ("stanza/gitattributes", ".gitattributes", "append-stanza-once"),
    ("stanza/gitignore", ".gitignore", "append-stanza-once"),
    ("stanza/CLAUDE.md", "CLAUDE.md", "marker-block"),
    ("stanza/CLAUDE.md", "AGENTS.md", "marker-block"),
]

GITHOOKS: List[Tuple[str, str]] = [
    ("githooks/post-merge", ".githooks/post-merge"),
    ("githooks/post-checkout", ".githooks/post-checkout"),
    ("githooks/post-rewrite", ".githooks/post-rewrite"),
    ("githooks/pre-commit", ".githooks/pre-commit"),
]

EMPTY_FILES = ["{dir}/journal.jsonl", "{dir}/files.jsonl", "{dir}/decisions.jsonl"]
KEEP_DIRS: List[str] = []  # areas/ and flows/ went with the pages
PLAIN_DIRS = ["{dir}/local", "{dir}/.cache"]


def templates_root(anchor: Optional[Path] = None) -> Optional[Path]:
    here = Path(anchor or __file__).resolve()
    # bin/sift.py -> bin/templates (installed); src/sift.py -> ../templates (this
    # repo); siftlib/templates.py -> either, depending on which copy is running.
    for base in (here.parent, here.parent.parent, here.parent.parent.parent):
        candidate = base / "templates"
        if candidate.is_dir():
            return candidate
    return None


def render(text: str, subs: Dict[str, str]) -> str:
    def repl(m: "re.Match[str]") -> str:
        key = m.group(1)
        return subs.get(key, m.group(0))
    return re.sub(r"\{\{(dir|repo|head|now|advisory)\}\}", repl, text)


def substitutions(dir_name: str, repo: str, head: str = "", now: str = "",
                  advisory: bool = True) -> Dict[str, str]:
    return {
        "dir": dir_name,
        "repo": repo,
        "head": head,
        "now": now or util.now_iso(),
        "advisory": " || true" if advisory else "",
    }


# ---------------------------------------------------------------------------
# Modes
# ---------------------------------------------------------------------------

def apply_copy_replace(dest: Path, text: str, force: bool = False) -> str:
    existed = dest.exists()
    if existed and util.read_text(dest) == text:
        return "skipped"
    util.atomic_write(dest, text)
    return "merged" if existed else "created"


def apply_copy_if_absent(dest: Path, text: str, force: bool = False) -> str:
    if dest.exists():
        return "skipped"
    util.atomic_write(dest, text)
    return "created"


def marker_split(text: str) -> Tuple[str, Optional[str], str]:
    """(before, block-including-markers, after). block is None when absent."""
    start = text.find(BEGIN)
    if start < 0:
        return text, None, ""
    end = text.find(END, start)
    if end < 0:
        return text, None, ""
    end += len(END)
    return text[:start], text[start:end], text[end:]


def heal_orphan_block(text: str) -> str:
    """Drop a closing marker whose opening marker was lost.

    Losing the opening marker makes the block invisible to `marker_split`, so
    an install appends a second copy and the repo ends up telling an agent the
    same thing twice, in two versions. Only our own stanza is removed: the
    search walks back to the `# sift` heading that every version of it starts
    with, and gives up if there is not one.
    """
    for _ in range(8):   # a file has no business holding more orphans than this
        end_pos = text.find(END)
        if end_pos < 0:
            return text
        begin_pos = text.find(BEGIN)
        if 0 <= begin_pos < end_pos:
            return text       # the first block is intact; nothing orphaned above it
        lines = text[:end_pos].splitlines()
        start_at = None
        for i in range(len(lines) - 1, -1, -1):
            if lines[i].strip() == "# sift":
                start_at = i
                break
        if start_at is None:
            return text
        rest = text[end_pos:].split("\n", 1)
        kept = lines[:start_at] + ([rest[1]] if len(rest) > 1 else [])
        while kept and not kept[0].strip():
            kept.pop(0)
        text = "\n".join(kept).strip("\n") + "\n" if kept else ""
    return text


def apply_marker_block(dest: Path, text: str) -> str:
    """Replace our marker block, leaving everything outside it untouched.

    An edit inside the markers goes, without a warning and without a way to
    keep it: the marker says so, in every block that is written. Local notes
    belong below the end marker, where nothing touches them.
    """
    if not dest.exists():
        util.atomic_write(dest, text if text.endswith("\n") else text + "\n")
        return "created"
    original = util.read_text(dest)
    current = heal_orphan_block(original)
    healed = current != original
    before, block, after = marker_split(current)
    if block is None:
        joined = current.rstrip("\n") + "\n\n" + text.lstrip("\n")
        util.atomic_write(dest, joined if joined.endswith("\n") else joined + "\n")
        return "merged"
    if block.strip() == text.strip() and not healed:
        return "skipped"
    if block.strip() == text.strip():
        # The block is current, but there was a second one above it to remove.
        util.atomic_write(dest, current if current.endswith("\n") else current + "\n")
        return "merged"
    joined = before + text.strip("\n") + after
    util.atomic_write(dest, joined if joined.endswith("\n") else joined + "\n")
    return "merged"


def apply_append_stanza_once(dest: Path, text: str, force: bool = False) -> str:
    """Idempotent by the sentinel comment on the stanza's first line."""
    first = text.strip().splitlines()[0].strip()
    current = util.read_text(dest) if dest.exists() else ""
    if first in current:
        return "skipped"
    joined = (current.rstrip("\n") + "\n\n" if current.strip() else "") + text.strip("\n") + "\n"
    util.atomic_write(dest, joined)
    return "merged" if current else "created"


def hook_entry_is_ours(entry: dict, dir_name: str) -> bool:
    for hook in entry.get("hooks", []) or []:
        if dir_name + "/bin/hook.sh" in str(hook.get("command", "")):
            return True
    return False


def apply_merge_json_hooks(dest: Path, text: str, dir_name: str,
                           force: bool = False) -> str:
    """Replace our hook entries in .claude/settings.json; leave others alone."""
    try:
        ours = json.loads(text)
    except ValueError:
        return "skipped"
    current: Dict[str, Any] = {}
    if dest.exists():
        loaded = util.read_json(dest, default=None)
        if isinstance(loaded, dict):
            current = loaded
    existed = dest.exists()
    hooks = current.get("hooks")
    if not isinstance(hooks, dict):
        hooks = {}
    for event, entries in ours.get("hooks", {}).items():
        keep = [e for e in hooks.get(event, []) or []
                if not hook_entry_is_ours(e, dir_name)]
        hooks[event] = keep + list(entries)
    current["hooks"] = hooks
    util.atomic_write(dest, json.dumps(current, indent=2, ensure_ascii=False) + "\n")
    return "merged" if existed else "created"


def remove_our_hooks(dest: Path, dir_name: str) -> int:
    """Used by eject and by the OpenWolf migration for `.wolf/` entries."""
    if not dest.exists():
        return 0
    data = util.read_json(dest, default=None)
    if not isinstance(data, dict) or not isinstance(data.get("hooks"), dict):
        return 0
    removed = 0
    for event, entries in list(data["hooks"].items()):
        keep = []
        for entry in entries or []:
            if hook_entry_is_ours(entry, dir_name):
                removed += 1
            else:
                keep.append(entry)
        data["hooks"][event] = keep
    if removed:
        util.atomic_write(dest, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    return removed
