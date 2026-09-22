#!/usr/bin/env python3
"""sift CLI.

    python3 _sift/bin/sift.py [--sift-dir DIR] [--json] [--quiet] <command>

Exit codes are part of the contract (BUILD-SPEC 9.1):
    0 success / no findings
    1 findings (lint issues, pending descriptions)
    2 usage error
    3 environment (not a git repo, python too old, git missing)
    4 blocked (OpenWolf detected by `init`)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

if sys.version_info < (3, 9):
    sys.stderr.write("error: python >= 3.9 required\n")
    raise SystemExit(3)

from siftlib import (  # noqa: E402
    config as config_mod, doctor as doctor_mod,
    gitutil, init as init_mod, journal as journal_mod, ledger as ledger_mod,
    lint as lint_mod, openwolf, paths, scan as scan_mod,
    search as search_mod, util,
)

VERSION = (HERE / "VERSION").read_text(encoding="utf-8").strip() if (HERE / "VERSION").exists() else "0.0.0"
DEFAULT_LINES = 40
# How many rows a browse-style listing shows before it caps and says "use
# --top". One value so `describe pending` and `search` do not each pick their
# own; `bug find` overrides it deliberately (a lookup wants the best few, not a
# page).
DEFAULT_TOP = 20

EXIT_OK, EXIT_FINDINGS, EXIT_USAGE, EXIT_ENV, EXIT_BLOCKED = 0, 1, 2, 3, 4



# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="sift", add_help=True,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="sift - context economy for a coding agent",
        epilog="installing and upgrading the tool itself are handled by the\n"
               "`sift` wrapper, not here:\n"
               "  sift install [repo] [flags]\n"
               "  sift update  [repo] [flags]")
    p.add_argument("--sift-dir", dest="sift_dir", default=None)
    p.add_argument("--repo", default=None,
                   help="act on the sift repo at this path instead of the "
                        "current directory (e.g. sift ledger --repo ~/x); has "
                        "no effect with --all, which walks --root")
    p.add_argument("--json", dest="json_mode", action="store_true")
    p.add_argument("--quiet", action="store_true")
    p.add_argument("--color", choices=["auto", "always", "never"], default="auto",
                   help="colour errors and next-step hints (default: auto, "
                        "only when writing to a terminal; NO_COLOR is honoured)")
    p.add_argument("--version", action="version", version=VERSION)
    sub = p.add_subparsers(dest="cmd", metavar="<command>")

    def cmd(name: str, summary: str, *examples: str) -> argparse.ArgumentParser:
        """A subparser that carries a one-line summary (shown in the command
        list) and worked examples (shown in `sift <cmd> -h`). Leading with an
        example is the single most useful thing a command's help can do."""
        return sub.add_parser(
            name, help=summary, description=summary,
            formatter_class=argparse.RawDescriptionHelpFormatter,
            epilog="example:\n" + "\n".join("  " + e for e in examples))

    def fleet(parser: argparse.ArgumentParser, what: str) -> None:
        """The `--all`/`--root` pair every fleet command shares: act across
        every sift repo found under a root (default: home), not just this one.
        One walk (`upgrade.discover`) backs them all, so the flags stay
        identical in meaning wherever they appear."""
        parser.add_argument("--all", action="store_true",
                            help="{} across every sift repo found under --root".format(what))
        parser.add_argument("--root", default=None,
                            help="where --all searches (default: your home directory)")

    def confirmable(parser: argparse.ArgumentParser, help_text: str) -> None:
        """`--yes`: the same "do not stop to ask me" flag on every command that
        pauses for confirmation, declared in one place so it means one thing."""
        parser.add_argument("--yes", action="store_true", help=help_text)

    s = cmd("init", "scaffold sift's tracked files into this repo",
            "sift init", "sift init --dry-run")
    s.add_argument("--migrate-openwolf", action="store_true")
    s.add_argument("--no-githooks", action="store_true")
    s.add_argument("--no-skills", action="store_true")
    s.add_argument("--dir", default=None)
    confirmable(s, "scaffold without the interactive prompts")
    s.add_argument("--dry-run", action="store_true")

    s = cmd("doctor", "check the installation, and optionally repair it",
            "sift doctor", "sift doctor --fix", "sift doctor --all")
    s.add_argument("--fix", action="store_true")
    s.add_argument("--check-upstream", action="store_true")
    confirmable(s, "skip the confirmation before --all --fix writes to every repo")
    fleet(s, "check every repo")

    cmd("version", "show the installed runtime and whether the clone would change it",
        "sift version")

    s = cmd("repos", "list every repo with sift installed, and its version",
            "sift repos", "sift repos --root ~/work")
    s.add_argument("--root", default=None,
                   help="where to search (default: your home directory)")

    s = cmd("scan", "rescan changed files and refresh the index",
            "sift scan", "sift scan --full")
    s.add_argument("--full", action="store_true")

    s = cmd("describe", "read or set a file's one-line description",
            "sift describe pending",
            'sift describe set src/app.ts "HTTP entrypoint"',
            "sift describe get src/app.ts")
    dsub = s.add_subparsers(dest="describe_cmd", metavar="{get,set,pending}")
    dget = dsub.add_parser("get", help="print one file's description")
    dget.add_argument("path")
    dset = dsub.add_parser("set", help="set one file's description")
    dset.add_argument("path")
    dset.add_argument("desc")
    dpend = dsub.add_parser("pending", help="files that still need a description")
    dpend.add_argument("--top", type=int, default=DEFAULT_TOP)
    # The pre-0.15 flag forms, kept working but off the help: get/set/pending
    # match `config get/set` now, and that is the one shape the docs teach.
    s.add_argument("--pending", action="store_true", help=argparse.SUPPRESS)
    s.add_argument("--top", type=int, default=DEFAULT_TOP, help=argparse.SUPPRESS)
    s.add_argument("--set", dest="set_", nargs=2, metavar=("PATH", "DESC"),
                   help=argparse.SUPPRESS)
    s.add_argument("--get", metavar="PATH", help=argparse.SUPPRESS)

    s = cmd("map", "sizes and symbol ranges to read instead of a whole file",
            "sift map src/siftlib", "sift map src/sift.py")
    s.add_argument("target")
    s.add_argument("--top", type=int, default=DEFAULT_LINES)

    s = cmd("search", "search the sift docs and file descriptions",
            'sift search "token budget"', 'sift search "ledger" --layer docs')
    s.add_argument("query")
    s.add_argument("--top", type=int, default=DEFAULT_TOP)
    s.add_argument("--layer", choices=["docs", "files", "all"], default="all")

    s = cmd("lint", "check the sift directory for secrets, stray paths and broken links",
            "sift lint", "sift lint --staged", "sift lint --all")
    s.add_argument("--staged", action="store_true")
    s.add_argument("--ci", action="store_true")
    s.add_argument("--fast", action="store_true")
    s.add_argument("--only", default=None)
    fleet(s, "lint every repo")

    s = cmd("log", "append a note to the journal",
            'sift log --kind session --detail "shipped ledger --all"')
    s.add_argument("--kind", required=True, choices=list(journal_mod.KINDS))
    s.add_argument("--detail", required=True)
    s.add_argument("--files", nargs="+", default=[])
    s.add_argument("--tags", nargs="+", default=[])

    s = cmd("bug", "record a fixed bug, or find a known one",
            'sift bug find "ECONNRESET"',
            'sift bug add --error "..." --root-cause "..." --fix "..."')
    bsub = s.add_subparsers(dest="bug_cmd", metavar="{add,find}")
    b = bsub.add_parser("add", help="record a fixed bug",
                        description="record a fixed bug",
                        formatter_class=argparse.RawDescriptionHelpFormatter,
                        epilog='example:\n  sift bug add --error "timeout" '
                               '--root-cause "no retry" --fix "added backoff"')
    b.add_argument("--error", required=True)
    b.add_argument("--root-cause", dest="root_cause", default="")
    b.add_argument("--fix", default="")
    b.add_argument("--files", nargs="+", default=[])
    b.add_argument("--tags", nargs="+", default=[])
    b = bsub.add_parser("find", help="find a known bug by error text",
                        description="find a known bug by error text",
                        formatter_class=argparse.RawDescriptionHelpFormatter,
                        epilog='example:\n  sift bug find "timeout"')
    b.add_argument("query")
    # A lookup wants the best few matches, not a page of them, so this keeps its
    # own small default rather than DEFAULT_TOP.
    b.add_argument("--top", type=int, default=5)

    s = cmd("decide", "record a decision worth remembering",
            'sift decide "Use BM25 for search" --context "..." '
            '--decision "..." --consequences "..."')
    s.add_argument("title")
    s.add_argument("--context", default="")
    s.add_argument("--decision", default="")
    s.add_argument("--consequences", default="")
    s.add_argument("--files", nargs="+", default=[])
    s.add_argument("--supersedes", default="")

    s = cmd("decisions", "read recorded decisions back",
            "sift decisions", "sift decisions D-20260917-03 --markdown")
    s.add_argument("id", nargs="?")
    s.add_argument("--markdown", action="store_true")

    s = cmd("ledger", "what the index and governor saved this period",
            "sift ledger", "sift ledger --all", "sift ledger --since 30.days",
            "sift ledger --repo ~/Projects/sftx-os")
    # The output is terse by design; the glossary that makes it readable lives
    # here, in `sift ledger --help`, not in every run. One line per metric, in
    # the order they print.
    s.description = (
        "what the index and governor kept out of context this period\n"
        "\n"
        "One balance, all in one-off tokens so the three figures subtract:\n"
        "  tokens avoided     tokens sift kept out of context.\n"
        "  tokens introduced  tokens that reached context anyway.\n"
        "  net tokens saved   avoided minus introduced.\n"
        "A read that was refused or de-duplicated never enters, so it counts\n"
        "whole to avoided; a condensed Bash flood is split -- the bytes cut go\n"
        "to avoided, the smaller remainder that entered goes to introduced.\n"
        "\n"
        "avoided, by source:\n"
        "  duplicate / ranged reads stopped  re-reads of a file already in\n"
        "      context, and whole-file reads steered to a range. Count, and\n"
        "      the measured tokens they would have added.\n"
        "  big reads refused -> ranges  big whole-file reads refused up front\n"
        "      so the model re-read a range instead. Count and their size.\n"
        "  Bash outputs condensed  large command outputs replaced in place\n"
        "      with a condensed form. Count and the tokens cut.\n"
        "\n"
        "introduced, by source:\n"
        "  whole files read anyway  big reads taken whole after ranges were\n"
        "      offered, so they landed in context. Count and their tokens.\n"
        "  Bash floods, after condensing  Bash outputs over the threshold, at\n"
        "      the size that actually entered (condensed where condensing was\n"
        "      on). Count is every flood; tokens are what entered.\n"
        "  Bash floods passed through  floods of a family the governor never\n"
        "      condenses (test, build, an unclassifiable chain). They entered\n"
        "      whole; the family tally says which. Only shown when there are\n"
        "      any.\n"
        "  context sift injected  context sift itself added (index hints,\n"
        "      warnings).\n"
        "\n"
        "index: lookups the index answered (hits) or could not (misses), and\n"
        "  commit / stop reminders the hooks raised (nudges). A miss records\n"
        "  its path and reason (stale = a real file not indexed, absent = a\n"
        "  path not on disk) in the ledger for evaluation, not in this summary.\n"
        "\n"
        "The one line in a different unit: because a token in context is\n"
        "re-sent on every later turn, condensing's saving is also counted in\n"
        "token-turns (tokens x the turns they would have ridden). Mostly cache\n"
        "reads at roughly a tenth of the fresh price, so do not price as fresh.")
    s.add_argument("--since", default="7.days",
                   help="time window to summarise, e.g. 7.days or 30.days "
                        "(default: 7.days)")
    fleet(s, "total the ledger")

    s = cmd("import", "import content from another tool into sift",
            "sift import", "sift import --dry-run")
    s.add_argument("--from-openwolf", dest="from_openwolf", action="store_true",
                   help=argparse.SUPPRESS)
    s.add_argument("--dry-run", action="store_true")
    confirmable(s, "import without the interactive prompts")

    s = cmd("config", "read or set a validated config value",
            "sift config get hooks.big_read_mode",
            "sift config set hooks.big_read_mode deny",
            "sift config set hooks.big_read_mode deny --all")
    csub = s.add_subparsers(dest="config_cmd", metavar="{get,set}")
    cg = csub.add_parser("get", help="print the effective value of a config key",
                         description="print the effective value of a config key",
                         formatter_class=argparse.RawDescriptionHelpFormatter,
                         epilog="example:\n  sift config get governance.enabled\n"
                                "  sift config get governance.enabled --all")
    cg.add_argument("key")
    fleet(cg, "read the value")
    cs = csub.add_parser("set", help="set one config key (validated)",
                         description="set one config key, validated against the schema",
                         formatter_class=argparse.RawDescriptionHelpFormatter,
                         epilog="example:\n  sift config set hooks.big_read_mode deny\n"
                                "  sift config set governance.enabled true --all")
    cs.add_argument("key")
    cs.add_argument("value")
    confirmable(cs, "skip the confirmation before --all writes to every repo")
    fleet(cs, "set it")

    return p


# ---------------------------------------------------------------------------
# Command implementations
# ---------------------------------------------------------------------------

class App:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.cmd = args.cmd
        self.out = util.Out(self.cmd, VERSION, args.json_mode, args.quiet,
                            color=getattr(args, "color", "auto"))
        try:
            # --repo points the resolver at another repo (or a path inside one)
            # so read commands like `ledger` run from anywhere; without it the
            # base is the current directory, as before.
            base = getattr(args, "repo", None) or Path.cwd()
            self.ctx = paths.resolve(base, args.sift_dir or getattr(args, "dir", None))
        except paths.NotARepo:
            # The fleet commands act on repos found under your home directory,
            # not on the one you are standing in, so they run from anywhere -
            # including a bare parent folder that holds many repos but is none.
            if _needs_repo(args):
                raise
            self.ctx = None
        self.cfg = config_mod.load(self.ctx.config_path) if self.ctx is not None else None

    # -- helpers ------------------------------------------------------------
    def truncated(self, rows: List[Any], top: int) -> List[Any]:
        """Slice for display; call `more_note` after the table so the hint
        lands under the rows it is about."""
        return rows[:top]

    def more_note(self, total: int, top: int) -> None:
        note = util.truncate_note(total, top)
        if note:
            self.out.line(note)

    # -- commands -----------------------------------------------------------
    def cmd_init(self) -> int:
        args = self.args
        found = openwolf.detect(self.ctx.root)
        if found and not args.migrate_openwolf:
            text = openwolf.report(found, self.ctx.dir_name, "sift init")
            if self.out.json_mode:
                self.out.fail("OPENWOLF_DETECTED", text)
            else:
                sys.stderr.write(text + "\n")
            return EXIT_BLOCKED

        src_root = HERE
        dry_run = bool(getattr(args, "dry_run", False))
        if dry_run:
            # `--dry-run` writes nothing at all, scaffold included.
            data = {"created": [], "merged": [], "skipped": [], "dry_run": True}
        else:
            data = init_mod.scaffold(
                self.ctx, self.cfg, githooks=not args.no_githooks,
                skills=not args.no_skills,
                advisory=bool(self.cfg.get("ci", "advisory", default=True)))
            if (HERE / "siftlib").is_dir() and not (self.ctx.dir / "bin" / "sift.py").exists():
                data["bin"] = init_mod.install_bin(self.ctx, src_root)
        data["openwolf"] = {"found": found}

        if found and args.migrate_openwolf:
            from siftlib import migrate_openwolf as mig_mod
            from siftlib.migrate_openwolf import Migration
            quiet = self.out.json_mode
            confirm = (lambda _m: True) if args.yes else self._ask
            if not quiet:
                self.out.line("Initialising sift in {}/, and importing what "
                              "OpenWolf knew.".format(self.ctx.dir_name))
            mig = Migration(self.ctx, self.cfg, dry_run=dry_run, confirm=confirm,
                            show=(None if quiet else self.out.line))
            data["openwolf"]["migration"] = mig.run_all()
            if not quiet:
                if mig.log:
                    self.out.line("")
                    self.out.line("Done:")
                    for line in mig.log:
                        self.out.line("  " + line)
                if dry_run:
                    self.out.line("")
                    self.out.line("(dry run - nothing was written)")
                else:
                    removed = self._offer_removal(mig_mod, args)
                    data["openwolf"]["guards"] = self._offer_guard_rescope(
                        mig_mod, args)
                    traces = self._offer_trace_cleanup(mig_mod, args, mig)
                    data["openwolf"]["pointers"] = self._report_pointers(mig_mod)
                    data["openwolf"]["removed"] = removed
                    data["openwolf"]["traces"] = traces
                    data["openwolf"]["committed"] = self._offer_commit(
                        mig_mod, args, mig, removed, traces)
                    self._say_pending()

        migrated = bool(found and args.migrate_openwolf)
        if not self.out.json_mode and not dry_run and not migrated:
            self.out.line("Initialising sift in {}/.".format(self.ctx.dir_name))
            for bucket, label in (("created", "Created"), ("merged", "Merged"),
                                  ("skipped", "Left alone (already there)")):
                items = data.get(bucket) or []
                if items:
                    self.out.line("")
                    self.out.line("{} {}:".format(label, len(items)))
                    for item in items[:20]:
                        self.out.line("  " + item)
                    if len(items) > 20:
                        self.out.line("  … and {} more".format(len(items) - 20))
            self.out.line("")
            self.out.line("Next: {}, then {}, then read {}/conventions.md".format(
                self.out.action("sift doctor"), self.out.action("sift scan"),
                self.ctx.dir_name))
            self._say_pending()
        self.out.emit(data)
        return EXIT_OK

    def _offer_removal(self, mig_mod, args) -> Optional[dict]:
        """Offer to finish the job. Ruling 11 keeps the deletion on a keystroke,
        which is what the prompt is - but a keystroke is not four commands, and
        `--yes` is not a keystroke about this, so it still only gets the list."""
        plan = mig_mod.removal_plan(self.ctx)
        if not (plan["wolf"] or plan["rules"]):
            return None
        asked = not (args.yes or not sys.stdin.isatty())
        if asked:
            self.out.line("")
        if not (asked and self._ask(mig_mod.removal_prompt(plan, self.ctx.dir_name))):
            for line in mig_mod.manual_removal_lines(plan):
                self.out.line(line)
            return None
        done = mig_mod.remove_openwolf(self.ctx)
        self.out.line("")
        if done["rescue"]:
            self.out.line("Copied to {}/local/ first: {}".format(
                self.ctx.dir_name, ", ".join(done["rescue"])))
        self.out.line("Removed: " + ", ".join(
            [d + "/" for d in done["wolf_dirs"]] + done["rules"]))
        return done

    def _offer_guard_rescope(self, mig_mod, args) -> Optional[List[str]]:
        """A privacy check pointed at `.wolf/` goes green and guards nothing
        once `.wolf/` is gone. Offer to point it at the sift directory, which
        is what auto-capture writes to now."""
        plan = mig_mod.guard_plan(self.ctx)
        if not plan:
            return None
        self.out.line("")
        self.out.line("These check for secrets or private material, but only "
                      "inside .wolf/ - which no longer exists:")
        for item in plan:
            self.out.line("  " + item["path"])
        self.out.line("")
        self.out.line(mig_mod.trace_diff(plan))
        if args.yes or not sys.stdin.isatty():
            self.out.line("")
            self.out.line("Left alone. Until they point at {}/, they pass "
                          "everything.".format(self.ctx.dir_name))
            return None
        if not self._ask("Point them at {}/ instead?".format(self.ctx.dir_name)):
            self.out.line("Left alone - but until they point at {}/, they pass "
                          "everything.".format(self.ctx.dir_name))
            return None
        applied = mig_mod.apply_traces(self.ctx, plan)
        self.out.line("Rescoped: " + ", ".join(applied))
        return applied

    def _report_pointers(self, mig_mod) -> List[dict]:
        """Never edited, always named: what these files point at is now private."""
        pointers = mig_mod.pointer_plan(self.ctx)
        if not pointers:
            return []
        self.out.line("")
        self.out.line("These send a reader to the imported cerebrum, which is now "
                      "in {}/local/ and gitignored - for a colleague the pointer "
                      "is dead. Promote what they rely on, or inline it:".format(
                          self.ctx.dir_name))
        for item in pointers[:10]:
            self.out.line("  {}:{}  {}".format(item["path"], item["line"],
                                               item["text"]))
        if len(pointers) > 10:
            self.out.line("  … and {} more".format(len(pointers) - 10))
        return pointers

    def _offer_trace_cleanup(self, mig_mod, args, mig) -> Optional[List[str]]:
        """Offer to clear what the import left behind, as one diff and one
        keystroke. Ruling 33 kept these edits off the automatic path because
        deleting prose nobody has read is not a migration's business; showing
        the whole diff and asking is a different act."""
        plan = mig_mod.trace_plan(self.ctx)
        if not plan:
            self.out.line("")
            self.out.line("Nothing in the repo mentions OpenWolf any more. What it "
                          "knew is in {}/local/.".format(self.ctx.dir_name))
            return []
        interactive = not (args.yes or not sys.stdin.isatty())
        if interactive:
            self.out.line("")
            self.out.line("Still mentioning OpenWolf:")
            for item in plan:
                self.out.line("  {} - {}".format(item["path"], item["what"]))
            self.out.line("")
            self.out.line(mig_mod.trace_diff(plan))
            if self._ask("Apply that and leave no trace?"):
                applied = mig_mod.apply_traces(self.ctx, plan)
                self.out.line("")
                self.out.line("Cleared: " + ", ".join(applied))
                self.out.line("What OpenWolf knew is in {}/local/; nothing else "
                              "in the repo mentions it.".format(self.ctx.dir_name))
                return applied
        for line in mig_mod.todo_lines(mig):
            self.out.line(line)
        return None

    def _offer_commit(self, mig_mod, args, mig, removed, traces) -> Optional[dict]:
        """The last step. What is staged is named rather than swept: `git add -A`
        would take whatever else the person had in flight, and the `git commit
        -am` this used to print would have missed the sift directory entirely."""
        touched = [self.ctx.dir_name + "/"]
        touched += [d["path"] for d in mig.data.get("diffs") or []]
        touched += list(traces or [])
        touched += [g["path"] for g in (mig.data.get("guards") or [])]
        touched += [".githooks/", ".gitignore", ".gitattributes",
                    ".claude/settings.json", ".codex/hooks.json"]
        if removed:
            touched += ([".wolf"] if removed.get("wolf") else []) + removed.get("rules", [])
        status = mig_mod.stage(self.ctx, touched)
        if not status:
            return None
        self.out.line("")
        self.out.line("Staged:")
        for line in status.splitlines():
            self.out.line("  " + line)
        if args.yes or not sys.stdin.isatty():
            self.out.line("")
            self.out.line('Left to run:  git commit -m "{}"'.format(
                mig_mod.COMMIT_MESSAGE))
            return None
        self.out.line("")
        if not self._ask('Commit that as "{}"?'.format(mig_mod.COMMIT_MESSAGE)):
            self.out.line("Left staged - commit it when you are ready.")
            return None
        result = mig_mod.commit(self.ctx)
        self.out.line("")
        for line in result["output"].splitlines()[:12]:
            self.out.line("  " + line)
        if not result["ok"]:
            self.out.line("")
            self.out.line("Not committed - a hook refused it. Nothing is lost; it "
                          "stays staged.")
        return result

    def _say_pending(self) -> None:
        """An install can pass every check and still be unfinished. Nobody
        should have to already know that."""
        from siftlib import followup as followup_mod

        items = followup_mod.pending(self.ctx, self.cfg)
        for line in followup_mod.lines(items, self.ctx.dir_name):
            self.out.line(line)

    def _ask(self, message: str) -> bool:
        # The question comes after whatever it is about, not before it.
        self.out.flush()
        try:
            answer = input(message + " [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return False
        return answer == "y"

    def cmd_doctor(self) -> int:
        if self.args.all:
            return self._doctor_all()
        if self._no_root_without_all():
            self.out.fail("USAGE", "--root only means something with --all")
            return EXIT_USAGE
        data = doctor_mod.run(self.ctx, self.cfg, fix=self.args.fix,
                              check_upstream=self.args.check_upstream)
        if not self.out.json_mode:
            rows = [["ok" if c["ok"] else "FAIL", c["id"], c["detail"]] for c in data["checks"]]
            self.out.table(rows, ["", "check", "detail"])
            fails = [c for c in data["checks"] if not c["ok"]]
            if fails:
                self.out.line("")
                for c in fails:
                    if c["fix"]:
                        self.out.line("fix {}: {}".format(c["id"], c["fix"]))
            if data["fixed"]:
                self.out.line("")
                self.out.line("fixed: " + ", ".join(data["fixed"]))
        self.out.emit(data)
        return EXIT_FINDINGS if data["failed"] else EXIT_OK

    def _doctor_all(self) -> int:
        root = self._fleet_root()
        if root is None:
            return EXIT_ENV
        targets = list(self._fleet_repos(root))
        if self.args.fix:
            # Reporting across the fleet is read-only, but --fix writes to every
            # repo, so it is the branch that has to ask first.
            ok = self._confirm_fleet("fix {} under {}?".format(
                util.count(len(targets), "repo"), root))
            if ok is None:
                self.out.fail("USAGE", "fixing across repos needs --yes when not interactive")
                return EXIT_USAGE
            if not ok:
                if not self.out.json_mode:
                    self.out.line("nothing fixed")
                self.out.emit({"root": str(root), "repos": []})
                return EXIT_OK
        repos: List[Dict[str, Any]] = []
        for rel, ctx in targets:
            cfg = config_mod.load(ctx.config_path)
            data = doctor_mod.run(ctx, cfg, fix=self.args.fix,
                                  check_upstream=self.args.check_upstream)
            repos.append({
                "repo": rel, "checks": len(data["checks"]),
                "failed": [c["id"] for c in data["checks"] if not c["ok"]],
                "fixed": data["fixed"]})
        bad = [r for r in repos if r["failed"]]
        if not self.out.json_mode:
            self.out.line("doctor across {} under {}".format(
                util.count(len(repos), "repo"), root))
            if repos:
                self.out.line("")
                self.out.table(
                    [[r["repo"], util.num(r["checks"]),
                      "ok" if not r["failed"] else "FAIL: " + ", ".join(r["failed"])]
                     for r in repos],
                    ["repo", "checks", "result"])
            if self.args.fix:
                for r in repos:
                    if r["fixed"]:
                        self.out.line("fixed in {}: {}".format(
                            r["repo"], ", ".join(r["fixed"])))
        self.out.emit({"root": str(root), "repos": repos})
        return EXIT_FINDINGS if bad else EXIT_OK

    def cmd_version(self) -> int:
        """What is installed here, what the clone would install, and whether
        those are the same thing. `--version` stays a bare string for scripts;
        this is the one that answers "should I update?"."""
        from siftlib import upgrade as upgrade_mod

        data = upgrade_mod.status(self.ctx)
        data["sift_dir"] = self.ctx.rel(self.ctx.dir / "bin")
        if data["state"] == upgrade_mod.SELF and data["clone"]:
            data["bump"] = upgrade_mod.bump_needed(Path(data["clone"]))
        if not self.out.json_mode:
            self.out.line("sift {} - {}".format(data["installed"], data["sift_dir"]))
            if data["clone"]:
                self.out.line("clone {} - {}{}".format(
                    data["clone_version"], data["clone"],
                    " (this repo)" if data["state"] == upgrade_mod.SELF else ""))
            self.out.line(data["detail"])
            if data["state"] in upgrade_mod.STALE:
                self.out.line("run: " + self.out.action("sift update"))
            bump = data.get("bump") or {}
            if bump.get("needed"):
                self.out.line("")
                self.out.line(_bump_note(bump))
        self.out.emit(data)
        return EXIT_OK

    def cmd_scan(self) -> int:
        data = scan_mod.run_scan(self.ctx, self.cfg, full=self.args.full)
        if not self.out.json_mode:
            self.out.line("{} scanned, {} changed, {} need a description".format(
                util.count(data["files"], "file"), util.num(data["changed"]),
                util.num(data["pending_descriptions"])))
            if data["compacted"]:
                self.out.line("files.jsonl: {} compacted".format(
                    util.count(data["compacted"], "duplicate line")))
        self.out.emit(data)
        return EXIT_OK

    def cmd_describe(self) -> int:
        # get/set/pending are subcommands now (matching `config get/set`); the
        # --get/--set/--pending flags are kept as hidden aliases, so both spell
        # the same three operations and this reads either.
        a = self.args
        sub = getattr(a, "describe_cmd", None)
        if sub == "set" or a.set_:
            path, desc = (a.path, a.desc) if sub == "set" else a.set_
            rec, err = scan_mod.set_description(self.ctx, self.cfg, path, desc)
            if err:
                self.out.fail("BAD_PATH", err)
                return EXIT_USAGE
            if not self.out.json_mode:
                self.out.line("set: {} - {}".format(rec["path"], rec["desc"]))
            self.out.emit({"path": rec["path"], "hash": rec["hash"]})
            return EXIT_OK
        if sub == "get" or a.get:
            target = a.path if sub == "get" else a.get
            rec = scan_mod.load_descriptions(self.ctx).get(target.replace("\\", "/"))
            if not self.out.json_mode:
                self.out.line(rec["desc"] if rec else "(no description)")
            self.out.emit(rec)
            return EXIT_OK
        if sub == "pending" or a.pending:
            top = a.top
            rows = scan_mod.pending_descriptions(self.ctx, self.cfg)
            shown = self.truncated(rows, top)
            if not self.out.json_mode:
                self.out.line("add one line saying what each file is for:")
                self.out.table([[r["reason"], str(r["churn"]), str(r["tokens"]), r["path"]]
                                for r in shown], ["why", "churn", "tok", "path"])
                self.more_note(len(rows), top)
            self.out.emit(shown)
            return EXIT_FINDINGS if rows else EXIT_OK
        self.out.fail("USAGE", "describe needs get, set or pending")
        return EXIT_USAGE

    def cmd_map(self) -> int:
        rows = scan_mod.build_map(self.ctx, self.cfg, self.args.target)
        shown = self.truncated(rows, self.args.top)
        if not self.out.json_mode:
            from siftlib import symbols as sym_mod
            dir_rows, here = scan_mod.rollup_map(rows, self.args.target)
            spec = (self.args.target or ".").rstrip("/")
            if not dir_rows and len(here) == 1 and here[0]["path"] == spec:
                # Single-file target: the point is the symbol ranges to read.
                r = here[0]
                if r["desc"]:
                    self.out.line("{}  {} tok  {}".format(r["path"], r["tokens"], r["desc"]))
                else:
                    self.out.line("{}  {} tok".format(r["path"], r["tokens"]))
                if r["symbols"]:
                    self.out.table(
                        [[s["name"], s["kind"], "L{}-{}".format(s["start"], s["end"]),
                          str(s.get("tokens", 0))] for s in r["symbols"]],
                        ["symbol", "kind", "lines", "tok"])
                else:
                    self.out.line("(no symbol ranges - read the whole file)")
                self.out.emit(shown)
                return EXIT_OK
            table = []
            for d in dir_rows:
                noun = "file" if d["files"] == 1 else "files"
                table.append([d["path"] + "/", str(d["tokens"]),
                              "{} {}, {} described".format(d["files"], noun, d["described"])])
            for r in here[:self.args.top]:
                syms = sym_mod.format_hint(r["symbols"], 3) if r["symbols"] else ""
                table.append([r["path"], str(r["tokens"]),
                              (r["desc"] or syms or "")[:88]])
            self.out.table(table, ["path", "tok", "description"])
            self.more_note(len(here), self.args.top)
        self.out.emit(shown)
        return EXIT_OK

    def cmd_search(self) -> int:
        rows = search_mod.search(self.ctx, self.cfg, self.args.query,
                                 top=self.args.top, layer=self.args.layer)
        if not self.out.json_mode:
            if not rows:
                self.out.line('no results for "{}"'.format(self.args.query))
            else:
                self.out.table([["{:.2f}".format(r["score"]), r["layer"], r["title"],
                                 r["snippet"][:70]] for r in rows],
                               ["score", "layer", "title", "snippet"])
        self.out.emit(rows)
        return EXIT_OK

    def cmd_lint(self) -> int:
        if self.args.all:
            return self._lint_all()
        if self._no_root_without_all():
            self.out.fail("USAGE", "--root only means something with --all")
            return EXIT_USAGE
        only = set(c.strip().upper() for c in self.args.only.split(",")) if self.args.only else None
        issues = lint_mod.run(self.ctx, self.cfg, fast=self.args.fast, only=only,
                              staged=self.args.staged)
        summary = lint_mod.summarise(issues)
        if not self.out.json_mode:
            if not issues:
                self.out.line("clean")
            for severity in (lint_mod.SEV_ERROR, lint_mod.SEV_WARNING, lint_mod.SEV_INFO):
                group = [i for i in issues if i["severity"] == severity]
                if not group:
                    continue
                self.out.line("{} ({})".format(severity, len(group)))
                for issue in group[:DEFAULT_LINES]:
                    self.out.line("  {} {}:{} {}".format(
                        issue["code"], issue["path"], issue["line"], issue["message"]))
                if len(group) > DEFAULT_LINES:
                    self.out.line("  ({} more, use --json)".format(
                        util.num(len(group) - DEFAULT_LINES)))
        self.out.emit({"issues": issues, "summary": summary})
        advisory = bool(self.cfg.get("ci", "advisory", default=True))
        if summary["errors"]:
            return EXIT_FINDINGS
        if self.args.ci and summary["warnings"] and not advisory:
            return EXIT_FINDINGS
        return EXIT_OK

    def _lint_all(self) -> int:
        """A fleet hygiene sweep: run lint in every repo and report which ones
        are not clean, with the offending lines for the errors -- that is the
        secret sweep the privacy rule wants across a machine, not just here."""
        root = self._fleet_root()
        if root is None:
            return EXIT_ENV
        only = set(c.strip().upper() for c in self.args.only.split(",")) if self.args.only else None
        repos: List[Dict[str, Any]] = []
        for rel, ctx in self._fleet_repos(root):
            cfg = config_mod.load(ctx.config_path)
            issues = lint_mod.run(ctx, cfg, fast=self.args.fast, only=only,
                                  staged=self.args.staged)
            summary = lint_mod.summarise(issues)
            repos.append({"repo": rel, "errors": summary["errors"],
                          "warnings": summary["warnings"], "issues": issues})
        dirty = [r for r in repos if r["errors"] or r["warnings"]]
        any_errors = any(r["errors"] for r in repos)
        if not self.out.json_mode:
            self.out.line("lint across {} under {}".format(
                util.count(len(repos), "repo"), root))
            if dirty:
                self.out.line("")
                self.out.table(
                    [[r["repo"], util.num(r["errors"]), util.num(r["warnings"])]
                     for r in dirty], ["repo", "errors", "warnings"])
                for r in dirty:
                    errs = [i for i in r["issues"] if i["severity"] == lint_mod.SEV_ERROR]
                    for issue in errs[:DEFAULT_LINES]:
                        self.out.line("  {} {}/{}:{} {}".format(
                            issue["code"], r["repo"], issue["path"],
                            issue["line"], issue["message"]))
            self.out.line("")
            self.out.line("{} clean".format(util.count(len(repos) - len(dirty), "repo")))
        self.out.emit({"root": str(root), "repos": repos})
        return EXIT_FINDINGS if any_errors else EXIT_OK

    def cmd_log(self) -> int:
        entry = journal_mod.append(self.ctx, self.args.kind, self.args.detail,
                                   files=self.args.files,
                                   tags=self.args.tags)
        if not self.out.json_mode:
            self.out.line("logged " + entry["id"])
        self.out.emit(entry)
        return EXIT_OK

    def cmd_bug(self) -> int:
        if self.args.bug_cmd == "add":
            entry = journal_mod.add_bug(self.ctx, self.args.error, self.args.root_cause,
                                        self.args.fix, files=self.args.files,
                                        tags=self.args.tags)
            if not self.out.json_mode:
                self.out.line("logged " + entry["id"])
            self.out.emit(entry)
            return EXIT_OK
        if self.args.bug_cmd == "find":
            rows = journal_mod.find_bugs(self.ctx, self.args.query, top=self.args.top)
            if not self.out.json_mode:
                if not rows:
                    self.out.line("no known bug matches that")
                for row in rows:
                    self.out.line("{}{}  {}".format(
                        "local " if row.get("local") else "",
                        row["id"], row.get("error", "")[:80]))
                    if row.get("root_cause"):
                        self.out.line("    cause: " + row["root_cause"][:100])
                    if row.get("fix"):
                        self.out.line("    fix:   " + row["fix"][:100])
            self.out.emit(rows)
            return EXIT_OK
        self.out.fail("USAGE", "bug needs `add` or `find`")
        return EXIT_USAGE

    def cmd_decide(self) -> int:
        missing = [f for f in journal_mod.FIELDS if not getattr(self.args, f).strip()]
        if missing:
            self.out.fail("USAGE", "decide needs " + ", ".join("--" + f for f in missing)
                          + " - a record is written complete, like `bug add`")
            return EXIT_USAGE
        data = journal_mod.add_decision(
            self.ctx, self.args.title, context=self.args.context,
            decision=self.args.decision, consequences=self.args.consequences,
            files=self.args.files, supersedes=self.args.supersedes)
        if not self.out.json_mode:
            self.out.line("recorded {} in {}/decisions.jsonl".format(
                data["id"], self.ctx.dir_name))
        self.out.emit({"id": data["id"], "anchor": data["anchor"]})
        return EXIT_OK

    def cmd_decisions(self) -> int:
        rows = journal_mod.decisions(self.ctx)
        if self.args.id:
            rows = [r for r in rows if r.get("id") == self.args.id]
            if not rows:
                self.out.fail("NOTFOUND", "no decision " + self.args.id)
                return EXIT_USAGE
        if not self.out.json_mode:
            if not rows:
                self.out.line("no decisions recorded - `sift decide` writes one")
            elif self.args.id or self.args.markdown:
                self.out.line(journal_mod.render_decisions(rows) if self.args.markdown
                              else journal_mod.render_decision(rows[0]))
            else:
                for r in rows:
                    self.out.line("{}  {}{}".format(
                        r.get("id", ""), r.get("title", ""),
                        "" if r.get("status") == "accepted"
                        else "  [{}]".format(r.get("status", ""))))
        self.out.emit(rows[0] if self.args.id else rows)
        return EXIT_OK

    # -- fleet helpers ------------------------------------------------------
    def _fleet_root(self) -> Optional[Path]:
        """The directory a `--all` walks from: `--root` if given, else home.
        Fails with ENV and returns None if it is not a directory, so every
        fleet command reports a bad root the same way."""
        root = (Path(self.args.root).expanduser() if self.args.root
                else Path.home()).resolve()
        if not root.is_dir():
            self.out.fail("ENV", "{} is not a directory".format(root))
            return None
        return root

    def _fleet_repos(self, root: Path):
        """(repo-relative name, Ctx) for every sift repo under `root`, in the
        one order `upgrade.discover` returns. The shared walk behind every
        `--all`, so all of them see the same repos in the same order."""
        from siftlib import upgrade as upgrade_mod
        for repo in upgrade_mod.discover(root):
            ctx = paths.Ctx(repo, paths.resolve_dir_name(repo))
            try:
                rel = repo.relative_to(root).as_posix()
            except ValueError:
                rel = str(repo)
            yield rel, ctx

    def _no_root_without_all(self) -> bool:
        """--root only means something with --all; used by the single-repo
        branch of every fleet command to reject a stray --root."""
        return bool(getattr(self.args, "root", None))

    def _confirm_fleet(self, prompt: str) -> Optional[bool]:
        """Confirm a write that will touch every repo under a root before it
        happens -- the guard clig.dev wants on a wide, hard-to-undo action, and
        the same stance `sift update --all` takes. `--yes` short-circuits to
        yes; a non-interactive run (piped, or under --json) has no one to ask,
        so it returns None and the caller refuses rather than guess. Otherwise
        the person answers."""
        if getattr(self.args, "yes", False):
            return True
        if self.out.json_mode or not sys.stdin.isatty():
            return None
        return self._ask(prompt)

    def cmd_repos(self) -> int:
        """List every repo with sift installed and the version each carries --
        the discoverable name for the fleet listing under all the `--all`
        walks. Read-only: it never says a repo is out of date it cannot see a
        clone for (state `unknown`), it just lists what is on disk."""
        from siftlib import upgrade as upgrade_mod
        root = self._fleet_root()
        if root is None:
            return EXIT_ENV
        clone = upgrade_mod.find_clone()
        clone_version = ""
        if clone is not None:
            clone_version = util.read_text(clone / "src" / "VERSION").strip()
        repos: List[Dict[str, Any]] = []
        for rel, ctx in self._fleet_repos(root):
            data = upgrade_mod.status(ctx, clone=clone)
            repos.append({"repo": rel, "version": data["installed"],
                          "state": data["state"]})
        if not self.out.json_mode:
            head = "{} with sift under {}".format(
                util.count(len(repos), "repo"), root)
            if clone_version:
                head += " (clone {})".format(clone_version)
            self.out.line(head)
            if repos:
                self.out.line("")
                self.out.table([[r["repo"], r["version"], r["state"]] for r in repos],
                               ["repo", "version", "state"])
        self.out.emit({"root": str(root), "clone_version": clone_version, "repos": repos})
        return EXIT_OK

    def cmd_ledger(self) -> int:
        if self.args.all:
            if getattr(self.args, "repo", None):
                self.out.fail("USAGE", "--repo names one repo; drop it, or use "
                              "--root to point --all at another tree")
                return EXIT_USAGE
            return self._ledger_all()
        if self.args.root:
            self.out.fail("USAGE", "--root only means something with --all")
            return EXIT_USAGE
        data = ledger_mod.summarise(self.ctx, self.args.since)
        if not self.out.json_mode:
            self._print_ledger(data, advise=True)
        self.out.emit(data)
        return EXIT_OK

    def _ledger_all(self) -> int:
        root = self._fleet_root()
        if root is None:
            return EXIT_ENV
        summary = ledger_mod.summarise_all(root, self.args.since)
        if not self.out.json_mode:
            self.out.line("since {}: ledger data in {} of {} under {}".format(
                summary["since"], util.num(summary["repo_count"]),
                util.count(summary["repos_found"], "repo"), summary["root"]))
            if summary["repos"]:
                self.out.line("")
                # The same three headline figures as the single-repo view, one
                # row per repo, computed the one way in `_ledger_flow`, and a
                # final total row -- the header above already gives the period
                # and repo count, and the legend the meaning, so no separate
                # total block repeats them. All one-off tokens.
                self.out.line("tokens: avoided = kept out of context, "
                              "introduced = reached context, net = the two "
                              "subtracted.")
                rows = []
                for r in summary["repos"]:
                    a, i, n, _ = self._ledger_flow(r)
                    rows.append([r["repo"], util.num(r["index_hits"]),
                                 util.num(a), util.num(i), util.num(n)])
                ta, ti, tn, _ = self._ledger_flow(summary["total"])
                rows.append(["total", util.num(summary["total"]["index_hits"]),
                             util.num(ta), util.num(ti),
                             self._net(util.num(tn), tn)])
                self.out.table(
                    rows, ["repo", "hits", "avoided", "introduced", "net"])
                # Where the total's avoided and introduced came from, across
                # every repo. The three headline numbers are the total row
                # above, so only the sources need repeating.
                self.out.line("")
                self.out.line("breakdown:")
                self._ledger_breakdown(summary["total"])
                self._ledger_carry_and_index(summary["total"])
            if summary["idle"]:
                # Named, not just counted: the header says how many are idle,
                # this says which, so the gap is actionable without a script.
                self.out.line("")
                self.out.line("{} with no ledger data in this window: {}".format(
                    util.count(len(summary["idle"]), "installed repo"),
                    ", ".join(summary["idle"])))
            if summary["repo_count"]:
                self._ledger_help_hint()
        self.out.emit(summary)
        return EXIT_OK

    def _net(self, text: str, n: int) -> str:
        """The net figure in green when it is a saving, red when it is negative,
        plain at zero. `paint` returns the text untouched whenever colour is off
        -- captured output, --json, NO_COLOR -- so this is safe to call always."""
        if n > 0:
            return self.out.paint(text, "green")
        if n < 0:
            return self.out.paint(text, "red")
        return text

    @staticmethod
    def _ledger_flow(data: dict) -> tuple:
        """The three headline figures, in one unit (one-off tokens), from one
        place so the table and the detail agree.

        avoided: tokens sift kept out of context -- reads not re-read or
        steered to ranges, big reads refused, and the bytes condensing shrank
        off a Bash flood. introduced: tokens that reached context -- whole-file
        reads that came back, Bash floods at their post-condensing size, the
        floods of families the governor never condenses that entered whole, and
        the context sift injected. net: avoided minus introduced. A refused or
        de-duplicated read never enters, so it is credited whole to avoided and
        nothing to introduced; a condensed flood is split -- the saving to
        avoided, the entered remainder to introduced."""
        saved_condensed = int(data.get("governed_saved_tokens", 0) or 0)
        avoided = (int(data.get("tokens_avoided", 0) or 0)
                   + int(data.get("denied_tokens", 0) or 0)
                   + saved_condensed)
        bash_entered = max(0, int(data.get("flood_seen_tokens", 0) or 0)
                           - saved_condensed)
        introduced = (int(data.get("read_flood_tokens", 0) or 0)
                      + bash_entered
                      + int(data.get("flood_passed_tokens", 0) or 0)
                      + int(data.get("tokens_injected", 0) or 0))
        return avoided, introduced, avoided - introduced, bash_entered

    def _ledger_breakdown(self, data: dict) -> None:
        """The two by-source tables that add up to `tokens avoided` and
        `tokens introduced`. Shared so the single-repo view and the `--all`
        total render the sources the one way."""
        _, _, _, bash_entered = self._ledger_flow(data)
        dup = data["dup_warned"] + data["dup_denied"]
        ranged = int(data.get("ranged_steered", 0) or 0)
        self.out.line("avoided, by source:")
        self.out.line("  {:<33} {:>6}   {} tokens".format(
            "duplicate / ranged reads stopped", util.num(dup + ranged),
            util.num(data["tokens_avoided"])))
        self.out.line("  {:<33} {:>6}   {} tokens".format(
            "big reads refused -> ranges", util.num(data["big_reads_denied"]),
            util.num(data["denied_tokens"])))
        self.out.line("  {:<33} {:>6}   {} tokens".format(
            "Bash outputs condensed", util.num(data["governed_calls"]),
            util.num(data["governed_saved_tokens"])))
        self.out.line("")
        self.out.line("introduced, by source:")
        self.out.line("  {:<33} {:>6}   {} tokens".format(
            "whole files read anyway", util.num(data["read_floods"]),
            util.num(data["read_flood_tokens"])))
        self.out.line("  {:<33} {:>6}   {} tokens".format(
            "Bash floods, after condensing", util.num(data["floods_seen"]),
            util.num(bash_entered)))
        passed = int(data.get("floods_passed", 0) or 0)
        if passed:
            fam = data.get("passed_families") or {}
            tail = " ({})".format(", ".join(
                "{} {}".format(v, k) for k, v in sorted(
                    fam.items(), key=lambda kv: -kv[1]))) if fam else ""
            self.out.line("  {:<33} {:>6}   {} tokens{}".format(
                "Bash floods passed through", util.num(passed),
                util.num(int(data.get("flood_passed_tokens", 0) or 0)), tail))
        self.out.line("  {:<33} {:>6}   {} tokens".format(
            "context sift injected", "-", util.num(data["tokens_injected"])))

    def _ledger_carry_and_index(self, data: dict) -> None:
        """The carry line (the one figure in token-turns, kept apart so it is
        never read against the tokens above) and the index line. Shared by the
        single-repo view and the `--all` total."""
        saved_tt = int(data.get("carry_saved", 0) or 0)
        if saved_tt:
            mult = saved_tt / max(1, int(data.get("governed_saved_tokens", 0) or 0))
            self.out.line("")
            self.out.line(
                "condensing also keeps those tokens out of every later turn: "
                "{} token-turns kept out (~{:.0f}x the one-off saving).".format(
                    util.num(saved_tt), mult))
        self.out.line("")
        self.out.line("index: {} hits, {} misses, {} nudges".format(
            util.num(data["index_hits"]), util.num(data["index_misses"]),
            util.num(data["nudges"])))

    def _ledger_help_hint(self) -> None:
        self.out.line("")
        self.out.line("what these mean: run {}".format(
            self.out.action("sift ledger --help")))

    def _print_ledger(self, data: dict, advise: bool) -> None:
        avoided, introduced, net, _ = self._ledger_flow(data)
        self.out.line("since {}:".format(data["since"]))
        self.out.line("")
        # The headline balance, in one unit so the three actually subtract:
        # kept out of context, reached context, and the difference.
        self.out.line("  tokens avoided     {:>13}   kept out of context".format(
            util.num(avoided)))
        self.out.line("  tokens introduced  {:>13}   reached context".format(
            util.num(introduced)))
        self.out.line("  net tokens saved   {}".format(
            self._net(util.num(net).rjust(13), net)))

        self.out.line("")
        self._ledger_breakdown(data)
        self._ledger_carry_and_index(data)

        # Advice is the live config's business, not the counts', and only for a
        # single repo -- a machine-wide total has no one setting to advise on.
        if advise and self.cfg is not None:
            gov_on = bool(self.cfg.get("governance", "enabled", default=False))
            mode = str(self.cfg.get("hooks", "big_read_mode", default="off"))
            if not gov_on and data["floods_seen"]:
                self.out.line("")
                self.out.line(
                    "governance is off; {} Bash outputs went over the "
                    "threshold this period. governance.enabled would condense "
                    "them.".format(util.num(data["floods_seen"])))
            if mode != "deny" and data["read_floods"]:
                self.out.line("")
                self.out.line(
                    "big_read_mode is off; {} whole-file reads came back over "
                    "the threshold. Set it to deny to turn those into ranged "
                    "reads.".format(util.num(data["read_floods"])))

        self._ledger_help_hint()

    def cmd_import(self) -> int:
        # OpenWolf is the only source there is, so `sift import` means it; the
        # old --from-openwolf still works (hidden) for anyone who typed it.
        from siftlib import migrate_openwolf as mig_mod
        from siftlib.migrate_openwolf import Migration
        quiet = self.out.json_mode
        confirm = (lambda _m: True) if self.args.yes else self._ask
        mig = Migration(self.ctx, self.cfg, dry_run=self.args.dry_run, confirm=confirm,
                        show=(None if quiet else self.out.line))
        data = mig.run_all()
        if not quiet:
            if mig.log:
                self.out.line("")
                self.out.line("Done:")
                for line in mig.log:
                    self.out.line("  " + line)
            if not self.args.dry_run:
                removed = self._offer_removal(mig_mod, self.args)
                data["guards"] = self._offer_guard_rescope(mig_mod, self.args)
                traces = self._offer_trace_cleanup(mig_mod, self.args, mig)
                data["pointers"] = self._report_pointers(mig_mod)
                data["removed"] = removed
                data["traces"] = traces
                data["committed"] = self._offer_commit(
                    mig_mod, self.args, mig, removed, traces)
        self.out.emit(data)
        return EXIT_OK

    def cmd_config(self) -> int:
        sub = getattr(self.args, "config_cmd", None)
        if sub == "get":
            return self._config_get()
        if sub == "set":
            return self._config_set()
        self.out.fail("USAGE", "config needs `get` or `set`")
        return EXIT_USAGE

    @staticmethod
    def _fmt_config(value: Any) -> str:
        # Booleans read as the words the setter accepts, so `get` and `set`
        # speak the same language; everything else is its plain string.
        if isinstance(value, bool):
            return "true" if value else "false"
        return str(value)

    def _config_get(self) -> int:
        key = self.args.key
        path, default = config_mod.lookup_default(key)
        if path is None:
            self.out.fail("USAGE", "unknown config key: " + key)
            return EXIT_USAGE
        if getattr(self.args, "all", False):
            return self._config_get_all(key, path, default)
        if self._no_root_without_all():
            self.out.fail("USAGE", "--root only means something with --all")
            return EXIT_USAGE
        value = self.cfg.get(*path, default=default)
        if not self.out.json_mode:
            self.out.line("{} = {}".format(key, self._fmt_config(value)))
        self.out.emit({"key": key, "value": value})
        return EXIT_OK

    def _config_get_all(self, key: str, path, default: Any) -> int:
        """Read one key across the fleet -- the read that pairs with
        `config set --all`: after setting governance on everywhere, this is how
        you confirm it took, or audit which repos have it on."""
        root = self._fleet_root()
        if root is None:
            return EXIT_ENV
        repos: List[Dict[str, Any]] = []
        for rel, ctx in self._fleet_repos(root):
            cfg = config_mod.load(ctx.config_path)
            repos.append({"repo": rel, "value": cfg.get(*path, default=default)})
        if not self.out.json_mode:
            self.out.line("{} across {} under {}".format(
                key, util.count(len(repos), "repo"), root))
            if repos:
                self.out.line("")
                self.out.table(
                    [[r["repo"], self._fmt_config(r["value"])] for r in repos],
                    ["repo", "value"])
        self.out.emit({"key": key, "root": str(root), "repos": repos})
        return EXIT_OK

    def _config_set(self) -> int:
        key, raw = self.args.key, self.args.value
        path, default = config_mod.lookup_default(key)
        if path is None:
            self.out.fail("USAGE", "unknown config key: " + key)
            return EXIT_USAGE
        value, err = config_mod.coerce_value(default, raw)
        if err:
            self.out.fail("USAGE", "{}: {}".format(key, err))
            return EXIT_USAGE
        eerr = config_mod.enum_error(key, value)
        if eerr:
            self.out.fail("USAGE", eerr)
            return EXIT_USAGE
        if self.args.all:
            return self._config_set_all(key, value)
        err = config_mod.set_value(self.ctx.config_path, key, value)
        if err:
            self.out.fail("ENV", err)
            return EXIT_ENV
        if not self.out.json_mode:
            self.out.line("set {} = {} in {}".format(
                key, self._fmt_config(value), self.ctx.dir_name))
        self.out.emit({"key": key, "value": value})
        return EXIT_OK

    def _config_set_all(self, key: str, value: Any) -> int:
        root = self._fleet_root()
        if root is None:
            return EXIT_ENV
        targets = list(self._fleet_repos(root))
        ok = self._confirm_fleet("set {} = {} in {} under {}?".format(
            key, self._fmt_config(value), util.count(len(targets), "repo"), root))
        if ok is None:
            self.out.fail("USAGE", "writing across repos needs --yes when not interactive")
            return EXIT_USAGE
        if not ok:
            if not self.out.json_mode:
                self.out.line("nothing written")
            self.out.emit({"key": key, "value": value, "set": [], "failed": []})
            return EXIT_OK
        done: List[str] = []
        failed: List[Dict[str, str]] = []
        for rel, ctx in targets:
            err = config_mod.set_value(ctx.config_path, key, value)
            if err:
                failed.append({"repo": rel, "error": err})
            else:
                done.append(rel)
        if not self.out.json_mode:
            self.out.line("set {} = {} in {} under {}".format(
                key, self._fmt_config(value), util.count(len(done), "repo"), root))
            for rel in done:
                self.out.line("  " + rel)
            for item in failed:
                self.out.line("  {}: {}".format(item["repo"], item["error"]))
        self.out.emit({"key": key, "value": value, "set": done, "failed": failed})
        return EXIT_FINDINGS if failed else EXIT_OK

    def dispatch(self) -> int:
        handler = getattr(self, "cmd_" + self.cmd.replace("-", "_"), None)
        if handler is None:
            self.out.fail("USAGE", "unknown command: " + str(self.cmd))
            return EXIT_USAGE
        if (self.ctx is not None and _needs_repo(self.args)
                and self.cmd not in ("init", "doctor", "version")
                and not self.ctx.exists):
            # A fleet `--all` acts on repos found under a root, not the one you
            # stand in, so it must not be blocked for standing in a non-sift
            # repo (or none) -- `_needs_repo` is the same test that let it here.
            self.out.fail("NO_SIFT", "no sift directory at {}, run: {}".format(
                self.ctx.rel(self.ctx.dir), self.out.action("sift init", err=True)))
            return EXIT_ENV
        return handler()


def _bump_note(bump: Dict[str, Any]) -> str:
    """Shipped content has moved and VERSION has not. Said here because this is
    where someone is already asking what version this is."""
    parts = []
    if bump.get("commits"):
        parts.append(util.count(len(bump["commits"]), "commit") + " since it was set")
    if bump.get("dirty"):
        parts.append("uncommitted: " + ", ".join(bump["dirty"][:4]))
    return "VERSION {} is owed a bump, shipped content changed ({}).".format(
        bump.get("version") or "?", "; ".join(parts))


def _needs_repo(args: argparse.Namespace) -> bool:
    """Most commands act on the repo you are standing in and cannot run without
    one. The fleet commands are the exception: they walk from your home
    directory (`--root` to change it) and add up, read or write across every
    installed repo, so they need no repo of their own and run from anywhere.

    `repos` is always a fleet listing; the rest are fleet only with `--all`
    (on either `config` subcommand)."""
    if args.cmd == "repos":
        return False
    if getattr(args, "all", False) and args.cmd in (
            "ledger", "doctor", "lint", "config"):
        return False
    return True


GLOBAL_SWITCHES = ("--json", "--quiet")
GLOBAL_OPTIONS = ("--sift-dir", "--color", "--repo")


def hoist_globals(argv: Sequence[str]) -> List[str]:
    """Accept the global flags after the subcommand as well as before it.

    `sift doctor --json` is what anyone types the first time, and argparse
    rejects it because the flags belong to the top-level parser. Rather than
    redeclare them on twenty subparsers, move them to the front (Ruling 30).
    """
    head: List[str] = []
    rest: List[str] = []
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--":
            rest.extend(argv[i:])
            break
        if arg in GLOBAL_SWITCHES:
            head.append(arg)
        elif arg in GLOBAL_OPTIONS and i + 1 < len(argv):
            head.extend([arg, argv[i + 1]])
            i += 1
        elif any(arg.startswith(o + "=") for o in GLOBAL_OPTIONS):
            head.append(arg)
        else:
            rest.append(arg)
        i += 1
    return head + rest


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(hoist_globals(list(argv if argv is not None else sys.argv[1:])))
    if not args.cmd:
        parser.print_help()
        return EXIT_USAGE
    try:
        app = App(args)
    except paths.NotARepo as exc:
        out = util.Out(args.cmd, VERSION, args.json_mode, args.quiet,
                       color=getattr(args, "color", "auto"))
        out.fail("NOT_A_REPO", str(exc))
        return EXIT_ENV
    try:
        return app.dispatch()
    except BrokenPipeError:
        return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
