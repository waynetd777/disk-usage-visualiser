"""`sift init --migrate-openwolf` / `sift import --from-openwolf` (BUILD-SPEC 15.2).

Imports what OpenWolf knew, removes its hooks, and shows you the diffs for the
prose changes. It never deletes `.wolf/`: removing a knowledge base is the
user's keystroke (Ruling 11), so the last thing printed is the command to do it.
"""
from __future__ import annotations

import difflib
import json
import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import gitutil, journal, templates, util
from .openwolf import remnants
from .config import Config
from .paths import Ctx

VERBATIM = [
    ("cerebrum.md", "openwolf-cerebrum.md"),
    ("cerebrum.local.md", "openwolf-cerebrum.local.md"),
    ("STATUS.md", "openwolf-STATUS.md"),
    ("identity.md", "openwolf-identity.md"),
]
NOT_IMPORTED = ["memory.md", "token-ledger.json", "backups/", "anatomy.md",
                "other .json state files"]
REVIEW_ONLY = [".claude/rules/openwolf.md", ".codex/prompts/reframe.md",
               ".codex/prompts/security-audit.md"]



# What the import reads. Everything else under `.wolf/` is copied out before
# the directory goes, because no version of this importer has read all of it:
# one repo's only copy of its bug log was a `migration-quarantine.jsonl` that
# `rm -rf .wolf` would have taken with it.
IMPORTED = {name for name, _ in VERBATIM} | {"anatomy-index.json", "buglog.json"}


def removal_plan(ctx: Ctx) -> Dict[str, Any]:
    """What `remove_openwolf` would do, so the prompt can say it out loud."""
    from .openwolf import wolf_dirs

    wolves = wolf_dirs(ctx.root)
    rescue, dirs = [], []
    for wolf in wolves:
        prefix = "" if wolf.parent == ctx.root else str(
            wolf.parent.relative_to(ctx.root)).replace("/", "-") + "-"
        for entry in sorted(wolf.iterdir()):
            if entry.is_dir():
                dirs.append(str(entry.relative_to(ctx.root)) + "/")
            elif entry.name not in IMPORTED or prefix:
                # Only the root `.wolf/` was ever imported, so a nested one's
                # files are all unread however they are named.
                rescue.append({"from": str(entry.relative_to(ctx.root)),
                               "as": "openwolf-" + prefix + entry.name})
    rules = [p for p in REVIEW_ONLY if (ctx.root / p).is_file()]
    return {"rescue": [r["as"].replace("openwolf-", "", 1) for r in rescue],
            "copies": rescue, "dirs": dirs, "rules": rules,
            "wolf": bool(wolves),
            "wolf_dirs": [str(w.relative_to(ctx.root)) for w in wolves]}


def removal_prompt(plan: Dict[str, Any], dir_name: str) -> str:
    where = ", ".join(d + "/" for d in plan["wolf_dirs"])
    bits = ["Remove OpenWolf now?" + ((" " + where) if where else "")]
    if plan["rescue"]:
        bits.append("({} file{} the import did not read → {}/local/ first)".format(
            len(plan["rescue"]), "" if len(plan["rescue"]) == 1 else "s", dir_name))
    if plan["dirs"]:
        bits.append("and " + ", ".join(plan["dirs"]))
    if plan["rules"]:
        bits.append("and " + ", ".join(plan["rules"]))
    return " ".join(bits) + ". Deleted, not committed"


def remove_openwolf(ctx: Ctx) -> Dict[str, Any]:
    """Copy out what was never imported, then delete OpenWolf.

    Ruling 11 keeps removal on the user's keystroke. A y/N prompt is that
    keystroke; what it stops being is four commands to retype, which is how
    people end up with half-migrated repos.
    """
    import shutil

    plan = removal_plan(ctx)
    for item in plan["copies"]:
        shutil.copyfile(str(ctx.root / item["from"]), str(ctx.local / item["as"]))
    paths = list(plan["wolf_dirs"]) + plan["rules"]
    if paths:
        # --cached: git forgets them, the working tree deletion is ours below.
        gitutil.run(ctx.root, ["rm", "-r", "--cached", "-q", "--ignore-unmatch"] + paths)
    for rel in plan["wolf_dirs"]:
        shutil.rmtree(str(ctx.root / rel), ignore_errors=True)
    for rel in plan["rules"]:
        target = ctx.root / rel
        if target.is_file():
            target.unlink()
    return plan

def todo_lines(mig: "Migration") -> List[str]:
    """What the migration deliberately did not do, because only a person can."""
    if not mig.todo:
        return []
    return ["", "Your call:"] + ["  - " + item for item in mig.todo]


def manual_removal_lines(plan: Dict[str, Any]) -> List[str]:
    """The removal as commands, for when the offer to do it was declined or
    could not be made (`--yes`, a pipe, `--json`)."""
    if not (plan["wolf"] or plan["rules"]):
        return []
    cmds = []
    for rel in plan["wolf_dirs"]:
        cmds.append("  git rm -r --cached -q --ignore-unmatch {0}; rm -rf {0}".format(rel))
    for path in plan["rules"]:
        cmds.append("  git rm -q --cached --ignore-unmatch {0}; rm -f {0}".format(path))
    if plan["rescue"]:
        cmds.insert(0, "  # first, these are in .wolf/ and nowhere else: "
                       + ", ".join(plan["rescue"]))
    return ["", "When you want OpenWolf gone (sift will not delete a knowledge "
            "base on its own):"] + cmds + [
        '  git commit -am "Replace OpenWolf with sift"']


_CLAUDE_LINE_RE = re.compile(r"@\.wolf/OPENWOLF\.md")
_CLAUDE_PARA_RE = re.compile(r"This project uses OpenWolf")


class Migration:
    """Collects everything the migration would do so `--dry-run` and the real
    run share one code path."""

    def __init__(self, ctx: Ctx, cfg: Config, dry_run: bool = False,
                 confirm: Optional[Callable[[str], bool]] = None,
                 show: Optional[Callable[[str], None]] = None) -> None:
        self.ctx = ctx
        self.cfg = cfg
        self.dry_run = dry_run
        self.confirm = confirm or (lambda _msg: True)
        # Where a diff goes before the question about it. Asking first and
        # printing after is how you get a yes to something unseen.
        self.show = show or (lambda _text: None)
        self.log: List[str] = []
        # Split from `log` because the two are read differently: `log` is what
        # happened and is skimmed once, `todo` is what only a person can decide
        # and has to survive the skim. One flat list buried the second in the
        # first.
        self.todo: List[str] = []
        self.data: Dict[str, Any] = {
            "descriptions": {"kept": 0, "dropped": 0},
            "bugs": {"imported": 0, "quarantined": 0},
            "verbatim": [], "not_imported": list(NOT_IMPORTED),
            "review_only": [], "hooks_removed": {}, "diffs": [],
            "stanzas": [], "wolf_dir": False,
        }

    # -- step 1: content ----------------------------------------------------
    def import_content(self) -> None:
        wolf = self.ctx.root / ".wolf"
        self.data["wolf_dir"] = wolf.is_dir()
        if not wolf.is_dir():
            self.log.append("no .wolf/ directory — nothing to import")
            return
        self._import_descriptions(wolf)
        self._import_bugs(wolf)
        absent = [name for name, key in (("anatomy-index.json", "descriptions"),
                                         ("buglog.json", "bugs"))
                  if self.data[key].get("absent")]
        if absent:
            self.log.append("{}: not in this .wolf/ — nothing to import from {}".format(
                " and ".join(absent), "them" if len(absent) > 1 else "it"))
        self._copy_verbatim(wolf)
        for path in REVIEW_ONLY:
            if (self.ctx.root / path).exists():
                self.data["review_only"].append(path)
        if not self.dry_run:
            journal.append(
                self.ctx, "migrate",
                "OpenWolf import: {} descriptions, {} bugs, {} quarantined".format(
                    self.data["descriptions"]["kept"], self.data["bugs"]["imported"],
                    self.data["bugs"]["quarantined"]))

    def _import_descriptions(self, wolf: Path) -> None:
        from . import scan as scan_mod
        anatomy = util.read_json(wolf / "anatomy-index.json", default=None)
        if not isinstance(anatomy, dict):
            self.data["descriptions"]["absent"] = True
            return
        files = anatomy.get("files")
        if not isinstance(files, dict):
            self.log.append("anatomy-index.json: no `files` map")
            return
        tracked = set(gitutil.ls_files(self.ctx.root))
        stage = gitutil.ls_files_stage(self.ctx.root)
        who = gitutil.user_email(self.ctx.root)
        now = util.now_iso()
        existing = scan_mod.load_descriptions(self.ctx)
        kept = dropped = 0
        for path, rec in sorted(files.items()):
            desc = ""
            if isinstance(rec, dict):
                desc = str(rec.get("description") or "").strip()
            elif isinstance(rec, str):
                desc = rec.strip()
            if not desc or path not in tracked:
                dropped += 1
                continue
            # The description predates hashing, so we stamp it with the current
            # blob: W09 would otherwise fire on every imported line. The cost is
            # that an imported description may already be slightly stale.
            existing[path] = {"path": path, "desc": " ".join(desc.split())[:160],
                              "hash": stage.get(path, "")[:16], "who": who, "ts": now}
            kept += 1
        self.data["descriptions"] = {"kept": kept, "dropped": dropped}
        self.log.append("anatomy-index.json: {} descriptions kept, {} dropped "
                        "(untracked or empty)".format(kept, dropped))
        if not self.dry_run and kept:
            util.write_jsonl(self.ctx.files_jsonl,
                             [existing[k] for k in sorted(existing)])

    def _import_bugs(self, wolf: Path) -> None:
        from . import lint as lint_mod
        raw = util.read_json(wolf / "buglog.json", default=None)
        entries: List[dict] = []
        if isinstance(raw, dict):
            candidates = raw.get("bugs") or raw.get("entries") or []
        elif isinstance(raw, list):
            candidates = raw
        else:
            self.data["bugs"]["absent"] = True
            return
        who = gitutil.user_email(self.ctx.root)
        quarantine: List[dict] = []
        for item in candidates:
            if not isinstance(item, dict):
                continue
            ts = str(item.get("timestamp") or item.get("ts") or util.now_iso())
            ts = _normalise_ts(ts)
            error = str(item.get("error_message") or item.get("error") or "")
            entry = {
                "id": journal.make_id(ts, who, error),
                "ts": ts, "who": who,
                "commit": str(item.get("commit") or ""),
                "kind": "bug",
                "detail": error or "imported bug",
                "error": error,
                "root_cause": str(item.get("root_cause") or ""),
                "fix": str(item.get("fix") or ""),
                "openwolf_id": item.get("id"),
            }
            files = item.get("file") or item.get("files")
            if isinstance(files, str):
                entry["files"] = [files]
            elif isinstance(files, list):
                entry["files"] = [str(f) for f in files]
            tags = item.get("tags")
            if isinstance(tags, list):
                entry["tags"] = [str(t) for t in tags]
            line = util.jdump(entry)
            problems: List[dict] = []
            # W20 as well as W15/W16: an auto-captured bug log records whatever
            # file a session touched, and in the first real migration that
            # included a vault note whose filename named a person and a
            # compensation change. The journal is committed; this is the last
            # point at which that can be stopped.
            lint_mod._secret_scan(problems, "buglog", [line], lambda code: True)
            (quarantine if problems else entries).append(entry)
        self.data["bugs"] = {"imported": len(entries), "quarantined": len(quarantine)}
        self.log.append("buglog.json: {} entries imported, {} quarantined "
                        "(secret or out-of-repo path)".format(len(entries), len(quarantine)))
        if self.dry_run:
            return
        for entry in entries:
            util.append_line(self.ctx.journal, util.jdump(entry))
        if quarantine:
            for entry in quarantine:
                util.append_line(self.ctx.local / "openwolf-quarantine.jsonl",
                                 util.jdump(entry))

    def _copy_verbatim(self, wolf: Path) -> None:
        for source, dest_name in VERBATIM:
            src = wolf / source
            if not src.is_file():
                continue
            self.data["verbatim"].append(dest_name)
            self.log.append("{} -> local/{} (verbatim)".format(source, dest_name))
            if not self.dry_run:
                util.atomic_write(self.ctx.local / dest_name, util.read_text(src))

    # -- step 2: hooks ------------------------------------------------------
    def strip_hooks(self) -> None:
        removed: Dict[str, int] = {}
        for name in (".claude/settings.json", ".claude/settings.local.json"):
            path = self.ctx.root / name
            count = _strip_wolf_hooks(path, self.dry_run)
            if count:
                removed[name] = count
        codex = self.ctx.root / ".codex/hooks.json"
        if codex.is_file():
            count = _strip_wolf_codex(codex, self.dry_run)
            if count:
                removed[".codex/hooks.json"] = count
        self.data["hooks_removed"] = removed
        for name, count in removed.items():
            self.log.append("{}: removed {} OpenWolf hook entries".format(name, count))

    # -- step 3: CLAUDE.md / AGENTS.md -------------------------------------
    def clean_agent_files(self) -> None:
        for name in ("CLAUDE.md", "AGENTS.md"):
            path = self.ctx.root / name
            if not path.is_file():
                continue
            current = util.read_text(path)
            cleaned = _strip_openwolf_prose(current)
            if cleaned != current:
                diff = "\n".join(difflib.unified_diff(
                    current.splitlines(), cleaned.splitlines(),
                    fromfile=name, tofile=name + " (after)", lineterm=""))
                self.data["diffs"].append({"path": name, "diff": diff})
                self.show("\n" + diff + "\n")
                if not self.dry_run and self.confirm(
                        "Remove that OpenWolf block from " + name + "?"):
                    util.atomic_write(path, cleaned)
                    self.log.append("{}: OpenWolf block removed".format(name))
            sections = [r for r in remnants(self.ctx.root) if r["path"] == name]
            if sections:
                self.data.setdefault("review_by_hand", []).append(
                    {"path": name, "sections": sections})
                self.todo.append(
                    "{} still has {} — left untouched. {}, or rewrite "
                    "for {}/:\n{}".format(
                        name,
                        "1 OpenWolf section" if len(sections) == 1
                        else "{} OpenWolf sections".format(len(sections)),
                        "Delete it" if len(sections) == 1 else "Delete each",
                        self.ctx.dir_name,
                        "\n".join("      § {} ({} line{})".format(
                            s["heading"], s["lines"], "" if s["lines"] == 1 else "s")
                            for s in sections)))

    # -- step 3b: keep the corpse out of the churn ratings ------------------
    def ignore_wolf(self) -> None:
        """Add `.wolf/` to `.siftignore`.

        OpenWolf's hooks rewrote `cerebrum.md` every session, so on a migrated
        repo it can be the second-hottest file in the whole history — hotter
        than almost all real source. Ignoring it keeps a dead system out of the
        churn ratings that `scan` derives from git history.
        """
        path = self.ctx.siftignore
        current = util.read_text(path)
        if ".wolf/" in current.split("#")[0] or any(
                line.strip() == ".wolf/" for line in current.splitlines()):
            return
        self.data["siftignore"] = True
        self.log.append("{}/.siftignore: ignoring .wolf/ so its churn does not "
                        "dominate the churn ratings".format(self.ctx.dir_name))
        if self.dry_run:
            return
        block = "\n# sift: OpenWolf's own state, kept out of the churn ratings\n.wolf/\n"
        util.atomic_write(path, current.rstrip("\n") + block if current.strip() else block.lstrip("\n"))

    # -- step 4: gitattributes / gitignore ---------------------------------
    def clean_stanzas(self) -> None:
        """Report the `.wolf/` lines; never delete them.

        Deleting them is actively harmful in the window before `.wolf/` is
        removed: stripping `.wolf/*` from `.gitignore` un-ignores the whole
        OpenWolf state directory, so the next `git add -A` commits its backups
        and scan state. The comment block above such a rule is also prose
        somebody wrote — in the repo this was found in, thirteen lines
        explaining a deliberate per-repo decision — and a migration has no
        business deleting that unseen. They are inert once `.wolf/` is gone.
        See BUILD-SPEC Ruling 33.
        """
        for name in (".gitattributes", ".gitignore"):
            path = self.ctx.root / name
            if not path.is_file():
                continue
            hits = [{"line": i, "text": line.strip()[:100]}
                    for i, line in enumerate(util.read_text(path).splitlines(), 1)
                    if line.strip().startswith((".wolf/", "!.wolf/"))]
            if hits:
                self.data["stanzas"].append({"path": name, "lines": hits})
                self.log.append("{}: {} .wolf/ line{} left alone — inert once "
                                ".wolf/ is gone".format(
                                    name, len(hits), "" if len(hits) == 1 else "s"))

    def run_all(self) -> Dict[str, Any]:
        self.import_content()
        self.strip_hooks()
        self.clean_agent_files()
        self.ignore_wolf()
        self.clean_stanzas()
        return self.data


def _normalise_ts(value: str) -> str:
    value = value.strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", value):
        return value
    m = re.match(r"^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})", value)
    if m:
        return m.group(1) + "T" + m.group(2) + "Z"
    m = re.match(r"^(\d{4}-\d{2}-\d{2})$", value)
    if m:
        return value + "T00:00:00Z"
    return util.now_iso()


def _strip_wolf_hooks(path: Path, dry_run: bool) -> int:
    data = util.read_json(path, default=None)
    if not isinstance(data, dict) or not isinstance(data.get("hooks"), dict):
        return 0
    removed = 0
    for event, entries in list(data["hooks"].items()):
        keep = []
        for entry in entries or []:
            commands = " ".join(str(h.get("command", ""))
                                for h in (entry.get("hooks") or []))
            if ".wolf/" in commands:
                removed += 1
            else:
                keep.append(entry)
        data["hooks"][event] = keep
    if removed and not dry_run:
        util.atomic_write(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    return removed


def _strip_wolf_codex(path: Path, dry_run: bool) -> int:
    """Codex configs come in two shapes in the wild: a flat map of event name to
    a list of entries, and the Claude-style `{"hooks": {Event: [...]}}`. Walk
    whatever is there and drop any list entry that mentions `.wolf/`."""
    data = util.read_json(path, default=None)
    if not isinstance(data, dict):
        return 0
    counter = [0]

    def prune(node: Any) -> Any:
        if isinstance(node, dict):
            return {k: prune(v) for k, v in node.items()}
        if isinstance(node, list):
            keep = []
            for entry in node:
                if ".wolf/" in json.dumps(entry):
                    counter[0] += 1
                else:
                    keep.append(prune(entry))
            return keep
        return node

    pruned = prune(data)
    if counter[0] and not dry_run:
        util.atomic_write(path, json.dumps(pruned, indent=2, ensure_ascii=False) + "\n")
    return counter[0]


def _strip_openwolf_prose(text: str) -> str:
    """Delete the `@.wolf/OPENWOLF.md` line, the `This project uses OpenWolf`
    paragraph, and a `# OpenWolf` heading immediately above either."""
    lines = text.splitlines()
    drop = set()
    for i, line in enumerate(lines):
        if _CLAUDE_LINE_RE.search(line):
            drop.add(i)
        elif _CLAUDE_PARA_RE.search(line):
            j = i
            while j < len(lines) and lines[j].strip():
                drop.add(j)
                j += 1
    for i in sorted(drop):
        j = i - 1
        while j >= 0 and not lines[j].strip():
            j -= 1
        if j >= 0 and lines[j].strip().lower().lstrip("# ").startswith("openwolf") \
                and lines[j].lstrip().startswith("#"):
            drop.add(j)
    kept = [l for i, l in enumerate(lines) if i not in drop]
    out = "\n".join(kept)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.rstrip("\n") + "\n" if out.strip() else ""


# ---------------------------------------------------------------------------
# "no trace left" — the last pass
# ---------------------------------------------------------------------------
def _heading_level(line: str) -> int:
    return len(line) - len(line.lstrip("#"))


def _cut_sections(text: str, headings: List[str]) -> str:
    """Drop whole `##` sections by heading, not the matching lines inside them.

    A section about a tool that is gone is gone as a unit: deleting only the
    lines that say "OpenWolf" leaves a heading over an orphaned paragraph,
    which reads like corruption rather than like a cleanup.
    """
    lines = text.splitlines()
    keep = [True] * len(lines)
    wanted = set(headings)
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("#") and line.lstrip("# ").strip() in wanted:
            level = _heading_level(line)
            keep[i] = False
            j = i + 1
            while j < len(lines):
                if lines[j].startswith("#") and _heading_level(lines[j]) <= level:
                    break
                # sift's own block ends the cut as surely as a heading does.
                # It is not a heading, so it used to be swallowed as part of
                # the section above it, which left the block with a closing
                # marker and no opening one -- and the next install, unable to
                # find a block, appended a second copy of the whole stanza.
                if "sift:begin" in lines[j]:
                    break
                keep[j] = False
                j += 1
            i = j
            continue
        i += 1
    out = [l for l, k in zip(lines, keep) if k]
    while out and not out[0].strip():
        out.pop(0)
    return "\n".join(out).rstrip("\n") + "\n" if out else ""


def _cut_wolf_rules(text: str) -> str:
    """Drop `.wolf/` rules, and the comment block sitting directly on top of
    them when that comment is itself only about OpenWolf."""
    lines = text.splitlines()
    keep = [True] * len(lines)
    for i, line in enumerate(lines):
        s = line.strip()
        if not (_is_wolf_rule(s)
                or (s.startswith("#") and _WOLF_WORD_RE.search(s) and _rule_follows(lines, i))):
            continue
        keep[i] = False
    out = [l for l, k in zip(lines, keep) if k]
    while out and not out[-1].strip():
        out.pop()
    return "\n".join(out) + "\n" if out else ""


def _is_wolf_rule(s: str) -> bool:
    """An ignore rule for something OpenWolf put there.

    Not only `.wolf/`: the artefacts are scattered — `.claude/rules/openwolf.md`,
    a `designqc` command and skill, a nested `ui/.wolf/designqc-captures/`. A
    rule naming a path that no longer exists is noise the next reader has to
    rule out by hand.
    """
    if not s or s.startswith("#"):
        return False
    return bool(_ARTEFACT_RE.search(s.lstrip("!")))


def _rule_follows(lines: List[str], i: int) -> bool:
    """True when this comment is part of the block introducing such a rule."""
    for line in lines[i + 1:]:
        s = line.strip()
        if s.startswith("#"):
            continue
        return _is_wolf_rule(s)
    return False


_WOLF_WORD_RE = re.compile(r"(?i)openwolf|\.wolf\b|\.wolf/|designqc")
# Paths in an ignore file that exist only because OpenWolf wrote them.
_ARTEFACT_RE = re.compile(r"(?i)(^|/)\.wolf/|(^|/)openwolf[.-]|designqc")


def guard_plan(ctx: Ctx) -> List[Dict[str, Any]]:
    """Scripts whose checks are scoped to `.wolf/`, with `.wolf/` replaced by
    the sift directory.

    This is the failure with no symptom. A repo-owned pre-commit hook that
    blocks vault paths "only inside `.wolf/`" keeps passing every commit after
    the migration, because nothing is inside `.wolf/` any more — the guard is
    green and guarding nothing, and the directory it should watch is the one
    this tool just created and writes to automatically.
    """
    plan: List[Dict[str, Any]] = []
    hooks = ctx.root / ".githooks"
    for path in sorted(hooks.glob("*")) if hooks.is_dir() else []:
        if not path.is_file() or path.name.startswith("."):
            continue
        before = util.read_text(path)
        if ".wolf/" not in before or "sift.py" in before:
            continue
        after = before.replace(".wolf/cerebrum.local.md", ctx.dir_name + "/local/")
        after = after.replace(".wolf/", ctx.dir_name + "/")
        plan.append({"path": ctx.rel(path), "before": before, "after": after,
                     "what": "checks scoped to .wolf/, which is gone"})
    return plan


def pointer_plan(ctx: Ctx) -> List[Dict[str, Any]]:
    """Tracked files that send a reader into the imported cerebrum.

    These cannot be rewritten by a tool and must not be deleted: on the repo
    this was found on, `bff/README.md` had had its rationale deliberately cut
    because "cerebrum.md already has it". After the import that reasoning is in
    `local/`, gitignored, so for everyone but the author it is simply gone.
    """
    from . import gitutil

    out = []
    for path, number, text in gitutil.grep(
            ctx.root, r"\.wolf/(cerebrum|STATUS|config)|cerebrum\.md",
            exclude=[ctx.dir_name + "/"]):
        out.append({"path": path, "line": number, "text": text.strip()[:120]})
    return out


def trace_plan(ctx: Ctx) -> List[Dict[str, Any]]:
    """Every remaining mention of OpenWolf this tool can remove safely, with
    the exact replacement text, so one keystroke can clear the lot."""
    from .openwolf import remnants

    plan: List[Dict[str, Any]] = []
    by_file: Dict[str, List[Dict[str, Any]]] = {}
    for item in remnants(ctx.root):
        by_file.setdefault(item["path"], []).append(item)
    for name, items in sorted(by_file.items()):
        path = ctx.root / name
        before = util.read_text(path)
        headings = [i["heading"] for i in items if i["heading"] != "(top of file)"]
        after = _cut_sections(before, headings) if headings else before
        if after != before:
            plan.append({"path": name, "before": before, "after": after,
                         "what": ", ".join("§ " + h for h in headings)})

    for name in (".gitignore", ".gitattributes", ctx.dir_name + "/.siftignore"):
        path = ctx.root / name
        if not path.is_file():
            continue
        before = util.read_text(path)
        after = _cut_wolf_rules(before)
        if after != before:
            n = len(before.splitlines()) - len(after.splitlines())
            plan.append({"path": name, "before": before, "after": after,
                         "what": "{} line{} about .wolf/".format(
                             n, "" if n == 1 else "s")})
    return plan


def trace_diff(plan: List[Dict[str, Any]]) -> str:
    out = []
    for item in plan:
        out.append("\n".join(difflib.unified_diff(
            item["before"].splitlines(), item["after"].splitlines(),
            fromfile=item["path"], tofile=item["path"] + " (after)", lineterm="")))
    return "\n\n".join(out)


def apply_traces(ctx: Ctx, plan: List[Dict[str, Any]]) -> List[str]:
    for item in plan:
        util.atomic_write(ctx.root / item["path"], item["after"])
    return [item["path"] for item in plan]


# ---------------------------------------------------------------------------
# the commit
# ---------------------------------------------------------------------------
COMMIT_MESSAGE = "Replace OpenWolf with sift"


def stage(ctx: Ctx, paths: List[str]) -> str:
    """Stage exactly what the migration touched and return `git status` for it.

    Pathspecs rather than `git add -A`: a migration has no business staging
    whatever else the person had in flight, and `git commit -am` — what this
    used to print — would have missed the sift directory entirely, because an
    untracked directory is not a modification.
    """
    # A pathspec matching nothing fails the whole `git add`, and several of
    # these are conditional: `.codex/hooks.json` is usually absent, `.wolf` is
    # present only as a staged deletion. Keep what git can actually see.
    wanted = []
    for path in dict.fromkeys(p for p in paths if p):
        bare = path.rstrip("/")
        if (ctx.root / bare).exists() or gitutil.run(
                ctx.root, ["ls-files", "--", bare]).stdout.strip():
            wanted.append(path)
    paths = wanted
    if not paths:
        return ""
    gitutil.run(ctx.root, ["add", "--all", "--"] + paths)
    return gitutil.run(ctx.root, ["status", "--short", "--"] + paths).stdout.rstrip("\n")


def commit(ctx: Ctx, message: str = COMMIT_MESSAGE) -> Dict[str, Any]:
    """Commit what is staged. The repo's own hooks run: if one refuses, that is
    the answer, and its output is what the person needs to see."""
    proc = gitutil.run(ctx.root, ["commit", "-m", message], timeout=120)
    out = (proc.stdout or "") + (proc.stderr or "")
    return {"ok": proc.returncode == 0, "output": out.rstrip("\n")}
