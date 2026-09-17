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

EXIT_OK, EXIT_FINDINGS, EXIT_USAGE, EXIT_ENV, EXIT_BLOCKED = 0, 1, 2, 3, 4



# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="sift", add_help=True,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="sift — context economy for a coding agent",
        epilog="installing and upgrading the tool itself are handled by the\n"
               "`sift` wrapper, not here:\n"
               "  sift install [repo] [flags]\n"
               "  sift update  [repo] [flags]")
    p.add_argument("--sift-dir", dest="sift_dir", default=None)
    p.add_argument("--json", dest="json_mode", action="store_true")
    p.add_argument("--quiet", action="store_true")
    p.add_argument("--version", action="version", version=VERSION)
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("init")
    s.add_argument("--migrate-openwolf", action="store_true")
    s.add_argument("--no-githooks", action="store_true")
    s.add_argument("--no-skills", action="store_true")
    s.add_argument("--dir", default=None)
    s.add_argument("--yes", action="store_true")
    s.add_argument("--dry-run", action="store_true")

    s = sub.add_parser("doctor")
    s.add_argument("--fix", action="store_true")
    s.add_argument("--check-upstream", action="store_true")

    sub.add_parser("version")

    s = sub.add_parser("scan")
    s.add_argument("--full", action="store_true")

    s = sub.add_parser("describe")
    s.add_argument("--pending", action="store_true")
    s.add_argument("--top", type=int, default=20)
    s.add_argument("--set", dest="set_", nargs=2, metavar=("PATH", "DESC"))
    s.add_argument("--get", metavar="PATH")

    s = sub.add_parser("map")
    s.add_argument("target")
    s.add_argument("--top", type=int, default=DEFAULT_LINES)

    s = sub.add_parser("search")
    s.add_argument("query")
    s.add_argument("--top", type=int, default=10)
    s.add_argument("--layer", choices=["docs", "files", "all"], default="all")

    s = sub.add_parser("lint")
    s.add_argument("--staged", action="store_true")
    s.add_argument("--ci", action="store_true")
    s.add_argument("--fast", action="store_true")
    s.add_argument("--only", default=None)

    s = sub.add_parser("log")
    s.add_argument("--kind", required=True, choices=list(journal_mod.KINDS))
    s.add_argument("--detail", required=True)
    s.add_argument("--files", nargs="+", default=[])
    s.add_argument("--tags", nargs="+", default=[])

    s = sub.add_parser("bug")
    bsub = s.add_subparsers(dest="bug_cmd")
    b = bsub.add_parser("add")
    b.add_argument("--error", required=True)
    b.add_argument("--root-cause", dest="root_cause", default="")
    b.add_argument("--fix", default="")
    b.add_argument("--files", nargs="+", default=[])
    b.add_argument("--tags", nargs="+", default=[])
    b = bsub.add_parser("find")
    b.add_argument("query")
    b.add_argument("--top", type=int, default=5)

    s = sub.add_parser("decide")
    s.add_argument("title")
    s.add_argument("--context", default="")
    s.add_argument("--decision", default="")
    s.add_argument("--consequences", default="")
    s.add_argument("--files", nargs="+", default=[])
    s.add_argument("--supersedes", default="")

    s = sub.add_parser("decisions")
    s.add_argument("id", nargs="?")
    s.add_argument("--markdown", action="store_true")

    s = sub.add_parser("ledger")
    s.add_argument("--since", default="7.days")

    s = sub.add_parser("import")
    s.add_argument("--from-openwolf", dest="from_openwolf", action="store_true")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--yes", action="store_true")

    return p


# ---------------------------------------------------------------------------
# Command implementations
# ---------------------------------------------------------------------------

class App:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.cmd = args.cmd
        self.out = util.Out(self.cmd, VERSION, args.json_mode, args.quiet)
        self.ctx = paths.resolve(Path.cwd(), args.sift_dir or getattr(args, "dir", None))
        self.cfg = config_mod.load(self.ctx.config_path)

    # -- helpers ------------------------------------------------------------
    def truncated(self, rows: List[Any], top: int) -> List[Any]:
        """Slice for display; call `more_note` after the table so the hint
        lands under the rows it is about."""
        return rows[:top]

    def more_note(self, total: int, top: int) -> None:
        if total > top:
            self.out.line("({} more — use --top)".format(total - top))

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
                    self.out.line("(dry run — nothing was written)")
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
            self.out.line("Next: sift doctor, then sift scan, then read "
                          + self.ctx.dir_name + "/conventions.md")
            self._say_pending()
        self.out.emit(data)
        return EXIT_OK

    def _offer_removal(self, mig_mod, args) -> Optional[dict]:
        """Offer to finish the job. Ruling 11 keeps the deletion on a keystroke,
        which is what the prompt is — but a keystroke is not four commands, and
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
                      "inside .wolf/ — which no longer exists:")
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
            self.out.line("Left alone — but until they point at {}/, they pass "
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
                      "in {}/local/ and gitignored — for a colleague the pointer "
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
                self.out.line("  {} — {}".format(item["path"], item["what"]))
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
            self.out.line("Left staged — commit it when you are ready.")
            return None
        result = mig_mod.commit(self.ctx)
        self.out.line("")
        for line in result["output"].splitlines()[:12]:
            self.out.line("  " + line)
        if not result["ok"]:
            self.out.line("")
            self.out.line("Not committed — a hook refused it. Nothing is lost; it "
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
            self.out.line("sift {} — {}".format(data["installed"], data["sift_dir"]))
            if data["clone"]:
                self.out.line("clone {} — {}{}".format(
                    data["clone_version"], data["clone"],
                    " (this repo)" if data["state"] == upgrade_mod.SELF else ""))
            self.out.line(data["detail"])
            if data["state"] in upgrade_mod.STALE:
                self.out.line("run: sift update")
            bump = data.get("bump") or {}
            if bump.get("needed"):
                self.out.line("")
                self.out.line(_bump_note(bump))
        self.out.emit(data)
        return EXIT_OK

    def cmd_scan(self) -> int:
        data = scan_mod.run_scan(self.ctx, self.cfg, full=self.args.full)
        if not self.out.json_mode:
            self.out.line("{} files scanned, {} changed, {} need descriptions".format(
                data["files"], data["changed"], data["pending_descriptions"]))
            if data["compacted"]:
                self.out.line("files.jsonl: {} duplicate lines compacted".format(data["compacted"]))
        self.out.emit(data)
        return EXIT_OK

    def cmd_describe(self) -> int:
        args = self.args
        if args.set_:
            path, desc = args.set_
            rec, err = scan_mod.set_description(self.ctx, self.cfg, path, desc)
            if err:
                self.out.fail("BAD_PATH", err)
                return EXIT_USAGE
            if not self.out.json_mode:
                self.out.line("set: {} — {}".format(rec["path"], rec["desc"]))
            self.out.emit({"path": rec["path"], "hash": rec["hash"]})
            return EXIT_OK
        if args.get:
            rec = scan_mod.load_descriptions(self.ctx).get(args.get.replace("\\", "/"))
            if not self.out.json_mode:
                self.out.line(rec["desc"] if rec else "(no description)")
            self.out.emit(rec)
            return EXIT_OK
        if args.pending:
            rows = scan_mod.pending_descriptions(self.ctx, self.cfg)
            shown = self.truncated(rows, args.top)
            if not self.out.json_mode:
                self.out.line("one line: what this file is *for*")
                self.out.table([[r["reason"], str(r["churn"]), str(r["tokens"]), r["path"]]
                                for r in shown], ["why", "churn", "tok", "path"])
                self.more_note(len(rows), args.top)
            self.out.emit(shown)
            return EXIT_FINDINGS if rows else EXIT_OK
        self.out.fail("USAGE", "describe needs --pending, --set or --get")
        return EXIT_USAGE

    def cmd_map(self) -> int:
        rows = scan_mod.build_map(self.ctx, self.cfg, self.args.target)
        shown = self.truncated(rows, self.args.top)
        if not self.out.json_mode:
            from siftlib import symbols as sym_mod
            dir_rows, here = scan_mod.rollup_map(rows, self.args.target)
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
                self.out.line("No results for: " + repr(self.args.query))
            else:
                self.out.table([["{:.2f}".format(r["score"]), r["layer"], r["title"],
                                 r["snippet"][:70]] for r in rows],
                               ["score", "layer", "title", "snippet"])
        self.out.emit(rows)
        return EXIT_OK

    def cmd_lint(self) -> int:
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
                    self.out.line("  ({} more — use --json)".format(len(group) - DEFAULT_LINES))
        self.out.emit({"issues": issues, "summary": summary})
        advisory = bool(self.cfg.get("ci", "advisory", default=True))
        if summary["errors"]:
            return EXIT_FINDINGS
        if self.args.ci and summary["warnings"] and not advisory:
            return EXIT_FINDINGS
        return EXIT_OK

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
                          + " — a record is written complete, like `bug add`")
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
                self.out.line("no decisions recorded — `sift decide` writes one")
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

    def cmd_ledger(self) -> int:
        data = ledger_mod.summarise(self.ctx, self.args.since)
        if not self.out.json_mode:
            self.out.line("since {}: {} index hits, {} misses, {} duplicate reads caught, "
                          "{} nudges".format(data["since"], data["index_hits"],
                                             data["index_misses"],
                                             data["dup_warned"] + data["dup_denied"],
                                             data["nudges"]))
            self.out.line("{:,} tokens avoided, {:,} tokens injected".format(
                data["tokens_avoided"], data["tokens_injected"]))
            if data["governed_calls"]:
                # Measured, unlike the line above: both numbers are the real
                # output, before and after. Re-runs sit next to the saving
                # because a condensation the model works around is a loss.
                self.out.line(
                    "governance: {} calls condensed {} -> {} tokens "
                    "({} saved, {:.0f}%), {} re-run".format(
                        data["governed_calls"], data["governed_original_tokens"],
                        data["governed_entered_tokens"], data["governed_saved_tokens"],
                        100.0 * data["governed_saved_tokens"]
                        / max(1, data["governed_original_tokens"]),
                        data["governed_reruns"]))
                for family, count in sorted(data["governed_families"].items()):
                    self.out.line("  {:<12} {}".format(family, count))
            else:
                # Which branch to print is the setting's business, not the
                # event count's. Telling someone to turn on a setting they
                # have already turned on is how a report loses its reader.
                on = bool(self.cfg.get("governance", "enabled", default=False))
                state = "on" if on else "off"
                if data["floods_seen"] and on:
                    self.out.line(
                        "governance: on, {} command(s) over the threshold and "
                        "none condensed yet this period — the ones counted "
                        "here ran before it was on.".format(data["floods_seen"]))
                elif data["floods_seen"]:
                    # The whole reason `advise` counts these: with condensation
                    # off there is otherwise no signal that it would have paid.
                    self.out.line(
                        "governance: off, but {} command(s) went over the "
                        "threshold. Turn it on with governance.enabled if you "
                        "want those condensed.".format(data["floods_seen"]))
                else:
                    self.out.line(
                        "governance: {}, and nothing went over the threshold "
                        "this period — nothing for it to do.".format(state))
            if data.get("read_floods"):
                # The Read tool's floods, beside Bash's. On the first repo
                # measured they outnumbered the Bash floods five to one.
                mode = str(self.cfg.get("hooks", "big_read_mode", default="off"))
                self.out.line(
                    "reads: {} whole-file read(s) over the threshold ({:,} tokens), "
                    "{} refused once and given ranges (big_read_mode: {}{})".format(
                        data["read_floods"], data["read_flood_tokens"],
                        data["big_reads_denied"], mode,
                        "" if mode == "deny"
                        else " -- set it to deny to turn these into ranged reads"))
            cost, saved = data.get("carry_cost") or 0, data.get("carry_saved") or 0
            if cost or saved:
                # The figures above are what a payload weighed once. These are
                # what it weighed for every turn that followed, which is the
                # number that decides whether any of this is worth doing.
                self.out.line(
                    "context carry: {:,} tokens carried, {:,} kept out by "
                    "condensing ({}; mostly cache reads, so do not price them "
                    "as fresh)".format(cost, saved, data["carry_basis"]))
        self.out.emit(data)
        return EXIT_OK

    def cmd_import(self) -> int:
        if not self.args.from_openwolf:
            self.out.fail("USAGE", "import needs --from-openwolf")
            return EXIT_USAGE
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

    def dispatch(self) -> int:
        handler = getattr(self, "cmd_" + self.cmd.replace("-", "_"), None)
        if handler is None:
            self.out.fail("USAGE", "unknown command: " + str(self.cmd))
            return EXIT_USAGE
        if self.cmd not in ("init", "doctor", "version") and not self.ctx.exists:
            self.out.fail("NO_SIFT", "no sift directory at {} — run `sift init`".format(
                self.ctx.rel(self.ctx.dir)))
            return EXIT_ENV
        return handler()


def _bump_note(bump: Dict[str, Any]) -> str:
    """Shipped content has moved and VERSION has not. Said here because this is
    where someone is already asking what version this is."""
    parts = []
    if bump.get("commits"):
        parts.append("{} commit{} since it was set".format(
            len(bump["commits"]), "" if len(bump["commits"]) == 1 else "s"))
    if bump.get("dirty"):
        parts.append("uncommitted: " + ", ".join(bump["dirty"][:4]))
    return "VERSION {} is owed a bump — shipped content changed ({}).".format(
        bump.get("version") or "?", "; ".join(parts))


GLOBAL_SWITCHES = ("--json", "--quiet")
GLOBAL_OPTIONS = ("--sift-dir",)


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
        out = util.Out(args.cmd, VERSION, args.json_mode, args.quiet)
        out.fail("NOT_A_REPO", str(exc))
        return EXIT_ENV
    try:
        return app.dispatch()
    except BrokenPipeError:
        return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
