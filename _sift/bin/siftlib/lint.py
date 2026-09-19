"""`sift lint` - the deterministic backstop for the rules that cannot be undone.

Six checks survive the removal of the pages. Three are hard errors the
pre-commit hook blocks on -- W15 secrets, W16 out-of-repo absolute paths, W19
a tracked file under `local/` or `local/` missing from `.gitignore` -- because
a later commit cannot undo any of them: a pushed secret has to be rotated, a
laptop path is a small leak and useless to a colleague, and a committed
`local/` file is a disclosure about a person that lives in history. W20 warns
on HR and compensation vocabulary, W04 catches a backticked path that does not
exist, and W21 catches two clones that picked the same decision id.

Why any of this remains, when the pages it used to check are gone: the rules
it enforces are the one thing the task eval could show a model does not
reconstruct on its own, and the content it checks is written *by* a model,
across long sessions, under the instruction decay `post_tool` re-injection
exists to slow. Re-injection lowers the odds of the rule being forgotten;
this catches it when it is forgotten anyway.

The ten page checks went with the pages, and four more went with them because
they were not worth their noise: `files.jsonl` hygiene (W09, W13, W18) is
machine-written and cheap when wrong, W14's whole-directory token budget is
meaningless for three files, and W17's one-sentence-per-line is a convention
that does not need a checker. They are in `git show v1-pages:src/siftlib/lint.py`
if the judgement was wrong.

The authoritative hard-fail list is `config.DEFAULTS["lint"]["hard_fail"]`, not
this docstring: an eval run caught a model answering "two" from a stale copy of
these lines rather than from the page, which was right.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from . import gitutil, ignore, templates, util
from .config import Config
from .paths import Ctx

SEV_ERROR = "error"
SEV_WARNING = "warning"
SEV_INFO = "info"

HISTORY_CODES: Set[str] = set()  # no check reads git history any more

SECRET_PATTERNS = [
    re.compile(r"(?i)(api[_-]?key|secret|token|password|passwd|pwd)\s*[:=]\s*['\"]?[A-Za-z0-9_\-/+=]{16,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)(postgres|mysql|mongodb(\+srv)?|redis|amqp)://[^\s'\"]+:[^\s'\"]+@"),
    re.compile(r"ghp_[A-Za-z0-9]{36}"),
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    re.compile(r"xox[bap]-[A-Za-z0-9-]+"),
]

# The character before the path may be anything that cannot itself be part of
# a path. Listing the openers instead -- whitespace, quote, paren -- missed the
# one form a model actually writes in markdown: `` `/Users/wayned/notes` ``
# passed `lint --staged` clean, and so did `path=/Users/...` and `at: ~/x`,
# which is a hard-fail privacy check failing open on its normal case.
ABSOLUTE_PATTERNS = [
    re.compile(r"(^|[^A-Za-z0-9_./~-])/(Users|home|Volumes|private|tmp|var)/"),
    re.compile(r"(^|[^A-Za-z0-9_./~-])~/"),
    re.compile(r"C:\\\\Users\\\\"),
    re.compile(r"C:\\Users\\"),
]
# Paths that are the same on every machine, so they are not a leak. Scrubbed
# from the line before W16 looks at it rather than excusing the line: skipping
# the whole line meant `ran it from /Users/someone/work 2>/dev/null` passed a
# hard-fail privacy check because it also mentioned /dev/null.
ABSOLUTE_ALLOW = ("/usr/bin/python3", "/dev/null")


def _without_allowed(line: str) -> str:
    for allowed in ABSOLUTE_ALLOW:
        line = line.replace(allowed, "")
    return line

# W20. Deliberately narrow: this cannot detect "Bob is struggling", and
# pretending otherwise would train people to ignore it. It catches the
# vocabulary that is almost never about code -- an HR process, or money
# attached to a person -- and says where that material belongs instead.
HR_PATTERNS = [
    re.compile(r"(?i)\b(performance review|performance improvement plan|\bPIP\b|"
               r"disciplinary|grievance|probation review|redundanc|severance|"
               r"succession plan|talent review|calibration session)\b"),
    # An amount, not any digit: "W20" sitting near the word "compensation" is
    # how a check like this starts crying wolf, and the first thing it cried
    # wolf at was the page documenting it.
    re.compile(r"(?i)\b(salary|remuneration|compensation|day ?rate|rate ?card|"
               r"cost to company|\bCTC\b|headcount cost|bonus|equity grant)\b"
               r"[^\n]{0,40}?(?:[R$£€]\s?\d|\d{3})"),
    re.compile(r"(?i)(?:[R$£€]\s?\d|\d{3})[\d ,.]*[^\n]{0,40}?\b(salary|remuneration|"
               r"compensation|day ?rate|rate ?card|cost to company|\bCTC\b|"
               r"per annum|p\.?a\.?)\b"),
]

MD_LINK_RE = re.compile(r"\[([^\]]*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
BACKTICK_RE = re.compile(r"`([^`\n]+)`")
JOURNAL_ID_RE = re.compile(r"^j-\d{8}-\d{6}-[0-9a-f]{4}$")
SENTENCE_RE = re.compile(r"[.!?]\s+[A-Z(\"']")
INLINE_CODE_RE = re.compile(r"`[^`\n]*`")

# Ruling 18: only links to real area/flow pages take part in the `links:`
# correspondence check. The scaffolding links (overview, conventions, index,
# decisions) are navigation, not relationships.
STRUCTURAL_TARGETS = {"overview.md", "conventions.md", "index.md", "README.md"}


class Issue(dict):
    def __init__(self, code: str, severity: str, path: str, line: int,
                 message: str, fix: str) -> None:
        super().__init__(code=code, severity=severity, path=path, line=line,
                         message=message, fix=fix)


def summarise(issues: Sequence[dict]) -> Dict[str, int]:
    return {
        "errors": sum(1 for i in issues if i["severity"] == SEV_ERROR),
        "warnings": sum(1 for i in issues if i["severity"] == SEV_WARNING),
        "info": sum(1 for i in issues if i["severity"] == SEV_INFO),
    }


def _in_fence(lines: Sequence[str]) -> List[bool]:
    out: List[bool] = []
    fence = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            out.append(True)
            fence = not fence
            continue
        out.append(fence)
    return out


def run(ctx: Ctx, cfg: Config, fast: bool = False,
        only: Optional[Set[str]] = None,
        staged: bool = False) -> List[dict]:
    issues: List[dict] = []

    def want(code: str) -> bool:
        if only and code not in only:
            return False
        if fast and code in HISTORY_CODES:
            return False
        return True

    if not ctx.exists:
        return issues

    staged_set: Optional[Set[str]] = None
    if staged:
        staged_set = {p for p in gitutil.staged_files(ctx.root) if ctx.is_sift_path(p)}

    def selected(rel_to_root: str) -> bool:
        return staged_set is None or rel_to_root in staged_set

    tracked = set(gitutil.ls_files(ctx.root))

    # --- W19: is local/ actually local? ------------------------------------
    if want("W19"):
        prefix = ctx.dir_name + "/local/"
        leaked = sorted(p for p in tracked if p.startswith(prefix))
        for path in leaked:
            issues.append(Issue("W19", SEV_ERROR, path, 1,
                                "a file under local/ is tracked by git - local/ is the "
                                "one place private material is allowed, and it only works "
                                "while nothing in it is committed",
                                "git rm --cached '{}' and check the .gitignore stanza "
                                "is intact".format(path)))
        if not leaked:
            ignored = util.read_text(ctx.root / ".gitignore")
            if prefix not in ignored:
                issues.append(Issue("W19", SEV_ERROR, ".gitignore", 1,
                                    "local/ is not gitignored, so nothing stops private "
                                    "material being committed",
                                    "add '{}' to .gitignore".format(prefix)))

    # --- W21: two clones, one decision id ----------------------------------
    # Union merge keeps both records now instead of merging them into one, so
    # nothing is lost -- but two records answering to `D-20260916-01` make every
    # citation of it ambiguous, and only a person can say which keeps the number.
    if want("W21"):
        from . import journal as journal_mod
        seen: Dict[str, int] = {}
        for rec in journal_mod.decisions(ctx):
            did = str(rec.get("id", ""))
            seen[did] = seen.get(did, 0) + 1
        for did, count in sorted(seen.items()):
            if count > 1:
                issues.append(Issue("W21", SEV_WARNING, ctx.rel(ctx.decisions), 1,
                                    "{} records share the id {} - two clones decided on "
                                    "the same day".format(count, did),
                                    "renumber all but one to the next free id for that "
                                    "date and fix any reference to it"))

    # --- every document in the sift directory, as text -------------------------------
    # Not "every page": there are no pages. Anything committed under the sift directory
    # is content a colleague will read, which is the only property
    # these checks care about.
    if staged_set is None:
        candidates = [p for p in sorted(ctx.dir.rglob("*")) if p.is_file()]
    else:
        # Include paths from the index even when the worktree copy was changed
        # or removed after staging. The index is the content the hook guards.
        candidates = [ctx.root / p for p in sorted(staged_set)]
    for path in candidates:
        if path.suffix.lower() not in (".md", ".jsonl"):
            continue
        rel_sift = path.relative_to(ctx.dir).as_posix()
        if rel_sift.startswith(("local/", ".cache/", "bin/")):
            continue
        rel = ctx.dir_name + "/" + rel_sift
        if not selected(rel):
            continue
        text = (gitutil.staged_text(ctx.root, rel)
                if staged_set is not None else util.read_text(path))
        if text is None:  # staged deletion: no content will enter the commit
            continue
        if not text:
            continue
        lines = text.splitlines()
        # `conventions.md` and the README state the privacy rule, which means
        # spelling out the vocabulary the rule is about. A check that fires on
        # its own documentation is noise -- but W15 and W16 still apply there,
        # because a secret in a generated file is still a secret.
        own_docs = rel_sift in ("conventions.md", "README.md")
        _secret_scan(issues, rel, lines, want, privacy=not own_docs,
                     generated=_generated_lines(text) if own_docs else frozenset())

        if want("W04") and path.suffix.lower() == ".md":
            fences = _in_fence(lines)
            for i, line in enumerate(lines):
                if fences[i]:
                    continue
                for token in re.findall(r"`([^`]+)`", line):
                    problem = _check_code_ref(ctx, token, tracked, path)
                    if problem:
                        issues.append(Issue("W04", SEV_WARNING, rel, i + 1,
                                            problem, "Fix the reference"))

    issues.sort(key=lambda i: (0 if i["severity"] == SEV_ERROR else
                               1 if i["severity"] == SEV_WARNING else 2,
                               i["code"], i["path"], i["line"]))
    return issues


_CODE_REF_RE = re.compile(r"^(?P<path>[A-Za-z0-9_.@/\-]+)(?::(?P<line>\d+))?$")


def _check_code_ref(ctx: Ctx, token: str, tracked: Set[str],
                    page_path: Path) -> Optional[str]:
    if " " in token or token.endswith("/") or "*" in token:
        return None
    m = _CODE_REF_RE.match(token)
    if not m:
        return None
    rel = m.group("path")
    if "/" not in rel:
        return None  # a bare word or filename is prose, not a code reference
    if rel.startswith(("http", "./", "../", "/", "-")):
        # A leading slash is a slash-command or an absolute path; neither is a
        # repo-relative code reference. W16 owns absolute paths.
        return None
    candidates = [rel]
    if not rel.startswith(ctx.dir_name + "/"):
        candidates.append(ctx.dir_name + "/" + rel)
    hit = next((c for c in candidates if c in tracked), None)
    if hit is None:
        on_disk = next((c for c in candidates if (ctx.root / c).exists()), None)
        if on_disk is None:
            return "code reference not found in the repo: " + token
        hit = on_disk
    line_no = m.group("line")
    if line_no:
        try:
            total = len((ctx.root / hit).read_text(encoding="utf-8", errors="replace").splitlines())
        except OSError:
            return None
        if int(line_no) > total:
            return "line {} is past the end of {} ({} lines)".format(line_no, hit, total)
    return None


def _generated_lines(text: str) -> "frozenset[int]":
    """The 0-based line numbers of our own generated marker block.

    Only `conventions.md` and `README.md` have one, and only W16 is waived
    inside it: the block is byte-identical on every machine by construction, so
    `/Users/...` in it is an example of the rule rather than a leak from
    somebody's laptop. Anything a user writes below the end marker is theirs
    and stays checked -- which is where the marker tells them to write.
    """
    before, block, _after = templates.marker_split(text)
    if block is None:
        return frozenset()
    first = before.count("\n")
    return frozenset(range(first, first + block.count("\n") + 1))


def _secret_scan(issues: List[dict], rel: str, lines: Sequence[str],
                 want, privacy: bool = True,
                 generated: "frozenset[int]" = frozenset()) -> None:
    """`privacy=False` for the files we generate ourselves: `conventions.md`
    states the rule, which means spelling out the vocabulary the rule is about.
    A check that fires on its own documentation is noise. W15 and W16 still
    apply there -- a secret in a generated file is still a secret -- except for
    W16 inside the generated block itself, which `generated` carries."""
    for i, line in enumerate(lines):
        if privacy and want("W20"):
            for pattern in HR_PATTERNS:
                if pattern.search(line):
                    issues.append(Issue(
                        "W20", SEV_WARNING, rel, i + 1,
                        "reads like HR or compensation material, which is never "
                        "what a committed sift directory is for",
                        "move it to local/ - that directory is gitignored and exists "
                        "for exactly this"))
                    break
        if want("W15"):
            for pattern in SECRET_PATTERNS:
                if pattern.search(line):
                    issues.append(Issue("W15", SEV_ERROR, rel, i + 1,
                                        "looks like a secret",
                                        "Remove it and rotate the secret"))
                    break
        if want("W16") and i not in generated:
            probe = _without_allowed(line)
            for pattern in ABSOLUTE_PATTERNS:
                if pattern.search(probe):
                    issues.append(Issue("W16", SEV_ERROR, rel, i + 1,
                                        "absolute path outside the repo",
                                        "Move it to local/ or make it repo-relative"))
                    break
